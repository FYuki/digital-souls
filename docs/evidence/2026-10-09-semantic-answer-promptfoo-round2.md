# 回答評価の間欠クラッシュ修正・第2ラウンド証跡（Issue #114）

日付: 2026-10-09。対象: FYuki/digital-souls-core、`feature/114-answer-promptfoo`。
起点: `7225efd`。実装revision: `94960e0d7a88acc0c62cd27224116c2dff01955e`。
指定worktreeで実装・local commitのみ。push / PR / mergeはNOT RUN。
正常10回・不正3種はいずれもこの実装revision、dirty=falseで実行した。後続commitは証跡だけ。

[Issue #114](https://github.com/FYuki/digital-souls-core/issues/114)、
[第1ラウンド](2026-10-09-semantic-answer-promptfoo.md)、
[評価README](../../core/evals/semantic/README.md)、
[本文を含まない機械可読証跡](2026-10-09-semantic-answer-promptfoo-round2.json)。

## 原因と根拠

診断だけを追加した起点のrunnerで正常fixtureを2回実行した。1回目は3 run PASS。
2回目はrun 1でpromptfooが **SIGABRT**、終了コードnull、raw exportなしとなり、runnerはexit 1。
固定Node 24.19.0 / promptfoo 0.117.2 / better-sqlite3 11.10.0の組合せで再現した。

私有stderrの既知パターン分類は `native-cleanup-hook-abort`。
ネイティブスタックの識別子は `node::RemoveEnvironmentCleanupHook` → `Statement::~Statement()` →
`better_sqlite3.node`。固定NodeのObjectWrap destructorはcleanup hookを除去し、旧bindingのStatementは
このObjectWrapを継承している。GCでStatementが解放される際、Node Environment取得のassertionでabortする。
失敗時のpromptfoo内部DBには全62結果行が保存済みだった。小さい合成1件のCLI/DB/exportだけを
30回動かした切り分けは30/30 PASSで、単純な起動・migration・exportの失敗ではなかった。
生ログ・raw・内部DBは調査後に削除し、公開証跡は分類・件数・識別子だけとした。

| 指摘の仮説 | 固定版ソース・実測による確認 |
| --- | --- |
| Python provider worker / timeout | 0.117.2のpythonUtilsはケースごとにPythonShell子プロセスを起動し、常駐workerは使わない。Python executable検証は2500msだがprovider実行自体にそのtimeoutは掛からない。evaluator timeout既定は0（無制限）。今回の停止はnative SIGABRT。 |
| bridge spawnSyncの30秒 | 30000msのまま。採点のtimeout延長なし。今回のスタックはPython/bridgeではなく内部DB bindingのdestructor。 |
| 内部SQLite書込競合 | PROMPTFOO_CONFIG_DIRは呼び出しごとの0700領域、maxConcurrency=1、各runは別プロセスを直列で完走。SQLITE_BUSYではなくnative cleanup hook assertion。 |
| exit 0/100以外 | eval passは0、assertion/provider errorでpass rate低下なら100。CLI引数エラー等は1/2の経路もある。今回は通常exitではなくSIGABRT、code=null。runner側exit 1と区別する。 |
| --outputの書込タイミング | doEvalはwriteMultipleOutputsをawaitし、JSONはtoEvaluateSummary取得後にwriteFileSync。今回のrawなしは正常終了後の書込遅延ではなく、export前のabort。runnerもexitではなくcloseを待ちstdioを回収する。 |
| PostgreSQL schema作成/削除負荷 | 全62結果行保存後のNode/SQLite native stackで発生。PG失敗やprovider timeoutのスタックではない。本番経路・使い捨てschema処理は変更せず、修正後30 runを同じ経路で検証。 |

## 根本修正と診断

promptfoo **0.117.2** は維持し、promptfoo配下の `better-sqlite3` を **13.0.3** へ固定overrideした。
この版はN-APIを使い、破綻する旧ObjectWrapの直接継承を撤去している。
[upstream 13.0.0 release](https://github.com/WiseLibs/better-sqlite3/releases/tag/v13.0.0)と
[13.0.3 package](https://github.com/WiseLibs/better-sqlite3/blob/v13.0.3/package.json)を確認した。
他の既存依存の版は不変。旧binding用依存28件を削除し、node-addon-apiを1件追加した。

runnerは内部DB依存の版も検証し、reportのtoolchainに記録する。全runのexecutionsへ試行数、
終了コード、signal、spawn error分類、rawの有無、stdout/stderrの固定分類を記録する。
異常終了・gate拒否でも本文を含まないFAIL reportを出す。生stdout/stderrは0700領域の0600ファイルに
保存し、通常終了時に削除する。明示 `--keep-private-artifacts` の場合だけ保持可能。
再試行・timeout延長は実装していない（全run attempts=1）。

合成カードには「ユーザーの質問と同じ言語で答える」だけを追加した。日本語goldとの言語だけの
不一致を避けるため、実モデル評価前に固定したプロンプトであり、評価結果を見て期待値を調整したものではない。
入力ケース・gold・回答fixtureは変更していない。

## npm install script

固定Nodeに同梱のnpm **11.17.0**では、allow-scriptsは事後のadvisoryであり、未設定のscriptも実行する。
固定npmのarborist/rebuild.jsと[公式説明](https://docs.npmjs.com/cli/v11/commands/npm-approve-scripts/)を照合した。
第1ラウンドの「警告」だけをscript拒否の証拠にはできない。旧better-sqlite3はネイティブbindingを
取得/ビルドするinstall scriptを必要としていた。

修正版では `.npmrc` に `ignore-scripts=true` を固定し、**全install scriptを拒否**した状態で
要求された `npm ci --no-audit --no-fund` を実行（575 packages、PASS）。npm config get ignore-scriptsもtrue。
13.0.3はLinux x64用N-API bindingをパッケージへ同梱し、install lifecycleもgyp自動buildも持たない。
DBの実読書き・GC回帰・全評価はscript無効のfresh installで成功した。評価経路に必要なscriptはない。
残るPlaywright Chromium / esbuild / sharp / fseventsのscriptも実行していない。許可設定の追加は行っていない。

## 最終品質ゲート

固定環境: Node 24.19.0、npm 11.17.0、uv 0.8.22、Python 3.12.3、TMPDIR=/dev/shm。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

| 実行コマンド | 結果 | 件数・第1ラウンドからの増減 |
| --- | --- | --- |
| uv sync --locked | PASS | lock変更なし |
| uv lock --check | PASS | 固定Python依存 |
| uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS | 対象不変 |
| uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS | 112 files |
| uv run --no-sync mypy | PASS | 112 source files |
| uv run --no-sync pytest -m ut -q | PASS | 608、増減0（Python変更なし） |
| uv run --no-sync pytest -m it1 -q | PASS | 590、増減0（Python変更なし） |
| uv build --no-build-isolation | PASS | sdist / wheel |
| bash tools/test-postgres.sh | PASS | 410、増減0（保存/本番経路変更なし） |
| (cd evals/semantic && npm ci --no-audit --no-fund) | PASS | script全無効、575 packages |
| node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs | PASS | 58、+4（native GC、script無効、診断分類、私有ログ/stdio close） |
| node tools/check-docs.mjs | PASS | 最終証跡をstage後にも実行 |
| git diff --check / git diff --cached --check | PASS | whitespace |

Native GC回帰は4000個のSQL Statementを作り、20回のGCとDB close後のGCでabortしないことを確認する。
REDの観測は修正前の実fixture SIGABRT。新規回帰試験は修正版で実行した。
製品試験・required Node試験のskipは0。既存Starlette/Pydanticとnpm deprecatedの警告は出た。

## 3 run × 10回連続（30 run）

各回は `bash tools/evaluate-semantic-answer.sh --runs 3 --output <report>`。
Python subprocessで01〜10のreportへ直列実行し、失敗したら停止する（再試行なし）。
さらに全reportを再読込し、各呼び出し3 run、各run62件、26分類100%、必須ゲート全合格、
終了コード0、rawあり、signalなし、attempts=1、同一manifest・revision・dirty=falseを照合した。

| 呼び出し | run 1 / 2 / 3 | ケース | 各run全分類 / 必須ゲート | 秒（PG起動/削除含む） |
| --- | --- | --- | --- | --- |
| 1 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 104.346 |
| 2 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.374 |
| 3 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.008 |
| 4 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.651 |
| 5 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.355 |
| 6 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.289 |
| 7 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.257 |
| 8 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 102.009 |
| 9 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 101.394 |
| 10 | PASS / PASS / PASS | 186/186 | 100% / 違反0 | 100.631 |

合計 **30/30 run PASS、1860/1860ケース**。正常呼び出しの実測合計1016.314秒。
fixture / classifier=synthetic / quality_evidence=false / cacheなし。モデル品質の証拠ではない。
各reportのSHA-256、各run全分類のrate、各終了コード・試行数・commitは機械可読証跡へ保存した。
元の本文を含まないreportは `/dev/shm/114-r2-validation-a8fmfbbf` に保持。
runner内部のraw・生ログ・DBは削除済み。

## 不正fixtureと未実施

`--runs 1 --fixture-variant forbidden|missing|error` をそれぞれ実行した。
全て上記実装revision、dirty=false。各62件、promptfoo exit 100、rawあり、attempts=1。

| variant | 実際の結果 | 拒否理由 |
| --- | --- | --- |
| forbidden | FAIL（期待どおり、runner exit 1） | 禁止事実の必須ゲート違反 |
| missing | FAIL（期待どおり、runner exit 1） | synonym 8/10、必須ゲート違反0、分類90%未達 |
| error | FAIL（期待どおり、runner exit 1） | report-gate / row-error。62行のうちprovider error 1件。no_memory成功へ変換しない |

errorだけは明示keep flagで私有rawの行数/error数を照合した後、生ログ・raw・DBを削除した。
この3種は実評価FAILであり、正常fixture PASSと混同しない。

- 今回の指摘範囲に未解決事項なし。
- 実モデル、実環境IT2/ST、GitHub CI: **NOT RUN**。実モデル受入はIssue #115で確認する。
- push / PR作成 / merge: **NOT RUN**（現在の依頼で禁止）。
