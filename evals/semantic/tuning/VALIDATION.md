# Issue #129 第2ラウンド検証（2026-10-10）

現行設定の注記（#131 / ADR 0024）: 以下は閾値0.54で実施した #129 の履歴です。
現在は閾値0.52・bge-m3 Q8_0（CLS、1024次元、接頭辞なし）を採用し、
本文・goldを維持して44記録の偽vectorだけを0.52前後へ追従しました。
答えあり0.5201〜0.521、該当なし0.519〜0.5199、別候補0.51、unrelated-nearは0.51 / 0.50です。
設計と再実行方法は [README](README.md#adr-0024-による偽vectorの追従) を参照してください。
以下の旧SHA・件数・結果は現在版の証拠として流用しません。

起点: `14d4c20`、branch `feature/129-tuning-cases`。
監督のr2指摘を受け、unrelatedの遠い型 / 近い型、日常題材、8記録での順位比較、候補語の非重複UTを追加しました。
実装・ローカル検証・追加commitだけを担当し、push・PR作成・mergeは実行していません。
実モデル・GPUは使用していません。schema・本番検索設定・合否基準・SPEC・ADRは変更していません。

## r2の件数と検証範囲

| 分類 | 全件 | 日常 | 日常の割合 | 6件以上の記録を持つケース |
| --- | --- | --- | --- | --- |
| synonym | 10 | 6 | 60% | 4 |
| paraphrase | 10 | 6 | 60% | 4 |
| cross_language | 24 | 12 | 50% | 8 |
| unrelated | 22 | 13 | 59.1% | 0 |
| threshold | 12 | 6 | 50% | 0 |
| privacy・Binding・失効・同等帯の11分類 | 各1（計11） | 0 | 0% | 0 |

合計89件、日常43件。主要5分類では43 / 78件（55.1%）、全体では48.3%。
8記録の16ケースは正解1件＋別属性・無関係な記録7件、同じBinding・別会話出典です。
偽vectorでは正解だけが閾値以上になります。実モデルの順位の合格を示す結果ではありません。

| 型 | 全件 | 日常 | 技術の記憶 | 答えあり / 該当なし |
| --- | --- | --- | --- | --- |
| unrelated-far | 12 | 8 | 4 | 0 / 12 |
| unrelated-near | 10 | 5 | 5 | 0 / 10 |
| threshold-near | 12 | 6 | 6 | 6 / 6 |

cross_languageは記憶が日本語 / queryが英語12件、その逆12件（各方向の日常6件・8記録4件）。
thresholdは日常・技術それぞれ答えあり3件 / 該当なし3件です。
詳細な題材・ID接頭辞の定義は [README](README.md) に記載しました。

固定62件の全記録・query・Fact更新文を目視確認しました。従来の完全一致・NFKC・NFKC→casefoldの
非重複UTは維持しています。固定goldのrequired_factsの2文字以上の候補語（正規化後77語）が、
調整用query・normalized_textに部分一致しないUTも追加しました。許可リストはありません。
Fact更新が追加された場合はその保存文も両検査の対象です。非一致のUTだけで意味上の独立性を証明するものではありません。
調整専用UTは14件。r1の11分類のprivacy・Binding・失効・同等帯の入力とgoldは完全一致で保持しました。

## r2の環境と最終ゲート

指定worktreeで、uv 0.8.22 / Node 24.19.0、以下の環境を使いました。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH TMPDIR=/dev/shm
P="src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py evals/semantic/provider.py"
```

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages解決 / 78 audited |
| `uv lock --check` | PASS、lock変更なし |
| `uv run --no-sync ruff check $P` | PASS、CIと同じ対象 |
| `uv run --no-sync ruff format --check $P` | PASS、115 files |
| `uv run --no-sync mypy` | PASS、115 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、719件（1001 deselected）、4.85秒 |
| `uv run --no-sync pytest -m it1 -q` | PASS、590件（1130 deselected）、11.64秒 |
| `uv build --no-build-isolation` | PASS、sdist・wheel |
| `bash tools/test-postgres.sh` | PASS、411件（1309 deselected）、pytest 110.49秒 / コマンド実時間110.39秒 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered tests、skip/TODO/cancelledなし |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` と `git diff --cached --check` | PASS |
| `git diff origin/epic/semantic-quality-improvement -- evals/semantic/cases.json evals/semantic/expectations.json` | PASS、空差分 |

追加で、CIと同じfrozen export→独立venvへのhash付き依存install→wheelのno-deps install→
`create_app().title` のimport確認をPASSまで実施しました。独立環境は `/dev/shm/dsc-129-r2-package-check`。
CI対象のshellの `bash -n` と `docker compose -f compose.postgresql.json config --no-interpolate --quiet` もPASS。
最終必須ゲートのFAIL・SKIPは0件。deselectedはmarkerの対象外で、SKIPではありません。
依存由来のdeprecation/type warningはあります。
各コマンドは30分以内の単位で実行しています。

### r2の直接検索fixture評価

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --output /dev/shm/dsc-129-r2-acceptance-report.json
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --cases evals/semantic/tuning/cases.json \
  --expectations evals/semantic/tuning/expectations.json \
  --output /dev/shm/dsc-129-r2-tuning-report.json
```

| dataset | 結果 | embedding calls | 使い捨てPGの起動・終了を含む実時間 |
| --- | --- | --- | --- |
| 固定62件 | PASS、各回62/62、全26分類100%、全必須ゲートPASS | 183 | 24.80秒 |
| 調整89件 | PASS、各回89/89、全16分類100%、全必須ゲートPASS | 264 | 37.14秒 |

両reportは `mode=fixture`, `quality_evidence=false`。実行時revisionはr1の `14d4c20`、dirty=true。
本文を含まないreportだけを `/dev/shm` に置いています。調整89×3回はCIの必須PostgreSQL試験でも確認します。
専用の使い捨てPG・timeout=360秒・既存の資源制限を維持し、既存storage suiteのDBと分離しています。
CIジョブの15分上限に対し、PostgreSQL全試験は約110秒、r1の106.88秒からの増加は約4秒です。
CI runnerでの所要時間は未測定で、ローカル計測値の保証はしません。

| ファイル | SHA-256 |
| --- | --- |
| `../cases.json` | `9f703b32ed79ac997320409dd38142324095084ec93164c60ea6b5aca9e50122` |
| `../expectations.json` | `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4` |
| `cases.json` | `bd25bcc99bf83e5aec7bc47a66464ce9a8557893644da59bb20115ec543c004f` |
| `expectations.json` | `200f8995ed497265a0d3ca9069900d1190cd877c28b04eccfa8412e110be57dc` |

### r2の開発途中のFAILと未実施

- 新しい件数・題材・型・記録数のUTをr1に適用したRed確認: **9 FAIL / 5 PASS**。
- 作成途中のデータ: 同じ本文に異なる偽vectorを割り当てたため、既存loaderが拒否し **14 FAIL**。
  別ケースの同じ本文は同じvectorとし、答えとなる文や閾値ケースは自然な別表現に分けて修正しました。
- 追加候補語UT: `flavored` 内の `red` と `持ち歩きます` 内の `歩き` の部分一致で **FAIL**。
  調整用の文を直し、許可リストで検査を除外していません。
- 開発途中のruff長行 / formatは **FAIL**。修正後の最終lint / formatはPASS。
- 外部データの再取得は **NOT RUN**（取得部分を変更していないため）。r1の取得PASSと出典は下記に保持。
  合成supplement UTは全UTゲートに含めて再実行しています。
- 実GPU・実モデル・調整用回答モデル評価・実環境IT2/ST・GitHub CIは **NOT RUN**。

r2の依頼範囲で未解決事項はありません。実モデルのscore・順位品質と接頭辞・モデル・閾値の比較は#130、
採用判断は#131です。after_answerの回答破棄自体は検索CLIの採点範囲外という既存の制約を維持します。

---

## 第1ラウンドの検証履歴（87件 / 2026-10-10）

起点: `86b7e243cc57845024f1420382274a6d6f0785f0`、branch `feature/129-tuning-cases`。
実装・ローカル検証だけを担当。push・PR作成・mergeは監督の担当で、実行していません。
実モデル・GPU・llama.cppコンテナは使っていません。
分類別件数・独立性・外部補助データの出典と版は [README](README.md) を参照してください。

### 環境と最終ゲート

全コマンドは指定された `129-tuning-cases` worktreeで実行しました。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH TMPDIR=/dev/shm
```

uv 0.8.22 / Node 24.19.0。次のlint/format対象（`P`）には追加したPythonも含め、
CIのlint/formatとmypy設定へ追加しています。

```sh
P="src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py evals/semantic/provider.py"
```

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS、80 packages解決 |
| `uv lock --check` | PASS、lock変更なし |
| `uv run --no-sync ruff check $P` | PASS |
| `uv run --no-sync ruff format --check $P` | PASS、115 files |
| `uv run --no-sync mypy` | PASS、115 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、712件（1001 deselected） |
| `uv run --no-sync pytest -m it1 -q` | PASS、590件（1121 deselected） |
| `uv build --no-build-isolation` | PASS、sdist・wheel |
| `bash tools/test-postgres.sh` | PASS、411件（1302 deselected）、106.88秒 |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered tests |
| `node tools/check-docs.mjs` | PASS、新規文書・JSONもstage後に確認 |
| `git diff --check` と `git diff --cached --check` | PASS |
| `git diff origin/epic/semantic-quality-improvement -- evals/semantic/cases.json evals/semantic/expectations.json` | PASS、空差分 |

各markerのdeselectedは選択対象外で、SKIPではありません。最終必須ゲートのFAIL・SKIPは0件。
既存依存のdeprecation/type warningはありますが、検証基準を変更していません。

#### 偽embeddingによる直接評価

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --output /dev/shm/dsc-129-acceptance-report.json
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --cases evals/semantic/tuning/cases.json \
  --expectations evals/semantic/tuning/expectations.json \
  --output /dev/shm/dsc-129-tuning-report.json
```

| dataset | 結果 | embedding calls |
| --- | --- | --- |
| 固定62件 | PASS、各回62/62、全26分類100%、全必須ゲートPASS | 183 |
| 調整87件 | PASS、各回87/87、全16分類100%、全必須ゲートPASS | 258 |

両reportは `mode=fixture`, `quality_evidence=false`。ケースとgoldのhashは以下で照合しました。
評価時は実装のcommit前だったため、reportの実行commitは起点、dirtyはtrueです。
reportは本文を含まず、`/dev/shm` のローカル揮発性証跡です。

| ファイル | SHA-256 |
| --- | --- |
| `../cases.json` | `9f703b32ed79ac997320409dd38142324095084ec93164c60ea6b5aca9e50122` |
| `../expectations.json` | `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4` |
| `cases.json` | `ea4d2cb88fb5598adfcd5576c9c13b15b2d9e371e97c29878db00271797c31ad` |
| `expectations.json` | `b41b6fe16928b3b8803837888cdbff41e39591a14a8e7e2920a5a50f45180d8a` |

#### 外部データのローカル取得

```sh
python3 -I tools/prepare-semantic-tuning-supplements.py \
  --output /dev/shm/dsc-129-supplements-final \
  --nomiracl-per-subset 50 --mkqa-pairs 100
```

PASS、NoMIRACL関連50問・非関連50問／976 passage、MKQA日英100ペア。
取得元・固定revision・カードのライセンス表示・最小形式はREADMEに記録済みです。
本文と変換結果は外部パスだけに置き、コミットしません。
実モデル利用・品質測定・IT2/STは **NOT RUN**（依頼範囲外、#130以降）。

### 初期の失敗と対応

- 調整データ未作成のRed確認: 新規UT7件 **FAIL**。データを作成後、7件PASS。
- 取得変換: 「relevantは最大10 passage」「corpus IDに重複なし」という初期前提で2回 **FAIL**。
  元の追加判定をすべて保持し、同一本文だけ重複排除するよう修正。矛盾する重複は拒否。
  合成UTを追加し、上記の最終取得・変換をPASSまで実施。
- 開発中のruff長行・format・mypy型注釈不足は **FAIL**。修正後、最終lint/format/mypyはPASS。
- 初回PG全試験: **FAIL**（309 passed、1 failed、101 errors）。追加261 schemaの評価後に、
  共有の使い捨てPGが `storage_unavailable` となりました。
  コンテナはcleanupで削除済みのため、OOM等の直接原因は未確認です。
  catalog/WAL増加を既存storage suiteの512 MiBコンテナへ重ねないため、新規評価だけを
  既存 `with-test-postgres.sh` の専用使い捨てPGへ分離。全試験を再実行し411件PASS。
  runnerの資源制限・既存テスト・検索基準を緩めていません。

### 未解決事項と判断

本実装の未解決事項はありません。実モデルのscore分布・接頭辞・モデル・閾値の選定は#130です。
外部データにはCoreの架空Binding・会話citation・記憶の正本がないため、synthetic schemaへ偽装せず、
判定付きquery/passagesと日英questionペアのローカル最小形式を採用しました。
既存schema/report/製品コード/SPEC/ADR・合否基準・検索設定を変更していません。
after_answerの回答破棄は検索CLIの範囲外であり、87件の回答モデル評価を完了したとは扱いません。
