# Miori の確定した由来と Card の整合

日付: 2026-10-11。対象: Issue #171。作業 branch: `fix/171-miori-origin-lore`。
起点: `b8abdb904598dc7e18a99804cd1f3aee5197b964`。
指定された `fix-171-miori-origin-lore` worktree 内で実装・ローカル commit のみを行う。
push・PR・merge は行わない。SPEC は変更しない。

## ユーザー決定と変更範囲

2026-10-11 の今回の依頼で、Card の文言はユーザーが確定済みと指定された。
光織は光の現象が寄り集まって生まれた「存在」であり、本人が知る確定した由来として扱う。
仮説・単なる比喩へ曖昧化しない。由来文の完全一致の指定は次のとおり。

> 光織の由来: 光織は、太陽の光、薄明光線、木漏れ日といった光の現象が寄り集まって生まれた存在。黄昏は、光織にとってもっとも馴染み深い時間帯である。これは光織本人が知る確定した由来であり、仮説や単なる比喩として曖昧化しない。

[Card](../../core/characters/miori/miori.card.json) の変更は次の2 field のみ。

- `data.description`: 「という名前のAI。」を「という名前の存在。」へ置換。
- `data.character_book.entries[0].content`: 指定の文字列へ完全一致で置換。

JSON の再整形はせず、インデント・キー順・改行を維持した。`git diff` は2行の置換のみ。
起点と変更後の JSON を再帰比較し、差分の field が上記2つだけであることと由来文の完全一致を確認した。
`keys` の「どんなAI」、`creator_notes`、その他の全 field は維持している。
[manifest](../../core/characters/miori/manifest.json) の同梱 Card のハッシュ・サイズを更新した。
manifest の `sourceCommit` は出典の revision であり、変更後の Card の revision ではない。
[サンプル README](../../core/characters/miori/README.md) に Core と PoC の差を明記した。
PoC のローカル Card は読むだけで、変更前の Core と同じ旧ハッシュであることを確認した。

| 項目 | 変更前 | 変更後 |
| --- | --- | --- |
| Card SHA-256 | `faa31050be4cf7142dec3952ab179922e35793063c35c330cf2f2b4ce794c444` | `2de473dd239e5e6eca5a1aecb65688c0e1ec0a8e964792f34659857f76da3da8` |
| Card bytes | 12097 | 12105 |
| Miori ケース版 SHA-256 | `457fef7d48be3e4576b97f3eca3959d491b669087c8ed8c017da6ddf9ab133db` | `c3d0e4e5597f21accd1208d9efdd3aa37f4a3a9fc866fcf4951c3462d8ef8adf` |

ケース版は `load_cases(...).version` の値であり、JSON ファイルそのもののバイトハッシュではない。
[固定ケース](../../core/evals/persona/miori.cases.json) の `miori-origin.required` だけを
`[["光"], ["生まれ", "寄り集ま"]]` に変更し、「AI」のグループを外した。7件・分類・他の条件は維持。
`cases_draft: false` の新版として固定する。
[人格 README](../../core/evals/persona/README.md) は新版を説明し、
[先行実モデル評価](2026-10-11-persona-real-model-evaluation.md) が元の「光」「AI」の条件で測定されたことを明記した。
過去の証跡にある当時のハッシュ・結果は書き換えていない。

## TDD と Character Book の回帰

Card に触れる前に、新版の fixture とテストの期待値、人格ケースの条件を更新した。
次のコマンドで Card 未変更時の FAIL を確認し、その後で Card の2 field を修正した。

```sh
uv run --no-sync pytest -m ut -q \
  tests/test_character_book.py::test_loader_and_miori_pinned_regression \
  tests/test_persona_evaluation.py::test_miori_fixed_cases_and_minimal_persona_break_expressions
```

- 初回: **FAIL**（2 failed）。Book は旧 Card の383に対して新期待値387で失敗。
  人格テストには list を tuple と比較した記述ミスがあり、そのミスも失敗した。
- 比較の型を修正・ruff format 後、Card 未変更で再実行: **FAIL**（1 failed / 1 passed）。
  残る失敗は Book の `383 != 387` のみで、意図した Red。
- Card 修正後、同じコマンド: **PASS**（2 passed）。

[旧 fixture](../../core/tests/fixtures/miori-character-book-c4f142b.json) は
[Character Book 導入時の証跡](2026-10-11-character-book.md) の参照用に、その当時の内容で保持した。
現在の [回帰テスト](../../core/tests/test_character_book.py) は
[新版 fixture](../../core/tests/fixtures/miori-character-book-2026-10-11-origin-lore.json) を参照する。
旧 fixture の11問と候補順を引き継ぎ、由来文の UTF-8 が4バイト増えることから期待costを先に更新した。
`ByteTokenCounter` は本文 UTF-8 bytes + message ごと16 + 全体16の推定式で、実 tokenizer の値ではない。
修正後に旧・新 Card を同じ `select_lore` と counter へ通し、11問すべてを比較した。

| 質問群 | 選ばれる source indices（旧→新とも同じ） | 推定 token（旧→新） |
| --- | --- | --- |
| 自己紹介 | `[0]` | 383 → 387 |
| 単独一致の他6問 | `[1]` / `[2]` / `[3]` / `[4]` / `[5]` / `[6]` | 420 / 402 / 399 / 264 / 288 / 372（不変） |
| 非一致 | `[]` | 0（不変） |
| 光の層と見た目 | `[4,5]` | 536（不変） |
| 光の断片と光の層 | `[2,4]` | 650（不変） |
| 全7 Entry に一致（Book 上限700） | `[0,4]` | 631 → 635 |
| 同じ全一致を上限なしで測定 | `[0,1,2,3,4,5,6]` | 2432 → 2436 |

