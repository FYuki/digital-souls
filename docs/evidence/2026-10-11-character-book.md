# Character Book 実装の合成検証（2026-10-11）

対象: [ADR 0028](../adr/0028-character-book.md)、[ADR 0026](../adr/0026-prompt-assembly.md)。
正本の PoC 文書は公開 revision `fce7382884d981c42be7fbd3ddaffe7469e27588` の
[Character Book runtime 決定](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-book-runtime-2026-08.md)
全文を読み取った。

作業 branch: `feature/153-character-book`。起点: `c4f142b549fae0352592376da4700e6d355e064b`。
本証跡を含む commit が実装 revision。push・PR・merge は行っていない。
既存のコンテナ・別の worktree は変更していない。

## 環境と境界

Python 3.12.3、uv 0.8.22、Node 24.19.0。各コマンドに次を設定した。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
```

依存はローカルキャッシュから取得。npm も `--offline` を付けた。
外部通信は許可された PoC 決定文書の公開 raw URL の読み取りだけ。
PostgreSQL 18 の固定 digest イメージはローカルに存在することを確認してから、既存の
`tools/test-postgres.sh` で network none・公開 port なしの使い捨てコンテナを実行した。
試験の終了時に、この呼び出しが作ったコンテナだけを削除する。

## 実装と TDD

- Book/Entry を型付きモデルへ置換。既知 field の欠落・不正は安全なカード内 path を持つ
  `CharacterCardValidationError` でカード全体を拒否。未知 field と extensions は保持する。
- selector は履歴削減前の共通 `split_messages` 結果を使用。user/assistant message ごとの深さ、
  NFKC・条件付き casefold・literal substring、activation の優先順、source index による dedupe を実装。
  `recursive_scanning=true` でも初期 literal/constant matching は行い、選んだ本文からの再帰はしない。
  regex と行頭 Decorator の Entry は保持して非選択にする。
- Book は Entry ごとの見出し付き system message を profile の同じ counter で計測。
  estimate でも上限を適用し、本文を切らず removal_key の小さい Entry を除外して再計測する。
  最終配置は before/Card/after、各 position は insertion_order/source_index 順。
  PromptBuilder の Lore 領域・全体予算にも同じ除外順を適用した。
- selector の診断と PromptUsage は本文を含めない。Prepared の payload も repr から隠す。
  Prepared usage の dropped.lore は Book と PromptBuilder の削減件数を合算する。
- 新しい合成 UT を先に作り、未実装 module による collection FAIL を確認した。
  追加の振る舞いテストでも、Lore 領域削減を total budget と誤分類する FAIL と、Prepared repr に
  入力本文が出る FAIL を確認後に実装修正し、PASS を確認した。
- 初回の全 IT1 は 17 FAIL / 794 PASS。既存の人格評価用合成 Card に必須の Book/Entry extensions と
  use_regex が欠落していたため、fixture を正規のモデルへ補正。評価内容や判定は変更していない。
  修正後の該当21試験と、全 IT1 の再実行が PASS。

## fixture とテスト境界

[合成 UT](../../core/tests/test_character_book.py)、[両 HTTP API の IT1](../../core/tests/test_character_book_api.py)、
[PromptBuilder UT](../../core/tests/test_prompt_builder.py) で次を確認した。

| 境界 | 確認内容 |
| --- | --- |
| Book/Entry loader | Bookなし・空Book、必須 field の欠落、disabled Entry の不正でも全体拒否、既知型不正と path、整数 field のbool/小数拒否、非負制約、optional null、未知 field/extensions の保持 |
| literal/activation | hit/miss、NFKC、case sensitive/insensitive、空key、message境界・改行、無効・空本文・Decorator・regex・constant・primary・selective の判定順、secondary key欠落/空/空文字、複数key/同idのsource index identity |
| scan | depth 0/1/複数/履歴より大きい値、assistantとuser個別カウント、null bodyのassistant tool callも1 message、system/tool本文は非対象、Card/記憶/呼び出し側指示/post_historyも非対象、再帰なし |
| budget/配置 | 0・一致・1未満・超過・複数除外、priority/insertion_order/source_index tie-break、async counterの独立system計測と再計測、before/after/省略、見出し・strip・原文非変更、0件ならBookなしと同じmessages |
| PromptBuilder | Lore/記憶/履歴/post_history の組み合わせ、8段階の全体削減順、Lore領域と全体予算の理由区別、必須領域保護、source indexでの配置tie-break |
| 共通runtime/診断 | Chat APIと会話履歴 APIの同じEntry選択、保存済みsanitized snapshot、会話隔離、古い履歴削減より前のscan、本文・keys・name/id/comment・extensionsがrepr/診断に出ない、無出力 |

PoC の WebSocket 境界は、Core の現在の2つの HTTP API と共通 Inference 経路に読み替えた。
外部サービス・実 LLM を合成試験の根拠にしていない。

## Miori の起点回帰

カード自体は変更していない。起点 `c4f142b` の loader/Lore.matches で期待値を求め、
[固定 fixture](../../core/tests/fixtures/miori-character-book-c4f142b.json) に質問・source index・推定costを保存した。
最初の9問は実装変更前に採取し、複数一致で予算内になる2問は `git show c4f142b:src/digital_souls_core/character.py`
で取り出した同じ起点実装を隔離した Python module として実行して採取した。
旧選択に Book 上限はなかった。cost は旧選択集合を新しい規定の Lore message に render した
ByteTokenCounter の値で、旧 prompt 全体の cost や実 tokenizer の値ではない。

| 代表ケース | 旧 source indices | 新 source indices | Book の推定 token |
| --- | --- | --- | --- |
| 単独一致7問（各Entry） | 0 / 1 / 2 / 3 / 4 / 5 / 6 | 同じ | 各383 / 420 / 402 / 399 / 264 / 288 / 372 |
| 非一致1問 | 空 | 空 | 0 |
| 予算内の複数一致2問 | [4,5] / [2,4] | 同じ並び | 536 / 650 |
| 全7件同時一致1問 | [0,1,2,3,4,5,6] | [0,4] | 2,432 → 631 |

合計11問。予算内10問は Entry と並びが同じで、各costも固定期待値に一致した。
全一致の質問は estimate が700を超えるため、除外順は source index **6→5→1→2→3**。
残る0と4の631 tokenは上限内で、他の5件は `omitted_by_lore_budget`。
これは ADR 0028 が採用した estimate での Book 上限による意図した変更。
見出し追加により最終 system の文字列は旧形式と変わる。応答品質・実token量は評価していない。

## 必須ゲート

以下は最終の製品コードとテストへの実行結果。すべて実 LLM・GPU なし。
`ruff` の対象は指定された src/tests と評価用3ファイルを含む。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS（ローカルcache、lock不変更） |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS（135 files） |
| `uv run --no-sync mypy` | PASS（135 source files） |
| `uv run --no-sync pytest -m ut -q` | PASS（954 passed、1253 deselected、skipなし） |
| `uv run --no-sync pytest -m it1 -q` | PASS（812 passed、1395 deselected、skipなし） |
| `uv build --no-build-isolation` | PASS（sdist / wheel） |
| `bash tools/test-postgres.sh` | PASS（441 passed、1766 deselected、skipなし。使い捨てコンテナ削除済み） |
| `(cd evals/semantic && npm ci --offline --no-audit --no-fund)` | PASS（575 packages。指定のnpm ciに通信禁止用offline flag追加） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS（59 registered required tests、skip/TODO/cancelなし） |
| `node tools/check-docs.mjs` | PASS |
| `git diff --check` | PASS |

既存依存の deprecation / ReadOnly 警告は出るが、skip/xfail ではない。
初回 PostgreSQL は441 passed、1753 deselectedでPASS。型表現のfixture移行後にも全試験を再実行した。

## NOT RUN / SKIP

- NOT RUN: 実 LLM の回答・実 llama.cpp tokenizer・GPU・IT2・ST、実運用履歴/DB。
- NOT RUN: CI、push、PR、merge（今回の作業権限外）。
- SKIP: 必須試験のskipなし。markerで別suiteがdeselectedになった件数は上表に明示。
- regex・再帰scan・Decorator実行・standalone Lorebook は決定済みの非対応機能。

## 監督側の再実行（2026-10-11）

`8cd25adddcfd50accb79e488ecf7103d11f597e0` に epic（`61924a66fb7c65ce11ce043b50667d3bc5c7cc4a`、人格評価の修正を含む）を衝突なく取り込んだうえで、差分を読み、同じ固定ツールで再実行した。
uv sync --locked・uv lock --check・ruff（新しい CLI を含む）・mypy（135 files）・UT（949 passed）・IT1（813 passed）・uv build・
test-postgres（441 passed）・npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。人格評価の標準 CLI（fixture、3回）も exit 0。
実 tokenizer での Book の予算、実モデルの評価は NOT RUN。
