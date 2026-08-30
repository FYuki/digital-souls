# セルフホストLiveKit

devでは追跡対象外の`infra/livekit/.env`へ`LIVEKIT_KEYS=<key>: <secret>`を設定してから、`docker compose -f infra/livekit/compose.yaml up -d`を実行する。key/secretは`livekit-server generate-keys`等で生成し、repositoryへ保存しない。API/WebSocketはloopbackの7880/TCP、ICEは7881/TCPと7882/UDPを使う。

dogfoodでは`bootstrap.sh`が`infra/dogfood/templates/livekit.yaml`へdogfood専用key／secretを埋め込み、`/etc/digital-souls/livekit.yaml`へ配置する。`digital-souls-livekit.service`がhost networkのdogfood専用Compose stackを起動・停止し、`digital-souls-dogfood.target`がapplicationより先にLiveKit readinessを待つ。手動で別のLiveKit Serverを同じportへ起動しない。TURN、Redis、TLSはこの初期構成では使用しない。
