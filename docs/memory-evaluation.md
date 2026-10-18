# 本番経路による意味検索・回答評価

[ADR 0023](adr/0023-semantic-evaluation-contract.md) の固定した合成62ケースで、
検索はPython、回答はpromptfoo 0.117.2を使って測定します。
入力・gold・分類・移行対応・語句判定の限界は [評価README](../core/evals/semantic/README.md)、
本番の検索・context契約は [記憶API](memory.md) と [ADR 0022](adr/0022-memory-retrieval-from-records.md) が正本です。
[実モデル証跡](evidence/2026-10-10-semantic-real-model-evaluation-bge-m3.md) に採用したbge-m3/0.52での各3回の結果を記録しています。
各回とも検索品質/全条件53/62、回答品質61/62・全条件53/62で **FAIL**、品質は未受入です。
日英は検索・回答とも10/10、検索の無関係は3/10、長文は検索・回答とも0/1、検索の閾値は0/1です。
必須ゲートは検索top_one 1件/no_match 8件、回答dispatch 1件/no_memory 8件の違反が残り、禁止情報/lifecycle違反は0です。
前回nomic/0.54の検索品質41/62・回答品質52/62からの分類別比較も証跡へ記録しています。
fixtureの成功をモデル品質の合格にせず、実モデルのFAILも記録します。

#199の[GPT-6 Luna（Codexサブスク）回答評価](evidence/2026-10-12-semantic-luna-answer-evaluation.md)も
同じbge-m3/0.52でcacheなし3回測定済みです。各回品質61/62・全条件53/62、長文0/1、
dispatch違反1件・no_memory違反8件、forbidden/lifecycle違反0件でgemmaと失敗ケースが一致し、**FAIL・品質未受入**です。
classifier=syntheticとCLI独自の指示・包装、既存token予算移行の条件差を記録しています。回答文比較・IT2/STはNOT RUNです。

## 評価経路と固定データ

[cases.json](../core/evals/semantic/cases.json) は入力schema 1、
[expectations.json](../core/evals/semantic/expectations.json) はgold schema 2です。
goldの分類・正解ID・禁止ID・期待順序・回答事実をモデルの入力へ含めません。
モデル依存の synonym / paraphrase / cross_language / unrelated は各10件、全26分類62件です。
評価結果を見る前に固定したケース・期待値・カード・プロンプトを使い、結果に合わせて変更しません。

各ケースはnetworkなし・公開portなし・digest固定の使い捨てPostgreSQL内で隔離します。
合成履歴を公開HistoryStore操作でappendし、Episode・Fact・EpisodeFactLink・Semanticを
`MemoryRecordStore.register` へ直接登録します。private化・出典削除・Fact更新等も公開操作を使います。
本番 `MemoryRetrieval` / `MemoryContext` を通し、回答は `Inference.prepare` / `Inference.check` とProvider portを使います。
逐語データや正本IDをモデル向けcontextへ注入する別経路は追加しません。

本番設定は候補20、最大5、relevance閾値0.52、同等帯0.002です。
採用モデルは [ADR 0024](adr/0024-multilingual-memory-embedding.md) の bge-m3 Q8_0（alias `bge-m3`、CLS、1024次元、接頭辞なし）です。
従来のnomicのFAILは[前回証跡](evidence/2026-10-09-semantic-real-model-evaluation-top5.md)に保持し、
この設定での実モデル再評価は #132 で実施済みです。無関係質問への対処はADR 0024の決定に従い後続Epicで扱います。
unit vectorの二乗L2距離から `1/(1+sqrt(distance))` を求めます。
同等帯は `last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC` で並べます。
Factは独立候補ではなく、有効なFactをEpisodeへ添付します。
検索ハーネスはembedding入出力からrelevanceを独立検算し、順位付け自体は製品へ任せます。

privacyは本番scanner・LocalClassifier・PrivacyPolicyを通しますが、分類器のprovider応答だけは
合成NOT_SENSITIVEです。`classifier=synthetic` と記録し、分類器品質は評価対象外です。
合成正本の直接登録は、会話から形成・保存・検索・利用までの実環境IT2/ST受入を置き換えません。

## 準備とmode

固定toolchainはuv 0.8.22、Python 3.12.3、Node 24.19.0です。
[開発規約](../CONTRIBUTING.md) に従い、依存取得にはネットワークが必要です。
評価ツールはモデル・GPU・サーバー・資格情報を用意しません。

