# Character Card V3とprompt合成境界（2026-07）

状態: **ACTIVE**。CCV3 `data.character_book`によるCharacter Loreの追加順序、独立した
token accounting、全体budget超過時の削減順は、後続ADR
`character-book-runtime-2026-08.md`を優先する。

## 決定

runtimeの人格定義は`chara_card_v3`かつ`spec_version: 3.0`のCharacter Cardを
Source of Truthとする。`personality.md`は設計資料として残すが、runtimeのpromptには読み込まない。
未対応の`spec`・`spec_version`は明示的に拒否し、未知フィールドは意味を変えずに保持する。

独自設定は`data.extensions.digital_souls`に置く。VOICEVOX設定は
`data.extensions.digital_souls.tts_config`だけから型付き境界を通して取得し、欠落と不正値を
異なるエラーとして扱う。

## Character Cardフィールド

| フィールド | 用途 | promptでの扱い |
|---|---|---|
| `description` | キャラクター概要 | 人格領域の先頭 |
| `personality` | 性格と話し方 | `description`の後 |
| `scenario` | ユーザーとの関係と世界観 | `personality`の後 |
| `system_prompt` | 必須の応答方針 | `scenario`の後 |
| `mes_example` | 会話・話し方の例 | 人格領域の末尾 |
| `first_mes` | 履歴がない場合の初回表示 | 通常promptへ含めない |
| `post_history_instructions` | 履歴を踏まえた最終指示 | 現在発言の直前 |

空文字の任意要素は見出しごと省略する。

## 合成順序と入力境界

`PromptBuilder`だけが、次の順でLLMメッセージを合成する。

1. Character Cardの人格領域
2. RAGコンテキスト
3. SQLiteに保存された過去のuser／assistant履歴
4. `post_history_instructions`
5. 現在ターンのuser原文

過去履歴は`MaskedHistory`、現在原文は`CurrentUserMessage`として別の型で受け取る。
現在原文は保存前の入力であり、privacy処理後の履歴へbuilderが追加することはない。
人格コアは将来この入力型を生成して利用できるが、`PromptBuilder`から人格コアの具体実装へは
依存しない。

## token budget

`TokenBudget`は全体、人格、RAG、履歴、現在発言、最終指示を個別に制限する。
人格、現在発言、直前1往復は必須とし、これらが個別上限または全体上限を超える場合は
`PromptInputLimitError`を返す。任意要素の削減はRAG、古い履歴、最終指示の順とする。

## ログ

完成prompt、Character Cardの会話例、RAG本文、履歴本文、現在発言はapplication logへ
記録しない。診断情報はメッセージ数、token数、省略件数など本文を含まないmetadataに限定する。
