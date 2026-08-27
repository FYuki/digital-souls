# セルフホストLiveKit

devでは追跡対象外の`infra/livekit/.env`へ`LIVEKIT_KEYS=<key>: <secret>`を設定してから、`docker compose -f infra/livekit/compose.yaml up -d`を実行する。key/secretは`livekit-server generate-keys`等で生成し、repositoryへ保存しない。API/WebSocketはloopbackの7880/TCP、ICEは7881/TCPと7882/UDPを使う。

dogfoodは `infra/dogfood/templates/livekit.yaml`を `/etc/digital-souls/livekit.yaml`へ配置し、管理者がLiveKit Serverの起動とsystemd有効化を行う。TURN、Redis、TLSはこの初期構成では使用しない。
