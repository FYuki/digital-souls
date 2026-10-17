# ADR 0016: 記憶の種別・正本・日時

Status: Accepted

実施注記（2026-10-08）: [逐語記憶撤去の証跡](../evidence/2026-10-08-verbatim-memory-removal.md)のschema版6で既存の逐語記憶を新形式へ移送せず削除しました。Episode・Fact・Semanticの正本登録・版・撤回は実装済みで、検索は[ADR 0022](0022-memory-retrieval-from-records.md)の正本を使います。形成・保存判定・保存文生成は未実装です。本文の「現行Core」は決定時点の背景として保持します。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（「影響と移行」でSQLiteとPostgreSQLの両adapterに同じ契約試験を用意する方針）。

日付: 2026-10-06

Accepted日: 2026-10-07

根拠: 2026-10-06のユーザー決定（[SPEC §4.1の決定表](../../SPEC.md#41-決定済み2026-10-06のユーザー決定)）と
[PR #67](https://github.com/FYuki/digital-souls-core/pull/67)のレビュー。

## 背景

[ADR 0015](0015-memory-model-reorganization.md)の移設方針に基づき、PoCの
[Episode・Fact・Semantic境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/episode-fact-semantic-boundaries-2026-09.md)、
[用語契約](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/memory-personality-terminology-2026-09.md)、
[Wave 2](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/wave2-memory-formation-retrieval-2026-08.md)第1〜4節を
Coreの記憶の種別と正本として採用します。現行Coreは`episode`/`semantic`の分類ラベルと
選択したuser発話の逐語本文だけを保存しており、以下の構造を持ちません。

## 決定

### 1. 履歴・記憶・記録を分ける

| 概念 | 用途 | Coreでの扱い |
| --- | --- | --- |
| Working Memory | 現在の入力・目的・処理中の文脈 | 推論中だけ保持し、長期保存しない |
| Conversation History | 同じ会話の再開・表示 | 既存の履歴store。別の会話から検索しない |
| Long-term Memory | 会話を越えて参照する経験・知識 | 本ADRの記録。Bindingで分離 |
| Domain Record | 日誌・レシピ等の正確な台帳 | 人格記憶へ保存しない。Coreは現状扱わない |
| Procedural Knowledge | Character定義、system prompt、code、skill | 通常の人格記憶へ保存しない |

会話由来の長期記憶は、元発言の履歴本文が保存済みであることを前提にします。元の履歴なしに
長期記憶だけが残る経路は作りません。

### 2. 長期記憶の種別

```text
Long-term Memory
├─ Episodic Memory（Episode）── ID参照 ── Related Facts（Fact）
├─ Semantic Memory（DIRECT_EXTRACTION / EXPERIENCE_DERIVED）
├─ Reflective Memory（Reflection）
└─ Procedural Memory（Skill）
```

- **Episode**：所有キャラクターが何を経験したか。会話・共同活動・会話外活動を問わない。
  「話を聞いた」も経験であり、聞いた内容の出来事にキャラクターを参加させない。
- **Fact**：その経験で得た、話題の対象についての情報・申告内容。外部世界で検証済みの真実ではない。
  Episodeとは独立した`fact_id`付きの記録とし、Episode–Fact参照で結ぶ。JSON配列だけに閉じ込めない。
- **Semantic**：世界・人物・自分について、知識として採用した事実・概念・傾向。
  形成方法（`formation_type`）は、明示命題の直接抽出（DIRECT_EXTRACTION）と、独立した2件以上の
  Episodeからの一般化（EXPERIENCE_DERIVED）を区別する。Factは自動でSemanticへ昇格しない。
- **Reflection**：本人が経験をどう意味づけたか。Semanticとは別の正本（[ADR 0020](0020-reflection-personality-relationship.md)）。
- **Procedural Memory / Skill**：実行・対話の結果から得た手続き。一般の会話検索の対象にしない。

意味分類できない内容を無制限に受け入れる汎用記憶型は作りません。

### 3. 5Wと欠損

EpisodeとFactは5Wで内容を表し、Episodeは「経験」の5W、Factは「話題の対象」の5Wとします。

| 項目 | 扱い |
| --- | --- |
| Whatの述語 | 必須 |
| Whatの対象等、Who、When、Where | 任意。不明は不明のまま保持し、推測で補わない |
| Why | 明示された理由だけ。推測した動機・因果を保存しない |

所有キャラクターが既知であることを、不明な行為者の補完に使いません。同名だけで人物を同定しません。
内容上は述語だけで成立しても、保存価値・privacy・出典・schemaの検証を通らなければ保存しません。

日常経験も保存対象とし、特別な節目でないことだけを理由に除外しません。明示的な仮定・創作
（「もし月へ旅行したら」）を話した経験も保存し、仮定・創作の文脈を記録・検索・会話利用で失いません。

一続きの経験は複数の発話・抽出実行にまたがっても1 Episodeとし、後から改めて語り直した経験は
新しいEpisodeとします。発話ごとのEpisode化や、同じ話題の常時1件化は採用しません。

### 4. 日時

| 項目 | 意味 |
| --- | --- |
| Episodeの経験日時 | 今回の経験が起きた日時。話を聞いたなら聞いた日時 |
| Factの対象日時 | 話題の出来事の日時・範囲・精度 |
| experienced_at | 所有キャラクターが経験を得た日時。内省・人格の時間基準 |
| stated_at | 根拠となる元発言の日時 |
| created_at | 記録の登録日時 |
| last_user_mentioned_at | ユーザーが明示的に言及・再言及・訂正した最新日時（[ADR 0018](0018-memory-retrieval-context.md)） |

タイムゾーンは設定で定義し、アプリケーションが適用します。LLMにタイムゾーンを生成させません。
相対日時は元発言日時を基準に解釈し、抽出の実行日時を基準にしません。使用したタイムゾーン・
精度・元発言への参照を保持し、設定変更や再試行で既存データを黙って再解釈しません。
「先月」を月初の特定日にせず、月精度の範囲を出来事の継続時間とみなしません。
不明な対象日時をstated_at・created_atで埋めません。

現行Coreの履歴は発話日時を保存しないため、stated_atの保存を履歴schemaへ追加します。

### 5. 出典・版・識別子

- `fact_id`は5Wのハッシュにせず、訂正後も安定させる。同じ5Wの別の出来事も表現できる。
- 出典は元発言（source参照・source epoch）と各参照元の版を保持する。複数根拠の版を単一の値で代用しない。
- 保存文（normalized text）は構造化値から所有者・行為者・対象・時刻を取り違えない形で生成し、
  根拠となる発言範囲（引用）を元発言の版と文字範囲へ結び付ける。根拠外の事実・日時を生成文に含めない。
- 同じ元発言をEpisode・Fact・Semanticへ表現しても、独立した根拠が増えたとは数えない。

### 6. Candidate

LLM等が生成し、privacy・根拠・schema・policyの検証前のデータをCandidateとします。
検証を通ったCandidateだけを自動で保存・有効化し、候補ごとの手動承認は追加しません。
未検証・拒否のCandidateを検索や人格更新に使わず、拒否本文を保存しません。

## 影響と移行

- 保存schemaは、Episode・Fact・Episode–Fact参照・Fact統合関係・Semantic・Reflectionの各正本と、
  日時・版・出典の列を持つ形へ拡張します。具体的な表・列は実装Issueで決めます。
- 既存の逐語本文の記憶は、ユーザーの決定（2026-10-06）により新形式へ移行せず削除します。
  dogfoodはCoreへの切替中で運用前のため、旧形式の互換・移送は要件にしません。会話履歴は保持します。
  運用開始後のschema変更では、backup・移行・検証・rollbackを必須にします。
- 物理schemaの変更前に、SQLiteとPostgreSQLの両adapterで同じ契約試験を用意します。

## 参照

- [ADR 0015](0015-memory-model-reorganization.md)：移設方針と読み替え
- [ADR 0007](0007-memory-provenance-and-revocation.md)：出典epochと撤回（維持）
