# MCP/Addonのruntime管理

左サイドバーの「Addon / 連携」から、登録済み接続の希望ON/OFFと接続状態を確認する。
外部MCPは「外部MCPを追加」と各行の「詳細・編集」から登録・接続設定編集・削除できる。
自作Addonの接続factoryは#221の範囲とし、External用CRUDを表示しない。

## 希望設定と状態

`desired_enabled`はCore全体で共通の希望設定。初回だけ接続Manifestの`core_policy.enabled`を取り込み、
以後はdata root直下の`addon-settings.json`を正本とする。再起動で旧Manifestの値へ戻さない。
ファイルはversion付きの`users.local`にconnection IDとbooleanだけを保存し、一時ファイルから原子的に置換する。
接続先、認証値、snapshot、healthはこのファイルへ保存しない。接続設定とcredentialは後述の専用SQLiteへ保存する。
データ移行・手動backupでは希望設定ファイルと専用SQLiteの両方を保持する。
置換前の保存失敗はAPIを失敗とし、runtimeの希望値を変更しない。MVPの競合はあと勝ち。
置換後のdirectory同期失敗は、ファイル・runtime・Gateを反映済みの値へ揃え、
503 `settings_durability_uncertain`で保存の耐久性が未確認であることを返す。
UIは状態を再取得し、同じ設定の再保存を案内する。通常の再起動では置換済みの値を読み込むが、
同期に失敗したままホストが異常終了した場合の保持は保証しない。
既存のsharing、binding、trustによる実行制限は希望ONでも適用する。

External MCPの確認結果は`available`（使用可能）/`unavailable`（使用不可）の2択。
確認前・ON復帰時は`unknown`とし、新規実行を拒否する。希望OFFから導出する`disabled`はavailabilityに含めない。
自作Addon向けには`degraded`を共通modelに用意する。信頼するCore側health集約が健全なoperationを
`tool:<name>` / `resource:<uri>`として指定し、そのうちeffective readだけを実行可能にする。
外部MCPには部分障害の集約や`degraded`判定を要求しない。

OFFは既存の接続やprocessを停止しない。Registry・secret_ref・snapshotを保持し、candidateから除外する。
接続の世代を更新して、dispatch待ち・retry待ち・追加回答待ちを無効化する。ONに戻しても古い処理は再開しない。
既に送信した処理の取消しや副作用の巻戻しは保証しない。ON復帰時はhealthとdiscoveryを再確認する。

## healthと復旧

