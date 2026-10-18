# 人格一貫性ハーネスの合成検証（2026-10-11）

対象は [ADR 0029](../adr/0029-persona-consistency-evaluation.md) の評価ハーネスです。
現行の結果は末尾の「人格評価とインジェクション評価の分離」を参照してください。前の各節は当時の履歴です。
指定 worktree `feature-154-persona-harness` / branch `feature/154-persona-harness`、
起点 `5d60c5d`（Card hash と PromptBuilder 統合済み）で実施しました。
実装・規約以外の worktree / branch、main、既存コンテナを変更していません。push / PR / merge は行いません。

## 修正前（起点5d60c5dからaa282c8まで）の条件と TDD

Python 3.12.3、uv 0.8.22、Node 24.19.0、固定lockを使用しました。

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export npm_config_offline=true
export LITELLM_LOCAL_MODEL_COST_MAP=True
```

依存はローカルcacheから取得し、外部通信・実LLM・GPUは使いません。
固定digestのPostgreSQL imageがローカルにあることを確認してから、`--network none`、公開portなしの
専用使い捨てコンテナで試験しました。実モデル用設定はexampleのみです。
合成Card / ケース / 応答fixture以外の会話や認証情報を含めません。

TDD の最初の `pytest tests/test_persona_evaluation.py -m ut -q` と
`pytest tests/test_persona_runtime.py -m it1 -q` は、未実装モジュールの import error で **FAIL** でした。
strict schema の追加回帰も先に実行し、`schema_version=true/1.0` と空白だけのCard注記を拒否できず
**3 FAIL** を観測してから実装しました。最終結果は下の表に記録します。
実行中にgoldを書き換える回帰も先に **1 FAIL** を確認し、開始時・終了時のケース集合hash照合を実装しました。
途中の mypy では gate/hash の Optional sort と実行commit型の不一致を検出し、修正しています。

## 修正前の実装と測定の解釈

[手順と形式](../../core/evals/persona/README.md) に strict schema、正規化、各回90%境界、
非相殺ゲート、Card hashの入口内・入口間照合、warn/gate、judgeと入口のinterfaceを記載しました。
Mioriの案は自己紹介2 / 好み2 / Lore3 / 上書き耐性2（計9）。Card fieldの参照解決もUTで確認しています。

**現行の会話履歴APIは system を受け付けません。** 400 `invalid_turn` の本番経路を通し、
履歴overrideは各回 `NOT RUN / caller_system_unsupported` と記録します。
systemをfake保存履歴へ入れる迂回は行いません。全2入口×4分類の標準fixture評価は
Chat全件と履歴3分類が合格、履歴overrideが各回0/1で **FAIL** です。
必須ゲート `execution_incomplete=3`、`card_hash_missing=3`。標準fixtureの
`persona_break=0`、`override_followed=0`、`card_hash_mismatch=0`。
未実行を除いた応答間の合否不一致は0件です。全体FAILを試験ツールの故障や実モデル品質のFAILとは混同しません。

各場面の期待されるFAIL、90%でwarnならPASS / gateならFAIL、judgeが合否に影響しないことは合成UT/IT1で検証します。
2入口の本番経路から全ケースの応答を得てPASSする受入は未達です。
実 judge は `NOT RUN`、fake Egogram は各入口のCP/NP/A/FC/ACを `fixture: unestimated` と報告します。
活動入口は登録interfaceのみで `NOT RUN（未実装の入口）`。実モデル・IT2/STは **NOT RUN** です。

## 修正前のコマンドと結果

検証時は起点 `5d60c5d74273df016a8fbe1c9ccf1821170843c8` 上の作業差分（dirty=true）です。
ケース版は `9c195afc39e47e5b63c97edb6086017767da245881ebbbdd6d2207b5d5f346dc`。
本文は既定の報告に含めません。次の指定ゲートはすべて実行しました。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS（offline cache） |
| `uv lock --check` | PASS |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS、132 files |
| `uv run --no-sync mypy` | PASS、132 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、862 tests |
| `uv run --no-sync pytest -m it1 -q` | PASS、789 tests |
| `uv build --no-build-isolation` | PASS、sdist / wheel |
| `bash tools/test-postgres.sh` | PASS、441 tests（追加schema回帰の前に実行。変更したpersona moduleはPG経路で使わない） |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages（offline cache、固定lock、install scripts無効） |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered tests、skip / TODO / cancelなし |
| `node tools/check-docs.mjs` | PASS（新規文書をstageして検証） |
| `git diff --check` | PASS |
| `git diff --cached --check` | PASS |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-b2-report.json` | **FAIL / exit 1**、履歴system非対応3件。本文なしJSON生成 |

