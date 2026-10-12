# Issue #208: 形成job台帳と有限drainの合成検証

日付: 2026-10-13（JST。実行は2026-10-12 UTC）
対象: [Issue #208](https://github.com/FYuki/digital-souls-core/issues/208)、Epic #127。
branch: `feature/208-formation-job-ledger`、base: `b062a49a32d261d0ce472938c40711c6cb41d7c0`。
本証跡を除く検証対象treeは、末尾の記録を参照してください。実装・文書・試験用資源だけを変更し、
push・PR・merge・Issueコメント、実モデル・GPU・運用DB操作は実施していません。

## 実装と境界

- schema版8の `formation_jobs` はBinding＋会話ごとに1行。本文・prompt・LLM出力を保持しない。
- 形成を明示有効化し、memory permissionがある履歴保存・revision更新と同transactionで予約する。
  pendingを集約し、leased中の更新は成功/失敗/中断後の回復で新版へ再予約する。
- 有限のPython drainと、注入時だけ存在するlocalhost HTTP drainを提供する。
  Host/Origin境界、不正入力・上限・store/policy不一致を検証する。
- SKIP LOCKED・lease owner token・session advisory lockで並行実行を制御する。
  processor await中にBinding lock/transactionを持たず、会話保存は形成を待たない。
- sourceのprivate・存在・指定除外・保存拒否を再確認する。会話削除は台帳もCASCADEで削除する。
- 冪等な合成processorが既存の正本portへ書き込んだ直後に中断し、再起動・lease回収後の再実行でも
  正本1件・登録結果1件を保つことを確認する。実抽出・保存判定・正本形成は#209以降。
- `FormationInput.origin_*` は予約起点だけを表す。processorは自分の認可portで読取範囲を決め、
  同一会話・同一Bindingを照合範囲の前提にしない。#77・#214の認可拡張は実装しない。
- `MemoryService.extract` はADR 0022で撤去済みのまま維持し、管理・試験用extractを復活させない。
  ADR 0017・0027の本文/Statusは変更しない。SPEC §2.3の該当1行だけを一部実装へ更新する。

APIの具体形・1行台帳・予約障害時の履歴rollback・上限後failed保持・private時のdrain除外・
運用値は仮置きです。選択肢・推奨理由は[記憶API](../memory.md)と[履歴API](../history-api.md)に記録しています。

## 環境とコマンド

Python 3.12.3 / uv 0.8.22 / Node 24.19.0、固定lockfileを使用しました。
`PATH` は `history-stage1-tools/bin`・同 `node/bin` を優先し、`TMPDIR=/dev/shm`、
`LITELLM_LOCAL_MODEL_COST_MAP=True` です。
PostgreSQLは `tools/test-postgres.sh` の公式18-bookworm digest固定・CPU 1・メモリ上限1 GiB・
ネットワークなし・公開portなしの使い捨てコンテナを使い、Unix socketで接続しました。
合成データだけを使い、終了時にコンテナとsocketを削除しました。

ruff対象はAPI CIと同じ `src tests` と7つの評価tool/providerです。

```sh
uv sync --locked && uv lock --check
uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py tools/evaluate-privacy-classifier.py
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q
uv run --no-sync pytest -m it1 -q
uv build --no-build-isolation
bash tools/test-postgres.sh
(cd evals/semantic && npm ci --no-audit --no-fund)
node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs
node tools/check-docs.mjs
git diff --check
```

## 最終結果

| gate | 結果 | 件数・範囲 |
| --- | --- | --- |
| locked sync / lock check | PASS | lockの80 package解決、78 installed package監査。lock変更なし |
| ruff check / format check | PASS | API CIと同じ対象、format 171 files |
| mypy | PASS | 171 source files |
| UT | PASS | 1,270件、skip/xfail/xpass 0 |
| IT1 | PASS | 1,011件、skip/xfail/xpass 0 |
| build | PASS | sdistとwheelの2 artifact |
| wheel独立install/import | PASS | hash付きlocked runtime 63 package、生成wheel再install。create_appとorigin契約を確認 |
| PostgreSQL | PASS | 482件、skip/xfail/xpass 0、165.36秒。形成/移行の新規36件を含む |
| npm ci | PASS | 固定lock通り575 package追加 |
| Node required reporter | PASS | 75件、skip/todo/cancel 0 |
| docs / diff | PASS | 最終の追跡文書・設定と空白差分 |
| shell構文 / Compose構文 | PASS | CIと同じ5 scriptとPostgreSQL Compose、実サービス起動なし |
| 回答fixture評価 | PASS | 62件×cacheなし3回。実モデル品質の証拠ではない |
| remote必須CI | NOT RUN | push禁止のため監督側で実行・確認する |
| 実運用DB版8適用・IT2/ST・実モデル形成 | NOT RUN | 今回の範囲外 |

## 途中のFAILと原因

1. 初回全PostgreSQLは479 PASS・1 FAIL。旧版5移行試験が追加表なしを期待していた。
   版8の空の `formation_jobs` だけを期待集合へ加え、旧行の全列一致と空台帳を検証する形へ更新した。
2. 512 MiBでの次回は474 PASS・6 FAIL（既存意味検索評価のstorage_unavailable）。
   当該実行のコンテナ状態は未採取のため、直接の原因は確定しない。単独の診断付き意味検索8件はPASS。
3. その後の全試験は381 PASS・101 ERROR。診断付き全試験でも同じ件数を再現した。
   Dockerは `OOMKilled=true`。PostgreSQL logでautovacuumのpg_attribute処理と復旧プロセスが
   signal 9で停止し、server shutdownとUnix socket消失を確認した。
   試験用コンテナのメモリ上限だけを512 MiB→1 GiBへ増やし、通常コマンドで482件PASSを確認した。
   テストの削除・skip・閾値変更・期待値の緩和はしていない。製品DB設定は変更していない。

意図的なTDDのREDとして、未実装moduleのcollection errorと、旧版の試行上限を新版へ引き継いで
中断後の更新jobが回復しないFAILを確認し、実装と回帰試験で修正しました。
正本書き込みと台帳完了の間の中断はat-least-once実行となるため、後続processorにも冪等性・
キャンセル遵守・送信前/登録直前のprivacy/source再検証が必要です。

検証対象tree（本証跡追加前）: `54862def2f2f2a994b9c3d47a15eb30136df3458`。
