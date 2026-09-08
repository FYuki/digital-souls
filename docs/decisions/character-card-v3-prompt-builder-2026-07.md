# Character Card V3とprompt合成境界（2026-07）

状態: **ACTIVE**。CCV3 `data.character_book`によるCharacter Loreの追加順序、独立した
token accounting、全体budget超過時の削減順は、後続ADR
`character-book-runtime-2026-08.md`を優先する。

経験により変化するMutable Personality、Relationship State、Life Stateのprompt反映は、後続ADR
`character-life-memory-personality-autonomy-2026-09.md`を優先する。

## 決定

runtimeの固定人格・世界観・応答方針の定義は`chara_card_v3`かつ`spec_version: 3.0`のCharacter Cardを
Source of Truthとする。`personality.md`は設計資料として残すが、runtimeのpromptには読み込まない。
未対応の`spec`・`spec_version`は明示的に拒否し、未知フィールドは意味を変えずに保持する。

一方、経験から形成されるMutable PersonalityはCharacter Cardを書き換えず、別のversionedなPersonality Stateを正本とする。
Big Five Aspects 10因子を内部数値として保持し、PromptBuilderへ渡す際は自然言語の人格コンテキストへ変換する。
Immutable Core / Character Cardの固定方針をMutable Personalityが上書きしてはならない。

Relationship StateとLife StateもCharacter Cardへ書き戻さず、相手・現在状態に応じたruntime contextとして合成する。
Egogramは表出一貫性の質的評価にのみ使用し、promptへ人格パラメータとして注入しない。

独自設定は`data.extensions.digital_souls`に置く。VOICEVOX設定は
`data.extensions.digital_souls.tts_config`だけから型付き境界を通して取得し、欠落と不正値を
異なるエラーとして扱う。

## Character Cardフィールド

| フィールド | 用途 | promptでの扱い |
|---|---|---|
| `description` | キャラクター概要 | 人格領域の先頭 |
| `personality` | 固定人格・話し方のベースライン | `description`の後 |
| `scenario` | ユーザーとの関係と世界観 | `personality`の後 |
| `system_prompt` | 必須の応答方針 | `scenario`の後 |
| `mes_example` | 会話・話し方の例 | 人格領域の末尾 |
| `first_mes` | 履歴がない場合の初回表示 | 通常promptへ含めない |
| `post_history_instructions` | 履歴を踏まえた最終指示 | 現在発言の直前 |

空文字の任意要素は見出しごと省略する。

## 合成順序と入力境界

`PromptBuilder`だけがLLMメッセージを合成する。

概念上の順序は次とする。

1. Character Cardの固定人格・世界観・応答方針
2. 確定済みCharacter Runtime Context
   - Mutable Personality（Big Five Aspectsから生成した自然言語）
   - 対象ユーザー等とのRelationship State
   - 会話に関連するLife State（Interest / Intention / Ongoing Activity等）
3. RAGコンテキスト
4. SQLiteに保存された過去のuser／assistant履歴
5. `post_history_instructions`
6. 現在ターンのuser原文

Character Runtime Contextは応答開始時点で確定済みのversionをsnapshotし、一つの応答途中にbackground処理でPersonality / Relationship / Life Stateが更新されても参照versionを切り替えない。

Life Stateを常に全件注入せず、現在会話に関連するものだけを選択する。Share Candidateは発話義務ではなく、会話文脈・idle条件等の別policyで利用可否を決める。

過去履歴は`MaskedHistory`、現在原文は`CurrentUserMessage`として別の型で受け取る。
現在原文は保存前の入力であり、privacy処理後の履歴へbuilderが追加することはない。
人格コアは将来この入力型を生成して利用できるが、`PromptBuilder`から人格コアの具体実装へは
依存しない。

## token budget

`TokenBudget`は全体、人格、RAG、履歴、現在発言、最終指示を個別に制限する。
Character Runtime Contextは人格領域の一部としてbudget計上する。

固定人格、現在発言、直前1往復は必須とし、これらが個別上限または全体上限を超える場合は
`PromptInputLimitError`を返す。Mutable Personality等のRuntime Contextは意味を壊す途中切断をせず、構造化された短縮表現または項目数削減で調整する。

任意要素の削減は、後続`character-book-runtime-2026-08.md`のCharacter Lore契約を優先しつつ、RAG、古い履歴、低関連のLife State等を対象にする。Immutable Coreをbudget都合で削除しない。

## ログ

完成prompt、Character Cardの会話例、Mutable Personalityの自然言語本文、Relationship / Life State本文、RAG本文、履歴本文、現在発言はapplication logへ記録しない。
診断情報はversion、メッセージ数、token数、省略件数など本文を含まないmetadataに限定する。
