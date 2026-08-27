# self-host LiveKit

devでは `docker compose -f infra/livekit/compose.yaml up -d`を実行する。API/WebSocketは7880/TCP、ICEは7881/TCPと7882/UDPを使う。`livekit.yaml`のkey/secretはローカルで生成した値へ必ず置き換え、repositoryへ実値を保存しない。

dogfoodは `infra/dogfood/templates/livekit.yaml`を `/etc/digital-souls/livekit.yaml`へ配置し、管理者がLiveKit Serverの起動とsystemd有効化を行う。TURN、Redis、TLSはこの初期構成では使用しない。