```sh
uv sync --locked
(cd evals/semantic && npm ci --no-audit --no-fund)
```

回答の依存はnpm lockでpromptfoo 0.117.2と内部DBのbetter-sqlite3 13.0.3を固定します。
`.npmrc` はinstall scriptを全て無効にし、同梱N-API bindingを使用します。
対応bindingがない環境はFAILであり、自動でbuild/download scriptを許可しません。

| mode | embedding / 回答 | 品質証拠 |
| --- | --- | --- |
| `fixture`（既定） | 固定偽vector / 独立した回答fixtureをProvider portで返すfake | `quality_evidence=false`。道具の検証だけ |
| `local_model` | 明示したLocalEmbedding / llama.cpp Chat profile | 実モデルの合成セット測定。品質合格やbackend真正性とは別 |

検索はprofile指定時だけlocal_model、回答はmode・実行flag・profileを全て明示したときだけlocal_modelです。
回答のflag/profile不足はNOT RUN（exit 3）。無効profile・通信・不正応答・実行errorはFAILです。
fixtureへfallbackしません。検索local_modelでもembedding実呼出しが0ならquality_evidence=falseです。

## cacheなし3回の実行

fixtureでのハーネス検証:

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 --output /dev/shm/retrieval-fixture.json
bash tools/evaluate-semantic-answer.sh --runs 3 --output /dev/shm/answer-fixture.json
```

承認された既存ローカルモデルでの測定:

依存の準備後、文書や証跡を書き始める前のcleanなcommitで実行し、reportのcommit.sha / dirty=falseを確認します。

```sh
bash tools/evaluate-semantic-retrieval.sh --runs 3 \
  --profile /absolute/private/embedding.local.json --output /absolute/private/retrieval.json
# 1回ずつ3コマンドで直列実行し、各回のoutputを分ける
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model --runs 1 \
  --profile /absolute/private/answer.local.json --output /absolute/private/answer-run-1.json
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model --runs 1 \
  --profile /absolute/private/answer.local.json --output /absolute/private/answer-run-2.json
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model --runs 1 \
  --profile /absolute/private/answer.local.json --output /absolute/private/answer-run-3.json
```

検索はvector cacheなしで毎回呼び直します。回答はpromptfoo `--no-cache` とcache無効環境を使い、
結果側でもcached=falseを検証します。各runは再試行なしで別promptfooプロセスを直列実行します。
全体が品質FAILでも3回の結果を残します。実行error・不完全reportは品質不合格と区別してFAILです。
長い処理は1コマンド30分以内へ分割します。#132は検索3回を1コマンド、回答を1回ずつ3コマンドで完走し、
同じmanifestと全ケースを確認した回答reportを既存 `finalizeReport` で3回に統合しました。
終了値1でも完走した品質FAILなら次の回を実行し、回の欠落をPASSにしません。

## 私有profileとサーバー

profileはgit管理外の絶対パス（0700の私有ディレクトリ、0600のファイル）へ置きます。
[embedding例](../core/examples/embedding.example.json) と
[回答例](../core/evals/semantic/answer-profile.example.json) は無効な例であり、実サービスの設定ではありません。

`LocalEmbeddingProfile` はprofile_id・alias・GGUF SHA-256のmodel_digest・実dimensions・
loopbackのapi_base・timeout（最大15秒）・enabled=trueを明示します。
`AnswerProfile` はトップレベルenabled=true、上記embedding、chatを持ちます。
chatは `transport=llamacpp_chat`、`model=openai/gemma4-12b`、loopback api_base、
external_send_allowed=true、timeoutを設定します。chatのProfileにmodel_digestフィールドはないため、
回答GGUFのSHA-256は証跡で別に記録します。資格情報を要する経路へfallbackしません。

bge-m3用embedding profileは `model=bge-m3`、
`model_digest=aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`、
`dimensions=1024`、`api_base=http://127.0.0.1:18082/v1`、timeout=15秒、接頭辞なしです。
例を私有 `embedding.local.json` へコピーしてenabled=trueにし、回答例を `answer.local.json` へコピーして
トップレベルenabled・embedding.enabled・chat.external_send_allowedをtrueにします。
chatは `api_base=http://127.0.0.1:18081/v1`、timeout=60秒です。既定無効の例をそのまま実行しません。

