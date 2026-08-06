# 光織 Memory Policy

光織の現行の記憶・記録モデル、保存同意、形成・検索方針は
`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`を正本とする。

`docs/decisions/archive/miori-memory-policy-2026-06.md`は初期検討時の履歴ADRとして保持する。

認識語彙・pattern・閾値等の実行時Source of Truthは
`backend/app/memory/memory_policy.json`。
RAG privacyの不変条件と、設定でも緩和できない絶対禁止は
`docs/decisions/rag-memory-privacy-policy-2026-07.md`を正本とする。
この Markdown は人間向けの案内のみであり、実装は参照しない。
