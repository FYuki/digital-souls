# 本番contextによる回答評価・promptfoo実装証跡（Issue #114）

日付: 2026-10-09。対象: FYuki/digital-souls-core、`feature/114-answer-promptfoo`。
起点: `d41edb9`（Epic #111）。実装revision: `8b851c4eb7267f63f627175c4f39e7c68d0f24f7`。
正常fixtureの3回実行時はdirty=false。証跡追加は文書だけのcommit。

正本: [Issue #114](https://github.com/FYuki/digital-souls-core/issues/114)、
[Epic #111](https://github.com/FYuki/digital-souls-core/issues/111)、[ADR 0023](../adr/0023-semantic-evaluation-contract.md)。
利用方法: [評価README](../../core/evals/semantic/README.md)。

## 実装と設計判断

- #113のisolated_case・履歴append・正本register・public mutationを共有。本番MemoryRetrieval / MemoryContext / Inferenceを通し、順位・context・chat promptを再実装しない。
- classifierは合成NOT_SENSITIVE応答だけを差し替える。回答fixtureは別JSON入力としてProvider portで返す。本番の送信直前checkと公開直前checkで拒否・破棄を確認する。
- 合成CCv3カードを固定。Mioriは使わず、検索Bindingに合わせるcharacter IDだけを置換する。
- providerはinput-only validatorでcases.jsonを検証し、expectations.jsonを開かない。assertion / gateは既存PythonのAnswerExpectation.matches_factsを共有（NFKC→casefold、全グループAND・グループ内OR）。
- dispatchはvalidとmemory_ids、回答破棄はdiscardedを照合。禁止事実・dispatch・破棄・空contextを必須ゲート、必須語句を分類別90%の品質条件として分ける。
- promptfoo 0.117.2をnpm lockで固定。固定版の実ソースとexportを確認し、custom assertionに明示的なassertion識別子とmetadataを返す。no-writeは使わず、専用0700一時領域で内部DB / exportを作り終了時に削除する。
- gateはraw exportの完全性・assertion・provider・cache・score・統計を検証してから全回答をPythonで再採点する。固定版のfailureReason=1と一致した決定論的assertion失敗だけを品質FAILとして認め、実行errorは拒否する。平均点・CLI終了コードだけでは合否を決めない。
- 実モデルmodeは明示profile＋実行flagを必要とし、既存の無認証managed loopback adapterだけを使う。既定無効、fallbackなし。モデル識別は設定値で、実backend同一性の独立検証ではない。
- 必須postgres-storageジョブへSHA固定setup-node、npm ci、required reporterによるJS試験、62件×3回のfixture gateを追加。required check名は維持。Python依存・入力ケース・gold・SPEC・docs/memory-evaluation.mdは変更していない。

## TDDと品質ゲート

固定環境: uv 0.8.22、Node 24.19.0、Python 3.12.3、TMPDIR=/dev/shm。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

TDD RED: Python採点17件・Node assertion/config/gate 3件・PostgreSQL provider 5件が未実装で実際にFAIL。
初期の実export検証もcritical assertion識別子欠落でFAILし、明示識別子を返す修正後に再検証した。

| コマンド | 結果 | 件数・補足 |
| --- | --- | --- |
| uv sync --locked | PASS | lockを変更しないinstall |
| uv lock --check | PASS | Python依存追加なし |
| uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS | 新providerも対象 |
| uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS | 112 files |
| uv run --no-sync mypy | PASS | 112 source files |
| uv run --no-sync pytest -m ut -q | PASS | 608（起点590、+18: 語句・behavior・dispatch・集合・分類集計） |
| uv run --no-sync pytest -m it1 -q | PASS | 590（起点581、+9: input/gold分離・設定拒否・profile境界） |
| uv build --no-build-isolation | PASS | sdist / wheel |
| bash tools/test-postgres.sh | PASS | 410（起点404、+6: payload・空context・送信拒否・回答破棄・provider error・gold未読） |
| npm ci --prefix evals/semantic --no-audit --no-fund | PASS | promptfoo 0.117.2、602 packages |
| node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs | PASS | 54（起点27、+27: gate・不正fixture・通信・環境・無効mode・lock） |
| node tools/check-docs.mjs | PASS | 新規ファイルをstage後も検証 |
| git diff --check / git diff --cached --check | PASS | whitespace |
| bash -n tools/evaluate-semantic-answer.sh / Node syntax checks | PASS | runner / gate |

既存Starlette / Pydanticの警告とnpmのdeprecated / install-script advisoryが出た。
製品試験のSKIPは0。required Node reporterは0件・skip・TODO・cancelを拒否する。

## 全62ケース・cacheなし3回

```sh
bash tools/evaluate-semantic-answer.sh --runs 3 \
  --output /dev/shm/semantic-answer-114-report.json
```

| 回 | ケース | 全分類 | 必須ゲート | 全体 |
| --- | --- | --- | --- | --- |
| 1 | 62/62 | 26分類すべて100% | 違反0 | PASS |
| 2 | 62/62 | 26分類すべて100% | 違反0 | PASS |
| 3 | 62/62 | 26分類すべて100% | 違反0 | PASS |

mode=fixture、quality_evidence=false、classifier=synthetic、promptfoo_version=0.117.2。
回答modelはfake-provider-port、embedding spaceはsynthetic / semantic-cases-v1 / 4次元 / fixture-no-cache。
本番検索設定: candidate_pool_size=20、max_retrieved_memories=5、relevance_threshold=0.54、equivalence_margin=0.002。

| 分類 | 各回の合格/件数（1・2・3回とも同じ） |
| --- | --- |
| synonym | 10/10 |
| paraphrase | 10/10 |
| cross_language | 10/10 |
| unrelated | 10/10 |
| negation | 1/1 |
| update | 1/1 |
| multisource | 1/1 |
| multisource_revocation | 1/1 |
| private | 1/1 |
| excluded | 1/1 |
| deleted_source | 1/1 |
| deleted_memory | 1/1 |
| binding_character | 1/1 |
| binding_subject | 1/1 |
| binding_client | 1/1 |
| long_text | 1/1 |
| after_search | 1/1 |
| after_answer | 1/1 |
| epoch_change | 1/1 |
| fact_attachment | 1/1 |
| fact_version | 1/1 |
| fact_revocation | 1/1 |
| semantic_direct | 1/1 |
| semantic_derived | 1/1 |
| equivalent_order | 1/1 |
| threshold | 1/1 |

入力SHA-256: `9f703b32ed79ac997320409dd38142324095084ec93164c60ea6b5aca9e50122`。
gold SHA-256: `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4`。
body-free report SHA-256: `e2b94ab586482cdbb4f10649afc2f1d5c30ef2fe4aaa330267ab2782cb3c4f7a`。
report JSONはローカルの上記パスに保持。本文・query・回答全文・raw exportは証跡へ含めない。

## 不正fixture・未実施

```sh
bash tools/evaluate-semantic-answer.sh --runs 1 --fixture-variant forbidden \
  --output /dev/shm/semantic-answer-114-forbidden.json
```

禁止語句混入fixture: **FAIL（期待どおり、exit 1）**。全62件を実行し、private-sourceのforbidden gateだけがfalse。
実行revisionとdirtyは正常fixtureと同じ。Node試験では必須事実欠落（synonym 8/10）でgate FAIL、
error応答でgate拒否を確認。PostgreSQLでもerror入力fixtureがprovider成功にならないことを確認。

追加のmissing variant全件実行は初回にexport段階でFAILした。rawはrunnerが削除するため原因は未特定。
同じ固定版CLI・引数・隔離環境で再実行するとexit 100、62件のexportが得られ、
独立gateはsynonym 8/10・必須ゲート違反0・全体FAILを再計算した。
この再実行は診断用の直接CLI呼び出しとinspectReport / finalizeReportで行った。
通常runnerでも再試行し、全62件・synonym8/10・必須ゲート違反0・全体FAIL（exit 1）のreportを得た。
再試行report: `/dev/shm/semantic-answer-114-missing-runner.json`（8b851c4、証跡編集中のdirty=true）。
初回のexport失敗は再現しておらず、原因未特定の追加診断として残す。
正常fixtureの3回PASSと、失敗した初回の追加診断を混同しない。

- 実モデル（GPU、llama.cpp、GPT-6 Luna）: **NOT RUN**。#115の範囲。fixture PASSはモデル品質合格ではない。
- 実環境IT2 / ST、形成〜利用の接続受入: **NOT RUN**。
- GitHub CI / push / PR / merge: **NOT RUN**。この担当の権限はlocal commitまで。監督のレビュー・統合が残る。

## 変更ファイル

- `.github/workflows/api.yml`
- `CONTRIBUTING.md`
- `docs/evidence/2026-10-09-semantic-answer-promptfoo.md`
- `evals/semantic/README.md`
- `evals/semantic/answer-fixtures.json`
- `evals/semantic/answer-profile.example.json`
- `evals/semantic/assertions.cjs`
- `evals/semantic/bridge.cjs`
- `evals/semantic/characters.json`
- `evals/semantic/config.cjs`
- `evals/semantic/evaluation.card.json`
- `evals/semantic/invalid-answer-fixtures.json`
- `evals/semantic/network_guard.cjs`
- `evals/semantic/package-lock.json`
- `evals/semantic/package.json`
- `evals/semantic/provider.py`
- `evals/semantic/report_gate.mjs`
- `pyproject.toml`
- `src/digital_souls_core/semantic_answer_bridge.py`
- `src/digital_souls_core/semantic_answer_evaluation.py`
- `src/digital_souls_core/semantic_answer_runtime.py`
- `src/digital_souls_core/semantic_evaluation_cases.py`
- `src/digital_souls_core/semantic_evaluation_runtime.py`
- `tests/test_postgres_semantic_answer.py`
- `tests/test_semantic_answer_evaluation.py`
- `tests/test_semantic_answer_provider.py`
- `tools/evaluate-semantic-answer.mjs`
- `tools/evaluate-semantic-answer.sh`
- `tools/semantic-answer.test.mjs`

## 第2ラウンドの追跡

上記の原因未特定だったexport失敗は、[第2ラウンドの調査・修正証跡](2026-10-09-semantic-answer-promptfoo-round2.md)で
better-sqlite3のnative GCクラッシュとして再現・特定した。固定bindingの修正後に30 run連続と不正fixture3種を検証した。