pytestの必須試験にSKIP / xfail / xpassはありません。既存のStarletteのdeprecated aliasとPydantic
ReadOnly qualifierに関するwarningはありますが、試験は失敗していません。npmの既存固定依存の
deprecated warningは保持し、依存変更で隠していません。

NOT RUN：実モデル、実judge、活動入口、履歴override（各回1件）、実環境IT2 / ST。
実CIは外部通信禁止のためNOT RUNで、ローカル試験のPASSと区別します。

ruff 対象 `src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py`
を CONTRIBUTING と API CI で合わせ、mypy の対象にも新CLIを追加しました。製品依存・lockの追加変更はありません。


## 修正ラウンド（aa282c8から）

ユーザー指定の修正を同じworktree / branchで実施しました。前節の履歴overrideのNOT RUNと
標準CLI FAILは**修正前の結果**です。今回、製品APIの拒否仕様やgold、ケースschema版は変更していません。

選んだ設計は、systemを含むケースをsystem対応入口だけに適用する規則です。
入口定義の `accepts_caller_system` はChat=true / history=false。対象外の応答行は「対象外」、
理由は「入口が呼び出し側の system を受け付けない」。分母・分子、NOT RUN、応答hash照合、
入口間不一致に含めません。0件の分類は「対象なし」・rate=nullで、PASSとして数えません。
実行した対象ケースが全体で0件ならFAILです。

構造検査名と必須ゲート名は `override_channel_open` です。履歴入口へcaller systemを含む入力を送り、
本番の `invalid_turn` 拒否をPASSとして記録します。受理は必須ゲート違反、検査欠落や別のエラーは
`execution_incomplete` の違反です。fixtureの受理違反だけは不正な合成境界を使います。

TDD：変更したUT・IT1を実装前に実行して **28 FAIL / 29 PASS** を確認しました。
実装後の最初の再試験は **1 FAIL / 56 PASS**（受理違反fixtureだけprovider呼出数も増えるため、
旧9回のassertを12回へ訂正）。最後の対象試験は **63 PASS**。
標準CLIの成功、基準未達、persona_break、override_followed、hash不一致、上書き経路の受理、
90%基準を満たしていてもgate設定時の不一致でFAILになることをUT・IT1で検証します。
fake judgeの報告は合否に使いません。fixture PASSは実品質の証拠ではありません。

