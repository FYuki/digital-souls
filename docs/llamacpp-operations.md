# Core用llama.cppの起動とrollback

対象はUbuntu-dogfoodのローカル単一利用者。CoreはPython 3.12.3のUbuntu側でも起動できる。
WSL内のloopback到達性は環境ごとに確認する。LAN公開や共有ユーザー向け認証は提供しない。
運用者endpointをcallerへ指定させず、秘密のAPIキーも不要。

## 構成と事前確認

- [固定Compose](../core/compose.llamacpp.yml): b11347の公式CUDA imageをdigest固定。
- model GGUFだけをread-onlyマウント。chatのSHAは[ADR 0003](adr/0003-local-llamacpp-provider.md)、embeddingのSHAは[ADR 0024](adr/0024-multilingual-memory-embedding.md)の値と照合する。
- chatのaliasはgemma4-12b。GGUF chat template、thinking無効、context 32768、GPU layers 99要求、
  f16 KV、Flash Attention on、8 threads、slot 1。自動fitや他モデルfallbackは使わない。
- chatのcontextとKV型は[Issue #147の実測証跡](evidence/2026-10-10-llamacpp-context.md)に基づく。
  RTX 4070 Ti SUPERでbge-m3同時起動・合成要求各3回を確認した。OpenClaw実接続の再受入は未実施。
- embeddingは`embedding`サービスのbge-m3（下記「bge-m3 embedding サーバーへの切替」）。chatのcontextとは独立に、
  ctx/batch/ubatch 2048・CLS poolingで127.0.0.1:18082だけを公開する。
- chatのhostは127.0.0.1:18081だけを公開。container内部8080、非root、read-only、cap_drop ALL、
  no-new-privileges、memory 12g、pids 256。Web UIとAgent機能は無効。
- Docker/Toolkitは既存設定を使う。追加security設定が必要なら停止して確認する。
- 起動前にGPU余裕と他の推論を確認する。既存Whisper/Irodoriは停止・変更しない。

ユーザーが通常ユーザー所有の場所へコピーしたGGUFを指定する。保護原本をroot containerで
読み取ったり、ACLを緩めたりしない。コピーと公式imageはrollback後も保持する。
`MODEL_PATH`には手元の検証済みGGUFの絶対pathを設定する。架空の汎用例は
`/example/models/model.gguf`であり、実際の配置や実行値ではない。未設定なら以下は停止する。

```bash
export CORE_LLAMACPP_MODEL="${MODEL_PATH:?Set MODEL_PATH to the verified absolute GGUF path}"
export CORE_LLAMACPP_EMBEDDING_MODEL="${EMBEDDING_MODEL_PATH:?Set EMBEDDING_MODEL_PATH to the verified bge-m3 GGUF path}"
docker pull ghcr.io/ggml-org/llama.cpp@sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7
nvidia-smi --query-gpu=memory.used,memory.free,utilization.gpu --format=csv
```

## bge-m3 embedding サーバーへの切替

[ADR 0024](adr/0024-multilingual-memory-embedding.md) の bge-m3 Q8_0 は alias `bge-m3`、
CLS pooling、1024次元、接頭辞なしです。モデルの固定revision・SHA-256・サイズはADRを参照します。
Composeの `embedding` サービスはchatと同じ固定image・非root・read-only・cap_drop ALL・
no-new-privileges・pids 256・memory 12g・64MiB tmpfs・read-only bind（create_host_path=false）です。
#130の測定と同じctx/batch/ubatch=2048、parallel=1、gpu-layers=99、8 threads、cache-ram=0で、
Web UIとログを無効にします。公開portは `127.0.0.1:18082` のみです。

`EMBEDDING_MODEL_PATH` に運用者所有の検証済み `bge-m3-q8_0.gguf` の絶対pathを設定します。
起動scriptはchatとembeddingの両ファイルのSHA-256を確認し、不一致・未設定ならComposeを呼びません。
profile例は既定無効です。モデル・endpointを確認後、git管理外の私有profileで有効化し、
trusted起動コードへ明示注入します。保存済みvectorはなく、データ移行は不要です。

手動作成の旧nomicコンテナ `digital-souls-core-llamacpp-embed` が18082を使うため、
影響を確認した運用者は**旧コンテナを停止しexitedを確認してから**起動します。
停止・確認に失敗した場合は起動せず、旧コンテナとモデルはrollback用に保持します。
GPUの余裕を事前に確認し、同じportで二つのembeddingサーバーを起動しません。

```bash
(
  set -euo pipefail
  docker stop digital-souls-core-llamacpp-embed
  [[ $(docker inspect --format '{{.State.Status}}' digital-souls-core-llamacpp-embed) == exited ]]
  bash tools/start-llamacpp.sh
  curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 1 --max-time 2 --retry 60 --retry-delay 2 \
    --retry-max-time 120 --retry-all-errors http://127.0.0.1:18082/health
  curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:18082/v1/models
)
```

新コンテナ名は `digital-souls-core-llamacpp-bge-m3` です。alias・image digest・GGUF SHA・
1024次元を照合し、profileのembedding endpointを18082へ合わせます（chatは18081）。
実モデルの検索・回答再評価は #132 で行います。fixtureのPASSはこの動作確認の代用ではありません。
embeddingだけを旧nomicへ戻す場合は、同じ環境変数で `docker compose -f compose.llamacpp.yml stop embedding`、
新コンテナのexited確認、旧コンテナの `docker start digital-souls-core-llamacpp-embed` の順です。
profileも旧model/digest/dimensionsへ戻し、旧閾値0.54との組合せは別途配備判断します。

## 手動で切り替える

Ollamaのunitが存在する場合、停止すると既存のOllama接続クライアントは利用できなくなる。
影響を確認した利用者がUbuntu-dogfoodで次を実行する。unitは削除・無効化せず、sudoを迂回しない。
unitが存在しない（`LoadState=not-found`）場合は停止操作が不要で、起動scriptから実行する。

```bash
# Ollamaのunitが存在する場合だけ、影響を確認して停止する。
sudo systemctl stop digital-souls-ollama.service
bash tools/start-llamacpp.sh
curl --noproxy '*' --fail --silent --show-error \
  --connect-timeout 1 --max-time 2 --retry 60 --retry-delay 2 \
  --retry-max-time 120 --retry-all-errors http://127.0.0.1:18081/health
```

起動scriptは `digital-souls-ollama.service` を次の条件で確認する。
`systemctl show` の成功を必須とし、空出力・不明な状態では停止を証明できないためfail-closedで拒否する。

| LoadState | ActiveState | 判定 |
| --- | --- | --- |
| `not-found` | 照会不要 | unitが存在しないため許可 |
| `loaded` | `inactive` | 停止済みのため許可 |
| `loaded` | `active` / `activating` / `reloading` / `deactivating` / `failed` など、`inactive`以外（空出力を含む） | 拒否 |
| `masked` / `error` など、`loaded`・`not-found`以外（空出力を含む） | 照会不要 | 拒否 |
| 必要な`systemctl show`の失敗 | 出力にかかわらず | 拒否 |

root実行やchat / embedding model SHA不一致・未設定時にも拒否する。条件を満たせば両Composeサービスを起動する。
unit不存在の確認は、手動起動されたOllamaや他のGPU処理の不在を保証しない。起動前にはGPU余裕と他の推論を確認する。
停止済みという確認は起動時の条件であり、
別の管理者が後からOllamaを再開することまで防ぐ排他機構ではない。両方を同時に起動しない。
health確認はcold load中の503や接続待ちを再試行する。再試行の開始期限は120秒、
最後の試行を含め最大約122秒で失敗する。失敗時はCoreを起動せず、containerの状態を確認する。
Coreのローカル専用HTTPクライアントも環境proxyを無視し、redirectを追跡しない。

Coreの[サンプルprofile](../core/examples/characters.llamacpp.json)を同じexamples内の
`characters.llamacpp.local.json`へコピーし、運用者がexternal_send_allowedだけをtrueにする。
サンプルのままでは送信しない。旧profileは別ファイルのまま保持する。

```bash
CORE_CHARACTER_CONFIG=examples/characters.llamacpp.local.json uv run --no-sync uvicorn digital_souls_core.api:create_app --factory --host 127.0.0.1 --port 18080 --no-access-log
```

Coreの`/v1/character/completions`にはcharacter_id=miori、互換入口にはmodel=mioriを指定する。
実provider modelはopenai/gemma4-12b、返却modelはサーバーのgemma4-12bで、character aliasとは別物。
profile/api_base/token_counterの変更はCoreを再起動して反映する。
サンプルは `token_counter: "llamacpp"` を明示する。固定revisionの `/apply-template` と `/tokenize` を
同じloopbackサーバーで使うため、これらのendpointが必要となる。tool定義とtool_choiceもtemplateへ渡す。
Chat Completionsと同じサーバー側のtemplate・Jinja設定を使い、Coreからtemplateを上書きしない。
endpointの失敗時は502、timeoutは504の `token_count_failed` とし、推定へ切り替えない。
`estimate` を選ぶ場合は運用者が設定を明示して再起動する。
今回の合成試験は実モデルのtoken数やGemma templateの受入を保証しない。
実サーバーでのIT2は[計数の証跡](evidence/2026-10-11-llamacpp-token-count.md)のNOT RUN範囲に残す。

## 常駐の判断

既定restart=noではDocker/OS再起動後に自動でモデルをロードしない。
自動復帰が必要な場合は、GPU占有（今回のGemma+bge-m3実測はシステム合計最大12913 MiB）、18081待受、
Ollamaの起動順との競合を確認してからrestart policyを変更する。今回は変更しない。
`unless-stopped`だけを設定しても、既存Ollama unitとの排他や起動順は保証されない。
自動起動の調整は別途レビューする。Core自身のsystemd常駐も今回は追加しない。

## rollback

今回起動したCoreプロセスを停止し、先にllamaとembeddingコンテナを停止する。
repoルートで実行し、Composeには起動時と同じ検証済み絶対モデルpath・uid/gidを指定する。
新しいシェルでも`MODEL_PATH`を起動時と同じ絶対pathに設定する。個人固有の値は公開しない。
以下のOllama再開手順は、旧unitを保持している環境が対象です。unitが存在しない環境では再開操作は行いません。
停止コマンドの成功とcontainer状態がexitedであることを確認できた場合だけOllamaを再開する。
停止失敗・inspect失敗・稼働中・状態不明では再開せず、原因を確認する。

```bash
(
  set -euo pipefail
  export CORE_LLAMACPP_MODEL="${MODEL_PATH:?Set MODEL_PATH to the verified absolute GGUF path}"
  export CORE_LLAMACPP_EMBEDDING_MODEL="${EMBEDDING_MODEL_PATH:?Set EMBEDDING_MODEL_PATH to the verified bge-m3 GGUF path}"
  export CORE_LLAMACPP_UID=$(id -u) CORE_LLAMACPP_GID=$(id -g)
  if docker compose -f compose.llamacpp.yml stop llama embedding &&
     embedding_state=$(docker inspect --format '{{.State.Status}}' digital-souls-core-llamacpp-bge-m3) &&
     [[ "$embedding_state" == exited ]] &&
     state=$(docker inspect --format '{{.State.Status}}' digital-souls-core-llamacpp) &&
     [[ "$state" == exited ]]; then
    sudo systemctl start digital-souls-ollama.service
  else
    echo 'llama.cppの停止を確認できないため、Ollamaを再開しません。' >&2
    exit 1
  fi
)
```

その後Coreを保持してある旧Ollama profileで再起動する。旧API/model/unit、ユーザーコピー、
imageは削除しない。必要なら停止した今回のcontainerだけを`docker compose ... rm llama`で除去する。
他projectへのdown、prune、volume削除は行わない。

## 他クライアントの移行対象

| 対象 | 今回 | 後続の確認箇所 |
| --- | --- | --- |
| Core | profileとlocal Chat契約を追加 | 起動時CORE_CHARACTER_CONFIGと運用者api_base |
| Core利用Agent/UI | HTTP契約は維持 | Core base URL、character alias、stream error処理 |
| Ollama native APIを直接使う既存PoC/Agent | 接続先は変更しない | /api/chatとOpenAI Chatの差、tools履歴、think設定、stream形式。URL置換だけでは移行不可 |
| 既存OpenAI互換クライアント | 接続先は変更しない | Core経由かllama直接か、model alias、未対応parameter。設定場所は未調査 |
| Whisper/Irodori | 推論先の移行対象外、変更なし | GPU競合のみ運用上確認 |

他projectの秘密設定や会話ログは棚卸しのために読まない。具体的な他クライアント設定変更は
対象が指定された段階で個別に検証する。
