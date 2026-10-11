# 人格一貫性ハーネスの合成検証（2026-10-11）

対象は [ADR 0029](../adr/0029-persona-consistency-evaluation.md) の評価ハーネスです。
指定 worktree `feature-154-persona-harness` / branch `feature/154-persona-harness`、
起点 `5d60c5d`（Card hash と PromptBuilder 統合済み）で実施しました。
実装・規約以外の worktree / branch、main、既存コンテナを変更していません。push / PR / merge は行いません。

## 条件と TDD

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

## 実装と測定の解釈

[手順と形式](../../evals/persona/README.md) に strict schema、正規化、各回90%境界、
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

## コマンドと結果

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
