# Architecture Decision Records

`docs/decisions/`直下には、現行設計の根拠として参照できるADRだけを置く。
完全に置換・失効したADRは`archive/`へ移動し、判断当時の経緯を残す。

## 状態

| 状態 | 意味 | 配置 |
|---|---|---|
| `ACTIVE` | 全体または一部が採用設計として有効。実装・main取り込み・deploy完了を意味しない | `docs/decisions/` |
| `ARCHIVED` | 全面失効、または有効な判断を現行文書へ統合済み | `docs/decisions/archive/` |

一部だけが有効なADRでは、状態タグを増やさず、状態欄に現行範囲と後続ADRを文章で記載する。
archive内の文書を現行仕様や実装のSource of Truthとして使用しない。

## 現行の優先関係

- [`episode-fact-semantic-boundaries-2026-09.md`](episode-fact-semantic-boundaries-2026-09.md) は、#352で確定したEpisode / Fact / Semantic / Reflectionの正本責務、述語のみ必須の5W、config timezone、FactのID参照・版・失効、同thread登録時照合と後続#354の別thread非同期整理を定める。この記憶再編の範囲ではCharacter Life・用語・Wave 2・RAG privacy ADRの旧記述より優先する。
- [`memory-personality-terminology-2026-09.md`](memory-personality-terminology-2026-09.md) は記憶・Fact・内省・気質・人格・処理の用語を定める。物理schemaの設計は#342 / #345等で具体化し、用語表を実装済みschemaと扱わない。
- [`addon-action-approval-recovery-2026-09.md`](addon-action-approval-recovery-2026-09.md) は、#185の接続・操作群・実行場面別承認、確認キュー、60秒待機、結果回復を定める。承認の効力・UI範囲はCharacter Life共通ADRより本ADRを優先する。
- [`character-life-runtime-2026-09.md`](character-life-runtime-2026-09.md) は、#249のtop-level runtimeへDBOSを採用し、domain正本・関連Epicとの責務境界を定める。記憶正本の所有権は上記#352 ADRを優先し、runtime採用判断は変更しない。
- [`character-life-memory-personality-autonomy-2026-09.md`](character-life-memory-personality-autonomy-2026-09.md) は、本人視点のEpisodeとFact、Semantic / Reflection分離、Life State、Big Five Aspects人格、Relationship、Autonomy Target、外部送信privacy、高影響操作の共通契約を定める。
- Character Lifeの範囲で`wave2-memory-formation-retrieval-2026-08.md`、`rag-memory-privacy-policy-2026-07.md`、`tool-use-foundation-2026-09.md`と競合する場合はCharacter Life ADRを優先し、そのうち記憶再編は#352 ADRを優先する。
- 長期記憶保存時の絶対禁止・保存拒否・SQLite正本 / Chroma派生index・transactional outbox、#104 / #182のCapability Snapshot / Execution Gate等、明示的に変更していない既存契約は継続する。記憶の新しい表現を理由にprivacyや外部実行権限を緩和しない。

現在のコード・runtimeは[システムアーキテクチャ](../system-architecture.md)、目標は[ロードマップ](../roadmap.md)、
実施順・分解は[エンハンス計画](../enhancement-plan.md)、作業進捗・受入条件はGitHub Issuesで管理する。

## Archive

archiveの一覧と置換先は[`archive/README.md`](archive/README.md)を参照する。
