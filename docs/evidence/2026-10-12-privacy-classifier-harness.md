# privacy 分類器評価ハーネスの証跡（Issue #190）

日付: 2026-10-12。対象: [Issue #190](https://github.com/FYuki/digital-souls-core/issues/190)、
[Epic #125](https://github.com/FYuki/digital-souls-core/issues/125)。
評価契約は [ADR 0032](../adr/0032-privacy-classifier-evaluation.md)（Proposed）。

## revision と範囲

- 起点: `7005efa5a69de4912fec216e16bfefc5bf010907`（epic/privacy-classifier-evaluation）。
- 実装 revision: `9fa2742d7e02b1944dcf82ef94642fbbc5b609f6`。本証跡はその実装 commit に続く文書 commit で追加する。
- worktree: `190-privacy-eval-harness`、branch: `feature/190-privacy-eval-harness`。
- ローカル commit のみ。push・PR・merge・GitHub 書き込みは実行しない（監督担当）。
- 実モデル・llama.cpp・GPU・稼働中コンテナには触れない。PostgreSQL は許可された使い捨て合成試験だけ。

## TDD と実装

1. 採点・集計・必須ゲート・ABSTAIN・safe 不変の UT を先に追加。
   RED: `pytest tests/test_privacy_classifier_evaluation.py -q` は未実装 AssessmentResult の ImportError、収集エラー1件、exit 1。
2. AssessmentResult、LocalClassifier.assess、純粋採点を実装。
   GREEN: 新 UT と既存 `tests/test_privacy.py` は89件 PASS。
3. fixture CLI と real の fake 組み立て試験を先に追加。
   RED: `pytest tests/test_privacy_classifier_runtime.py -q` は未実装 runtime の ModuleNotFoundError、収集エラー1件、exit 1。
4. fixture/runtime/CLI を実装。初回 lint/type 検査の FAIL を修正（loop の閉包、型の推論、format）。
   GREEN: 新 UT/IT1 合計63件 PASS。境界・参考指標・実 timeout/cancel・不正 fixture/real 設定を追加し、79件 PASS。
5. 全必須ゲートを実行。文書検査の初回は作成途中の証跡リンクが存在せず FAIL（exit 1）。
   証跡を追加して stage し、最終の文書・空白検査は PASS（exit 0）。

`assess()` は有効 Assessment と本文を持たない failure を分離する。
scan_refused、policy_mismatch、timeout、provider_exception、invalid_response、invalid_json、invalid_assessment。
`safe()` は有効な NOT_SENSITIVE だけ True、それ以外・Exception は False、CancelledError は従来どおり伝播する。
UT で各出力・失敗の bool 不変、同一 payload、timeout cleanup・cancel を検証し、既存 privacy 試験も変更せず実行した。
起点と実装の AST を比較し、payload の代入・scan 呼び出し・timeout の式が完全一致することを確認。
prompt・max_completion_tokens=256・min(profile.timeout_seconds,15)・版定数は変更しない。
入力・gold、SPEC、ADR 0032 は起点と bytes 一致を確認。

## 合否と report

各回・各 group 90%以上、false_negative/invalid_output/timeout/execution_incomplete が0件、最低3回すべて合格を要求。
8件 group は8/8。欠落・重複・未知 id/回・0件も実行不完了。分類の平均による相殺なし。
ABSTAIN の既定値は設けず、CLI で accept/reject/separate を明示必須にした。
separate の境界は厳密な10%未満、60件なら5件 PASS・6件 FAIL。
全 policy で ABSTAIN 件数・率を記録する。

report は実行 commit/dirty、入力/gold/結合 hash、分類器/prompt/policy 版、model/digest、安全な profile 識別、
cache=false、parallelism=1、開始・終了 UTC、各回/各 group の採点とゲート、ケースごとの構造化結果・失敗・latency、
参考の言語/tag/subject/category/FP/median/p95、status と FAIL 理由を持つ。
本文・応答の生テキスト・endpoint・認証情報・設定パスは出さない。
実行と report の詳細は [評価 README](../../core/evals/privacy/README.md) を参照。

real の IT1 は fake provider だけで180呼び出しを検証し、case.text の文字列だけを送ること、
profile を保持すること、no-cache/no-store・caching=false・cache_prompt=false、実行条件の記録を確認した。
PrivacyPolicy/EvaluationPolicy の常に True の分類器は使わない。
fixture の provider には通信処理がなく、実 provider 作成を禁止する IT1 でも動作した。

## 必須品質ゲート

固定環境: uv 0.8.22、Node 24.19.0、TMPDIR=/dev/shm、UV_OFFLINE=1、LITELLM_LOCAL_MODEL_COST_MAP=True。
ruff の対象は `.github/workflows/api.yml` と同一（新 tool を含む）。

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages resolved、lock 不変 |
| `uv lock --check` | PASS、80 packages resolved |
| `uv run --no-sync ruff check <api.yml の対象一覧>` | PASS、下記158ファイル対象 |
| `uv run --no-sync ruff format --check <同一覧>` | PASS、158 files already formatted |
| `uv run --no-sync mypy` | PASS、158 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、1210 passed、1428 deselected、SKIP 0、7.48秒 |
| `uv run --no-sync pytest -m it1 -q` | PASS、984 passed、1654 deselected、SKIP 0、150.40秒 |
| `uv build --no-build-isolation` | PASS、sdist と wheel の2成果物 |
| `bash tools/test-postgres.sh` | PASS、444 passed、2194 deselected、SKIP 0、163.94秒 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、固定 lock で導入、lock 不変 |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered required tests、skip/todo/cancel 0 |
| `node tools/check-docs.mjs` | PASS、最終 stage の全追跡文書と参照先 |
| `git diff --check` / `git diff --cached --check` | PASS |

ruff の実行コマンド（workflow と CONTRIBUTING と一致）:

```sh
uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
```

標準 fixture CLI を3方針それぞれ実行した。各回60/60正解、全 group 正解、ABSTAIN 0/60、必須ゲート0。
3回計180/180で各 PASS/exit 0、quality_evidence=false。report は `.venv/privacy-harness-gates/` の非追跡領域へ保存した。
report の検証対象 revision は起点 SHA、dirty=true（実装・試験・文書を未commitで評価したため）。

```sh
uv run --no-sync python tools/evaluate-privacy-classifier.py --abstain-policy accept --runs 3 --output .venv/privacy-harness-gates/fixture-accept.json
uv run --no-sync python tools/evaluate-privacy-classifier.py --abstain-policy reject --runs 3 --output .venv/privacy-harness-gates/fixture-reject.json
uv run --no-sync python tools/evaluate-privacy-classifier.py --abstain-policy separate --runs 3 --output .venv/privacy-harness-gates/fixture-separate.json
```

違反 fixture CLI の IT1 は false_negative・invalid_output・timeout・execution_incomplete・group_accuracy の各 FAIL/exit 1 を確認。
abstain_5/separate は PASS、abstain_6 は accept のみ PASS、separate/reject は FAIL、必須ゲートは0を維持した。
これらの期待した評価 FAIL はハーネスの検出成功であり、モデル品質 PASS と報告しない。

UT の warning は1件、IT1/PostgreSQL は各3件（既存 anyio alias 非推奨・Pydantic ReadOnly の warning）。
npm の既存依存にも非推奨 warning がある。新たな依存・lock 更新は行わない。

## NOT RUN・未決定

- **NOT RUN**: 実モデル評価・実 LLM/socket/GPU・llama.cpp 操作。#191 で監督が実行する。
- **決定待ち**: ADR 0032 の ABSTAIN A/B/C。ハーネスは切替だけ実装し、決定・既定の固定は行わない。
- fixture と fake 組み立ての PASS は **quality_evidence=false / 道具の検証** で、実モデル品質の証拠ではない。
  fake で real の metadata を検証する試験の quality_evidence=true はモード記録の確認だけである。
- SPEC の状態欄、ADR 0032 本文、入力・期待値、分類器 prompt、依存は変更しない。

## 追記（2026-10-12、ABSTAIN の決定）

ユーザーが ADR 0032 の ABSTAIN の扱いを C（別集計）に決定しました（「Q1：C案で進めて」）。
これに合わせ、`--abstain-policy` の既定を separate にしました（上の「決定待ち」は当時の記録）。ADR 0032 は Proposed のままです。
