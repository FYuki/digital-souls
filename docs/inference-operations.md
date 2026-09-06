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
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_MEMORY_EXTRACTION_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_MEMORY_CONSOLIDATION=ollama/gemma4:12b
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_MEMORY_CONSOLIDATION_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_EMBEDDING=ollama/nomic-embed-text:latest
INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS=8192
```

任意の`_OPTIONS_JSON`、`_TIMEOUT_SECONDS`、`_MAX_CONCURRENCY`はTargetごとに指定する。未知のTarget／suffix、未知のOption、非正数の上限、`privacy`へのcloud Provider割当ては起動時エラーになる。旧Ollama用途別設定は移行契約ではなく、1つでも指定すると起動を拒否する。

画面知覚を有効にする場合だけ、optionalなVision Targetを設定する。設定しない環境では`unconfigured`となり、Backendと通常Chatは従来どおり起動する。画像の取得・共有session・cloud同意経路が接続される前に、この設定だけで画面送信が開始されることはない。

```env
INFERENCE_TARGET_VISION=ollama/gemma4:e4b
INFERENCE_TARGET_VISION_MAX_INPUT_TOKENS=7168
INFERENCE_TARGET_VISION_MAX_OUTPUT_TOKENS=1024
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
`openai-api`へ切り替える場合は実行前の利用者承認とBackend専用secretが必要である。

参照ruleの固定fixtureは次で評価する。

```bash
npm run eval:screen-reference:conformance
```

この結果は本文非保持のsynthetic route conformanceであり、実モデル品質の代用ではない。実接続証跡は
UTC日時、commit、環境区分、Provider／Model、合成fixture ID、成功可否、分岐、区間遅延だけを#217へ
記録する。対象特定率と実質回答開始P50／P95はモデルごとの反復結果から別途集計する。
