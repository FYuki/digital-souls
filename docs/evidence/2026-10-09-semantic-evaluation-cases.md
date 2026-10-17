# 意味検索・回答評価ケース固定の実施証跡

対象: [Issue #112](https://github.com/FYuki/digital-souls-core/issues/112)、[Epic #111](https://github.com/FYuki/digital-souls-core/issues/111)。

起点: `12ff21c5f55dbda32a301391bc0d0505ed5316a9`（`epic/semantic-evaluation-redo`）。
作業branch: `feature/112-evaluation-cases`。2026-10-09に実行。
第2ラウンドの起点: `5c52330`。以下の品質ゲート表は第2ラウンドの最終状態です。

## 変更と範囲

[ADR 0023](../adr/0023-semantic-evaluation-contract.md)でV1〜V7・旧評価の置換を記録しました。
[評価データREADME](../../core/evals/semantic/README.md)に62ケースの分類件数・旧19件の対応と公開操作への読み替えを示しました。
[検証モジュール](../../core/src/digital_souls_core/semantic_evaluation_cases.py)はstrict/extra forbidの入力とgoldを別型で読み、参照・citation・vector・初期登録構造を純粋に検証・変換します。

先にUTを書き、モジュール未作成の `ModuleNotFoundError`（collection error）でREDを確認しました。
追加UTのREDで、除外Episodeを根拠にするSemanticが登録batchに残る検証漏れを確認し、修正しました。
第1ラウンドのUTは49件追加で、うち43件は不正データ拒否のparametrizeです。
残る6件はデータ変換・件数/旧対応・gold分離・本文非開示・domain値保持・純粋な本番順位関数との整合です。

入力にgoldを混入せず、同一本文の偽vectorを統一しました。旧ミント本文は同義語ケースの非関連候補と日英ケースの関連候補でvectorが矛盾していたため、日英ケースのquery軸を合わせています。
これはCIの道具の検証用であり、モデル品質の証拠ではありません。

## 第2ラウンドの回答gold修正

監督の指摘に従い、全62ケースのrequired/forbiddenを見直しました。非空のgoldを持つ51ケースはany-ofグループへ変換、11ケースは両側の空配列を維持しています。意味・許容表記を修正した22ケースと理由の一覧は[評価データREADMEの監査表](../../core/evals/semantic/README.md#第2ラウンドの全件gold監査)に記録しました。残る40ケースの確認結果、短い色名の誤一致、否定関係の検出限界も同じ箇所に記載しました。

第1ラウンドのgoldは未公開・回答評価未実行です。prompt・設定調整と評価結果を見る前の基準修正であり、結果に合わせた緩和ではありません。`cases.json` は変更せず、goldのschemaのみ2に更新し、旧schema 1 / 平坦配列を拒否します。候補は合成記憶・会話の事実と自然な訳・表記揺れに限定しました。

ADR 0023とREADMEに、全requiredグループの各1候補以上、全forbidden候補の不在、Unicode NFKC→casefold後の部分文字列一致を明記しました。Pydanticモデルで空グループ・空文字・空白だけ・正規化後のグループ内/間重複・required/forbidden間の同一候補を拒否します。純粋な語句判定は `AnswerExpectation.matches_facts` に置き、guard/behavior/discardやハーネス全体の実装は含めません。

先に追加UTを書き、**RED: 38 failed / 66 passed** を確認しました（既存平坦形式の受理、新predicateの不在など）。実装・データ修正後は対象UT **106 passed**。第2ラウンドの追加は57件です。

| 追加UTの内容 | 件数 |
| --- | --- |
| required/forbiddenの不正グループ・空文字・重複等の拒否 | 18 |
| 正規化後のrequired/forbidden重複拒否・旧schema拒否 | 2 |
| 全グループAND / 候補OR / 禁止候補・NFKC/casefold | 8 |
| 固定goldで自然な日英・語順・助詞・名詞・時期表記を受理 | 19 |
| 固定goldで不足・誤事実・禁止事実を拒否 | 8 |
| 半角カナ・Unicode結合文字の正規化 | 2 |

## 品質ゲート

共通環境:

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

`uv --version`: **uv 0.8.22**、`node --version`: **v24.19.0**。

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages resolved / 78 installed packages audited |
| `uv lock --check` | PASS、lock変更なし |
| `uv run --no-sync ruff check src tests tools/evaluate-memory-search.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-memory-search.py` | PASS、102 files |
| `uv run --no-sync mypy` | PASS、102 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、579 passed / 977 deselected |
| `uv run --no-sync pytest -m it1 -q` | PASS、579 passed / 977 deselected |
| `uv build --no-build-isolation` | PASS、sdistとwheel生成 |
| `bash tools/test-postgres.sh` | PASS、398 passed / 1158 deselected、使い捨てDocker PostgreSQL |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、27 registered required tests |
| `node tools/check-docs.mjs` | PASS、新規ファイルをstageした状態で確認 |
| `git diff --check` / `git diff --cached --check` | PASS |

第1ラウンドの実装途中のruff/mypy指摘は修正済み。第2ラウンドでは先行UTのRED後、指定品質ゲートの最終実行は全PASSです。途中のFAILをPASSとして扱いません。
pytestのdeselectedは別markerの試験で、SKIPではありません。skip/xfail/xpassはありません。
StarletteのBlockingPortal非推奨警告、PydanticのReadOnlyに関する既存警告は残っています。

| 試験 | 起点の件数 | 最終件数 | 増減理由 |
| --- | --- | --- | --- |
| UT | 473 | 579 | +106（第1ラウンド49、第2ラウンド57）、検証/変換・固定データ・回答goldの試験追加 |
| IT1 | 579 | 579 | 変更なし |
| PostgreSQL | 398 | 398 | 変更なし、新データの実登録試験はIssue #113 |
| Node | 27 | 27 | 変更なし |

## 後続と未実施

- 新ケースのPostgreSQLへの実登録・本番MemoryRetrieval/contextを使うハーネス: **NOT RUN / 未実装**、Issue #113の範囲。
- promptfoo回答評価: **NOT RUN / 未実装**、Issue #114の範囲。
- 実モデル・cacheなし3回の品質評価、実環境IT2/ST: **NOT RUN**、この変更の合成PASSで代用しません。
- 旧fixture・旧ツール・memory_evaluation.pyの撤去と利用文書同期: 未実施、ADR記載の後続Issueへ引継ぎ。
- push・PR作成・merge: 未実施、監督の担当。

未解決の製品判断はありません。実装詳細として、会話の論理IDを後続ハーネスで生成IDへ一括写像する形式、Fact添付用goldを候補IDと分ける形式、dispatch直前と応答公開前の判定を区別する形式を採用しました。