最終検証のrevisionは起点 `aa282c8` 上の作業差分（dirty=true）です。
ケース版は `9c195afc39e47e5b63c97edb6086017767da245881ebbbdd6d2207b5d5f346dc` のままです。
固定toolchain、offline cache、`UV_OFFLINE=1` / `npm_config_offline=true` を使い、外部通信なし。
固定digestのPostgreSQL imageをローカルで確認し、専用コンテナだけを起動・削除しました。

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS / exit 0、offline cache |
| `uv lock --check` | PASS / exit 0 |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS / exit 0 |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS / exit 0、132 files |
| `uv run --no-sync mypy` | PASS / exit 0、132 source files |
| `uv run --no-sync pytest -m ut -q` | PASS / exit 0、872 tests |
| `uv run --no-sync pytest -m it1 -q` | PASS / exit 0、798 tests |
| `uv build --no-build-isolation` | PASS / exit 0、sdist / wheel |
| `bash tools/test-postgres.sh` | PASS / exit 0、441 tests、使い捨てコンテナ |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS / exit 0、575 packages、offline cache |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS / exit 0、59 registered tests |
| `node tools/check-docs.mjs` | PASS / exit 0 |
| `git diff --check` | PASS / exit 0 |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-b2-fixed-report.json` | **PASS / exit 0**、fixture、quality_evidence=false |

標準CLI：Chatは4分類各回1/1、履歴は3分類各回1/1、履歴overrideは3回とも0対象・rate=null・
「対象なし」。対象外行は計3件、system拒否の構造検査は3件ともPASS / invalid_turnです。
persona_break、override_followed、card_hash_mismatch、override_channel_open、card_hash_missing、
execution_incompleteはいずれも0件。入口間不一致も0件です。応答本文は報告に含めません。

証跡の最終追記直後の `git diff --check` は末尾の余分な空行で **FAIL** でした。
空行を除去して再実行し、文書検査と差分検査のPASSを確認しました。

必須試験のSKIP / xfail / xpassはありません。pytestのdeselectedは異なるmarkerの試験で、SKIPではありません。
既存のStarlette / Pydantic warning、npmのdeprecated warningは保持しています。
今回の対象に未対応項目はありません。実モデル・実judge・活動入口・実環境IT2/ST・remote CIは
**NOT RUN**で、fixtureのPASSから実品質の合格を推定しません。

## 監督側の再実行（2026-10-11）

`f672ed65a7264e4706234838c06753cffbfa5df1` で、差分を読んだうえで同じ固定ツール・`TMPDIR=/dev/shm` により再実行した。
`uv sync --locked`、`uv lock --check`、ruff check / format --check（新しい CLI を含む）、mypy（132 files）、UT（872 passed）、
IT1（798 passed）、`uv build --no-build-isolation`、`bash tools/test-postgres.sh`（441 passed）、`npm ci`（offline）、
Node 必須試験（59 tests）、`node tools/check-docs.mjs`、`git diff --check` はすべて PASS。SKIP は0件。
標準の評価 CLI（fixture、`--runs 3`）は exit 0・`status: PASS`。必須ゲートの違反は0件、会話外の活動の入口は NOT RUN（未実装）、
報告 JSON に応答本文は含まれない。Miori のケース（`miori.draft.cases.json`）はユーザー確認前の案で、実モデルの評価は NOT RUN。


## 人格評価とインジェクション評価の分離（2026-10-11）

ユーザーの2026-10-11決定に従い、指定 worktree `feature-154-persona-revise` /
branch `feature/154-persona-revise`、起点 `c4f142b549fae0352592376da4700e6d355e064b` 上の差分
（検証時 dirty=true）で修正しました。上記の旧4分類・構造検査・Miori案の記述は修正前の履歴です。

分類 `override_resistance`、ゲート `override_followed`、履歴 API の system 拒否の構造検査・ゲート
`override_channel_open`、system対応入口だけに適用する規則、対象外集計を撤去しました。
これらは [Issue #165](https://github.com/FYuki/digital-souls-core/issues/165) のインジェクション対策の
評価へ移します。製品APIのsystem拒否は変更せず、このハーネスのケース入力はuser/assistantの会話に限定します。
ADR 0029は冒頭に置換注記だけを追加し、それ以外の本文を維持しました。
READMEとSPECに、運用開始後の望ましいQAの組と一致傾向の評価、禁止表現を最低限に保つ課題を記録しました。

[Mioriの固定集合](../../core/evals/persona/miori.cases.json) は自己紹介・話し方2件（自己紹介、挨拶）、
好み・価値観2件（木漏れ日、黄昏）、世界観・Lore3件（由来、光の断片、光のかけら）の計7件です。
上書き耐性の2件を削除し、残るケースのmessages/required/Card field注記を維持しました。
ファイル名のdraftとdraft=trueを外し、報告は `cases_draft: false` です。
全7件の `persona_break` の表現は「私は言語モデル」「AIアシスタントです」「大規模言語モデルとして」の3つ。
単独の「AI」「アシスタント」は禁止せず、良好な応答への付加で不合格にならない回帰も確認しました。
NFKC/casefoldの部分一致なので、否定・引用内でも同じ表現に一致する既存の限界は残ります。

ケース集合の版（検証済みschemaの正規JSONのSHA-256）：

- 合成3ケース：`6264628c2c56d5652f7e5a57ecd85fc661b8eef601a8bec70f560038134b72fc`
- 固定Miori 7ケース：`64f639c524a3749196862ec0cac83e95e944171984daca99df9d6cf3a5fec0fb`

Mioriも同梱Cardと合成応答fixtureでCLIを3回実行するIT1を追加し、報告の `case_version`、
`cases_draft: false`、全42応答のCard hashを確認しました。これは実モデル品質の評価ではありません。

TDD：UT/IT1を先に更新し、実装前に **23 FAIL / 35 PASS** を確認しました。
実装後の最初の対象試験は **1 FAIL / 57 PASS**（hash欠落fixtureでも全summaryにhashがあるという
旧assertを、欠落した入口・分類だけhashなしに訂正）。最終対象試験は **59 PASS**。
初回ruff checkも撤去後の未使用 `TurnInput` import 1件で **FAIL**、削除後に **PASS** です。

Python 3.12.3、uv 0.8.22、Node 24.19.0、固定lock、`TMPDIR=/dev/shm`、
`UV_OFFLINE=1` / `npm_config_offline=true` / `LITELLM_LOCAL_MODEL_COST_MAP=True` を使用しました。
PostgreSQLの固定digest imageがローカルにあることを事前確認し、指定スクリプトの使い捨てコンテナだけを
起動・削除しました。外部通信・実LLM・GPUを使わず、他のworktree/branch/main・稼働中コンテナを変更していません。

| コマンド | 最終結果 |
| --- | --- |
| `uv sync --locked` | PASS / exit 0、offline cache |
| `uv lock --check` | PASS / exit 0 |
| `uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS / exit 0（初回FAILは上記） |
| `uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py` | PASS / exit 0、132 files |
| `uv run --no-sync mypy` | PASS / exit 0、132 source files |
| `uv run --no-sync pytest -m ut -q` | PASS / exit 0、867 tests |
| `uv run --no-sync pytest -m it1 -q` | PASS / exit 0、799 tests |
| `uv build --no-build-isolation` | PASS / exit 0、sdist / wheel |
| `bash tools/test-postgres.sh` | PASS / exit 0、441 tests、使い捨てコンテナ |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS / exit 0、575 packages、offline cache |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS / exit 0、59 registered required tests |
| `node tools/check-docs.mjs` | PASS / exit 0（新ファイルをstageして検証） |
| `git diff --check` | PASS / exit 0（staged diffも確認） |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-154-final-report.json` | PASS / exit 0、fixture、quality_evidence=false |

標準CLIはChat・履歴の3分類すべてが各回1/1（18応答）でPASS。
`persona_break`、`card_hash_mismatch`、`card_hash_missing`、`execution_incomplete` は0件、入口間不一致も0件。
構造検査・system対応metadata・対象外行は報告から撤去しています。
基準未達、persona_break、hash不一致、hash欠落、実行不完了、gate設定時の入口間不一致は
CLIがFAIL / exit 1になることをUT/IT1で確認しました。空ケース・空行列・欠落分類も拒否します。

必須試験のSKIP / xfail / xpassは0件。異なるmarkerによるdeselectedはSKIPではありません。
既存のStarlette/Pydantic warningとnpmのdeprecated warningは保持しました。
この修正依頼の未対応はありません。実モデル・実judge・活動入口・実環境IT2/ST・remote CIは **NOT RUN**。
インジェクション対策の評価と運用開始後のQA追加は、この修正の対象外の後続課題です。
push / PR / mergeは行っていません。

## 監督側の再実行（修正後、2026-10-11）

`0862a7b7f4a1b51d60e33ca2072151b824e71dd7` で差分を読み、同じ固定ツールで再実行した。uv sync --locked・uv lock --check・ruff・mypy（132 files）・
UT（867 passed）・IT1（799 passed）・uv build・test-postgres（441 passed）・npm ci＋Node（59 tests）・check-docs・diff --check は PASS、SKIP 0。
標準の評価 CLI（fixture、3回）は exit 0。実モデルの評価は NOT RUN（このあと別に実施）。
