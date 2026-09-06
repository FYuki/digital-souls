# 画面知覚contract

このディレクトリはIssue #212の画面知覚境界を管理する。詳細な判断理由と予算は`docs/decisions/browser-screen-perception-2026-09.md`を正本とする。

| ファイル | 境界 |
|---|---|
| `screen-perception.schema.json` | Frontend／Backend間の共有session・取得制御wire event。protocol `1.0` |
| `screen-reference-decision.schema.json` | 画像取得前のCore内部LLM判定出力。wireへ送らない |
| `screen-grounding-observation.schema.json` | Visionが返す質問対象のCore内部一時観測。wire・履歴へ保存しない |
| `fixtures/explicit-reference-cases.json` | 現在発言だけで明確な要求を決めるfast path |
| `fixtures/contextual-reference-cases.json` | 共有・許可・直近会話を含む3分岐の期待結果 |
| `fixtures/grounding-observations.json` | 単一候補、複数候補、対象なし、読取不能と不正観測 |

`screen-perception.schema.json`だけがquicktype生成の入力である。内部schemaをFrontend／Backendの共有event unionへ加えたり、生成型を直接編集したりしない。
