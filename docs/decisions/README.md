# Architecture Decision Records

`docs/decisions/`直下には、現行設計の根拠として参照できるADRだけを置く。
完全に置換・失効したADRは`archive/`へ移動し、判断当時の経緯を残す。

## 状態

| 状態 | 意味 | 配置 |
|---|---|---|
| `ACTIVE` | 全体または一部が現行仕様として有効 | `docs/decisions/` |
| `ARCHIVED` | 全面失効、または有効な判断を現行文書へ統合済み | `docs/decisions/archive/` |

一部だけが有効なADRでは、状態タグを増やさず、状態欄に現行範囲と後続ADRを文章で記載する。
archive内の文書を現行仕様や実装のSource of Truthとして使用しない。

## 現行の優先関係

- [`addon-action-approval-recovery-2026-09.md`](addon-action-approval-recovery-2026-09.md) は、#185の接続・操作群・実行場面別承認、確認キュー、60秒待機、結果回復を定める。承認の効力・UI範囲はCharacter Life共通ADRより本ADRを優先する。

- [`character-life-runtime-2026-09.md`](character-life-runtime-2026-09.md) は、#249のtop-level runtimeへDBOSを採用し、domain正本・関連Epicとの責務境界を定める。

- [`character-life-memory-personality-autonomy-2026-09.md`](character-life-memory-personality-autonomy-2026-09.md) は、本人視点のEpisode、Semantic/Reflection分離、Life State、Big Five Aspects人格、Relationship、Autonomy Target、外部送信privacy、高影響操作の共通契約を定める。
- 上記範囲で `wave2-memory-formation-retrieval-2026-08.md`、`rag-memory-privacy-policy-2026-07.md`、`tool-use-foundation-2026-09.md` と競合する場合は、2026-09のCharacter Life ADRを優先する。
- 長期記憶保存時の絶対禁止・SQLite正本／Chroma派生index・transactional outbox、#104/#182のCapability Snapshot / Execution Gate等、Character Life ADRで明示的に変更していない既存契約は継続する。

## Archive

archiveの一覧と置換先は[`archive/README.md`](archive/README.md)を参照する。
