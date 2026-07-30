# Character Card V3とPromptBuilderの採用 (2026-07)

## 決定

runtimeの人格定義にはCharacter Card V3を使用し、`characters/<character>/<character>.card.json`を唯一の正規情報源とする。`personality.md`は設計資料として残すが、runtimeでは読み込まない。

カードは`spec: chara_card_v3`かつ`spec_version: 3.0`だけを受理する。`spec`が異なるカードと未対応のversionは拒否する。未知のルートフィールドと`data`フィールドは`extra_fields`へ分離し、未知データを欠落させず再帰的に不変化してruntimeモデルに保持する。`data.extensions`も未知namespaceとその値を含めて再帰的に不変化し、runtimeモデルに保持する。

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
| `creator` | カード作成者のメタデータ。promptには含めない |
| `character_version` | キャラクター定義の版。promptには含めない |
| `creator_notes` | 作成者向け注記。promptには含めない |
| `alternate_greetings` | 明示的に選択する代替初回表示。通常promptには含めない |
| `group_only_greetings` | グループ会話専用の初回表示。通常promptには含めない |
| `tags` | 検索・分類用メタデータ。promptには含めない |
| `extensions` | application固有設定のnamespace。promptには含めない |
| `extra_fields` | 前方互換のため保持する未知データ。promptには含めない |

`first_mes`は履歴がない新規セッションの初回assistant表示データとして扱うが、通常の応答生成promptには含めない。HTTP／WebSocketへの表示配線はIssue #26へ委ねる。`alternate_greetings`は利用者が明示的に選択した場合だけ代替初回表示に使用し、現行runtimeでは自動選択しない。`group_only_greetings`はグループ会話専用だが、現行runtimeにはグループ会話の入口がないため使用しない。Digital Souls固有設定は`data.extensions.digital_souls`に置き、VOICEVOX設定は`data.extensions.digital_souls.tts_config`から読む。

## PromptBuilder境界

LLM向けmessageの合成は`PromptBuilder`だけが担当する。分類の配置順は次のとおりとする。

1. Character Card
2. RAGコンテキスト
3. privacy処理後に永続化された会話履歴
4. 現在ターンのユーザー原文
5. `post_history_instructions`

Character Card内は`description`、`personality`、`scenario`、`system_prompt`、`mes_example`の順で、空でない値を1つのsystem messageへ配置する。RAGは記憶単位のsystem messageとし、履歴は保存されたuser／assistantのroleと順序を維持する。空の任意要素からmessageは生成しない。

現在ターンの未加工原文は`CurrentUserOriginalText`、privacy処理後の永続化本文は`PersistedMaskedText`として別の型と入口で受ける。これにより、未加工原文をprompt履歴へ渡す取り違えを型検査で拒否する。privacy scanner・sanitizerとSQLite履歴からこの型を生成する配線は本タスクでは実装しない。

## Token budget契約

`PromptTokenBudget`は人格およびsystem指示、RAG、履歴、現在発言、最終指示、prompt全体の上限を個別に保持する。

保持優先順位は人格およびsystem指示、現在発言、直前1往復とする。削減優先順位はRAG、古い履歴とする。本タスクでは、これらのbudget値と優先順位を表現し、`PromptBuilder`へ渡し、`BuiltPrompt`で保持できるinterfaceのみを実装する。

実token数の計測、token上限の適用、自動削減、context超過判定、入力上限エラーの型定義と実行時送出は未実装である。そのため、現在の`PromptBuilder`はbudget値を理由にmessageを削減、除外、切り詰めせず、上限エラーも送出しない。これらの実処理は後続のIssue #24で実装する。

failed履歴にも同じ上限を適用し、履歴は可能な限りuser／assistantの1往復単位で扱う。必須保持部分だけで上限を超える場合は黙って切り詰めず、入力上限エラーとする。

## ログと依存方向

application logへprompt全文、現在発言、RAG本文、履歴本文を記録しない。診断情報はmessage件数、RAG件数、履歴件数など、内容を含まないメタデータに限定する。

将来の人格コア分離では、人格コア側がCharacter Card loaderやLLM adapterへ依存しない。runtimeが人格コアから得た結果とCharacter CardをPromptBuilderへ渡す方向とし、PromptBuilderは安定した入力型とCharacter Cardモデルだけに依存する。

## 関連Issueとの責務境界

- Issue #3: 人格コアのプロセス、namespace、API分離
- Issue #24: 履歴選択、failed履歴の復元、token計測、実際の削減、入力上限判定
- Issue #25: privacy scanner、sanitizer、`PersistedMaskedText`の生成
- Issue #26: HTTP／WebSocketとconversation lifecycleの統合
- 本タスク: Character Card V3 loader、PromptBuilderの入力・合成契約、token budget interface