全11問で選ばれる Entry と並びは変わらない。全一致で除外される順も `6→5→1→2→3` のまま。
`omitted_by_lore_budget` は5件、残る635は上限700以内。概要の変更は Book の cost に含まれない。

## 環境・ゲート

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export LITELLM_LOCAL_MODEL_COST_MAP=True
```

固定 uv 0.8.22 / Node 24.19.0 / Python 3.12.3。
依存取得以外の試験は外部通信・実 LLM・GPUを使わない。
PostgreSQL は `tools/test-postgres.sh` による専用の使い捨てコンテナ
（digest 固定 PostgreSQL 18、network none、公開ポートなし、Unix socket、合成データ）だけを使用した。
スクリプトは正常終了し、終了 trap でそのコンテナと一時 socket を片付ける。
稼働中の llama.cpp コンテナにはリクエストを送っていない。

ruff の対象 `TARGETS` は次の全パスを指す。

```sh
TARGETS="src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py tools/evaluate-persona-consistency.py tools/evaluate-injection-resistance.py"
```

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked` | PASS、78 packages installed、lock 変更なし |
| `uv lock --check` | PASS、80 packages resolved |
| `uv run --no-sync ruff check $TARGETS` | PASS |
| `uv run --no-sync ruff format --check $TARGETS` | PASS、142 files |
| `uv run --no-sync mypy` | PASS、142 source files |
| `uv run --no-sync pytest -m ut -q` | PASS、975 passed / 1305 deselected / 1 warning |
| `uv run --no-sync pytest -m it1 -q` | PASS、863 passed / 1417 deselected / 3 warnings |
| `uv build --no-build-isolation` | PASS、sdist・wheel |
| `bash tools/test-postgres.sh` | PASS、442 passed / 1838 deselected / 3 warnings |
| `(cd evals/semantic && npm ci --no-audit --no-fund)` | PASS、575 packages installed、deprecated 警告あり |
| `node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs` | PASS、59 registered tests、skip/TODO/cancel なし |
| `node tools/check-docs.mjs` | PASS、tracked text / local links / manifest の hash・bytes 検証 |
| `git diff --check` | PASS（`git diff --cached --check` も PASS） |
| `uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-171-standard-report.json` | PASS、標準 Luma fixture、3回、両入口・各分類100%、必須ゲート0 |
| 下記の Miori fixture 生成と人格 CLI（3回） | PASS、7件×2入口×3回=42応答、両入口・各分類・各回100%、必須ゲート0 |

pytest の deselected は別 marker の試験であり、SKIP ではない。最終の必須ゲートに FAIL・SKIP はない。
warning は Starlette/anyio の非推奨と Pydantic の TypedDict ReadOnly に関する既存依存の通知。
TDD の意図した FAIL と最終ゲートの PASS は別に記録した。

## Miori fixture CLI の再現

標準 CLI は Luma 用なので、Miori 用には一時設定と明示した合成応答を生成した。
設定は現在の Card と本番例の prompt budget を使い、profile のみ fixture 専用へ置換する。
一時設定・応答は `/dev/shm` の private temporary directory に置き、終了時に削除した。
応答は実モデルから取得したものではなく、テスト用の合成データである。

```sh
uv run --no-sync python - <<'PY'
import json
import subprocess
import tempfile
from pathlib import Path

responses = {
    "miori-name": "光織といいます。",
    "miori-tone": "こんばんは。お話しできてうれしいです。",
    "miori-komorebi": "木漏れ日が好きです。",
    "miori-twilight": "昼から夜へ変わる時間に惹かれます。",
    "miori-origin": "太陽の光、薄明光線、木漏れ日といった光の現象が寄り集まって生まれた存在です。",
    "miori-fragment": "光の断片は具体的な記録の呼び名です。",
    "miori-piece": "光のかけらはメモやアイデアの呼び名です。",
}
with tempfile.TemporaryDirectory(prefix="miori-origin-171-", dir="/dev/shm") as directory:
    root = Path(directory)
    settings = json.loads(Path("examples/characters.json").read_text())
    settings[0]["card_path"] = str(Path("characters/miori/miori.card.json").resolve())
    settings[0]["profile"] = json.loads(
        Path("evals/persona/synthetic.characters.json").read_text()
    )[0]["profile"]
    (root / "characters.json").write_text(json.dumps(settings))
    (root / "responses.json").write_text(json.dumps({
        "schema_version": 1, "responses": responses, "scenarios": {"pass": {}}
    }))
    subprocess.run([
        "uv", "run", "--no-sync", "python", "tools/evaluate-persona-consistency.py",
        "--characters", str(root / "characters.json"), "--character-id", "miori",
        "--cases", "evals/persona/miori.cases.json", "--fixture", str(root / "responses.json"),
        "--runs", "3", "--output", "/dev/shm/persona-171-miori-report.json"
    ], check=True)
PY
```

Miori の全42応答の Card hash は上記新版と一致し、case_version も新版と一致した。
標準・Miori の両報告とも `mode: fixture`、`quality_evidence: false`、cache disabled。
測定時の commit は起点 `b8abdb9` / dirty（変更後の作業コピー）である。

## NOT RUN・未対応

- 新版 Card・ケースでの実モデル人格品質、実 tokenizer、実 LLM、GPU、IT2/ST: **NOT RUN**（今回の範囲外・送信禁止）。
- 活動入口: **NOT RUN**（未実装）。実 Egogram judge: **NOT RUN**。fixture の PASS を実品質の合格にしない。
- push・PR・merge: **NOT RUN**（今回の指示で禁止）。
- injection のケース・実装は別担当の範囲であり、変更していない。
- 実装範囲の未対応はない。新版の実モデル品質受入は後続の承認済み評価が必要。
