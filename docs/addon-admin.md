# MCP/Addonのruntime管理

左サイドバーの「Addon / 連携」から、登録済み接続の希望ON/OFFと接続状態を確認する。
外部MCPの登録・接続設定編集・削除は#242、自作Addonの接続factoryは#221の範囲とする。

## 希望設定と状態

`desired_enabled`はCore全体で共通の希望設定。初回だけ接続Manifestの`core_policy.enabled`を取り込み、
以後はdata root直下の`addon-settings.json`を正本とする。再起動で旧Manifestの値へ戻さない。
ファイルはversion付きの`users.local`にconnection IDとbooleanだけを保存し、一時ファイルから原子的に置換する。
接続先、認証値、snapshot、healthはこのファイルへ保存しない。データ移行・手動backupではこのファイルも保持する。
保存失敗時はAPIを失敗とし、runtimeの希望値を変更しない。MVPの競合はあと勝ち。
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
既存接続は1秒間隔で切断フラグを監視し、通常30秒間隔、5秒timeoutのMCP pingで無操作時の障害も検出する。
一時的なhealth失敗は3回連続で`unavailable`とする（通常の検知上限は約105秒＋event loopの遅延）。
明確な切断、認証・protocol失敗は即反映する。個別Toolの単発timeout、5xx、引数不正、対象不存在や権限拒否は
connection全体の障害にしない。health目的でToolを実行しない。

接続・初回discoveryの上限は20秒。接続失敗後は5秒から最大300秒の指数backoffで再接続し、希望ONを保持する。
稼働中sessionでhealthが回復した場合も、利用再開前にdiscoveryを確認する。snapshotの反映は既存の次loop境界を守る。
OFF中はpingを休止する。OFF操作によってprocessの再起動や停止を行わない。

## APIと表示名

- `GET /addon-admin/connections`: snapshotの有無に関係なく登録済み全件を取得。
- `PATCH /addon-admin/connections/{connection_id}`: `{"desired_enabled": true}`または`false`だけを受理。
- responseはID、表示名、source種別、希望値、availability、effective state、固定error code、最終確認時刻だけ。
- 未知connectionは404、不正bodyは固定422、設定保存失敗は固定503。raw errorや入力JSONを反射しない。

`DS_MCP_CONFIG`の任意項目`display_names`に`{"connection-id": "表示名"}`を指定できる。
表示名の未設定時はconnection IDを使い、endpoint、command、secretから生成しない。
接続設定は従来どおり管理側が用意する。Frontendから任意URLやcommandを送信する経路は追加しない。

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
更新前に始まったpollの遅延responseで更新結果を上書きしない。取得・更新のtimeoutは10秒。
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
既存サービス・dogfoodへ接続せず、起動したprocessをteardownする。成功後の公開証跡は
`docs/artifacts/addon-admin-184/browser-public.json`。起動時に旧成功証跡を無効化し、途中失敗やteardown失敗で成功を残さない。
生のprocess logとブラウザ画像は一時領域へ限定し、終了時に削除する。
