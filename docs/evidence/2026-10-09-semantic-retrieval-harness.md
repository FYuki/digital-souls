# Issue #113: 本番経路の意味検索評価ハーネス

2026-10-09。正本は [Issue #113](https://github.com/FYuki/digital-souls-core/issues/113)、
[Epic #111](https://github.com/FYuki/digital-souls-core/issues/111) V1〜V7、
[ADR 0023](../adr/0023-semantic-evaluation-contract.md)。形成・実モデル・回答評価は今回の対象外です。

## revision と実行条件

- 作業 branch: `feature/113-retrieval-harness`、起点 `b9fab0061a1ded83d4b5aa76a9840ae18dc8609c`。
- 実装: `9dca5609d0104321611f1d1e421fb5e36737ed0e`。
- Node の移設先追従: `2726bfd`。
- 最終実装・品質ゲート・CLI の対象: `9a855557c2def9a3703cb2822a847932979dba29`。
- CLI の実行開始時の作業ツリーは clean（report の `commit.dirty=false`）。本証跡と report は実行後に追加。
- Ubuntu、Python 3.12.3、uv 0.8.22、Node 24.19.0。依存追加・lock 変更なし。
- PostgreSQL 18 の既存固定 digest、network none、公開ポートなし、専用 Unix socket、合成データのみ。
  DB プロセスはコンテナ、Python は host で動きます。明示 profile の host loopback embedding と両立します。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

## 実装と設計判断

[semantic_evaluation_runtime.py](../../core/src/digital_souls_core/semantic_evaluation_runtime.py) を #114 用の共通 API としました。
`prepare_case` は `HistoryStore` / `MemoryRecordStore` の port、trusted `CaseClock`、embedding を受け取ります。
`isolated_case` はその PostgreSQL 構成を作り、各ケース・各回に新しい UUID schema を割り当て、終了時に当該 schema だけを削除します。
同じ Binding・`target` ID を持つ異なるケースを同時に開き、本文が独立し、他方の候補に `current` が false、
片方を閉じても他方が有効という PostgreSQL 試験で混入を否定しました。

実行順は create → revision 順 append（trusted stated_at、append 時の除外）→ `registration_batches` の登録 →
除外記録の登録拒否 probe → before_search mutation です。論理会話 ID を生成 ID へ写像し、citation、
5W の理由の citation、Semantic の EpisodeEvidence source を一緒に置換します。
controls、selected turn deletion、conversation deletion、`FactUpdateMutation.batch()` の register を使い、
source・epoch の SQL 書換えはしません。SQL は専用 schema の初期化・削除に限り、製品の正本操作は既存 adapter を通します。

`CapturingRetrieval.search` は本番 `MemoryRetrieval.search` へ委譲します。本番 `MemoryContext.context` が内部で呼ぶ
検索を捕捉するので、採点と context は同じ検索の返却 snapshot を使い、1ケースにつき検索を1回だけ行います。
context の保存文・Fact が返却 snapshot と一致することも確認します。
本番 context が CoreError を空 context に変換しても、評価側は捕捉した失敗を拒否し、正常な検索0件と区別します。
取得時点の正本の有効性を先に検証し、after_search mutation の後に `GuardedContext.valid()` と gold の
`dispatch.valid` を照合します。after_answer はここでは適用せず、dispatch 前の有効性だけを確認しました。

privacy は本番の LocalClassifier・scanner・PrivacyPolicy・MemoryRetrieval を通します。
分類器 provider の応答だけが合成の NOT_SENSITIVE です。report は `classifier=synthetic`。
**分類器品質はこの検索評価の対象外**で、SPEC §3.2 の記憶判断評価で扱います。

[semantic_retrieval_evaluation.py](../../core/src/digital_souls_core/semantic_retrieval_evaluation.py) で採点・集計・report 検証を行います。
relevant ID の全包含、no_match の空結果、relevant 候補への必須 Fact 添付を品質として判定します。
禁止 ID・禁止 Fact、未検証正本、閾値未満、期待順序の不一致、dispatch guard、context 不一致は必須ゲートです。
各分類の品質率と必須ゲートは別に集計し、全回・全ケースの必須ゲートを平均で相殺しません。
90% は inclusive（9/10 は合格、8/10 は不合格）です。

embedding の入出力をメモリ内の `RecordingEmbedding` で記録し、unit vector の二乗 L2 距離から relevance を独立に検算します。
順位付けは製品に任せ、評価側は再実装しません。no_match では返却0件に加え、記録した適格候補に閾値以上が0件であることも検証します。
vector cache はなく、各回の embedding を呼び直します。入力・vector・query は report に書きません。
0ケース、結果欠落・重複・未知 ID、error、非有限 score、改変した合否・分類集計を拒否します。
外部から report を読む場合も `EvaluationReport` の parse に加え、`validate_report(report, data, expected_runs)` を呼びます。

profile なしは fixture、明示した enabled=true の `LocalEmbeddingProfile` だけが local_model です。
無効・不正・未指定 enabled の profile は失敗し、fixture へ fallback しません。
model digest は宣言であり、backend 真正性は未検証（`backend_identity_verified=false`）です。

#114 の呼出し例（実モデル・回答生成は未実行）:

```python
with isolated_case(postgres_config, case, embedding) as runtime:
    results, guarded = await runtime.search_context()
    runtime.mutate("after_search")
    dispatch_valid = guarded.valid()
    # 有効時だけ回答を生成し、公開前に以下を行う（#114 の責任）。
    runtime.mutate("after_answer")
    publish_valid = guarded.valid()
```

## 変更ファイル

- 新規本体: `src/digital_souls_core/semantic_evaluation_runtime.py`、`semantic_retrieval_evaluation.py`。
- 新規 CLI / 共通 DB runner: `tools/evaluate-semantic-retrieval.py`、`evaluate-semantic-retrieval.sh`、`with-test-postgres.sh`。
- 新規試験: `tests/test_semantic_retrieval_evaluation.py`、`test_semantic_evaluation_cli.py`、`test_postgres_semantic_evaluation.py`。
- 参照更新: `.github/workflows/api.yml`、`CONTRIBUTING.md`、`pyproject.toml`、`tools/test-postgres.sh`、`tools/check-docs.test.mjs`。
- 利用文書: `docs/memory-evaluation.md` のコマンド・新ツール入口・合成分類器の説明を更新。
  旧指標等の参考説明は同期待ちと明記し、全面同期は #115 に残しました。
- 撤去: `tests/fixtures/memory-retrieval-evaluation.json`、`tools/evaluate-memory-search.py`、
  `src/digital_souls_core/memory_evaluation.py`、`tests/test_memory_evaluation.py`。
- 本証跡と [全ケース JSON report](2026-10-09-semantic-retrieval-harness.json) を追加。
- cases・expectations・ADR・SPEC・本番検索実装・依存は変更していません。

## TDD と途中失敗

最初に採点・集計・profile の UT を追加し、未実装モジュールの ModuleNotFoundError による RED を確認しました。
実装後に26件 PASS、report 再検証・境界等を追加して新規 UT は49件です。

初回の全ゲートで Node 1件 FAIL（残り26件 PASS）:
既存 digest 一致試験が移設前の `test-postgres.sh` から image を読んでいたためです。
`with-test-postgres.sh` へ検証対象を移し、両 wrapper が共通 runner を呼ぶ確認を追加して27件 PASS。
この初回 FAIL を PASS と扱いません。

最初は検索と context を別々に実行していましたが、context 内部の本番検索結果を捕捉する形へ整理しました。
最終版は1回だけ検索し、関連 Python ゲートと PostgreSQL・CLI を再実行しました。

## 最終品質ゲート

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py` | PASS、105 files |
| `uv run --no-sync mypy` | PASS、105 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、590件 |
| `uv run --no-sync pytest -m it1 -q` | PASS、581件 |
| `uv build --no-build-isolation` | PASS、sdist / wheel |
| `bash tools/test-postgres.sh` | PASS、404件、67.14秒 |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、27件 |
| `node tools/check-docs.mjs` | PASS（証跡追加後にも実行） |
| `git diff --check` | PASS（証跡追加後にも実行） |
| `bash -n tools/test-postgres.sh tools/with-test-postgres.sh tools/evaluate-semantic-retrieval.sh` | PASS |

全必須試験に skip・xfail・xpass はありません。deselected は marker で別 suite に分離した試験です。
既存依存の Starlette/AnyIO deprecation と Pydantic ReadOnly の warning は残っています。

| suite | 起点 | 撤去 | 追加 | 最終 | 増減の理由 |
| --- | ---: | ---: | ---: | ---: | --- |
| UT | 579 | 38 | 49 | 590 | 旧評価 UT を撤去し、新採点・report・profile・90%境界を追加 |
| IT1 | 579 | 3 | 5 | 581 | 旧 CLI 試験を撤去し、新 CLI の拒否・秘匿を追加 |
| PostgreSQL | 398 | 0 | 6 | 404 | 62ケース×3回の CLI 必須試験、隔離・写像・除外・エラー検知 |
| Node | 27 | 0 | 0 | 27 | 既存 digest 試験の対象移設、共通 runner 呼出しを確認 |

## 偽 embedding の CLI 実行

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --output docs/evidence/2026-10-09-semantic-retrieval-harness.json
```

全ケース JSON report を保存。`mode=fixture`、`quality_evidence=false`、`classifier=synthetic`。
検索設定は候補20・閾値0.54・同等帯0.002・最大5件。
入力 schema_version=1、gold schema_version=2、両ファイルの SHA-256 は report に保存。

| 回 | ケース | 分類別品質率 | 必須ゲート違反 | 全体 |
| --- | ---: | --- | ---: | --- |
| 1 | 62 | 全26分類100% | 0 | PASS |
| 2 | 62 | 全26分類100% | 0 | PASS |
| 3 | 62 | 全26分類100% | 0 | PASS |

embedding 呼出しは183回（61ケース×3回）。epoch 変更ケースは適格候補0件で embedding を呼びません。
各回の検索0件は12ケース（無関係10・epoch変更・閾値未満）です。ケース/結果の欠落とは別です。
CI の `postgres-storage` は `tools/test-postgres.sh` の postgres marker でこの CLI の全62ケース×3回を必須実行し、
report の parse・再検証・必須ゲート・分類集計・本文非収録を確認します。

## NOT RUN・残る範囲

- 実 embedding・ローカルモデル起動・GPU操作・モデル品質の受入: **NOT RUN**、#115。
- promptfoo による回答評価・after_answer の破棄判定: **NOT RUN**、#114。
- 実運用 DB の schema 適用・IT2・ST・形成から利用までの実接続受入: **NOT RUN**。
- GitHub CI: **NOT RUN**（workflow の必須試験はローカルで実行）。push・PR・merge は依頼の禁止に従い未実施。
- 未解決の仕様判断: なし。合成 PASS を分類器品質・実モデル品質の PASS としません。
