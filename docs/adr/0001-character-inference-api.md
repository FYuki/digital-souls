# ADR 0001: キャラクター文脈付き推論API

Status: Proposed

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。Core は tool 実行ループを持たず Agent framework は不要とする判断と、tool を Core で実行しない記述。会話中のツール利用は Core が持つ。呼び出し側の function tools をそのまま返す動作は互換のため維持）。

一部を[ADR 0028](0028-character-book.md)の Character Book の解釈で置き換えます（2026-10-11 ユーザー承認。Lore を最新の user 発言だけとの単純な key 一致で選び、非対応の field を起動時に拒否する扱い。Lore を記憶と分ける扱いは維持）。

一部を[ADR 0026](0026-prompt-assembly.md)のプロンプトの組み立てで置き換えます（2026-10-11 ユーザー承認。呼び出し側の system を人格の後ろに置く扱い、Card の field の連結方法、Lore を最新の user 発言だけで照合する前提の組み立て、`context_budget_bytes` によるバイト予算。Provider port・送信 policy・parameter の扱いは維持）。

日付: 2026-10-03

## 背景と選択肢

最初の受入れは、公開Mioriカードをsystemへ反映し、通常会話と外部Agentのtool call往復を
キャラクターが保持するモデル設定で推論できることです。製品言語は初期repoで未選定でした。
ユーザーはPython/FastAPI/LiteLLMを第一候補として実装を了承しています。
Nodeは文書検証用に既存採用されていますが、製品言語の選定ではありません。

独自の各社HTTP/SSE adapterは保守範囲が広く、Agent frameworkはtool実行ループを持たないCoreに
不要です。LiteLLM SDKのcompletion adapterを採用し、Agentは独立した外部clientに置きます。
Responses先行は現時点で必須client/modelが特定されていないため保留します。

## 決定案

Python 3.12.3、FastAPI、Pydanticによる厳密な入力contractとLiteLLM SDKで小さい構成を作ります。
依存は[pyproject](../../core/pyproject.toml)と[uv.lock](../../core/uv.lock)で固定します。
HTTP/SDKから独立したapplicationのProvider portを、通常入口とalias入口で共用します。
schema/applicationはSDKやHTTPへ依存しません。実行中のCharacter/Profileはimmutable snapshotです。
明示的な送信policyとparameter allowlistがない能力は拒否し、fallbackとsilent dropを禁止します。

人物の技能や記憶は`ContextSource`の境界だけを用意し、初期版は空です。callerのtoolsは目的に
応じて外部Agentが渡し、LLMが生成したtool callをそのまま返します。Coreで実行しません。
認証scopeはサーバー由来の型を境界に渡し、初期HTTPは信頼された単独利用者のlocalhost専用です。
multi-tenantや私的記憶の公開配信分離を実装済みとは称しません。

## 影響と制約

対応範囲・エラー・stream・Lore・tokenとbyte budgetの区別は[API契約](../api.md)に記載します。
SDKの将来変更から外向きcontractを守るため、socket禁止のUT/IT1と実SDKのmock transportを用います。
実モデルのpersona品質、全provider/model能力はこのテストでは確認できません。
SDK依存は大きいため固定版の更新時もこの契約テストとsecurity reviewが必要です。
親reviewでLiteLLM 1.77.7のAnthropic stream iteratorがHTTP Responseのclose所有権を
公開しないことを確認したため、実streamは所有closeをmock transportで実証したネイティブ
OpenAI adapterに限定します。他providerの独自SSEや内部iteratorへの場当たり的なclose実装は
追加せず、送信前に未対応エラーとします。`openai/`内にもResponses自動変換があるため、
pinned SDKと同じローカルroute probeで`mode=chat`のnative経路だけを許可し、既知Responses・
明示Responses・未知modeを拒否します。SDK変更後もadapterごとの実証が必要です。

参考: [LiteLLM入力](https://docs.litellm.ai/docs/completion/input)、
[stream](https://docs.litellm.ai/docs/completion/stream)、
[function calling](https://docs.litellm.ai/docs/completion/function_call)。
関連Issue・PR・正確な検証revisionは[証跡](../evidence/2026-10-03-inference-api.md)で管理します。
