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
INFERENCE_TARGET_MEMORY_EXTRACTION=ollama/gemma4:e4b
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_MEMORY_CONSOLIDATION=ollama/gemma4:12b
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS=7680
INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=512
INFERENCE_TARGET_EMBEDDING=ollama/nomic-embed-text:latest
INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS=8192
```

任意の`_OPTIONS_JSON`、`_TIMEOUT_SECONDS`、`_MAX_CONCURRENCY`はTargetごとに指定する。未知のTarget／suffix、未知のOption、非正数の上限、`privacy`へのcloud Provider割当ては起動時エラーになる。旧Ollama用途別設定は移行契約ではなく、1つでも指定すると起動を拒否する。

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

`chat`のprobe失敗は起動を中止する。その他のTarget失敗はwarningと`degraded`または`invalid`を記録し、各Callerのfail-safeへ委ねる。定期probeは行わず、起動時と実Inference結果で状態を更新する。

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
