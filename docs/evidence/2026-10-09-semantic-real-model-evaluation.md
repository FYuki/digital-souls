# 本番経路による意味検索・回答の実モデル評価

日付: 2026-10-09。対象: FYuki/digital-souls-core、`docs/115-real-model-evaluation`。
実行commit: `ad127f73a8c5bb25836dccb18079effabfb2b65c`、検索・回答とも **dirty=false**。
このcommitの固定ケース・gold・カード・プロンプト・本番設定で各3回実行した。
後続の変更は文書と本文を含まないreportのみ。ハーネス・製品コード・依存・閾値・期待値は変更していない。

正本: [Issue #115](https://github.com/FYuki/digital-souls-core/issues/115)、
[Epic #111 V1〜V7](https://github.com/FYuki/digital-souls-core/issues/111)、
[ADR 0023](../adr/0023-semantic-evaluation-contract.md)。
利用手順: [評価ガイド](../memory-evaluation.md)、[ケースREADME](../../core/evals/semantic/README.md)。
公開report: [検索](2026-10-09-semantic-real-model-retrieval.json)、
[回答](2026-10-09-semantic-real-model-answer.json)。

## 実行環境とモデル確認

Python 3.12.3、uv 0.8.22、Node 24.19.0、npm 11.17.0、promptfoo 0.117.2、
better-sqlite3 13.0.3、OpenAI SDK 2.54.0。`TMPDIR=/dev/shm`。
GPUはNVIDIA GeForce RTX 4070 Ti SUPER（16376 MiB）、Linux driver 590.57 / host driver 591.86、CUDA表示13.1。
起動前（11:18 UTC）は2770 MiB使用、GPU使用率8%、nvidia-smiの推論process表示なし。
process一覧でも他のllama / Ollama / Whisper / Irodori推論は検出しなかった。
Ollamaはinactive、両評価コンテナはexited、既存LiveKitは稼働中だった。

既存GGUFのSHA-256をローカルファイルから計算し、GemmaはADR 0003と照合した。
新モデルDL・image取得・資格情報利用は行っていない。

| 用途 | alias / GGUF | SHA-256 | loopback port |
| --- | --- | --- | --- |
| embedding | nomic-embed-text / nomic-embed-text-v1.5.f16 | `970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6` | 18082 |
| 回答 | gemma4-12b / Gemma 4 12B Q4_K_M | `1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606` | 18081 |

両コンテナのimage digestは
`sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
`docker exec ... /app/llama-server --version` は build **11347** / commit `5fc4f3c8c`、
固定revisionは `5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd`（GNU 14.2.0、Linux x86_64）。

`docker start digital-souls-core-llamacpp-embed digital-souls-core-llamacpp` で既存コンテナを起動し、
両portの `/health` がok、`/v1/models` のidが上記aliasと一致することを確認した。
nomicのmodel metadataは768次元、合成probeの `/v1/embeddings` 応答も実測768次元だった。
Gemmaのmetadataは11907350576 parameters / Q4_K - Mediumだった。

| サーバー | 主要設定（既存値のまま） |
| --- | --- |
| embedding | container内8080、embeddings、pooling=mean、ctx/batch/ubatch=2048、parallel=1、gpu-layers=99、threads=8、no-webui |
| 回答 | container内8080、ctx=4096、parallel=1、gpu-layers=99、fit=off、KV=f16/f16、flash-attn=on、threads/threads-batch=8、cache-ram=0、no-warmup、reasoning=off、enable_thinking=false、no-webui、no-agent |

両方の公開bindは127.0.0.1のみ、GGUFはread-only mount。
起動後VRAMは11265 MiB、検索中11314 MiB（GPU 41%）、回答中は11336〜11445 MiB、
別の回答中sampleでGPU 91%。約11.0〜11.2 GiB使用で、約4.5〜4.7 GiBの空きがあった。
これはsnapshotの概数であり、連続測定したpeakではない。
Ollama・Whisper・Irodori・LiveKit等のサービスの停止・変更・起動はしていない。

profileはリポジトリ外の0700ディレクトリ、各0600ファイルに保存した。
embeddingはenabled=true、nomic alias・上記digest・768次元、loopback `/v1`、timeout=15秒。
回答はAnswerProfile.enabled=true / embedding.enabled=true / chat.external_send_allowed=true、
chatのmodel=openai/gemma4-12b、transport=llamacpp_chat、loopback `/v1`、timeout=60秒。
profile_idは `nomic-v15-f16-115` / `gemma4-12b-1278394b-115`。
chatのProfileにはmodel_digestフィールドがないため、回答GGUF digestは本証跡に別記した。
温度・seed・生成上限を追加せず、固定providerのpayloadとサーバー既定を使った。

## 固定データとコマンド

本番検索設定: candidate_pool_size=20、max_retrieved_memories=5、relevance_threshold=0.54、equivalence_margin=0.002。
input schema=1、gold schema=2、合成62ケース・26分類。classifier=synthetic。

| 固定ファイル | SHA-256 |
| --- | --- |
| `evals/semantic/cases.json` | `9f703b32ed79ac997320409dd38142324095084ec93164c60ea6b5aca9e50122` |
| `evals/semantic/expectations.json` | `0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4` |
| `evals/semantic/evaluation.card.json` | `17093470ae0139424d127bfc3308057b9bcface15dd70d0cf8bc45728c4bf3fa` |
| `evals/semantic/characters.json` | `0cfdd7aec0e6a69ecf601ab0ab16ad3696e53b987cf022a43c73ca09d533726f` |

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --profile <private-embedding-profile> --output <private-retrieval-report>
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model --runs 3 \
  --profile <private-answer-profile> --output <private-answer-report>
```

実際にはgit管理外の絶対pathを指定。profileと本文を含むrawは公開しない。
検索にはvector cacheなし、回答にはpromptfoo --no-cache / cache無効とcached=false検証を使用した。
再試行なし、server設定変更なし。long_textはubatch=2048のまま検索・回答各3回とも完走した。
本番contextから回答を生成し、goldをモデルへ渡していない。

| 評価 | 開始UTC | 終了UTC | wrapper終了値 |
| --- | --- | --- | --- |
| retrieval | 2026-10-09T11:19:41.998799+00:00 | 2026-10-09T11:20:20.588259+00:00 | 1（FAIL） |
| answer | 2026-10-09T11:20:20.588329+00:00 | 2026-10-09T11:23:13.019824+00:00 | 1（FAIL） |

回答の各promptfooプロセスはexit 100（決定論的assertion FAIL）、signalなし、raw_present=true、attempts=1。
report gateの再採点まで完了し、provider error / 実行error / 欠落runはない。
stdoutの固定分類はunclassified、stderrはempty。生ログ・raw export・内部DBはrunnerが削除した。

## 各回の全体結果

品質条件の件数はquality_passed、全条件の件数は品質と全必須ゲートを満たしたcase.passed。
全体割合は参考値であり、判定は各回・各分類90%以上と必須ゲート全件合格。

| 評価 | 回 | 完走 | 品質条件 | 全条件 | 必須ゲート違反ケース | 合否 |
| --- | --- | --- | --- | --- | --- | --- |
| 検索 | 1 | 62/62 | 41/62（66.13%） | 40/62（64.52%） | 12 | FAIL |
| 検索 | 2 | 62/62 | 41/62（66.13%） | 40/62（64.52%） | 12 | FAIL |
| 検索 | 3 | 62/62 | 41/62（66.13%） | 40/62（64.52%） | 12 | FAIL |
| 回答 | 1 | 62/62 | 52/62（83.87%） | 17/62（27.42%） | 45 | FAIL |
| 回答 | 2 | 62/62 | 52/62（83.87%） | 17/62（27.42%） | 45 | FAIL |
| 回答 | 3 | 62/62 | 52/62（83.87%） | 17/62（27.42%） | 45 | FAIL |

検索のembedding呼出しは183（各回61ケース、epoch_changeは候補0件で呼出しなし）。
検索・回答のquality_evidence=trueは「実モデルで測定した」の意味であり品質PASSではない。
前回のretrieval17/20・answer16/20は今回の判定へ使用していない。

## 分類別品質条件

表は各回のquality_passed / total。必須ゲートはこの割合に含まれない。
回答のunrelated / thresholdはgroundedではないためquality_passed=trueでも、空context等のゲートでFAILとなる。

| 分類 | 検索1 | 検索2 | 検索3 | 回答1 | 回答2 | 回答3 |
| --- | --- | --- | --- | --- | --- | --- |
| synonym | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| paraphrase | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| cross_language | 0/10 | 0/10 | 0/10 | 0/10 | 0/10 | 0/10 |
| unrelated | 0/10 | 0/10 | 0/10 | 10/10 | 10/10 | 10/10 |
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
| long_text | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
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

検索はcross_language=0%、unrelated=0%、threshold=0%が品質基準未達。
回答はcross_language=0%が未達。ほかの分類品質は各回100%でも、必須ゲート違反は相殺しない。

## 必須ゲート

違反件数（同一ケースで複数の違反を重複計上）。ゲート名はreportのキー。

| 評価 / ゲート | 回1 | 回2 | 回3 |
| --- | --- | --- | --- |
| 検索 / forbidden_ids | 0 | 0 | 0 |
| 検索 / threshold | 0 | 0 | 0 |
| 検索 / verified_records | 0 | 0 | 0 |
| 検索 / expected_order | 1 | 1 | 1 |
| 検索 / dispatch | 0 | 0 | 0 |
| 検索 / context | 0 | 0 | 0 |
| 検索 / no_match | 11 | 11 | 11 |
| 回答 / forbidden | 0 | 0 | 0 |
| 回答 / dispatch | 45 | 45 | 45 |
| 回答 / lifecycle | 0 | 0 | 0 |
| 回答 / no_memory | 11 | 11 | 11 |

検索のno_match違反は無関係10件とbelow-thresholdで、閾値以上の適格候補が残った。
thresholdゲート（返却scoreが0.54以上）は違反0であり、「該当なしにすべきケースの候補が
実vectorで閾値以上になる」ことと「閾値未満を混入する」ことを区別する。
equivalent-band-orderではrelevant IDを取得して品質条件は満たしたが、goldの全順序とは不一致。
実vectorがgoldを固定した偽vectorと同じ距離・同等帯になるとは保証しない。
この結果だけで本番の同等帯sort実装の不具合と断定せず、契約どおりFAILと記録する。

回答のdispatchゲートは有効性に加えてgoldのmemory_idsと取得IDの**順序を含む完全一致**を要求する。
関連する正解が取得できても余分な候補や順序差でFAILとなる。無関係・thresholdは空contextにも違反する。
禁止事実・lifecycleゲート違反は0。dispatchゲートFAILを一律にprivacy違反・無効context送信と同一視しない。
生回答を保存していないため、以下は機械採点で観測した条件だけを述べる。

## 失敗したケースID

各runで品質・失敗ゲートの組合せは同一（行順は異なる場合がある）。
「品質」は必須事実/正解集合等の品質条件未達、「—」はその評価で全条件PASS。
本文・query・回答全文は含めない。

| case ID | 分類 | 検索の失敗条件（全3回） | 回答の失敗条件（全3回） |
| --- | --- | --- | --- |
| synonym-warm-drink-ja | synonym | — | dispatch |
| paraphrase-weekend-ja | paraphrase | — | dispatch |
| cross-language-herb | cross_language | 品質 | 品質, dispatch |
| unrelated-observatory | unrelated | 品質, no_match | dispatch, no_memory |
| negated-coffee-preference | negation | — | dispatch |
| synonym-02 | synonym | — | dispatch |
| synonym-03 | synonym | — | dispatch |
| synonym-04 | synonym | — | dispatch |
| synonym-05 | synonym | — | dispatch |
| synonym-06 | synonym | — | dispatch |
| synonym-07 | synonym | — | dispatch |
| synonym-08 | synonym | — | dispatch |
| synonym-09 | synonym | — | dispatch |
| synonym-10 | synonym | — | dispatch |
| paraphrase-02 | paraphrase | — | dispatch |
| paraphrase-03 | paraphrase | — | dispatch |
| paraphrase-04 | paraphrase | — | dispatch |
| paraphrase-05 | paraphrase | — | dispatch |
| paraphrase-06 | paraphrase | — | dispatch |
| paraphrase-07 | paraphrase | — | dispatch |
| paraphrase-08 | paraphrase | — | dispatch |
| paraphrase-09 | paraphrase | — | dispatch |
| paraphrase-10 | paraphrase | — | dispatch |
| cross-language-02 | cross_language | 品質 | 品質, dispatch |
| cross-language-03 | cross_language | 品質 | 品質, dispatch |
| cross-language-04 | cross_language | 品質 | 品質, dispatch |
| cross-language-05 | cross_language | 品質 | 品質, dispatch |
| cross-language-06 | cross_language | 品質 | 品質, dispatch |
| cross-language-07 | cross_language | 品質 | 品質, dispatch |
| cross-language-08 | cross_language | 品質 | 品質, dispatch |
| cross-language-09 | cross_language | 品質 | 品質, dispatch |
| cross-language-10 | cross_language | 品質 | 品質, dispatch |
| unrelated-02 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-03 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-04 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-05 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-06 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-07 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-08 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-09 | unrelated | 品質, no_match | dispatch, no_memory |
| unrelated-10 | unrelated | 品質, no_match | dispatch, no_memory |
| direct-semantic | semantic_direct | — | dispatch |
| derived-semantic | semantic_derived | — | dispatch |
| equivalent-band-order | equivalent_order | expected_order | dispatch |
| below-threshold | threshold | 品質, no_match | dispatch, no_memory |

## コンテナ停止

評価wrapperはfinallyで、品質FAIL時も両コンテナへdocker stopを実行した。
コンテナ・image・GGUFは削除していない。

```text
/digital-souls-core-llamacpp-embed exited 2026-10-09T11:23:13.29515349Z
/digital-souls-core-llamacpp exited 2026-10-09T11:23:13.279552653Z
```

後続のdocker inspectでも両方exitedを確認した。Ollamaはinactiveのまま、既存LiveKitは稼働中。

## 公開reportの確認

検索reportはEvaluationReport / validate_report、回答reportはraw検査・Python再採点と
metadata照合を経た。各3run×62ケース、dirty=false、同じmanifestを確認した。
公開コピー前に全JSONキーと文字列値を確認し、本文・query・normalized_text・answer・output・vector・
資格情報・私有profile pathがないことと、ケースの入力本文が文字列値に入っていないことを検査した。
識別alias・digest・port・profile_id・ID・score・boolean・固定診断分類のみを公開する。

| 公開report | SHA-256 |
| --- | --- |
| [2026-10-09-semantic-real-model-retrieval.json](2026-10-09-semantic-real-model-retrieval.json) | `e27df654647a0704641eb9c05def06a5e0e350a750664d4d7bf11c28f41e8d55` |
| [2026-10-09-semantic-real-model-answer.json](2026-10-09-semantic-real-model-answer.json) | `50b406e5c523c42ab1fbe1c0d1c325b4ebc14c97e5fdb2573da479086755ab86` |

## 文書同期

- docs/memory-evaluation.mdを全面更新。旧候補fixture・旧指標の説明を削除し、本番登録・Python検索・promptfoo回答・mode/profile・report・採点・制約・証跡手順を記載。
- README / SPEC §2.4・§3.2・§5 / docs/memory.md / evals/semantic/README.mdへ「実モデル評価各3回実施済み、検索・回答FAIL、品質未受入」と証跡参照を反映。
- docs/semantic-postgresql-integration.mdの後続品質・配備確認を現状に同期。CONTEXTの語彙・概念に変更は不要で、参照先を確認。
- SPECの状態にIssue/PR番号を使わない。ADR本文・評価コード・製品実装・入力/gold・カード・依存は変更しない。

## 品質ゲート

指定toolchain、TMPDIR=/dev/shmで自分で実行。コード変更なし、起点の試験件数から増減0。

| コマンド | 結果 |
| --- | --- |
| uv sync --locked | PASS |
| uv lock --check | PASS |
| uv run --no-sync ruff check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS |
| uv run --no-sync ruff format --check src tests tools/evaluate-semantic-retrieval.py evals/semantic/provider.py | PASS、112 files |
| uv run --no-sync mypy | PASS、112 source files |
| uv run --no-sync pytest -m ut -q | PASS、608件 |
| uv run --no-sync pytest -m it1 -q | PASS、590件 |
| uv build --no-build-isolation | PASS、sdist / wheel |
| bash tools/test-postgres.sh | PASS、410件（79.77秒） |
| (cd evals/semantic && npm ci --no-audit --no-fund) | PASS、575 packages、install scripts無効 |
| node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs | PASS、58件 |
| node tools/check-docs.mjs | PASS（新規report・証跡をstage後も確認） |
| git diff --check / git diff --cached --check | PASS |

テストのskip / xfail / xpass / TODO / cancelは0。
既存Starlette / Pydanticの警告とnpm deprecatedの警告あり。
この合成DB試験は実運用DBの合格ではない。GitHub CIはpush禁止のため **NOT RUN**。

## 制約と未解決事項

- 実モデル品質: **FAIL**。検索の分類未達・no_match/期待順序ゲート、回答の日英未達・dispatch/空contextゲートが残る。
- classifier=syntheticのため分類器の実モデル品質は対象外、**NOT RUN**。
- model digestはprofileの宣言、GGUFのファイル照合とhealth/alias確認を行っても接続backendの真正性は独立検証していない。
- 回答は部分文字列のAND of ORs判定で、複合語・否定・時間関係の完全な意味判定ではない。LLM judge / 出典表記の採点なし。
- direct registerの合成正本であり、形成・保存判定・再生成・TOUCHの受入ではない。実運用DBのschema版6適用、形成〜利用、実環境IT2/STは **NOT RUN**。
- 私的実会話・本番corpus、性能・資源上限、人格品質、prompt injection耐性を検証していない。
- ハーネスの明らかな不具合は今回確認していない。期待順序・dispatchの厳密goldと実vectorの差を、結果に合わせて修正しない。監督による結果・文書レビューが残る。
- push / PR作成 / mergeは **NOT RUN**（担当範囲で禁止）。

## 監督レビューの注記（2026-10-09）

- 回答評価の `dispatch` ゲートは、dispatch の有効性に加えて gold の `dispatch.memory_ids` と取得IDの順序を含む完全一致を求める。gold は偽 embedding の結果に合わせて作られている。SPEC §3.2 と ADR 0023 の必須ゲート（privacy・Binding 違反0件、閾値未満の混入0件、検証できない記憶への代替0件、同等帯の並び順）と、正解が返却結果に含まれるかを測る品質の定義には、この完全一致は含まれない。実モデルでは、禁止されていない余分な候補が返るだけでこのゲートが FAIL になる（回答の45件の違反の多くはこれ）。
- 検索評価の `expected_order` ゲートも、偽 vector で作った gold の全順序を実 vector の結果と比べている。実モデルでは同等帯に入る候補が変わり得るので、「同等帯の中の並びが規則どおりか」とは別のことを測っている。
- 上の2つはゲートの定義の問題として、後続で扱う。結果を見た後なので、この評価では判定基準を変えず、FAIL のまま記録する。どちらの定義でも、検索（日英・無関係・閾値の分類が未達）と回答（日英の分類が未達）の不合格は変わらない。


## 新基準による再評価への参照（2026-10-09）

ユーザー決定により実モデルの順序完全一致を上位5件へのtop-1包含へ置き換え、
同じケース・期待値・モデル・設定で検索/回答をcacheなし各3回再実行しました。
[新基準の証跡](2026-10-09-semantic-real-model-evaluation-top5.md)を参照してください。
この証跡の旧基準による結果・reportは履歴として保持し、遡ってPASSへ変更しません。