`AddonRuntime`は会話用LLMから独立し、Tool Routing未設定でも一覧・設定変更・接続監視を提供する。
初期接続はbackgroundで開始し、接続先が応答しなくても会話UIの起動を待たせない。
既存接続は1秒間隔で切断フラグを監視し、通常30秒間隔、5秒timeoutの副作用のないMCP確認で無操作時の障害も検出する。
2026-07-28以降は`server/discover`、旧protocolは`ping`を使用する。新仕様ではpingが廃止されているため、
接続したprotocol versionに合わせる（[MCP変更点](https://modelcontextprotocol.io/specification/2026-07-28/changelog)）。
一時的なhealth失敗は3回連続で`unavailable`とする（通常の検知上限は約105秒＋event loopの遅延）。
明確な切断、認証・protocol失敗は即反映する。個別Toolの単発timeout、5xx、引数不正、対象不存在や権限拒否は
connection全体の障害にしない。health目的でToolを実行しない。

接続・初回discoveryの上限は20秒。接続失敗後は5秒から最大300秒の指数backoffで再接続し、希望ONを保持する。
稼働中sessionでhealthが回復した場合も、利用再開前にdiscoveryを確認する。snapshotの反映は既存の次loop境界を守る。
OFF中はpingを休止する。OFF操作によってprocessの再起動や停止を行わない。

## APIと表示名

このAPIは既存の会話・設定APIと同じ、ローカル単一利用者の信頼境界で提供する。
管理者認証・ユーザー別認可の仕組みは現MVPには含まれない。
Frontend／Backendはloopbackで利用し、未認証でLANへ公開しない
（`docs/infrastructure-policy.md`）。`DS_BACKEND_HOST`等のbind指定を公開許可として扱わない。
SNSログイン・ユーザー別管理を導入する際は、既存の設定APIを含めた認証境界を定義する。

- `GET /addon-admin/connections`: snapshotの有無に関係なく登録済み全件を取得。
- `PATCH /addon-admin/connections/{connection_id}`: `{"desired_enabled": true}`または`false`だけを受理。
- responseはID、表示名、source種別、希望値、availability、effective state、固定error code、最終確認時刻だけ。
- 未知connectionは404、不正bodyは固定422、設定保存失敗は固定503。raw errorや入力JSONを反射しない。

`DS_MCP_CONFIG`の任意項目`display_names`に`{"connection-id": "表示名"}`を指定できる。
表示名の未設定時はconnection IDを使い、endpoint、command、secretから生成しない。
`DS_MCP_CONFIG`のExternal接続は初回だけ管理SQLiteへ取り込む。以後はUIで保存した設定を正本とし、
古い設定ファイルの内容で上書きしない。削除した既存connection IDはtombstoneを残して再取り込みを防ぐ。
既存のsharing、binding requirement、trust、restriction、budgetは取り込み・編集で維持する。
既存Bearer credentialは取り込み時に環境変数から読み、接続専有のsecret_refへ移す。

## 外部接続の登録・編集・削除

新規接続はOFFで登録し、同じ処理でMCP接続・protocol確認・Capability discoveryを行う。
Toolが0件でも正常discoveryなら成功。失敗しても設定はOFFで保存し、接続未確認と固定文言を表示する。
成功時も自動ONにしない。「接続テスト」は保存済み設定の再確認用。
管理sessionを再利用し、確認のためにToolを実行しない。Tool RoutingのLLM設定を必要としない。

入力は表示名とtransport別のtyped fieldに限定する。HTTPはURLとnone/Bearer認証、stdioは
単一のcommandとargs配列を受け取る。shell展開・pipeline・redirectや任意環境変数を注入するUIは設けない。
stdioはBackend環境でプログラムを起動するため、内部環境の開発者が起動するプログラムへの信頼を判断する。
権限管理や事前の実行許可リストは追加しない。

### 接続・credentialの保存

data rootの`mcp-admin/connections.sqlite3`に接続設定・設定revision・現在設定の確認履歴と
接続専有credentialを保存する。専用ディレクトリは0700、DBは0600。credentialを含むため、
このSQLiteを公開成果物へ含めない。OSのファイル権限で保護し、DB自体の暗号化は行わない。
SQLiteのforeign keyとsecure_deleteを有効にし、接続とcredentialを同一トランザクションで削除する。

同じAPIキーを複数接続へ保存できるが、保存レコードとsecret_refは共有しない。
credentialは専用APIから受け取り、通常の接続payloadには混在させず、平文再表示もしない。
authをnoneに変更すると当該接続のcredentialを同じ更新トランザクションで削除する。
削除確認にはcredentialも消えることを明示する。外部サービス側のAPIキー失効は行わない。
更新前の秘密値も、進行中の外部応答から漏らさないためprocess内のredaction対象として終了まで保持する。

### 確認履歴と実行中の変更

現在設定の確認履歴はavailabilityとは別に保存し、一度も成功していない「接続未確認」と、
成功後の「接続不可（最終成功日時）」を区別する。OFFでもこの情報を閲覧できる。
起動後のavailabilityは再確認するが、同じ設定の過去成功日時は保持する。

URL、transport/auth、command/args、credential変更ではrevisionを進め、成功履歴と取得済み一覧をリセットする。
表示名だけの変更は接続確認をやり直さない。希望ONの接続は編集後もONを維持し、確認成功まで実利用を止める。
未確認のOFF接続をONにする操作は、確認成功後だけ希望ONを保存する。後からOFFにした場合、古いON要求の
完了によって復活させない。古い設定revisionの確認結果も採用しない。

Tool/Resource実行中は、接続設定・credentialの変更と削除をBackendで拒否する。表示名だけは変更できる。
Gateで編集中の新規dispatchも拒否し、旧session終了後に設定を入れ替える。古いloop・回答待ちは失効させる。
既に送信した外部副作用の取消しや巻戻しは保証しない。

### 管理API

- `POST /addon-admin/external-connections`: `{display_name, settings}`で登録・接続確認。
- `GET /addon-admin/external-connections/{id}`: 設定、credential設定有無、確認履歴、Capability件数とTool一覧。
- `PUT /addon-admin/external-connections/{id}`: typed設定の置換。最終更新を正とする。
- `PUT /addon-admin/external-connections/{id}/credential`: `{token}`だけを受理して専有credentialを更新・再確認。
- `POST /addon-admin/external-connections/{id}/check`: 現在設定を再確認。
- `DELETE /addon-admin/external-connections/{id}`: 接続と専有credentialを原子的に削除。

`settings`は`{transport: "streamable_http", endpoint, auth: "none" | "bearer"}`、
または`{transport: "stdio", command, args: string[]}`。
既存のON/OFF APIと一覧を維持し、Externalの一覧には設定revision、最終成功・試行日時、固定確認エラーを追加する。
設定編集のURL field以外へendpointを反射しない。secret_ref、token、raw例外をresponseへ含めない。
実行中・競合・接続未確認のON拒否は固定409、入力不正は固定422、保存失敗は固定503を返す。

Tool一覧は名前・説明・状態の閲覧のみ。原則すべての対応Toolを利用対象とし、既存の実行ポリシーは適用する。
個別無効化UIは#185側のBackend対応に合わせて扱う。


## 検証

unitは`backend/tests/unit/test_addon_admin.py`で設定復元、あと勝ち、失敗時の値保持、sanitized API、
OFFと実行待ちの競合、MRTR無効化、外部MCPの二択、自作Addonの部分障害、health閾値と復旧を確認する。

公開MCPとの実接続は次の入口で行う。test所有のEverything processを停止して無操作時の検知を確認する。
LLMやdogfood credentialは使用しない。制御fixtureの成功を第三者MCPの証跡に置換しない。

```bash
RUN_MCP_REAL_SERVICE_TESTS=true \
MCP_REAL_SERVER_ROOT="$PWD/infra/testing/mcp-real-servers" \
backend/.venv/bin/python -m pytest -s backend/tests/integration/test_addon_admin_real_service_integration.py
```

管理画面は5秒間隔とwindow focus復帰時に状態を再取得する。対象行の更新中は多重送信を止め、
更新前に始まったpollの遅延responseで更新結果を上書きしない。取得・更新のtimeoutは30秒。
一覧の並びは障害→有効→無効、同一グループ内は日本語の表示名順とし、同名はIDで安定化する。
無効な接続はbadge対象外。errorをwarningより優先し、warningは自作Addonの部分障害だけに使う。

Frontendのunit/moduleは管理client・controller・component、モックE2Eは
`frontend/e2e/addon-admin.spec.ts`で1280px/320px、keyboard、switchのアクセシブルな名前・状態、focus復帰を検証する。

実Core・Vite・公開MCPをブラウザから通す受入:

```bash
backend/.venv/bin/python scripts/acceptance_addon_admin.py
```

Tool Routingを設定せず、独立したtest data rootと動的portで起動する。通信をmockせず一覧・toggleを操作し、
Backend再起動後のOFF復元、ON後の再確認、MCP停止時の無操作での障害badgeとOFF操作を確認する。
ONの2接続と初期OFFの1接続を登録し、未確認・snapshot未取得のOFF接続も一覧に残ること、
別接続の状態で誤って通過しないよう対象行内で状態を確認することを受入条件に含める。
既存サービス・dogfoodへ接続せず、起動したprocessをteardownする。成功後の公開証跡は
`docs/artifacts/addon-admin-184/browser-public.json`。起動時に旧成功証跡を無効化し、途中失敗やteardown失敗で成功を残さない。
ブラウザの各assert通過時に出力した固定eventだけを`browser-execution.jsonl`へ保存し、
公開証跡からファイル名とSHA-256で参照する。余分なfieldや欠落・順序違反は受入失敗とする。
生のprocess logとブラウザ画像は一時領域へ限定し、終了時に削除する。


## 外部接続管理の受入（#242）

`backend/.venv/bin/python scripts/acceptance_mcp_admin.py`で、実Core・Vite・ブラウザと別processの
制御MCP（HTTP none/Bearer・stdio）を通す。Ollamaは既存モデルを読み取るテスト所有processを一時ポートで
起動し、Core起動時のモデル存在確認だけに使用する。Tool Routingや実会話を呼び出さない。
モデル保存先は`MCP_ACCEPTANCE_OLLAMA_MODELS`で指定でき、既定値は`/usr/share/ollama/.ollama/models`。
モデルを取得・再構築せず、dogfoodへ接続しない。

受入結果と検証範囲は[外部MCP管理の受入記録](mcp-admin-242-acceptance.md)を参照する。

接続確認のtimeoutは`last_check_error=confirmation_timeout`で区別する。OFF時も詳細に前回の失敗理由を固定文言で表示する。
