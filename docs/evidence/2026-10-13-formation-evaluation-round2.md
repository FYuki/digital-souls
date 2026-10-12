# Issue #206 第2ラウンド: seed・元発言日時と移植範囲の再評価

日付: 2026-10-13（JST）

対象: [Issue #206](https://github.com/FYuki/digital-souls-core/issues/206)、
[ADR 0033](../adr/0033-memory-formation-evaluation.md)（Proposed）、
[全件/分類/保留理由と入力写像](../../evals/formation/README.md)。
第1ラウンドの結果は[前回証跡](2026-10-13-formation-evaluation-corpus-migration.md)に保持する。

## revision と範囲

- 作業 branch: docs/206-formation-eval-cases、指定された同名 worktree 内で作業。
- 元第1ラウンド: 6935e33。git fetch origin 後、origin/epic/memory-formation
  `14d15769cc1fde1c1dea5440127043c905199e5d` へ rebase。
  競合0件、rebase 後の第1ラウンド commit は `fcff2c602d2b29f6afa722f0d28b83a40f795f32`。
- 第2ラウンドの対象差分は本証跡と同じ commit。SHA は commit 後の最終報告で通知する。
- PoC の元 commit は `fce7382884d981c42be7fbd3ddaffe7469e27588`。
  provider.py と Semantic fixtures.py をその commit の git show で確認した。
- 元404件、移植287件（+70）、保留117件（-70）。原本 bytes は未変更。
  原本文、元 gold hash、seed の known 全値、固定 provider 時刻と出典を UT で照合。
  第1ラウンドの移植217件の gold オブジェクトも git show と比較し、すべて完全一致。
  PoC の merge pair 正確に1件/no_merges=0件を merge_count に固定し、その形式修正後も UT/IT1・型検査・build を再実行した。
- 入力 seed は既存正本の投影。Binding は Core の scope(subject/client/audience)+character_id。
  元にない scope とランダム会話/記録 ID の命名は明記した合成値。期待 target/operation は gold 側だけ。
  sources=[] の既存 catalog へ過去引用・保存本文を推測で足さない。
- quality の元固定 2026-09-13 UTC、Semantic fixture の 2026-09-15 UTC と pipeline の Asia/Tokyo を保持。
  相対日時は元発言基準の PartialDateTime。年不明の月は保留。
- dataset/分類ごとの元件数・移植・保留・必要正解数、全 id と理由は README に固定。
  追加は quality52、Semantic15（reaffirm10・foreign-character安全5）、boundary3件。
- 別スレッド/別利用者の Fact 照合範囲ケースは今回の原本に0件。
  2026-10-13 のユーザー決定を旧 Binding 完全一致で上書きせず、#214 の ADR 承認待ちと記録。
- context7件の enum 表現は保留でも、否定/肯定・実施/予定を一致根拠にしないことは #210 の要件。
  predicate/object だけで照合する実装は欠落となる。same_event の不一致条件を移植した。
- Q1 は残る117件だけへ改訂。Q7 の入力説明を改訂し、Q9（seed の正本接続・操作/関係の観測）を追加。
  Q2〜Q6・Q8 の提案は保持。閾値/ゲートの緩和、ADR の Accepted 化、SPEC 状態の変更はなし。
- 採点・ハーネス・prompt・形成処理・製品公開 API は追加していない。

## 品質ゲート

固定 toolchain: Python 3.12.3、uv 0.8.22、Node v24.19.0。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH="$T/bin:$T/node/bin:$PATH" TMPDIR=/dev/shm
uv sync --locked && uv lock --check
R=$(sed -n 's/.*uv run --no-sync ruff check \(.*\)$/\1/p' .github/workflows/api.yml | head -1)
uv run --no-sync ruff check $R && uv run --no-sync ruff format --check $R
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q && uv run --no-sync pytest -m it1 -q
uv build --no-build-isolation
bash tools/test-postgres.sh
(cd evals/semantic && npm ci --no-audit --no-fund) && node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs
node tools/check-docs.mjs
git diff --check
```

| ゲート | 結果 | 件数・補足 |
| --- | --- | --- |
| uv sync --locked / lock --check | PASS | 80 packages resolved、78 audited、lock 変更なし |
| ruff check / format --check | PASS | API CI の対象一覧、167 files formatted |
| mypy | PASS | 167 source files |
| UT | PASS | 1411 passed、1448 deselected、SKIP/xfail 0 |
| うち形成形式 UT | PASS | 72 passed、287件の入力/gold、404件のid分割、元本文・known値・時刻出典、Binding/版/状態/gold混入の拒否 |
| IT1 | PASS | 1002 passed、1857 deselected、SKIP/xfail 0 |
| build | PASS | sdist・wheel の2成果物 |
| PostgreSQL 合成契約 | PASS | 446 passed、2413 deselected、SKIP/xfail 0 |
| npm ci | PASS | 575 packages、lock 変更なし |
| Node 必須テスト | PASS | 75 registered tests、skip/TODO/cancel 0 |
| 文書検査 | PASS | 本証跡を stage 後、追跡 Markdown 参照・text/JSON を検証 |
| diff --check | PASS | 作業コピー・stage とも空白エラー0 |
| 採点/ハーネス/実モデル品質・IT2/ST | NOT RUN | 範囲外、実モデル・GPU・外部 inference は使っていない |

UT の初回は新形式に対する旧入力の拒否で49 FAIL/3 PASS、データ写像後の版 hash 固定未更新で
5 FAIL/47 PASS。その後、版 hash の更新・追加回帰を含む最終72件で PASS。
整形前の ruff E501 は修正した。過程の FAIL を最終 PASS へ読み替えず、修正後の結果を区別する。
既存依存の deprecation 警告はテストの失敗ではない。deselected は別レベルの試験であり SKIP ではない。

push・PR 作成・Issue コメント・merge は実施していない。
