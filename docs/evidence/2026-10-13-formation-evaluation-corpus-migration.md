# Issue #206: 形成評価ケース移行・形式検証の証跡

日付: 2026-10-13（JST、検証開始〜終了は2026-10-12 UTC）

対象: [Issue #206](https://github.com/FYuki/digital-souls-core/issues/206)、
[Proposed ADR 0033](../adr/0033-memory-formation-evaluation.md)、
[データと全件対応表](../../evals/formation/README.md)。

## revision と範囲

- repository: FYuki/digital-souls-core
- branch: `docs/206-formation-eval-cases`
- base: `b062a49a32d261d0ce472938c40711c6cb41d7c0`（epic/memory-formation）
- 対象変更: 本証跡と同じ commit のデータ、schema、strict loader/validator、形式 UT、ADR/索引。
- PoC source: `fce7382884d981c42be7fbd3ddaffe7469e27588`。
  原本 bytes の SHA-256 はデータ README と migration.json に固定し、UT の literal でも照合。
- 元404件、移植217件、保留187件。本文は元と一致。入力と gold を分離。
  全移植 id、保留 id と理由、原本 gold、生成 schema を評価・prompt 調整前に固定した。
- source/*.jsonl は合成原本の監査・保留資料。gold を含むため推論入力にしない。
- 実装したのは形式検証だけ。採点・集計・ハーネス・prompt・形成処理・実モデル実行は追加していない。
  SPEC の状態欄を変更せず、ADR は Proposed。

## 環境とコマンド

Python 3.12.3、uv 0.8.22、Node v24.19.0。次の固定ツールを使用した。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH="$T/bin:$T/node/bin:$PATH" TMPDIR=/dev/shm
uv sync --locked && uv lock --check
uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q
uv run --no-sync pytest -m it1 -q
uv build --no-build-isolation
bash tools/test-postgres.sh
(cd evals/semantic && npm ci --no-audit --no-fund) && node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs
node tools/check-docs.mjs
git diff --check
git diff --cached --check
```

ruff の対象は API CI と一致。PostgreSQL は指定 script の使い捨て合成 fixture だけである。
推論 adapter の試験も fake transport を使い、実 llama.cpp/GPU/実モデルへ接続していない。

## 結果

| ゲート | 結果 | 件数・補足 |
| --- | --- | --- |
| sync --locked / lock --check | PASS | lock 変更なし、80 packages resolved |
| ruff check | PASS | API CI と同じ一覧 |
| ruff format --check | PASS | 165 files |
| mypy | PASS | 165 source files |
| UT | PASS | 1289 passed、1448 deselected、SKIP/xfail 0 |
| うち追加の形成形式 UT | PASS | 45件。217件の本文/gold/演算子対応、404件のid分割、原本bytes hash、ケース集合版を照合 |
| IT1 | PASS | 1002 passed、1735 deselected、162.74秒、SKIP/xfail 0 |
| build | PASS | sdist と wheel の2成果物 |
| PostgreSQL 合成契約 | PASS | 446 passed、2291 deselected、175.82秒、SKIP/xfail 0 |
| npm ci | PASS | lock に従う575 packages |
| Node 必須テスト | PASS | 75 registered tests、skip/TODO/cancel 0 |
| 文書検査 | PASS | 新規ファイルを stage 後、追跡 Markdown 参照・text/JSON |
| diff --check（作業コピー/ステージ） | PASS | 空白エラー0 |
| 採点/ハーネス/実モデル品質・IT2/ST | NOT RUN | この Issue の範囲外。#212/#213 と後続実環境受入 |

TDD: loader 未実装時に新 UT が ModuleNotFoundError で FAIL（collection error 1件）となることを確認し、
実装後は45件 PASS。整形前の ruff E501 9件 FAIL は修正し、最終品質ゲートは上表のとおり。
既存依存の BlockingPortal deprecation と Pydantic TypedDict ReadOnly 警告は出たが、テスト失敗ではない。
marker による deselected は他レベルの試験であり、SKIP や品質 PASS と混同しない。

## 制約と引き継ぎ

ADR の Q1〜Q8 はユーザー確認待ち。保留ケースを捨てる承認、閾値/採点方式の変更の承認ではない。
Fact 照合/訂正、privacy60件の形成時の分担、別 Binding fixture、境界の残る3件、相対日時・年不明月、
否定/予定/条件・primary 所有範囲は移植待ち。原本と gold を保持した。
actor の Who 投影、ground/actor の入力経路、種別・創作文脈投影も仮置きであり、品質評価を確定していない。

push・PR 作成・merge・Issue コメントは実施していない。
