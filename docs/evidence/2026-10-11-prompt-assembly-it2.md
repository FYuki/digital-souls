# プロンプトの組み立ての実サーバー試験（IT2）

日付: 2026-10-11

対象: [Issue #152](https://github.com/FYuki/digital-souls-core/issues/152) / [ADR 0026](../adr/0026-prompt-assembly.md)

実施: 監督サブスレッド（ユーザーが llama.cpp コンテナの作り直しと IT2 を許可、2026-10-11）

## 環境

- llama.cpp：main（`5b9ea26`）の [compose.llamacpp.yml](../../core/compose.llamacpp.yml) から `digital-souls-core-llamacpp` だけを `--force-recreate` で作り直した。
  起動前に Ollama の停止とモデルの SHA-256（ADR 0003 の値）を確認。起動後の `/props`：`n_ctx` 32768、slot 1、build `b11347-5fc4f3c8c`。
  VRAM（RTX 4070 Ti SUPER 16 GiB）：起動前 4922 MiB 使用 → 起動後 13329 MiB 使用（空き 2732 MiB。他の GPU プロセス分を含む）。
- Core：Python 3.12、固定 uv 0.8.22。Card は同梱の Miori。`post_history_instructions` の確認だけ、Miori のカードに合成の一文を足した複製（`/dev/shm`）を使った。
- 設定：`examples/characters.llamacpp.json` を `/dev/shm` に複製し、`external_send_allowed` だけを true にした（`token_counter: llamacpp`）。推定式の比較用に `token_counter: estimate` の profile も用意した。
- スクリプト：[it2_prompt.py](2026-10-11-prompt-assembly-it2/it2_prompt.py)。`Inference.prepare` で組み立て、本番の `LiteLLMProvider` で送る。`max_completion_tokens` 24、`temperature` 0、各ケース3回。
  記録は token 数・role の並び・終了理由・成否だけで、応答本文は記録していない。

列は「数えた token 数（Core の llama.cpp counter）」「サーバーの `usage.prompt_tokens`」「推定式の値」。

## 修正前（epic `c4f142b549fae0352592376da4700e6d355e064b`）

| ケース | 成功 | 数えた数 | 実測 | 推定 | message の並び |
| --- | --- | --- | --- | --- | --- |
| user_only | 3/3 | 622 | 622 | 2883 | system・user |
| caller_system_mid | 3/3 | 609 | 609 | 2880 | system・system・user・assistant・user |
| post_history | 3/3 | 566 | 566 | 2650 | system・system・user |
| leading_assistant | 0/3 | CoreError:token_count_failed | - | - | - |
| history_and_system_and_phi | 3/3 | 647 | 647 | 3009 | system・system・user・assistant・user・assistant・system・user |
| estimate_counter | 3/3 | 2520 | 541 | 2520 | system・user |

**FAIL**：会話が assistant から始まる要求（`leading_assistant`）が 502 `token_count_failed`。原因は、領域ごとの token 数を「その領域だけ」の並びで数えたため、
履歴の領域が `[assistant]` だけになり、`/apply-template` が Gemma 4 の template で 500 を返したこと（サーバーへの直接の確認：`[assistant]` だけ → 500、
`[system]` だけ → 200、`[user, assistant]` → 200、`[system, assistant, user]` → 200）。

## 修正後（`c01a35cbf7839fb532d258327cfa85492bf09487`、epic `9a9250d` の Character Book を含む）

領域ごとの数を、空の user で前後を包んだ並びと、空の枠だけの並びとの差で数える（全体の数は完成した messages をそのまま数える）。

| ケース | 成功 | 数えた数 | 実測 | 推定 | message の並び |
| --- | --- | --- | --- | --- | --- |
| user_only | 3/3 | 627 | 627 | 2909 | system・user |
| caller_system_mid | 3/3 | 609 | 609 | 2880 | system・system・user・assistant・user |
| post_history | 3/3 | 566 | 566 | 2650 | system・system・user |
| leading_assistant | 3/3 | 552 | 552 | 2569 | system・assistant・user |
| history_and_system_and_phi | 3/3 | 647 | 647 | 3009 | system・system・user・assistant・user・assistant・system・user |
| leading_assistant_long | 3/3 | 574 | 574 | 2646 | system・assistant・user・assistant・user |
| tool_free_history_only_assistant_first | 3/3 | 607 | 607 | 2850 | system・system・assistant・system・user |
| estimate_counter | 3/3 | 2520 | 541 | 2520 | system・user |

## 結果

- **PASS**：Gemma 4 の chat template は、途中の system（呼び出し側の指示、`post_history_instructions`）と、assistant から始まる会話を受け付けた。
- **PASS**：llama.cpp の counter で数えた token 数は、全ケース・全回でサーバーの `usage.prompt_tokens` と一致した。
- **観測**：推定式（UTF-8 バイト数＋message×16＋16）は、実測の約4.6〜4.7倍だった（例 推定 2520／実測 541）。ADR 0026・0028 の「2〜3倍」は過小で、両 ADR に実測の注記を付けた。
- 修正前の `user_only` が 622、修正後が 627 なのは、間に Character Book の実装（Lore の見出しの追加）が入ったため。

## NOT RUN

- 実サーバーで予算を超える長い会話を送り、履歴が往復単位で削られることの確認（IT1 で fake を使って確認済み）。
- 実モデルの人格の評価（別の証跡で扱う）。ST。
