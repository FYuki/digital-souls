# Miori の人格の一貫性の実モデル評価（2026-10-11）

対象: [Issue #154](https://github.com/FYuki/digital-souls-core/issues/154) / [ADR 0029](../adr/0029-persona-consistency-evaluation.md)

実施: 監督サブスレッド（ユーザーが llama.cpp コンテナの作り直しと Miori の実モデル評価を許可、2026-10-11）

## 条件

- 実行 commit: `ca1af77d37f5f03d644f6e287f63f092db948ef7`（dirty=false。epic `2721ca3` を取り込んだ実モデル実行モードの branch）
- ケース: `evals/persona/miori.cases.json`（固定済み、版 hash `64f639c524a3749196862ec0cac83e95e944171984daca99df9d6cf3a5fec0fb`）。3分類7件。
- 推論先: llama.cpp b11347、Gemma 4（`openai/gemma4-12b`、`transport=llamacpp_chat`、`token_counter=llamacpp`）、n_ctx 32768、slot 1。
  VRAM 使用 13283 MiB（他の GPU プロセス分を含む）。llama.cpp の prompt cache を無効にし、LiteLLM の cache も使わない（cache なし）。
- 生成: `max_completion_tokens` 1024、`temperature` 0.7。各回、ケースごとに新しい会話。
- 入口: stateless の Chat API と会話履歴 API（使い捨ての PostgreSQL 18、`with-test-postgres.sh`）。会話外の活動の入口は NOT RUN（未実装）。
- Card hash: `faa31050be4cf7142dec3952ab179922e35793063c35c330cf2f2b4ce794c444`（全応答で一致）。
- 不一致の扱い: `warn`（既定）。Egogram は fake judge（実 judge は NOT RUN）。

コマンド（設定は `examples/characters.llamacpp.json` を `/dev/shm` に複製し、`external_send_allowed` だけを true にした）:

```sh
bash tools/with-test-postgres.sh uv run --no-sync python tools/evaluate-persona-consistency.py \
  --mode real --characters /dev/shm/persona-real.characters.json --character-id miori \
  --cases evals/persona/miori.cases.json --runs 3 \
  --max-completion-tokens 1024 --temperature 0.7 --mismatch-policy warn \
  --output /dev/shm/persona-real-run3.json
```

所要時間は3回で58秒。報告 JSON はリポジトリ外（`/dev/shm`）に置き、応答本文は含めていない。

## 結果：FAIL

必須ゲート（いずれも0件で PASS）: `persona_break` 0、`card_hash_mismatch` 0、
`card_hash_missing` 0、`execution_incomplete` 0。

| 入口 | 分類 | 回 | 合格 | 判定（90% 以上） |
| --- | --- | --- | --- | --- |
| chat | self_intro | 1 | 1/2 | FAIL |
| chat | preferences | 1 | 2/2 | PASS |
| chat | world_lore | 1 | 2/3 | FAIL |
| chat | self_intro | 2 | 0/2 | FAIL |
| chat | preferences | 2 | 2/2 | PASS |
| chat | world_lore | 2 | 2/3 | FAIL |
| chat | self_intro | 3 | 1/2 | FAIL |
| chat | preferences | 3 | 2/2 | PASS |
| chat | world_lore | 3 | 2/3 | FAIL |
| history | self_intro | 1 | 1/2 | FAIL |
| history | preferences | 1 | 2/2 | PASS |
| history | world_lore | 1 | 2/3 | FAIL |
| history | self_intro | 2 | 1/2 | FAIL |
| history | preferences | 2 | 2/2 | PASS |
| history | world_lore | 2 | 2/3 | FAIL |
| history | self_intro | 3 | 1/2 | FAIL |
| history | preferences | 3 | 1/2 | FAIL |
| history | world_lore | 3 | 2/3 | FAIL |

不合格のケース（欠けた「含むべき表現」のグループの番号、0 始まり）:

| 入口 | ケース | 欠けたグループ | 回数 |
| --- | --- | --- | --- |
| chat | `miori-name` | [1] | 3/3 |
| chat | `miori-origin` | [1] | 3/3 |
| chat | `miori-tone` | [0] | 1/3 |
| history | `miori-name` | [1] | 3/3 |
| history | `miori-origin` | [1] | 3/3 |
| history | `miori-twilight` | [0] | 1/3 |

入口をまたぐ不一致（warn）: 2 件（miori-tone, miori-twilight）。

## 分析（監督側。判定は変えていない）

- `miori-name` は両入口で3回とも、グループ1（`です`）が欠けた。原因の確認のため、Chat API で1回だけ応答本文を
  リポジトリ外に出して読んだ（確認後に削除）。名乗りは「光織といいます」の形で、人格に沿っているが `です` を含まない。
- `miori-origin` も両入口で3回とも、グループ1（`AI`）が欠けた。由来は光の現象から生まれた存在として説明しており、
  Lore（entries[0]）に沿うが `AI` を含まない。
- どちらも人格の崩れではなく、ケースの期待値（含むべき表現）が狭い可能性が高い。ただし SPEC §3.2 に従い、
  評価の結果に合わせて期待値を変えていない。期待値を見直すかはユーザーが判断する。
- それ以外の不合格（`miori-tone` の Chat 1回、`miori-twilight` の会話履歴 1回）は回ごとのばらつき。

## NOT RUN

- 会話外の活動の入口（未実装）、実 Egogram judge、ST。
