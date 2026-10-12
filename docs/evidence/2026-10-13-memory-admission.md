# 型付き保存判定の合成検証（2026-10-13）

対象: [Issue #207](https://github.com/FYuki/digital-souls-core/issues/207)。
作業branchは `feature/207-admission-evaluator`、起点は
`b062a49a32d261d0ce472938c40711c6cb41d7c0`（`epic/memory-formation`）。
本証跡と同じcommitの実装・UT・文書差分をローカルで検証しました。以下のSHA-256で
試験対象の新規ファイルを固定します。実施日は日本時間、journalの日時はUTCです。

| ファイル | SHA-256 |
| --- | --- |
| `src/digital_souls_core/memory_admission.py` | `4fc5c92fe46ebe106746db7f81c1863e2111e0255af27185468e9492d85b4c9d` |
| `tests/test_memory_admission.py` | `a841ae59a4af495241dee388030c5199402404a3c45ed1ac5be6dd50e3732601` |

## 対象と制約

`evaluate` は同期の純粋関数、`admit` はscanner→分類器→evaluateの段です。
UTは合成データとfake分類器・fake Providerだけを使用しました。scanner・分類器・
PrivacyPolicy・既存テスト・ADR・schemaは変更していません。
絶対禁止の対応表と検出の穴、仮置きの写像は[記憶ガイド](../memory.md#型付きの保存判定)にあります。
形成からの呼出しは未接続です。ALLOW_STRUCTUREDの結果だけでpermission・根拠・
Binding・schema・現在sourceの検証が完了したことにはなりません。

先にUTを追加し、モジュール未実装のimportエラーで初回FAILを確認してから実装しました。
途中のfailure列挙ではPythonのtype aliasからLiteralの引数を取り出せず1件がSKIPとなりました。
aliasの値から全7種を列挙するよう修正しました。途中のFAIL・SKIPはPASSと扱わず、
最終実行は追加UT95件、SKIP・xfailなしです。

## 環境と実行

Python 3.12.3、uv 0.8.22、Node 24.19.0。指定したtoolchainを使いました。

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
```

ruffの対象は `.github/workflows/api.yml` と同じです。PostgreSQLは既存scriptが起動・削除する
ネットワークなし・公開portなしの使い捨て合成DBだけです。実モデル・GPU・llama.cppは使用していません。

## 結果

| ゲート | 結果 | 件数・補足 |
| --- | --- | --- |
| uv sync --locked / uv lock --check | PASS | lock 80 package、78 packageをaudit |
| ruff check | PASS | api.ymlと同じ対象、違反0 |
| ruff format --check | PASS | 165ファイル |
| mypy | PASS | 165ファイル |
| 追加の保存判定UT | PASS | 95件 |
| pytest -m ut | PASS | 1339件、1448 deselected |
| pytest -m it1 | PASS | 1002件、1785 deselected |
| uv build --no-build-isolation | PASS | sdist・wheelの2成果物 |
| tools/test-postgres.sh | PASS | 446件、2341 deselected |
| npm ci / Node必須試験 | PASS | 575 packageを取得、75件 |
| check-docs.mjs | PASS | 追跡対象text・ローカル参照を検証 |
| git diff --check | PASS | 空白・差分検査、違反0 |
| remoteの必須CI | NOT RUN | push・PRを行わず、監督側で確認 |
| 保存判定経路の実モデル / IT2 / ST | NOT RUN | 今回はfakeと合成DBのみ |

最終Python・Node試験のSKIP・xfail・TODO・cancelledは0件です。pytestのdeselectedは
他markerの試験を意図的に選択しなかった件数で、SKIPではありません。
既存依存由来の警告はUT1件、IT1/PostgreSQL各3件あり、失敗ではありません。
npmの固定依存にはdeprecated通知があり、今回依存版を変更していません。
CI内の独立環境へのwheel install・Compose検査・62ケース×3回の回答fixture gateは
この依頼のコマンド一覧に含まれず、今回追加実行していません（NOT RUN）。
ローカルゲートの成功をremote CI全体のPASSとして扱いません。

## 要確認・未対応

- source不明・履歴未保存→ABSTAIN_UNKNOWN、scannerの拒否語→DENY_USER_REQUEST、
  明示価値flag→NOT_MEMORY_WORTHYの写像は監督指針に沿う仮置きです。
- policyは現在のprivacy版のみ対応し、未知版と分類器由来欠落をABSTAIN_UNKNOWNとしました。
  許可型集合の変更を独立の保存policy版で管理する選択肢があり、後続接続時に分離の要否を要確認です。
- source・価値flagは必須引数で、未知はNoneです。既存RecordKindを再利用しています。
  形成側で許可する実際の型集合と価値flagの生成契約は後続Issueで確認します。
- ADR 0019 §7の「現在userの拒否語」とassistantの引用の区別は、話者を持たない評価器では行いません。
  形成側がscannerへ渡す本文とsource保留の対応を要確認です。
- 無印秘密値・未知ID/連絡先・無印住所/自然文位置にはscannerの検出の穴があります。
  health・服薬・生体情報・第三者非公開情報の検出は意味分類に依存し、実品質は保証しません。
  現行promptは服薬・生体情報・住所/位置・私用連絡先を明示列挙していません。
- 形成接続・permissionとawait後の失効照合は#209、抽出・job・保存・APIは範囲外です。
- 保存判定経路の実モデル品質・IT2/ST、remoteの必須CIはNOT RUNです。
  push・PR作成・merge・Issueコメントは監督が行うため実施していません。
