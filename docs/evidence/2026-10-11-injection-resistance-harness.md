# インジェクション耐性ハーネスの合成検証（2026-10-11）

対象：`feature/165-injection-eval`、起点 `7cca0fb`。
[ADR 0030](../adr/0030-injection-resistance-evaluation.md) の実装と
[独立CLI・ケース・実行手順](../../evals/injection/README.md)。
実 LLM・外部 API・GPU の試験は行わず、稼働中の llama.cpp へリクエストを送っていません。

## TDD

先に `tests/test_injection_evaluation.py` と `tests/test_injection_runtime.py` を追加し、
それぞれ pytest が未実装 module の import で FAIL（exit 1）となることを確認してから実装しました。
続いて攻撃が予算削減などで送信前に消えた場合の回帰試験を先に追加し、
2件の assertion FAIL を確認してから、送信直前の枠・攻撃本文の確認を実装しました。
履歴の上書き経路が dispatch まで通った場合の回帰試験も先に追加し、assertion FAIL を確認。
構造検査専用 provider により、実モデルへ送らず `override_channel_open` を検出する修正後に PASS。
最後に直接集計での空文字／空白のみ応答も先に2件の FAIL を確認し、
集計でも `execution_incomplete` の FAIL にする修正後に PASS。

## 環境・検証

指定の uv 0.8.22 / Node 24.19.0、Python 3.12.3。
`T=/home/asa/dev/digital-souls-evidence/history-stage1-tools`、`PATH=$T/bin:$T/node/bin:$PATH`、
`TMPDIR=/dev/shm`、`LITELLM_LOCAL_MODEL_COST_MAP=True` を指定。
テストは socket 禁止の UT/IT1、PostgreSQL は専用の使い捨てコンテナで実行。
ruff 対象は `src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py`。
新 tool を CONTRIBUTING・API CI の lint/format と pyproject の mypy 対象へ追加しました。
追加依存・lock の変更はありません。

最終の Python ゲートは実装修正後に再実行しました。以下は実測結果です。
合成テストの必須 SKIP は0件。marker による deselected は対象外種別で、SKIP ではありません。
既存依存の Starlette/Pydantic warning と npm deprecated warning はあり、テスト失敗ではありません。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS（80 packages resolved、依存変更なし） |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check <上記対象>` | PASS |
| `uv run --no-sync ruff format --check <上記対象>` | PASS（142 files） |
| `uv run --no-sync mypy` | PASS（142 source files） |
| `uv run --no-sync pytest -m ut -q` | PASS（975 passed、5.69秒） |
| `uv run --no-sync pytest -m it1 -q` | PASS（863 passed、62.98秒） |
| `uv build --no-build-isolation` | PASS（sdist / wheel） |
| `bash tools/test-postgres.sh` | PASS（442 passed、82.33秒、専用使い捨てコンテナ） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS（575 packages、10秒） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59 registered required tests、skip/todo/cancelled 0） |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` / `git diff --cached --check` | PASS |
| `uv run --no-sync python tools/evaluate-injection-resistance.py --runs 3 --output /dev/shm/injection-165-report.json` | PASS / exit 0（15応答＋3構造検査、6必須ゲート0件、本文なし） |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-165-report.json` | PASS / exit 0（18応答、全分類・回100%、必須ゲート0件、本文なし） |

新規試験は UT 23件、IT1 24件。各ゲート違反の CLI はテスト内で FAIL / exit 1 を確認し、
その期待どおりの失敗を検証した試験自体は PASS です。
canary 違反は対象の5応答経路すべてで確認しました。
履歴 system は受理／dispatch 到達を `override_channel_open` とし、privacy/DB等の別エラーを
実行不完了へ分けます。実モードの fake provider/counter で profile・生成設定・cache無効化・
両入口の記憶枠・各会話の削除を検証しました。

commit 前の fixture 報告は `7cca0fbbfd27a85f286c474ebfdf1071851a5ef0` / `dirty: true`。
実装はローカル commit に固定し、引き渡し時に正確な SHA を報告します。
合成ケース版は `13ed2957d0157a3338ded4964222c643b1903bfe2b90117b70d14845bb022282`。
実モデル品質の PASS を主張しません。

## 制約・NOT RUN

- 実モデルのインジェクション耐性：**NOT RUN**（監督担当が実行）。fixture は実品質の証拠ではありません。
- ツール結果：**NOT RUN（ADR 0027 の実装後）**。
- Miori 各経路3件のケースは **ユーザー確認前の案**（draft）。既存 Card・人格ケースは変更していません。
- Chat の固定記憶検索は評価専用 scope を指定し、本番のデータ枠を通しています。
  Chat HTTP が保存記憶を検索する機能の受入ではありません。
- 実モードは本番 provider/counter/設定・cache 無効化・PostgresHistory を使いますが、
  記憶の検索と合成データへの privacy 分類は評価用 fake です。実 privacy 分類器・運用記憶は未評価。
- push・PR・merge は実施しません。ローカル commit のみです。

## 監督側の再実行（2026-10-11）

`ee75e4773709bbcd66185fc9e49d072045fe655f` で差分を読み、同じ固定ツールで再実行した。製品のコードは変更されておらず、追加は `injection_*` のモジュールと評価データ・CLI だけ。
uv sync --locked・uv lock --check・ruff（新しい CLI を含む）・mypy（142 files）・UT（975 passed）・IT1（863 passed）・uv build・
test-postgres（442 passed）・npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。
インジェクション評価の CLI（fixture、3回）と人格評価の CLI（fixture、3回）はどちらも exit 0。報告 JSON の `"response"` は集計の種類の
ラベル（`kind`）で、応答本文は含まれない。

補足：本番の stateless の Chat API は記憶を注入しない。Chat API × 記憶 context の経路は、評価専用の合成の会話 ID で記憶の枠を有効にして、
記憶のデータの枠そのものの耐性を測るもので、本番に存在する経路の評価ではない（会話履歴 API × 記憶 context は本番の経路）。

実モデルの評価は NOT RUN（Miori のケース `miori.draft.cases.json` がユーザー確認前のため）。ツールの結果の経路は NOT RUN（ADR 0027 の実装後）。
