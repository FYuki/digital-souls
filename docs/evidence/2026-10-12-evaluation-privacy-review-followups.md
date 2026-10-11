# 評価経路の privacy レビュー修正（2026-10-12）

対象 branch: `fix/182-review-followups`。
起点: `684b474a9ee773466fc34792276a006f04f9d891`。
以下はこの起点に本変更を適用した作業ツリーでのローカル検証。
修正 revision はこの文書を含む commit。push・PR 作成・merge は実施していない。

## 変更と TDD

- 人格評価の実モード Chat API に `privacy` を渡し、会話履歴 API と
  インジェクション評価も共通の `EvaluationPolicy` を使うよう変更した。
  合成データの同意は評価の Binding 一つに限定し、未設定時は拒否する。
  `PrivacyPolicy` の scan により秘密・判定不能を拒否する。
- 人格評価の現在入力・過去入力それぞれに秘密または判定不能な合成本文を入れる
  回帰試験4件を先に実行した。修正前は counter に到達して4件 FAIL、修正後は
  counter/provider 呼び出し0、内容を含まない ERROR と `execution_incomplete` を確認した。
- インジェクション評価では、判定不能な記憶本文がプロンプトの枠へ変換された後に
  scan されると拒否されないことを追加試験で確認した（Chat/履歴の2件 FAIL）。
  固定の記憶を枠へ変換する前に `memory` の認可を行うことで修正した。
  秘密・判定不能の記憶と入力の拒否、counter/provider に送られないことを6件で固定した。
- 空ケース試験は schema 2 に変更し、Pydantic のエラーが `cases` の `too_short`
  だけであることを確認する。版不一致での拒否ではない。
- 到達しない `caller_system` の条件分岐を削除した。
  記憶または user の攻撃が欠落する試験の不完了件数は、両入口×3回の6件。
- 安全なケースの正常動作・profile・cache・履歴保存は既存の実モード組立試験でも確認した。

## 環境とゲート

Python 3.12.3、uv 0.8.22、Node 24.19.0。
`T=/home/asa/dev/digital-souls-evidence/history-stage1-tools`、
`PATH=$T/bin:$T/node/bin:$PATH`、`TMPDIR=/dev/shm`。
テストは fake/fixture/MockTransport または使い捨ての PostgreSQL を使用。
実 LLM・GPU・稼働中の llama.cpp コンテナには接続していない。
依存取得のみネットワークを使用した。

ruff の対象は両コマンドとも
`src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py`。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check <上記対象>` | PASS |
| `uv run --no-sync ruff format --check <上記対象>` | PASS（143ファイル） |
| `uv run --no-sync mypy` | PASS（143ファイル） |
| `uv run --no-sync pytest -m ut -q` | PASS（987件） |
| `uv run --no-sync pytest -m it1 -q` | PASS（935件） |
| `uv build --no-build-isolation` | PASS（sdist/wheel） |
| `bash tools/test-postgres.sh` | PASS（442件、専用コンテナは終了時削除） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59件、skip/TODO/cancelなし） |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` | PASS |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --mode fixture --runs 3 --output /dev/shm/fix-182-persona-report.json` | PASS（全必須ゲート0件） |
| `uv run --no-sync python tools/evaluate-injection-resistance.py --mode fixture --runs 3 --output /dev/shm/fix-182-injection-report.json` | PASS（全必須ゲート0件） |

回帰試験13件の個別実行も PASS。
Python 試験では既存依存の deprecation/ReadOnly 警告、npm ci では依存の deprecation 警告がある。
fixture 評価は `quality_evidence=false` であり、実モデル品質の受入ではない。
人格の活動入口、インジェクションの `tool_results`、実モデル IT2/ST は NOT RUN。
今回指定されたレビュー指摘の未対応はなし。

## 監督側の再実行（2026-10-12）

`585cb13bea53fb52461c06826cc2a97865cb6c8e` で差分を読み、同じ固定ツールで再実行した。uv sync --locked・uv lock --check・ruff・mypy（143 files）・
UT（987 passed）・IT1（935 passed）・uv build・test-postgres（442 passed）・npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。
人格評価とインジェクション評価の CLI（fixture、各3回）は exit 0。実モデルでの再評価は NOT RUN（評価の経路に privacy の認可を足しただけで、
合成の Miori ケースは秘密を含まないため結果は変わらない想定。確認はしていない）。
