# Inference Provider運用手順

## 目的

Inference Targetの環境設定、起動時確認、readiness、実接続受入を安全に運用する。Provider／Modelはインフラ条件に依存するため、version管理された共通YAMLではなく各環境のローカルenvへ置く。設計上の正本は[`decisions/inference-provider-foundation-2026-09.md`](decisions/inference-provider-foundation-2026-09.md)とする。

## Target設定

各Targetは`provider/model`形式と入力上限を必須とし、生成系Targetは出力上限も指定する。Model ID内の追加`/`は保持される。

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
INFERENCE_TARGET_CHAT=ollama/gemma4:e4b
INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS=7168
INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS=1024
INFERENCE_TARGET_PRIVACY=ollama/gemma4:e4b
INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_PRIVACY_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_MEMORY_EXTRACTION=ollama/gemma4:e4b
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS=32768
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=4096
INFERENCE_TARGET_MEMORY_EXTRACTION_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_MEMORY_CONSOLIDATION=ollama/gemma4:12b
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_MEMORY_CONSOLIDATION_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_EMBEDDING=ollama/nomic-embed-text:latest
INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS=8192
```

任意の`_OPTIONS_JSON`、`_TIMEOUT_SECONDS`、`_MAX_CONCURRENCY`はTargetごとに指定する。未知のTarget／suffix、未知のOption、非正数の上限、`privacy`へのcloud Provider割当ては起動時エラーになる。旧Ollama用途別設定は移行契約ではなく、1つでも指定すると起動を拒否する。

会話で外部MCPを利用する場合はoptionalな`tool-routing` Targetを設定する。
`INFERENCE_TARGET_TOOL_ROUTING`と入力・出力上限を指定し、structured generationとtoken estimateに対応する
Provider／Modelを選ぶ。未設定時はTool利用を無効にして通常会話を維持する。
管理設定、秘密情報の扱い、実接続受入は[会話からの外部MCP利用](tool-use.md)を参照する。

画面知覚を有効にする場合だけ、optionalなVision Targetを設定する。設定しない環境では`unconfigured`となり、Backendと通常Chatは従来どおり起動する。画像の取得・共有session・cloud同意経路が接続される前に、この設定だけで画面送信が開始されることはない。

```env
INFERENCE_TARGET_VISION=ollama/gemma4:e4b
INFERENCE_TARGET_VISION_MAX_INPUT_TOKENS=7168
INFERENCE_TARGET_VISION_MAX_OUTPUT_TOKENS=1024
INFERENCE_TARGET_VISION_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_VISION_TIMEOUT_SECONDS=30
INFERENCE_TARGET_VISION_MAX_CONCURRENCY=1
```

Vision入力はPNGまたはJPEGの1枚に限定し、各辺2,560 px、decode後4,194,304 pixel、encoded 5 MiBを上限とする。CoreがMIME、magic bytes、実decode、寸法を検証し、AdapterだけがProvider payload用Base64を作る。任意URL／pathはInference契約に公開しない。画像tokenは1枚1,120 tokenを含むProvider別の保守的推定とし、実usageやexact計数とは区別する。

構造化出力ではCoreのJSON Schemaを検証の正本とする。AdapterはProviderのgrammar実装が受け付けない制約だけを送信schemaから除外し、生成結果はRouterで除外前の完全schemaに再検証する。現在はOllama向けに文字列長・配列長制約、OpenAI API向けに`allOf`、`if`、`then`などの未対応合成制約を除外する。互換化によってCoreの受入条件を緩めない。

画面参照の競合判定は独立Targetを増やさず、`screen-reference` callerからChat Targetを利用する。
ruleで確定できるturnではLLMを呼ばず、構造化出力不正、timeout、未設定時は画像を送らない分岐へ
縮退する。判定へ渡せる履歴も画面lineageとcloud派生履歴の同意で制限し、新しい共有同意を過去sessionの
履歴へ遡って適用しない。

## OpenAI認証

OpenAI APIとChatGPTサブスクリプションは別Providerとして設定する。

### API key

`openai-api/<model>`を割り当てたBackendだけへ`OPENAI_API_KEY`をsecretとして渡す。env exampleやGit管理対象へ実値を保存しない。endpointは公式OpenAI APIに固定し、互換gateway用の上書き変数は受け付けない。

```env
INFERENCE_TARGET_HEAVY_REASONING=openai-api/<model>
INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS=24576
INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS=8192
INFERENCE_TARGET_HEAVY_REASONING_OPTIONS_JSON={"reasoning_effort":"high"}
OPENAI_API_KEY=<backend専用secret>
```

### ChatGPTサブスクリプション

`openai-codex/<model>`では公式Codex runtimeへ認証を委譲する。Backend専用の既存絶対directoryを`OPENAI_CODEX_HOME`に、必要なら実行可能なCodex絶対pathを`OPENAI_CODEX_EXECUTABLE`に指定する。共用の認証cacheを複製せず、API key loginはサブスクリプション認証として受け付けない。

```env
INFERENCE_TARGET_HEAVY_REASONING=openai-codex/<model>
INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS=24576
INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS=8192
INFERENCE_TARGET_HEAVY_REASONING_OPTIONS_JSON={"reasoning_effort":"high"}
OPENAI_CODEX_HOME=/absolute/path/to/backend-codex-home
OPENAI_CODEX_EXECUTABLE=/absolute/path/to/codex
```

## 起動確認とhealth

Backend起動時はInferenceを送らず、次だけを確認する。

- Ollama: endpoint到達性とModel metadata
- OpenAI API: 課金されないModel取得によるcredential、endpoint、Model確認
- Codex runtime: version／必須隔離機能と`codex login status`

OllamaのVisionではModel metadataの`vision` Capabilityも確認する。OpenAIのModel取得は入力modalitiesを返さないため、接続確認後もVisionを`unverified`とし、最初の実リクエスト成功後に`verified`へ更新する。`chat`のprobe失敗は起動を中止する。その他のTarget失敗はwarningと`degraded`または`invalid`を記録し、各Callerのfail-safeへ委ねる。Visionのmodel非対応、model不在、接続不能も通常Chatを停止しない。定期probeや自動retry／Provider fallbackは行わず、起動時と実Inference結果で状態を更新する。

```json
{"status":"ready"}
```

`GET /health/ready`は上記または`{"status":"not_ready"}`だけを返し、HTTP statusは200／503とする。`GET /health/inference`はTarget名、状態、検証度、要求Capability、共通error category、最終確認時刻だけを返す。Provider、Model、endpoint、auth詳細、raw errorは返さない。

Inference requestの構造化logはrequest ID、Caller、Target、Capability、Provider、Model、auth kind、latency、外部request回数、TokenEstimate／InferenceUsage、成否、共通error categoryだけを含む。prompt、response、secret、authorization header、認証cache path、raw provider errorを記録しない。

## Real-service受入

合成入力だけを用い、対象Providerに必要なTarget設定と認証をローカルenvへ設定して実行する。

```bash
RUN_INFERENCE_REAL_SERVICE_TESTS=true \
INFERENCE_ACCEPTANCE_PROVIDER=ollama \
INFERENCE_ACCEPTANCE_ENVIRONMENT=dev \
npm run test:integration:inference
```

`INFERENCE_ACCEPTANCE_PROVIDER`は`ollama`、`openai-api`、`openai-codex`のいずれかを指定する。テストは設定済み全Targetの起動probe後、選択Providerの要求Capabilityを実行し、標準出力へ本文を含まないJSON証跡を出す。Ollamaではtext、stream、structured、embedding、token estimate、OpenAI APIではtext、structuredと設定時のembedding、Codexではstateless textを確認する。

#181のIssueコメントへCapabilityごとに次の形式で転記する。失敗時もraw errorを転記しない。

```text
- 実行時刻: <UTC ISO 8601>
  commit SHA: <full SHA>
  環境区分: dev | dogfood
  Provider / Model: <provider> / <model>
  Capability: <capability>
  結果: success | failure
