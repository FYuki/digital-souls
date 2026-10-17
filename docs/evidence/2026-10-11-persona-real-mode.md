# 人格一貫性評価の実モデルモード：合成検証

実施日: 2026-10-11（UTC 10:23〜10:35）。branch: `feature/154-persona-real-mode`。
起点: `9a9250d063377800e617c3c1e535be1a9f938051`。
実装時の標準fixture報告はこの起点の SHA と `dirty: true` を記録しています。
最終ローカル commit は担当の引継ぎ報告で識別します。

[ADR 0029](../adr/0029-persona-consistency-evaluation.md) §7 の実モードを追加しました。
実行手順と設定の作り方は [人格評価 README](../../core/evals/persona/README.md) に記載しています。
この証跡は fake provider / fake token counter / MockTransport と使い捨て PostgreSQL の検証です。
実モデルへの送信・GPU 使用・実 Egogram judge は **NOT RUN** です。
合成試験の PASS を実モデルの人格品質の PASS として扱いません。

## TDD

1. 実モードの組み立て・報告・保存先・実行エラーのテストを先に追加し、
   `uv run --no-sync pytest tests/test_persona_real.py -q` を実行。
   **FAIL**: 未実装の `persona_real` がないため collection error。
2. 実装後に fake provider を実モードの経路へ注入し、既存fixtureと合わせて PASS。
3. 空の応答（SDKで本文を持たない拒否の正規化結果）のテストを先に追加し、
   `uv run --no-sync pytest tests/test_persona_real.py -k refusal -q` を実行。
   **FAIL**: `execution_incomplete` が期待18件に対し0件。
4. 空・空白だけの応答を実行不完了へ計上する修正後、同じテストは **PASS**。

## 経路・保存先・報告

- 実モードは本番の `LiteLLMProvider` / `Inference` / `Conversations` を使い、
  運用者設定の transport・token counter・Card/Lore・prompt budget を保持します。
- 保存は製品の `PostgresHistory`。`tools/with-test-postgres.sh` が作る専用 Unix socket、
  合成 DB/user、評価ごとのランダム schema を使います。既存DBのDSN入力はありません。
  この方法は、fixtureの部分的なstoreの代わりに製品保存契約とCard metadataを検証できます。
- ケース・回ごとに新しい coordinator と会話を使い、過去の合成履歴をseedし、終了時に会話を削除。
  コンテナ全体はwrapperが終了時に削除します。他のコンテナは操作しません。
- LiteLLM のcache読み書きを無効にし、llama.cpp生成には `cache_prompt=false` を指定。
  MockTransportでSDKからのwire形式と、cache有効状態でも同じ入力が2回生成されcacheへ保存されないことを確認しました。
- `temperature` / `max_completion_tokens` 等は両入口の入力検証と `allowed_parameters` を通します。
  timeout・接続失敗・設定による推論拒否・途中終了・空応答・保存失敗は内容を含まないエラーとなり、
  `execution_incomplete` で FAIL。caseを黙って飛ばしません。
- 実モード報告にはcommit/dirty、ケース版、Card hash、profileの識別子、モデル、生成設定、
  各回の結果、必須ゲート、不一致warn/gate、`quality_evidence: true` を記録します。
  本文はopt-inのみ。endpoint・設定パス・認証情報を報告しません。実judgeは `NOT RUN`。

## 指定ゲート

固定 toolchain: uv 0.8.22 / Node 24.19.0、`TMPDIR=/dev/shm`。
ruffの対象（下表の `<対象>`）は
`src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py`。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check <対象>` | PASS |
| `uv run --no-sync ruff format --check <対象>` | PASS（137 files） |
| `uv run --no-sync mypy` | PASS（137 source files） |
| `uv run --no-sync pytest -m ut -q` | PASS（951件） |
| `uv run --no-sync pytest -m it1 -q` | PASS（824件） |
| `uv build --no-build-isolation` | PASS（sdist / wheel） |
| `bash tools/test-postgres.sh` | PASS（442件、使い捨てコンテナ） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS（575 packages、既存依存のdeprecated警告あり） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59件） |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` / `git diff --cached --check` | PASS |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-fixture-feature154.json` | PASS（両入口・3分類・各3回100%、必須ゲート0件、quality_evidence=false） |

必須試験のSKIPは0件。markerによる他種別のdeselectedは、対象試験のSKIPではありません。
Pythonの試験には既存Starlette/Pydantic依存のwarningがあります。未実施の実モデル・実judge・活動入口は
**NOT RUN** のままです。`prompt_builder.py` / `token_count.py`、固定Mioriケースは変更していません。
push・PR・mergeは行っていません。

## 監督側の再実行（2026-10-11）

`e0d009890aa9555ecf4e4c5cd44f207f39d9309a` に epic `2721ca3` を衝突なく取り込んだ `ca1af77` で、差分を読み、同じ固定ツールで再実行した。
uv sync --locked・uv lock --check・ruff・mypy（137 files）・UT（952 passed）・IT1（839 passed）・uv build・test-postgres（442 passed）・
npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。fixture の標準 CLI（3回）も exit 0。
このモードで行った Miori の実モデル評価は [別の証跡](2026-10-11-persona-real-model-evaluation.md) にある（結果は FAIL）。
