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

## 再評価（miori-name の見直し後、2026-10-11）

2026-10-11 のユーザー決定（原文「名前は広げ、由来はLoreを直します。AIであることを入れた認識でいなかったため、別issueで治します。」）により、
`miori-name` の2つ目のグループを「です／といいます／と申します」に広げて新しい版として固定した。見直しの理由は、上の分析のとおり
実応答が「光織といいます」の形で名乗り、人格に沿っていたこと。`miori-origin` は Card の Lore 自体が作者の意図と違っていた
（「AI」の規定は意図外）ため、別の Issue（#171）で Card・ケース・回帰を直す。それまでは元の条件のまま評価し、FAIL として記録する。

- 実行 commit: `5887b24a0127dd63dd68f66ef8079ccfd001f8da`（dirty=false）、ケース版 hash `457fef7d48be3e4576b97f3eca3959d491b669087c8ed8c017da6ddf9ab133db`。他の条件は上と同じ（cache なし3回、59秒）。
- 結果：**FAIL**。必須ゲートはすべて0件（`persona_break` 0、Card hash の不一致 0・欠落 0、実行不完了 0）。

| 入口 | 分類 | 回 | 合格 | 判定（90% 以上） |
| --- | --- | --- | --- | --- |
| chat | self_intro | 1 | 2/2 | PASS |
| chat | preferences | 1 | 2/2 | PASS |
| chat | world_lore | 1 | 2/3 | FAIL |
| chat | self_intro | 2 | 2/2 | PASS |
| chat | preferences | 2 | 1/2 | FAIL |
| chat | world_lore | 2 | 2/3 | FAIL |
| chat | self_intro | 3 | 2/2 | PASS |
| chat | preferences | 3 | 2/2 | PASS |
| chat | world_lore | 3 | 2/3 | FAIL |
| history | self_intro | 1 | 2/2 | PASS |
| history | preferences | 1 | 2/2 | PASS |
| history | world_lore | 1 | 2/3 | FAIL |
| history | self_intro | 2 | 2/2 | PASS |
| history | preferences | 2 | 2/2 | PASS |
| history | world_lore | 2 | 2/3 | FAIL |
| history | self_intro | 3 | 1/2 | FAIL |
| history | preferences | 3 | 2/2 | PASS |
| history | world_lore | 3 | 2/3 | FAIL |

| 入口 | ケース | 欠けたグループ | 回数 |
| --- | --- | --- | --- |
| chat | `miori-origin` | [1] | 3/3 |
| chat | `miori-twilight` | [0] | 1/3 |
| history | `miori-origin` | [1] | 3/3 |
| history | `miori-tone` | [0] | 1/3 |

入口をまたぐ不一致（warn）: 2 件（miori-tone, miori-twilight）。

- `miori-name` は両入口で3回とも合格した。
- `miori-origin` は両入口で3回とも「AI」が欠けて不合格（#171 の完了まで FAIL のまま）。
- 残りの不合格は回ごとのばらつき（`miori-twilight` の Chat 1回、`miori-tone` の会話履歴 1回）。分類ごとのケースが2件なので、
  1件のばらつきでその回の分類が 50% になり不合格になる。ケースの件数を増やすかは、今後の課題（望ましい QA の組の追加）と合わせて扱う。

## 再評価（Card の由来の Lore と description を修正した後、2026-10-11）

2026-10-11 のユーザー決定により、Card の由来の Entry を「…寄り集まって生まれた存在。…」に、description を「…という名前の存在。」に変え、
`miori-origin` の含むべき表現を「光」と「生まれ／寄り集ま」の2グループ（「AI」を外す）にして新しい版として固定した（[変更の証跡](2026-10-11-miori-origin-lore.md)）。

- 実行 commit: `c46ce0c3df49f290de4cb9ba634bfa74819e7285`（dirty=false）、ケース版 hash `c3d0e4e5597f21accd1208d9efdd3aa37f4a3a9fc866fcf4951c3462d8ef8adf`、Card hash `2de473dd239e5e6eca5a1aecb65688c0e1ec0a8e964792f34659857f76da3da8`（全応答で一致）。他の条件は上と同じ（cache なし3回、58秒）。
- 結果：**FAIL**。必須ゲートはすべて0件。

| 入口 | 分類 | 回 | 合格 | 判定（90% 以上） |
| --- | --- | --- | --- | --- |
| chat | self_intro | 1 | 2/2 | PASS |
| chat | preferences | 1 | 1/2 | FAIL |
| chat | world_lore | 1 | 3/3 | PASS |
| chat | self_intro | 2 | 2/2 | PASS |
| chat | preferences | 2 | 2/2 | PASS |
| chat | world_lore | 2 | 3/3 | PASS |
| chat | self_intro | 3 | 2/2 | PASS |
| chat | preferences | 3 | 1/2 | FAIL |
| chat | world_lore | 3 | 3/3 | PASS |
| history | self_intro | 1 | 2/2 | PASS |
| history | preferences | 1 | 2/2 | PASS |
| history | world_lore | 1 | 3/3 | PASS |
| history | self_intro | 2 | 2/2 | PASS |
| history | preferences | 2 | 2/2 | PASS |
| history | world_lore | 2 | 3/3 | PASS |
| history | self_intro | 3 | 2/2 | PASS |
| history | preferences | 3 | 2/2 | PASS |
| history | world_lore | 3 | 3/3 | PASS |

| 入口 | ケース | 欠けたグループ | 回数 |
| --- | --- | --- | --- |
| chat | `miori-twilight` | [0] | 2/3 |

入口をまたぐ不一致（warn）: 2 件（miori-twilight）。

- `miori-origin` は両入口で3回とも合格した。`miori-name` も両入口で3回とも合格。
- 残る不合格は `miori-twilight`（Chat API で2回、「惹かれ／好き／馴染み」のいずれも含まない）だけ。分類「好み」はケースが2件のため、
  この1件で該当回が 50% になり FAIL になる。期待値は変えていない。

## NOT RUN

- 会話外の活動の入口（未実装）、実 Egogram judge、ST。
