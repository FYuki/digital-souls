# privacy 分類器ケース移行の証跡（Issue #189）

日付: 2026-10-12

対象: [Epic #125](https://github.com/FYuki/digital-souls-core/issues/125) の
[Issue #189](https://github.com/FYuki/digital-souls-core/issues/189)。
[ADR 0032](../adr/0032-privacy-classifier-evaluation.md) と[評価データ](../../core/evals/privacy/README.md)。

## Revision と範囲

- branch: `feature/189-privacy-eval-cases`
- base: `c731cdcab90f50d1b29ece0042ccef29fc3d1896`（`epic/privacy-classifier-evaluation`）
- 実装・ケース固定 commit: `ad40185df123ec11b5fc5cb7ea9265692cfc75bb`。全品質ゲートを実行したコード・ケースを含みます。
- 後続の証跡commitはこのSHAの注記だけを更新し、検証済みのコード・ケース・goldを変更しません。
- 指定の隔離 worktree 内で実装し、push・PR 作成・merge・GitHub 書き込みは実施していません。
- 依存、分類器 prompt/挙動、SPEC の状態欄は変更していません。src/tests は既存の品質ゲート対象のため
  pyproject・workflow・CONTRIBUTING の対象一覧への追加は不要です。

## 出典と移行結果

PoC: 公開 `FYuki/digital-souls-poc` の `fce7382884d981c42be7fbd3ddaffe7469e27588`。
元の `backend/evals/privacy_classifier/cases.jsonl` の bytes SHA-256 は
`0b52ea880fb811bda8c2bf393fc79aae6916055d8fb75c26b4e5d1331c397a83`。
60件を移行し、移さなかったケースは **なし（0件）**。全件が合成で、実在人物・実ユーザーの会話を含みません。

| group | 件数 |
| --- | --- |
| HEALTH | 12 |
| FINANCIAL | 12 |
| MENTAL_STATE | 10 |
| NONE | 10 |
| SELF_HARM | 8 |
| ABUSE | 8 |

日英30 pair・日本語30件/英語30件、機微40件/非機微20件。
入力と gold の正規化 JSON のケース集合版は
`5a77017bfd9006d42d42c15409124e20932a0fa559da6554be51eb2a244fada1`。

### PoC との機械照合（PASS、60/60）

以下を worktree root で実行し、本文の UTF-8 bytes、subject、sensitive、許容 classification の配列、
pair/language/tags、元の vars と poc 対応情報、指定の group/category 対応を全60行で確認しました。
期待値の追加・削除はなく、指定の第三者機微 category の併記以外に許容値を広げていません。

```sh
uv run --no-sync python - <<'PY'
import hashlib, json, subprocess
from pathlib import Path
from digital_souls_core.privacy_evaluation_cases import load_cases
raw = subprocess.check_output([
    "git", "-C", "/home/asa/dev/digital-souls", "show",
    "fce7382:backend/evals/privacy_classifier/cases.jsonl",
])
assert hashlib.sha256(raw).hexdigest() == "0b52ea880fb811bda8c2bf393fc79aae6916055d8fb75c26b4e5d1331c397a83"
source = [json.loads(line) for line in raw.splitlines()]
fixed = load_cases(Path("evals/privacy/cases.json"), Path("evals/privacy/expectations.json"))
inputs = {c.id: c for c in fixed.cases.cases}
gold = {g.id: g for g in fixed.expectations.expectations}
assert len(source) == len(inputs) == len(gold) == 60
assert {r["id"] for r in source} == set(inputs) == set(gold)
groups = {
    "HEALTH": "HEALTH", "MENTAL_STATE": "MENTAL_STATE", "SELF_HARM": "SELF_HARM",
    "ABUSE_OR_SEXUAL_VIOLENCE": "ABUSE", "FINANCIAL_SITUATION": "FINANCIAL", "NONE": "NONE",
}
for r in source:
    c, g = inputs[r["id"]], gold[r["id"]]
    assert c.text.encode("utf-8") == r["text"].encode("utf-8")
    assert (c.pair_id, c.language, c.tags) == (r["pair_id"], r["language"], r["tags"])
    assert r["vars"] == {"text": r["text"], "case_id": r["id"]}
    assert g.expected_subject == r["expected_subject_scope"]
    assert g.sensitive is r["sensitive"]
    assert g.allowed_classifications == r["allowed_classifications"]
    assert g.poc.model_dump() == {key: r[key] for key in (
        "id", "pair_id", "language", "topic_category", "synthetic", "case_kind",
    )}
    assert g.group == groups[r["topic_category"]]
    categories = [g.group] if r["sensitive"] else ["NONE"]
    if r["sensitive"] and r["expected_subject_scope"] == "THIRD_PARTY":
        categories.append("THIRD_PARTY_PRIVATE")
    assert g.allowed_categories == categories
print("PASS: 60/60 matched; omitted=0")
print(fixed.version)
PY
```

## 環境と品質ゲート

Python 3.12.3、uv 0.8.22、Node 24.19.0。
全 Python/Node コマンドは次の固定環境で実行しました。
PostgreSQL は専用のネットワークなし・公開ポートなし・使い捨ての Docker 合成試験です。
llama.cpp・GPU・実運用コンテナは操作していません。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
```

ruff の対象 `$quality_targets` は API CI の一覧と同じです。

```sh
quality_targets="src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py"
```

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages resolved / 78 installed |
| `uv lock --check` | PASS、80 packages resolved、lock変更なし |
| `uv run --no-sync ruff check $quality_targets` | PASS、全153 Pythonファイルが対象、指摘0 |
| `uv run --no-sync ruff format --check $quality_targets` | PASS、153 files already formatted |
| `uv run --no-sync mypy` | PASS、153 source files、指摘0 |
| `uv run --no-sync pytest tests/test_privacy_evaluation_cases.py -m ut -q` | PASS、115件（全体UTにも含む） |
| `uv run --no-sync pytest -m ut -q` | PASS、1161 passed / 1398 deselected / SKIP 0 / 警告1 |
| `uv run --no-sync pytest -m it1 -q` | PASS、954 passed / 1605 deselected / SKIP 0 / 警告3 |
| `uv build --no-build-isolation` | PASS、sdistとwheelの2成果物 |
| `bash tools/test-postgres.sh` | PASS、444 passed / 2115 deselected / SKIP 0 / 警告3、コンテナ終了・削除を含めexit 0 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages added、既存依存の非推奨警告あり |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered required tests / SKIP・TODO・cancelled 0 |
| `node tools/check-docs.mjs` | PASS、追跡済みtext・ローカルMarkdown参照先の検証、違反0 |
| `git diff --check`（新規ファイルをstageして検査） | PASS、空白・改行の違反0（cached差分も確認） |

新規UT115件は固定データ契約1件・Assessmentとの互換性1件・scan60件・版hash1件・schema2件・
不正形式/意味の拒否47件・JSON/ファイル読み込み拒否3件です。
scan は全60件で secret=false / failed=false。実分類器への到達前にブロックされるケースはありません。
UT の警告1件と IT1 の警告のうち1件は既存 Starlette TestClient の anyio BlockingPortal alias の非推奨です。
IT1 の残る2件は Pydantic の TypedDict ReadOnly qualifier に関する既存警告です。
PostgreSQL 試験も同じ種類の警告3件です。いずれもテスト失敗やSKIPではありません。

### 実装途中の失敗と修正

- TDD RED: 新規UTの最初の実行は未実装 module の ImportError で収集 ERROR 1（exit 1）。PASSではありません。
  loader とデータを実装後、115件がPASSです。
- 初期 ruff check は整形前の E501 3件でFAIL。format適用後の全体 check/format は上表のPASSです。
- 初期 mypy は set の推論型に THIRD_PARTY_PRIVATE / NONE を追加できず2件でFAIL。
  `set[Category]` を明示し、最終の全体 mypy は153ファイルでPASSです。

## ADR の要点・未解決・未実施

ADR 0032 は **Proposed**。Core の分類器/prompt は1つのため PoC の2 profileを統合し、
本番 llama.cpp gemma4-12b・本文文字列・gold非送信で classification/subject/category を測る案です。
各回各group90%以上（8件の分類は8/8）、機微NOT_SENSITIVEと不正出力/timeout/実行不完了は0件を要求します。
ABSTAIN は A 合格・B 不合格・C 別集計（AI推奨、各回10%未満=最大5件）を併記し、決定欄は空欄です。

- **要決定**: ABSTAIN の採点。C を採用済みと扱っていません。
- **NOT RUN**: ハーネス・採点/集計（#190）、実モデル3回（#191）。形式検証・mock成功は品質の証拠ではありません。
- **NOT RUN**: 検索/回答品質、GPT-6 Luna、形成、実接続IT2/ST、prompt改善（範囲外）。
- #190への気づき: 現行 `LocalClassifier.safe` は bool を返し、SENSITIVE・ABSTAIN・不正出力・timeoutを
  すべてfalseにまとめます。ハーネスは本番の分類器経路を保ちつつ有効Assessmentと実行エラーを区別して
  記録する必要があります。このIssueでは製品の分類器を変更していません。
