# Character Card V3とPromptBuilderの採用 (2026-07)

## 決定

runtimeの人格定義にはCharacter Card V3を使用し、`characters/<character>/<character>.card.json`を唯一の正規情報源とする。`personality.md`は設計資料として残すが、runtimeでは読み込まない。

カードは`spec: chara_card_v3`かつ`spec_version: 3.0`のみ受理する。未知のルートフィールドと`data`フィールドはカード自体の読み込みを妨げないが、runtimeモデルには保持せず無視する。`data.extensions`は未知namespaceとその値を含めて再帰的に不変化し、runtimeモデルに保持する。一方、未対応の`spec`および将来の`spec_version`は、意味が変化したカードを誤解釈しないよう読み込み時に拒否する。

## Character Cardフィールド

runtimeでは各フィールドを次の用途に限定する。

| フィールド | 用途 |
|---|---|
| `description` | キャラクター概要 |
| `personality` | 性格と話し方 |
| `scenario` | ユーザーとの関係と世界観 |
| `system_prompt` | 常に守る応答方針 |
| `first_mes` | 履歴がない場合にUIへ表示する初回assistant発言 |
| `mes_example` | 会話と話し方の例 |
| `post_history_instructions` | 現在発言より後に適用する最終指示 |

`first_mes`は通常の応答生成promptには含めない。Digital Souls固有設定は`data.extensions.digital_souls`に置き、VOICEVOX設定は`data.extensions.digital_souls.tts_config`から読む。

## PromptBuilder境界

LLM向けmessageの合成は`PromptBuilder`だけが担当する。分類の配置順は次のとおりとする。

1. Character Card
2. RAGコンテキスト
3. privacy処理後に永続化された会話履歴
4. 現在ターンのユーザー原文
5. `post_history_instructions`

Character Card内は`description`、`personality`、`scenario`、`system_prompt`、`mes_example`の順で、空でない値を1つのsystem messageへ配置する。RAGは記憶単位のsystem messageとし、履歴は保存されたuser／assistantのroleと順序を維持する。空の任意要素からmessageは生成しない。

現在ターンの未加工原文は`CurrentUserOriginalText`、privacy処理後の永続化本文は`PersistedMaskedText`として別の型と入口で受ける。`PersistedMaskedText`は会話履歴の永続化契約が所有し、保存入力、保存済みturn、PromptBuilder入力まで同じ型を維持する。これにより、未加工原文を永続化またはprompt履歴へ渡す取り違えを型検査で拒否する。

## Token budget契約

`PromptTokenBudget`は人格およびsystem指示、RAG、履歴、現在発言、最終指示、prompt全体の上限を個別に保持する。

保持優先順位は人格およびsystem指示、現在発言、直前1往復とする。削減優先順位はRAG、古い履歴とする。Issue #6では、これらのbudget値と優先順位を表現し、`PromptBuilder`へ渡し、`BuiltPrompt`で保持できるinterfaceのみを実装する。入力上限を表す`PromptInputLimitError`も型契約として定義する。

実token数の計測、token上限の適用、自動削減、context超過判定、入力上限エラーの実行時送出は未実装である。そのため、現在の`PromptBuilder`はbudget値を理由にmessageを削減、除外、切り詰めせず、上限エラーも送出しない。これらの実処理は後続のIssue #24で実装する。

会話セッションは`ConversationHistoryRepository`の会話IDを保持する。prompt生成時には、その会話に属する完了turnだけを読み、privacy処理後に保存されたuser本文とassistant本文を交互に`PersistedConversationMessage`へ変換する。処理中、失敗、privacy skipのturnはprompt履歴へ含めない。

HTTP `/chat`では、初回リクエストで会話を作成し、応答の`conversation_id`を返す。継続リクエストはそのIDをリクエストの`conversation_id`へ明示的に指定する。同じキャラクターに属する既存会話だけを再開し、存在しない会話IDは404として拒否する。WebSocketでは接続開始時に作成した会話IDをセッション内で保持する。

## ログと依存方向

application logへprompt全文、現在発言、RAG本文、履歴本文を記録しない。診断情報はmessage件数、RAG件数、履歴件数など、内容を含まないメタデータに限定する。

将来の人格コア分離では、人格コア側がCharacter Card loaderやLLM adapterへ依存しない。runtimeが人格コアから得た結果とCharacter CardをPromptBuilderへ渡す方向とし、PromptBuilderは安定した入力型とCharacter Cardモデルだけに依存する。
