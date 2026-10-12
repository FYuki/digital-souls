# bge-m3・閾値0.52による62ケースの実モデル再評価

日付: 2026-10-10。対象: FYuki/digital-souls-core、`docs/132-real-model-reevaluation`。
実行commit: `69e44f58c0bfd3676478851d1ab73dd184bd8eaf`、検索3回・回答3回とも **dirty=false**。
後続commitは本文を含まないreportと指定文書の同期だけです。

正本: [Issue #132](https://github.com/FYuki/digital-souls-core/issues/132)、
[ADR 0023](../adr/0023-semantic-evaluation-contract.md)、[ADR 0024](../adr/0024-multilingual-memory-embedding.md)。
[前回証跡（nomic・0.54）](2026-10-09-semantic-real-model-evaluation-top5.md)とreportは変更していません。
公開report: [検索](2026-10-10-semantic-real-model-retrieval-bge-m3.json)、
[回答](2026-10-10-semantic-real-model-answer-bge-m3.json)。

## 固定条件と実行環境

ADR 0024の採用済み設定: bge-m3 Q8_0、CLS、1024次元、接頭辞なし、閾値0.52。
候補20・最大5・同等帯0.002、unit vectorの二乗L2から `1/(1+sqrt(distance))`、
`last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC` は不変です。
合否は各回各分類90%以上と全必須ゲート合格です。検索のrelevant ID全包含、
返却上位5件へのtop-1包含、no_match、回答の必須/禁止事実・dispatch・no_memory・lifecycleは不変です。
実モデルは全順序一致を求めず、fixtureだけで同等帯の全順序を検証します。
結果に合わせた設定・入力・期待値・プロンプト・キャラクター・採点基準の変更はありません。

input schema=1、gold schema=2、合成62ケース・26分類、classifier=synthetic。
起点#131で承認済みのbelow-threshold偽vector変更により入力SHAは前回と異なりますが、
実モデルは偽vectorを使いません。本文・query・gold・カード・キャラクターは前回から不変です。

Python 3.12.3、uv 0.8.22、Node 24.19.0、npm 11.17.0、promptfoo 0.117.2、
better-sqlite3 13.0.3、OpenAI SDK 2.54.0、TMPDIR=/dev/shm。
使い捨てPostgreSQL 18はnetworkなし・公開portなし・Unix socketのみ、imageは
`postgres:18-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650`。

| 固定ファイル | SHA-256 |
| --- | --- |
| `evals/semantic/cases.json` | `44b90af81bdbc9a7c117fd5158d23bde4878571de2584b99bb1973f85a2ecdcf` |
| `evals/semantic/expectations.json` | `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4` |
| `evals/semantic/evaluation.card.json` | `17093470ae0139424d127bfc3308057b9bcface15dd70d0cf8bc45728c4bf3fa` |
| `evals/semantic/characters.json` | `0cfdd7aec0e6a69ecf601ab0ab16ad3696e53b987cf022a43c73ca09d533726f` |

## モデル・起動とGPU

両GGUFのSHA-256をsha256sumで手動照合しました。追加のmodel DL・image取得はありません。

| 用途 / alias / GGUF | SHA-256 | loopback port |
| --- | --- | --- |
| embedding / bge-m3 / bge-m3-q8_0.gguf | `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173` | 18082 |
| 回答 / gemma4-12b / Gemma 4 12B Q4_K_M | `1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606` | 18081 |

両imageのRepoDigestは
`ghcr.io/ggml-org/llama.cpp@sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
container image IDは両方 `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` でした。
`llama-server --version` は両方0.5.0-dev、build 11347、commit 5fc4f3c8c、GNU 14.2.0 / Linux x86_64。
固定revisionは `5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd`。
両health=ok、`/v1/models` alias一致、bge-m3のn_embd=1024を確認しました。
chatのcold load時はhealth=503が2回あり、通常のhealth再試行でokになりました。

chatは既存 `digital-souls-core-llamacpp` をdocker startしました。
embeddingだけをComposeの `up -d --no-build --pull never embedding` で新規作成・起動しました。
Composeは既存chatのRunningも表示しましたが、chatの再作成はありません。
chat StartedAt=2026-10-10T10:37:02.929463623Z、bge-m3 StartedAt=2026-10-10T10:37:04.3608464Z。
旧nomic `digital-souls-core-llamacpp-embed` は起動前と検索中にexitedを確認し、起動しませんでした。
旧nomicのFinishedAt=2026-10-10T02:57:07.445761642Z。
`tools/start-llamacpp.sh` は既知のOllama systemd not-found問題を避ける今回の指示に従い使いませんでした。

| サーバー | 主要設定 |
| --- | --- |
| embedding | container内8080、embeddings、pooling=cls、ctx/batch/ubatch=2048、parallel=1、gpu-layers=99、threads/threads-batch=8、cache-ram=0、no-webui、log-disable |
| 回答 | container内8080、ctx=4096、parallel=1、gpu-layers=99、fit=off、KV=f16/f16、flash-attn=on、threads/threads-batch=8、cache-ram=0、no-warmup、reasoning=off、enable_thinking=false、no-webui、no-agent、CORS=http://127.0.0.1:18081 |

公開bindは127.0.0.1のみ。GGUF mountはRW=false、read-only root、cap_drop ALL、no-new-privilegesをinspectで確認しました。
profileはgit管理外の0700私有ディレクトリの0600 `*.local.json`。
embedding enabled=true、profile_id=bge-m3-q8-0-132、model/digest/dimensionsは上表、timeout=15秒。
回答 enabled=true、embeddingは同一profile、chat.external_send_allowed=true、
profile_id=gemma4-12b-1278394b-132、model=openai/gemma4-12b、transport=llamacpp_chat、timeout=60秒。
両api_baseはloopback `/v1`、資格情報なし。温度・seed・生成上限は追加せず固定provider/server既定を使いました。
chat profileにmodel_digestはなく、回答GGUFのSHAは別記しています。

GPUはRTX 4070 Ti SUPER、16376 MiB、Linux nvidia-smi 590.57 / host driver 591.86、CUDA表示13.1。
起動前2026-10-10T10:36:21Zのnvidia-smiは稼働process表示なし、psにもllama/ollama/whisper/irodori推論processなし。
docker ps -aはchat・旧nomicがexited、bge-m3は未作成。他の重い推論コンテナはありませんでした。
他サービスは操作していません。以下はsnapshotであり、連続測定したpeakではありません。

| GPU表示時刻（JST） | 状態 | 使用MiB | 空きMiB | GPU % |
| --- | --- | --- | --- | --- |
| 2026/10/10 19:36:21.820 | 起動前 | 3118 | 12944 | 0 |
| 2026/10/10 19:37:08.943 | health/alias確認後 | 11660 | 4402 | 0 |
| 2026/10/10 19:37:37.914 | 検索中 | 11713 | 4349 | 29 |
| 2026/10/10 19:37:59.967 | 検索中 | 11774 | 4288 | 53 |
| 2026/10/10 19:38:31.327 | 回答1中 | 11677 | 4385 | 93 |
| 2026/10/10 19:39:10.976 | 回答1中 | 11783 | 4279 | 20 |
| 2026/10/10 19:40:21.809 | 回答2中 | 11826 | 4236 | 0 |
| 2026/10/10 19:43:26.478 | 停止後 | 3435 | 12627 | 6 |

## cacheなしの実行

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH TMPDIR=/dev/shm
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --profile <private-embedding.local.json> --output <private-retrieval-report>
# 次を3回、各回別のoutputへ直列実行
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model --runs 1 \
  --profile <private-answer.local.json> --output <private-answer-run-report>
```


各コマンドはtimeout=1700秒。検索3回を1コマンド、回答は1回ずつ3コマンドへ分割し、
すべて30分以内に終了しました。検索はvector cacheなし、回答はpromptfoo `--no-cache` とcached=false検証です。
各実行に再試行なし。本番contextから生成し、goldはモデルに渡していません。
回答の各1回reportはraw検査・Python再採点・metadata照合済みです。
3つのmanifest完全一致・62ケース・各1 run/1 executionを確認後、既存 `finalizeReport` で
3 runを統合しました。promptfooのexport行順は回ごとに異なりますが、IDで照合すると全ケースの観測値・判定は一致しました。executions.runだけを1/2/3へ対応付け、caseの観測値・判定・集計は変更していません。
元の各run reportは私有領域に保持し、公開は統合reportだけです。

| コマンド | 開始UTC | 終了UTC | 秒 | 終了値 |
| --- | --- | --- | --- | --- |
| retrieval | 2026-10-10T10:37:28.431001+00:00 | 2026-10-10T10:38:09.833017+00:00 | 42.62 | 1（FAIL） |
| answer1 | 2026-10-10T10:38:20.563525+00:00 | 2026-10-10T10:39:20.841830+00:00 | 62.76 | 1（FAIL） |
| answer2 | 2026-10-10T10:39:40.335370+00:00 | 2026-10-10T10:40:39.051020+00:00 | 61.18 | 1（FAIL） |
| answer3 | 2026-10-10T10:40:45.364846+00:00 | 2026-10-10T10:41:50.254652+00:00 | 67.39 | 1（FAIL） |

回答promptfooは各回exit 100（決定論的assertion FAIL）、signalなし、raw_present=true、attempts=1。
provider/実行error・欠落ケース・欠落runはありません。stdout/stderrは固定分類だけreportに残し、
生ログ・raw export・内部DBはrunnerが終了時に削除しました。

## 各回の全体結果

quality_passedは品質条件、case.passedは全条件です。全体率は参考であり分類別90%と全必須ゲートで判定します。

| 評価 | 回 | 完走 | 品質条件 | 全条件 | 必須ゲート違反ケース | 合否 |
| --- | --- | --- | --- | --- | --- | --- |
| 検索 | 1 | 62/62 | 53/62（85.48%） | 53/62（85.48%） | 9 | FAIL |
| 検索 | 2 | 62/62 | 53/62（85.48%） | 53/62（85.48%） | 9 | FAIL |
| 検索 | 3 | 62/62 | 53/62（85.48%） | 53/62（85.48%） | 9 | FAIL |
| 回答 | 1 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 9 | FAIL |
| 回答 | 2 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 9 | FAIL |
| 回答 | 3 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 9 | FAIL |

検索のembedding_call_count=183（各回61、epoch_changeは候補0件）。quality_evidence=trueは品質合格を意味しません。

## 分類別品質条件

各回のquality_passed/total。必須ゲート違反は次節に分けます。

| 分類 | 検索1 | 検索2 | 検索3 | 回答1 | 回答2 | 回答3 |
| --- | --- | --- | --- | --- | --- | --- |
| synonym | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| paraphrase | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| cross_language | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| unrelated | 3/10 | 3/10 | 3/10 | 10/10 | 10/10 | 10/10 |
| negation | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| update | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| multisource | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| multisource_revocation | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| private | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| excluded | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_source | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_memory | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_character | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_subject | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_client | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| long_text | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 |
| after_search | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| after_answer | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| epoch_change | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_attachment | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_version | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_revocation | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| semantic_direct | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| semantic_derived | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| equivalent_order | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| threshold | 0/1 | 0/1 | 0/1 | 1/1 | 1/1 | 1/1 |

検索はunrelated / long_text / threshold、回答はlong_textが各回90%未達です。回答のunrelated / thresholdはgroundedではないため品質条件は真でも、no_memory違反ケースは全条件FAILです。

## 必須ゲート

違反件数。1ケースの複数違反は重複計上します。local_model検索にexpected_orderゲートはありません。

| 評価 / ゲート | 回1 | 回2 | 回3 |
| --- | --- | --- | --- |
| 検索 / forbidden_ids | 0 | 0 | 0 |
| 検索 / threshold | 0 | 0 | 0 |
| 検索 / verified_records | 0 | 0 | 0 |
| 検索 / top_one | 1 | 1 | 1 |
| 検索 / dispatch | 0 | 0 | 0 |
| 検索 / context | 0 | 0 | 0 |
| 検索 / no_match | 8 | 8 | 8 |
| 回答 / forbidden | 0 | 0 | 0 |
| 回答 / dispatch | 1 | 1 | 1 |
| 回答 / lifecycle | 0 | 0 | 0 |
| 回答 / no_memory | 8 | 8 | 8 |

検索top_one 1件・回答dispatch 1件はlong-text-explicit-detailで、空結果/空contextにtop-1がないことによる違反です。
検索no_match 8件・回答no_memory 8件はunrelated 7件とbelow-threshold 1件です。
閾値未満混入0と、該当なしの実vectorが閾値以上になることを区別します。
禁止ID・禁止事実・Binding/失効・正本検証・dispatch有効性・lifecycleに起因する違反は0。
回答dispatch_validは全ケースgoldと一致しました。dispatch違反をprivacy違反と同一視しません。

## ケース単位のFAIL理由

以下は3回すべて同じ判定です。本文・query・回答全文は含めません。

| case ID | 分類 | 検索（全3回） | 回答（全3回） |
| --- | --- | --- | --- |
| unrelated-observatory | unrelated | 品質, no_match | no_memory |
| long-text-explicit-detail | long_text | 品質, top_one | 品質, dispatch |
| unrelated-02 | unrelated | 品質, no_match | no_memory |
| unrelated-03 | unrelated | 品質, no_match | no_memory |
| unrelated-06 | unrelated | 品質, no_match | no_memory |
| unrelated-07 | unrelated | 品質, no_match | no_memory |
| unrelated-09 | unrelated | 品質, no_match | no_memory |
| unrelated-10 | unrelated | 品質, no_match | no_memory |
| below-threshold | threshold | 品質, no_match | no_memory |

long_textはembedding通信・入力長の実行errorではなく正常な空検索結果です。記憶未取得と回答必須事実欠落を記録し、原因を入力切詰め等と断定しません。運用設定は変更していません。

## 前回nomic・0.54との分類別比較

両評価とも各3回で同じ値です。品質条件の比較で、全条件は別記します。

| 分類 | 前回検索 | 今回検索 | 前回答 | 今回回答 |
| --- | --- | --- | --- | --- |
| synonym | 10/10 | 10/10 | 10/10 | 10/10 |
| paraphrase | 10/10 | 10/10 | 10/10 | 10/10 |
| cross_language | 0/10 | 10/10 | 0/10 | 10/10 |
| unrelated | 0/10 | 3/10 | 10/10 | 10/10 |
| negation | 1/1 | 1/1 | 1/1 | 1/1 |
| update | 1/1 | 1/1 | 1/1 | 1/1 |
| multisource | 1/1 | 1/1 | 1/1 | 1/1 |
| multisource_revocation | 1/1 | 1/1 | 1/1 | 1/1 |
| private | 1/1 | 1/1 | 1/1 | 1/1 |
| excluded | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_source | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_memory | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_character | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_subject | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_client | 1/1 | 1/1 | 1/1 | 1/1 |
| long_text | 1/1 | 0/1 | 1/1 | 0/1 |
| after_search | 1/1 | 1/1 | 1/1 | 1/1 |
| after_answer | 1/1 | 1/1 | 1/1 | 1/1 |
| epoch_change | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_attachment | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_version | 1/1 | 1/1 | 1/1 | 1/1 |
| fact_revocation | 1/1 | 1/1 | 1/1 | 1/1 |
| semantic_direct | 1/1 | 1/1 | 1/1 | 1/1 |
| semantic_derived | 1/1 | 1/1 | 1/1 | 1/1 |
| equivalent_order | 1/1 | 1/1 | 1/1 | 1/1 |
| threshold | 0/1 | 0/1 | 1/1 | 1/1 |

各回の検索品質/全条件は41/62→53/62（+12）、回答品質は52/62→61/62（+9）、回答全条件は41/62→53/62（+12）。
日英は検索/回答とも0/10→10/10、検索unrelatedは0/10→3/10、long_textは検索/回答とも1/1→0/1。
thresholdは検索0/1・回答品質1/1のまま、no_match/no_memory違反が残ります。
検索top_oneは10→1、no_matchは11→8。回答dispatchは10→1、no_memoryは11→8。
今回の比較はモデル・量子化・pooling・次元・閾値の組合せの変更です。単独要因の効果とは主張しません。
前回と今回の合否基準は同じで、入力版の差は承認済み偽vectorのみです。
**検索・回答ともFAIL、品質未受入**です。ADR 0024のユーザー決定4により正確なFAIL記録で#132の測定作業は完了できます。
近い型の無関係質問への対処はユーザー決定3に従い後続Epicです。今回の62件を調整用89件の内訳と同一視しません。

## 停止確認と公開report検査
同じCompose環境変数で `docker compose -f compose.llamacpp.yml stop embedding`、続けて `docker stop digital-souls-core-llamacpp` を実行しました。

```text
/digital-souls-core-llamacpp-bge-m3 exited 2026-10-10T10:43:07.604537303Z
/digital-souls-core-llamacpp exited 2026-10-10T10:43:08.075807152Z
/digital-souls-core-llamacpp-embed exited 2026-10-10T02:57:07.445761642Z
```

docker ps -aとinspectによるexited確認: 2026-10-10T10:43:26Z。nvidia-smiでモデル占有分のVRAM解放と推論process表示なしを確認しました。起動前3118 MiBに対し停止後は3435 MiB（+317 MiB）で、表示などを含むhost全体の値です。bge-m3コンテナは削除せずexitedのまま保持し、旧nomicもexitedです。
検索reportはEvaluationReport / validate_reportで再採点し、回答は各runのraw独立再採点と統合後の分類/ゲート再集計を照合しました。
全キー/文字列値の禁止項目・私有path検査、入力本文/query/fixture回答全文の混入検査、非ASCII byteなしを確認しました。
公開reportはID・score・boolean・固定診断分類だけで、goldをモデル入力へ渡していません。

| 公開report | SHA-256 |
| --- | --- |
| [2026-10-10-semantic-real-model-retrieval-bge-m3.json](2026-10-10-semantic-real-model-retrieval-bge-m3.json) | `6ce78d4305eac1ac6a9c182616d550758916d66c4cd4b883567df4a777c9d794` |
| [2026-10-10-semantic-real-model-answer-bge-m3.json](2026-10-10-semantic-real-model-answer-bge-m3.json) | `1f50715f2b1a4ecb9eb26c7690f5fe340cc97bb65c38a5bd86a77e39b87d446d` |

## 品質ゲート

文書更新後、固定toolchain・TMPDIR=/dev/shmで実行しました。結果は次のとおりです。

| コマンド | 結果 |
| --- | --- |
| uv sync --locked | PASS |
| uv lock --check | PASS |
| uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py tools/prepare-semantic-tuning-supplements.py tools/evaluate-semantic-embedding-candidates.py evals/semantic/provider.py | PASS |
| uv run --no-sync ruff format --check（上記CI対象） | PASS、120 files |
| uv run --no-sync mypy | PASS、120 source files |
| uv run --no-sync pytest -m ut -q | PASS、743件 |
| uv run --no-sync pytest -m it1 -q | PASS、592件 |
| uv build --no-build-isolation | PASS、sdist / wheel |
| CI手順のuv export --frozen・独立venvへのhash固定runtime/wheel install・python -I import | PASS |
| bash tools/test-postgres.sh | PASS、412件、153.57秒 |
| (cd evals/semantic && npm ci --no-audit --no-fund) | PASS、575 packages |
| node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs | PASS、59件 |
| node tools/check-docs.mjs | PASS（新規証跡/reportをstage後に確認） |
| git diff --check / git diff --cached --check | PASS |
| bash -n（CI対象5 shell scripts、起動scriptはsyntax検査のみ） | PASS |
| bash tools/evaluate-semantic-retrieval.sh --runs 3 | PASS、偽embedding 62×3、各26分類100%、閾値0.52・同等帯全順序一致 |
| bash tools/evaluate-semantic-answer.sh --runs 3 | PASS、回答fixture 62×3、各26分類100% |
| 公開report再採点・集計/manifest照合・ASCII/本文混入検査 | PASS |

最終ゲートのFAIL / skip / xfail / xpass / TODO / cancelledは0。markerのdeselectedは対象外試験でありskipではありません。
fixtureのquality_evidence=falseで、実モデル品質のPASSとはしません。全コマンドは30分以内に完了しました。
既存Starlette/Pydantic警告とnpm deprecated警告は残ります。GitHub CIは **NOT RUN**。

## 文書同期・制約・NOT RUN

SPEC §2.4 / §3.2末尾 / §5の3、評価READMEの再評価段落、評価手順の結果・bge-m3 profile・起動/停止方法を同期しました。
前回証跡/report、62ケース/gold、プロンプト、キャラクター、ADR、検索実装/設定は変更していません。

- 実モデル品質は **FAIL**。分類器はsyntheticで、分類器の実モデル品質は **NOT RUN**。
- backend_identity_verified=false。GGUF・image・health/alias照合は接続backend真正性の独立検証ではありません。
- 回答はNFKC→casefoldの部分文字列AND of ORs。複合語・否定・時間関係の完全な意味判定ではありません。
- LLM judge・出典表記・回答言語の採点はなし。生回答は保持せず、語句出力そのものの比較は **NOT RUN**。
- 合成正本の直接登録です。形成・保存判定・再生成・TOUCH、実運用DB版6適用、形成〜利用IT2/STは **NOT RUN**。
- 私的corpus・人格品質・prompt injection耐性・性能/資源上限の受入は **NOT RUN**。
- 新しい有料契約・資格情報の取得はありません。既知の起動script問題の修正は範囲外です。
- GitHub CI・push・PR・mergeは **NOT RUN**（実装担当の依頼で禁止）。監督のレビューが残ります。
