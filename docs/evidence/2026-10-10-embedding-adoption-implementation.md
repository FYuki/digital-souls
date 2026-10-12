# ADR 0024 の embedding 設定実装と合成検証

2026-10-10、Issue #131。指定branch `feature/131-embedding-adr`、起点
`812a56dc2773957068febde04263d1d655a4bf79` に実装しました。
[ADR 0024](../adr/0024-multilingual-memory-embedding.md) が採用設定の正本です。
以下の検証はcommit前の作業tree（reportのcommitは起点SHA、dirty=true）で実行しました。
実モデル・GPU・llama.cppコンテナの作成/起動・push・GitHub PR操作・mergeは **NOT RUN**。
使い捨ての合成PostgreSQLだけを既存runnerで起動し、終了時にrunnerが削除しました。

## 実装と入力の保持

`RetrievalPolicy` の閾値は0.52です。UTで既定値・0.52ちょうどの採用・0.52−1e-9の除外を確認します。
候補20・最大5・同等帯0.002・relevance式・タイブレーク・合否基準・adapterは変更していません。
候補比較のbaseline照合は本番既定値を参照します。#130のreportと過去の証跡は書き換えていません。

固定62ケースの変更は `below-threshold` / `below-threshold-record` のvector一つだけです。

- 旧値: `[0.63, 0.7765951326141569, 0.0, 0.0]`、relevance約0.53757。
- 新値: `[0.5565557545480253, 0.8308102623821387, 0.0, 0.0]`、query `[1, 0, 0, 0]` に対するrelevance **0.515**。
- 新閾値0.52直下の該当なしを保つ、ユーザー承認済みQ3例外です。実モデル評価は偽vectorを使いません。
- `git diff origin/epic/semantic-quality-improvement -- evals/semantic/expectations.json` は空です。
  casesのdiffはvectorの数値2行だけです。新vectorを旧値に戻したbyte列がbaseのファイルと完全一致することも確認しました。

調整89件は境界付近の44記録のvectorだけを変更し、本文・query vector・goldを保持しました。
旧relevanceから0.02引き、`d = 1/relevance - 1`、`x = 1 - d²/2`、
`[x, sqrt(1-x²), 0, 0]` で有限・非ゼロの単位vectorを作ります。
答えあり6件は0.5201〜0.521、該当なし6件は0.519〜0.5199、別候補12件は0.51、
unrelated-nearの20記録は0.51 / 0.50です。それ以外のvectorは不変です。
vector差分を戻したJSONの完全一致とgoldの空差分を確認しました。

[Compose](../../compose.llamacpp.yml) のembeddingサービスはbge-m3 Q8_0、CLS、alias `bge-m3`、
ctx/batch/ubatch=2048、parallel=1、gpu-layers=99、threads/threads-batch=8、cache-ram=0です。
chatと同じ固定image、非root、read_only、cap_drop ALL、no-new-privileges、pids 256、memory 12g、
64MiB tmpfs、モデルread-only bind、create_host_path=falseを使用し、127.0.0.1:18082だけを公開します。
[起動script](../../tools/start-llamacpp.sh) はchatとembedding両ファイルのSHA-256を検証します。
旧手動nomicコンテナの停止・exited確認・切替・rollbackは [運用手順](../llamacpp-operations.md) に記載しました。
profile例は無効のまま、bge-m3の実digest・1024次元・18082へ更新しています。
ローカルGGUFのSHA-256と634553760 bytesは実ファイルを読み取り確認し、ADRの固定値と一致しました。

永続化なしは `MemoryRetrieval.search` のembed→ローカル `vectors`→`rank_records` の経路、
`MemoryRecordStore` のport、`postgres_record_schema.COLUMNS` と版6 `postgres_schema` を照合しました。
vector列・書込経路・永続embedding indexはなく、データ移行不要です。ADR 0024にも確認済みと記録しました。

## 品質ゲート

環境はuv 0.8.22、Python 3.12.3、Node 24.19.0です。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH TMPDIR=/dev/shm
P="src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py"
```

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages解決、lock変更なし |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check $P` | PASS、API CIと同じ対象 |
| `uv run --no-sync ruff format --check $P` | PASS、120 files |
| `uv run --no-sync mypy` | PASS、120 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、743件 |
| `uv run --no-sync pytest -m it1 -q` | PASS、592件 |
| `uv build --no-build-isolation` | PASS、sdist/wheel |
| `bash tools/test-postgres.sh` | PASS、412件、155.04秒（pytest） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered tests、SKIP/TODO/cancelledなし |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` / `git diff --cached --check` | PASS |
| `docker compose -f compose.llamacpp.yml config --quiet` | PASS、UID/GID=1001・両モデルpathはダミーを指定、起動なし |
| `docker compose -f compose.postgresql.json config --no-interpolate --quiet` | PASS |
| API CI対象5 shellの `bash -n` | PASS |

さらにCIと同じ `uv export --frozen --no-dev --no-emit-project --format requirements-txt`、
独立venvへのhash付き依存install、wheelのno-deps install、`create_app().title` のimport確認もPASSです。
最終必須ゲートのFAIL・SKIPは0件。markerのdeselectedは対象外でSKIPではありません。
依存由来のdeprecation/type/npm警告と、別filesystemでのhardlinkからcopyへのfallback通知があります。
初期Red確認は9 FAIL / 102 PASSで、旧設定では新UTが失敗することを確認しました。
設定・fixture更新後、対象テスト299件と上記全ゲートがPASSです。
各コマンドは30分以内で完了しました。

## cacheなしfixture評価

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 --output /dev/shm/dsc-131-retrieval.json
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --cases evals/semantic/tuning/cases.json \
  --expectations evals/semantic/tuning/expectations.json --output /dev/shm/dsc-131-tuning.json
bash tools/evaluate-semantic-answer.sh --runs 3 --output /dev/shm/dsc-131-answer.json
```

| 評価 | 各回の結果 | embedding calls |
| --- | --- | --- |
| 検索62件×3 | PASS、62/62、全26分類100%、全必須ゲートPASS | 183 |
| 検索・調整89件×3 | PASS、89/89、全16分類100%、全必須ゲートPASS | 264 |
| 回答fixture62件×3 | PASS、62/62、全26分類100%、全必須ゲートPASS、各run再試行なし | reportにcall数の項目なし |

全reportはmode=fixture、quality_evidence=false、本番閾値0.52です。
reportは本文なしの一時ファイルで、上記 `/dev/shm` に保持しています。

| 入力 | SHA-256 |
| --- | --- |
| 固定cases | `44b90af81bdbc9a7c117fd5158d23bde4878571de2584b99bb1973f85a2ecdcf` |
| 固定gold | `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4` |
| 調整cases | `2016bec4aa1248a10201b6570dba6711c35910bd64372d82438a20e4f0153de1` |
| 調整gold | `200f8995ed497265a0d3ca9069900d1190cd877c28b04eccfa8412e110be57dc` |

実モデルの62件再評価・コンテナ起動は#132で監督が行います。近い型の無関係質問は後続Epicです。
GitHub上のCI、実GPU/実モデル、分類器品質、形成〜利用IT2/STはNOT RUNで、この合成PASSから推定しません。
SPECの過去の実モデルFAILは保持しています。