起動前にGPUのVRAM・他の推論処理を確認し、余裕がなければ起動しません。
既存コンテナの起動手順・主要設定は [llama.cpp運用](llamacpp-operations.md) を参照します。
health・`/v1/models` alias・GGUF digest・image digest・build・portを確認します。
bge-m3 のpoolingは `cls`、ctx/batch/ubatchは2048、parallelは1です。ubatchは最長入力のtoken数以上とし、
long_textの失敗があれば設定値と失敗を記録します。運用設定を変えた場合は理由を記録し3回をやり直します。
モデルの追加DL、他サービスの停止・変更、Ollamaの起動はこの評価手順に含めません。
評価後は失敗時も起動した評価コンテナを停止し、inspectのexitedを記録します。

### #132で使った既存chatとembeddingだけの起動・停止

この節は、Issue #132の再評価で実際に使った手順の記録です。当時の `tools/start-llamacpp.sh` は
Ollamaのunitが存在しない（`LoadState=not-found`）環境で起動を拒否したため、ユーザー指定の次の手順で起動しました。
現在の起動scriptはunitの不存在を許可します（Issue #142）。今後の評価では、
[llama.cpp運用](llamacpp-operations.md#手動で切り替える)の起動scriptでchatとembeddingを起動できます。
`MODEL_PATH` / `EMBEDDING_MODEL_PATH` は運用者所有の既存GGUFへ設定します。
まず `nvidia-smi`（VRAM・process）と `docker ps -a` を確認し、他の重い推論があれば起動せず待機/報告します。

```bash
export CORE_LLAMACPP_UID=$(id -u) CORE_LLAMACPP_GID=$(id -g)
export CORE_LLAMACPP_MODEL="${MODEL_PATH:?set verified gemma4 GGUF path}"
export CORE_LLAMACPP_EMBEDDING_MODEL="${EMBEDDING_MODEL_PATH:?set verified bge-m3 GGUF path}"
(
  set -euo pipefail
  sha256sum "$CORE_LLAMACPP_MODEL" "$CORE_LLAMACPP_EMBEDDING_MODEL"
  [[ $(sha256sum "$CORE_LLAMACPP_MODEL" | cut -d ' ' -f 1) == 1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606 ]]
  [[ $(sha256sum "$CORE_LLAMACPP_EMBEDDING_MODEL" | cut -d ' ' -f 1) == aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173 ]]
  [[ $(docker inspect --format '{{.State.Status}}' digital-souls-core-llamacpp-embed) == exited ]]
  [[ $(docker inspect --format '{{.State.Status}}' digital-souls-core-llamacpp) == exited ]]
  docker start digital-souls-core-llamacpp
  docker compose -f compose.llamacpp.yml up -d --no-build --pull never embedding
  for port in 18081 18082; do
    curl --noproxy '*' --fail --silent --show-error \
      --connect-timeout 1 --max-time 2 --retry 60 --retry-delay 2 \
      --retry-max-time 120 --retry-all-errors "http://127.0.0.1:$port/health"
    curl --noproxy '*' --fail --silent --show-error "http://127.0.0.1:$port/v1/models"
  done
)
```

aliasは18081がgemma4-12b、18082がbge-m3であることを確認します。chatをComposeで再作成せず、旧nomicを起動しません。

注記：上の手順は#132の再評価で使ったものです。#132のchatは2026-10-06作成の既存コンテナ（`--ctx-size 4096`）で、
**#132の結果はctx 4096のchatによるもの**です。その後[#147](evidence/2026-10-10-llamacpp-context.md)でComposeのchatはcontext 32768へ変わり、
Epic #150で既存のchatコンテナもctx 32768（build b11347）で再作成済みです（2026-10-11）。
コンテナは自動ではCompose定義に追従しないため、今後の実モデル評価でも `docker inspect` でchatの `--ctx-size` を記録し、
Compose定義と違う場合は運用者が再作成するか、差異を証跡に明記してから実行します。
評価終了時・失敗時とも、同じ環境変数で次を実行し、exitedの時刻とnvidia-smiのVRAMを記録します。
起動途中で失敗した場合も、今回起動したコンテナを停止して確認します。

```bash
docker compose -f compose.llamacpp.yml stop embedding
docker stop digital-souls-core-llamacpp
docker ps -a
docker inspect --format '{{.Name}} {{.State.Status}} {{.State.FinishedAt}}' \
  digital-souls-core-llamacpp-bge-m3 digital-souls-core-llamacpp
nvidia-smi
```

bge-m3コンテナは削除せずexitedで保持します。

## 採点と合否

| 対象 | 分類別の品質条件（各回90%以上） | 必須ゲート（各ケース全件合格） |
| --- | --- | --- |
| 検索 | relevant IDの全包含、該当なしの空結果、relevant Episodeへの必須Fact添付 | forbidden IDs/Fact不在、閾値未満混入0、正本検証、上位5件へのtop-1包含（fixtureは全順序一致も必須）、dispatch guard、context一致、no_match時の閾値以上適格候補0 |
| 回答 | grounded回答の全必須事実グループを満たす | 禁止事実不在、dispatch有効性一致と送信上位5件へのtop-1包含（無効時はID空）、送信/公開拒否と破棄、no_memoryの空context |

top-1はgoldの `expected_order` があればその先頭、なければ `relevant_ids` の先頭、空なら該当なしです。
期待値は変更しません。検索の `top_one` は両modeで必須、`expected_order` はfixtureだけに残し、
実モデルの品質ゲートには含めません。fixtureの62×3ではequivalent-band-orderの全順序を検証します。
回答は両modeで順序・余分な非禁止IDを問わず包含で判定し、top-1なしならID照合を省きます。
禁止ID・事実、valid=falseのID空、no_memoryの空context、lifecycleは維持します。

品質率は `quality_passed` の割合です。必須ゲートと分けて集計するため、分類品質100%でも
必須ゲート違反があればFAILです。全体件数の割合や3回平均で、90%未達の回・分類を相殺しません。
各1件の分類では1/1を要求します。該当なしの正常0件と、実行結果0件を区別します。

回答はNFKC→casefoldの部分文字列一致で、requiredは全グループAND・グループ内OR、
forbiddenはいずれかの候補の出現を違反とします。LLM judge・単語境界・文意解析は使いません。
複合語の誤一致や否定・時間関係を完全には判定できません。出典表記・回答言語は採点対象外です。
本番contextの検索後mutationをdispatch直前で再検証し、回答後mutationでは公開前に回答を破棄します。

欠落・重複・unknown ID・0件・実行error・非有限score・skip・cache利用・欠落assertion・
改変集計を拒否します。回答はraw exportをPythonで再採点し、metadataと照合します。
promptfooの平均scoreや終了コードだけでは合格にしません。
正常な完走のCLI終了値は全体PASS=0、品質または必須ゲートFAIL=1です。

## reportと公開証跡

| 項目 | 内容 |
| --- | --- |
| 共通manifest | commit.sha / dirty、mode、quality_evidence、classifier、embedding space、本番retrieval設定、case_version（schema・入力/gold SHA-256）、runs、passed |
| 各run | cases（ID・分類・quality_passed・gates・passed）、categories（total・passed・rate）、gates_passed・passed |
| 検索固有 | backend_identity_verified=false、embedding_call_count、取得ID・添付Fact ID・score・検証済みID・閾値以上適格候補数・dispatch/contextの観測 |
| 回答固有 | answer_model（profile_id・model・transport）、入力/カード/character/fixture SHA-256、promptfoo_version、toolchain（Node・内部DB依存版）、executions（attempts・exit_code・signal・raw_present・固定log分類）、送信ID・送信/破棄/空contextの観測 |
| 異常終了 | 回答は部分runsとfailure.stage / reasonを残す。検索の実行errorはreportを生成できない場合があり、終了値とNOT RUNの残る回を証跡へ記録する |

reportのmodel/revisionはprofileの宣言です。GGUFファイルを照合しても、接続先backendがそのモデルを
使用した真正性の独立検証にはなりません。quality_evidence=trueは品質合格を意味しません。

回答のraw export・生stdout/stderr・内部DBは専用0700一時領域へ保存し通常は終了時に削除します。
runnerは資格情報・proxy・dotenv・libpq・caller Node設定を子へ継承せず、Node通信も拒否します。
`--keep-private-artifacts` は明示診断用で、調査後にraw・生ログ・DBを削除します。
公開するのは本文・query・vector・回答全文を含まないreportだけです。公開前に構造と文字列値を確認します。

日付付き証跡には、cleanな実行commit・日時・コマンド・モデルalias/GGUF SHA-256・image digest/build・
server主要設定・起動前と評価中のGPU資源・ケース版・各回全体/分類/ゲート・失敗ケースID・終了値・
コンテナ停止確認・未検証範囲を記録し、安全なreportへリンクします。
分類器品質、形成〜利用の実接続、実運用DB適用、実環境IT2/ST、私的corpusの検索精度、
prompt injection耐性、速度・資源上限を、この合成セットの結果だけで合格としません。

## 外部回答評価（#198・#201の経路、#199の実測）

`external_model` は回答生成だけを外部 Provider へ渡す opt-in mode です。
#198 の LiteLLM/API 方式に加え、#201 は Codex CLI の既存 ChatGPT ログインを使います。
**#198・#201実装時の実送信はNOT RUN、#199でCodex方式の実送信済み**です。
[手順と無効な profile 例](../core/evals/semantic/README.md#外部モデルでの回答生成external_model)を参照してください。
profile は git 管理外の絶対パスです。API 方式のキー・接続先は環境変数名だけを指定します。
dry-run 以外では明示実行 flag または profile が欠けると NOT RUN（exit 3）。

本番の外部送信許可・privacy 認可・送信直前の検証を通し、embedding は管理 loopback、
分類器は synthetic 応答＋本番ローカル classifier/scanner/policy を維持します。

external の manifest/identity には `classifier_reason="baseline-parity-no-classifier-export"` を記録します。
比較基準の gemma4-12b 評価（[2026-10-10 証跡](evidence/2026-10-10-semantic-real-model-answer-bge-m3.json)）も synthetic で条件をそろえるためです。
分類器は外部モデルへ送らずローカルのまま維持し、実分類器での受入は Issue #128 の範囲です。
fixture/local_model の report にはこのフィールドを追加しません。

`--mode external_model --profile <絶対パス> --dry-run` は実行 flag なしで見積もります。
`--dry-run` と `--execute-external-model` の同時指定は引数エラー（FAIL、送信なし）。
`--dry-run` はローカル embedding・使い捨て PG で prepare し、送信可能件数×runs と
既存 token 推定器による入力見込みを本文なしで記録します。価格は任意 CLI 単価だけ、
出力見込みは明示した仮定だけを使います。実送信 report には provider/model/transport、
external_send、cache 無効、実行回数、commit を残し、認証情報・接続 URL・本文は出しません。
実測結果とSPECの同期は#199で記録済みです。実送信前の見込みは183呼び出し・入力202,530 token
（byte推定、CLI独自指示は除外）、サブスク内で従量課金なし・新規有料契約なしでした。

Codex 方式の profile・認証・無効化一覧は
[手順](../core/evals/semantic/README.md#codex-サブスク方式201)と
[#201 証跡](evidence/2026-10-12-codex-subscription-answer-provider.md)を参照してください。
`answer_provider="codex_subscription"` と CLI/CODEX_HOME の絶対パスを指定し、
API キー環境変数は不要・転送なしです。認証ファイルを Core が開く・複製する経路はありません。
本番の external 認可と直前検証を通ってから、空の一時 cwd で CLI を起動します。
ユーザー設定・rules・hooks・MCP・plugins・web 検索・shell・各種ツールを無効化します。
dry-run は CLI を起動せず、版は null です。実行 report は許可済み dispatch の CLI 版を照合します。
`provider="codex_subscription"` / `model="gpt-6-luna"` / `codex_cli_version` に加え、
`condition_note="codex-cli-adds-own-instructions-not-pure-api"` を記録します。
CLI 独自の指示と messages の JSON 包装による条件差です。純粋な API 条件と同一とは扱いません。
公開出力は本文・認証情報・CODEX_HOME のパスと内容を含めません。
#199ではCLI 0.162.1でLunaへ各回61件×3回送信しました。tool eventをFAILにするProviderの正常応答による
無効化の間接確認済みですが、生JSONLの直接再監査と実環境IT2/STは **NOT RUN** です。
結果は冒頭の[Luna証跡](evidence/2026-10-12-semantic-luna-answer-evaluation.md)を参照してください。