```

prompt／response、token／credential、endpoint／hostname、個人情報をIssue、artifact、実行logへ追加しない。必要な全Providerのsuccess記録が#181へ揃うまでIssueをcloseしない。

## 画面知覚の実接続受入

#217では公開・合成画像だけを使い、Ollamaの`gemma4:e4b`と、利用者が明示承認した場合だけ
`openai-api`の画像入力を個別に確認する。起動probeだけをVision成功とは扱わず、質問対象を1つ特定する
case、複数候補、対象なし、判読不能を実推論する。OpenAI APIは課金と外部送信を伴うため、credentialが
存在しても実行前に毎回利用者確認を得る。API key、画像、質問、観測本文、endpoint、raw errorは証跡へ
残さない。

```bash
RUN_SCREEN_VISION_REAL_TESTS=true \
SCREEN_VISION_ACCEPTANCE_PROVIDER=ollama \
INFERENCE_ACCEPTANCE_ENVIRONMENT=dev \
npm run test:integration:screen-vision
```

実行環境には通常Target一式と、選択Providerを指す`INFERENCE_TARGET_VISION`を設定する。
Ollamaの構造化Vision受入では生成揺らぎを抑えるため`INFERENCE_TARGET_VISION_OPTIONS_JSON={"temperature":0}`も設定する。
`openai-api`へ切り替える場合は実行前の利用者承認とBackend専用secretが必要である。

参照ruleの固定fixtureは次で評価する。

```bash
npm run eval:screen-reference:conformance
```

この結果は本文非保持のsynthetic route conformanceであり、実モデル品質の代用ではない。実接続証跡は
UTC日時、commit、環境区分、Provider／Model、合成fixture ID、成功可否、分岐、区間遅延だけを#217へ
記録する。対象特定率と実質回答開始P50／P95はモデルごとの反復結果から別途集計する。


## Episode / Fact抽出の入力と出力予算

#340の構造化抽出はスレッドの発言範囲と既存Episode / Factの文脈を使うため、
設定例のMEMORY_EXTRACTIONを入力32,768・出力4,096 tokensにする。
実モデルがこの合計contextを扱えることをdevの実接続で確認し、必要に応じて設定する。
既存環境の設定やdogfood稼働データは、文書・設定例の更新だけでは変更されない。

会話応答は抽出を待たない。予約は会話履歴と同じSQLite transactionで確定し、
通常起動時に未処理予約を回収する。抽出失敗・モデル識別情報の取得失敗では予約を残して再試行する。
推論時間は既存のMEMORY_FORMATION_LLM_TIMEOUT_SECONDSとTOTAL_TIMEOUT_SECONDSで調整する。
入力予算を超えた発言は分割して処理し、完了したように見せて切り捨てない。
既存一覧が入力予算を超える場合は、現在の会話から取得した候補を固定し、同一スレッドの
保存済み一覧を予算内のページに分けて全件照合する。全ページの結果が揃うまで登録せず、
一意に確認できた対象だけを補足・訂正する。ページをまたいで複数候補がある場合や、
候補が1件でも確定できない場合は、既存本文を上書きせず新規情報として保持する。
更新対象が定まった後は、その現在内容を読み、補足・訂正内容を個別に構成する。

生成schemaは未知の項目もnullや空配列で明示させ、操作に必要な本文の省略を防ぐ。
schemaはモデルの入力にも提示し、その分も入力予算へ含める。
操作・対象候補が揃った後、内容を変更しないREFERENCEを除いて、一件ずつ出典から5Wを再検証する。
聞いた経験の人物・場所と話題の人物・場所、明言された相対日時を区別するための段階である。
下書きの人物・日時・場所・理由・文脈を再提示せず、対象話題と操作・出典から独立に読み直す。
所有キャラクターの情報を明示する。会話履歴の当事者・発言者はアプリ側で確定し、
新規Episodeではuser発言なら聞いた経験、assistant発言なら語った経験として人物ID・役割・述語を設定する。
継続時は既存Episodeの視点を維持し、途中の返答で話し手と聞き手を反転させない。
会話履歴はキャラクターの居場所を持たないため、Episodeの場所はunknownとし、
発言内の店・旅行先はFactの場所として扱う。経験日時も従来どおり元発言からアプリが設定する。
再検証へ全保存済み一覧を渡さず、対象がある場合はその一件だけを渡す。
操作・対象ID・anchor・変更項目はこの段階では再選定させない。
再検証後も型・日時出典・引用・privacy・保存直前の版検証を通し、全件が揃うまで登録しない。

出典の全範囲はSQLiteに保持する。推論へは現在見えている発言と重なる出典範囲、
最初/最後の取得時刻、出典件数を渡し、版の蓄積だけで入力が膨張しないようにする。
推論時間の上限は各抽出・照合・訂正構成・5W再検証の呼出しに適用し、ページ間では停止要求を確認する。
中断・失敗時には未完了予約を残し、既存のlease更新と再実行の冪等性で回復する。
実モデルによる抽出・照合品質と大量Factでの処理時間は#340の最終受入で確認する。


### #340の専用実接続runtime

commit済みのworktreeで `python scripts/acceptance_episodic_memory.py` を実行すると、
devの既存Ollamaを外部サービスとして利用し、新しいtest data rootと動的portで
Backend・Frontendを起動する。gemma4:e4b / nomic-embed-text:latestが導入済みであることを確認する。
常用の.env・data rootはコピーせず、ローカル推論Target・token予算を専用processへ設定する。

出力されるrunRoot内のruntime-manifest.jsonに、commit・model digest・予算・所有processとURLを記録する。
通常会話・管理UIの試験はそのURLへ行う。停止は同じrunRootに `stop` ファイルを作成するか、
runnerへ終了signalを送る。所有するBackend/Frontendだけを停止し、共有Ollamaは操作しない。
合成データとmanifestは試験結果の調査用に残す。
正常停止後は `python scripts/acceptance_episodic_memory.py --resume-root <runRoot>` で、
同じtest data rootを保持して新しいcommit・portで再起動できる。
実行中のrootや通常/dogfoodのdata rootは受け付けず、前回manifestをrun ID付きで保持する。
会話ログは採用した記憶ID・内容版・日時精度の既存metadataだけを追加で有効にする。
モデルが未ロードの状態での既存realtime privacy timeoutと、明示的にロード済みの機能検証を区別して記録する。
このrunnerのreadyは起動確認だけであり、#344の抽出・検索・管理操作・応答の合格証跡ではない。

訂正・削除の試験では、操作前に別スレッドで一度検索・応答まで通し、その返答を元にした
非同期抽出が落ち着いてから管理UIを操作する。その後さらに新しいスレッドで検索し、
訂正後のID・内容版だけでなく、操作前の検索回答から作られた記録が古い内容を返さないことを調べる。
元Factの新版が検索されたことだけでは合格にしない。旧内容も同時に採用・回答された場合は不合格とする。
これは既存の訂正・削除による参照失効の確認であり、別スレッド間のFact統合（#354）の実施を前提にしない。
応答本文は通常ログへ追加せず、採用ID・版、合成シナリオの期待内容を含む/旧内容を含まないという判定、
実行commit・model・環境を証跡に残す。失敗と未検証の結果も保存する。

### 検索回答の出典依存

persona-memory schema v4は、本文を持たない回答元情報と、参照した記憶ID・内容版を追加する。
v2/v3からの追加migrationは既存レコードを保持し、旧backupの読取検証も維持する。
テキスト会話では、回答完了の保存・抽出予約より先に参照情報を記録する。
生成に採用した履歴の回答についても依存版を引き継ぎ、元Factの訂正後に同じ内容を
別Fact・Episode・直近履歴の回答から使い直す経路を出典で判定する。
循環・欠損・旧版・失効した参照関係は有効な根拠として扱わない。

保存前に採用版が変わった場合も、採用した旧版への依存を記録して再抽出を拒否する。
抽出入力・保存直前・SQLiteからの検索投影に加え、直近履歴の投影からも失効した回答を除く。
履歴の元本文は書き換えない。回答依存のないユーザー発言や独立した新版の根拠と区別する。

参照情報を記録しなかった過去runの回答について、migrationだけで依存を推測復元しない。
実接続の訂正・削除受入には、依存記録を有効にして新しく開始した合成会話を使い、
以前の失敗runは失敗の証跡として保持する。音声経路への記録接続、
元会話の更新に対する依存先の再検証と実接続の完了状態は#344で確認する。

削除はFactの内容版から統合・Episode参照・回答元をたどり、依存する本文と索引削除予約を
同一transactionで確定する。旧preferenceとそのconsolidation由来も元発言・ID参照を追跡する。
独立して手動訂正された最新内容は保持し、依存する旧版の回答から作られた記憶は消去する。
Episodeは本文を消去・無効化し、IDと経験日時を残す。
Fact訂正後の旧preferenceも無効化して検索から除外し、無効出典からの再保存・既に計画された再統合を拒否する。

実接続の再検証では、通常画面からの応答と回答由来metadataの記録に成功しても、
非同期抽出が再試行を経て `MIXED` となり、Episodeのみ保存されてFact・取得参照が欠けるケースを確認した。
この結果は形成受入の不合格とし、訂正・削除の実応答検証へ進めたことにはしない。
抽出例外ログは例外の型とコード位置だけを記録し、例外本文・元会話・localsを出力しない。
