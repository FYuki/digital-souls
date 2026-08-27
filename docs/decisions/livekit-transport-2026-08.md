# LiveKit transport基盤

## 状態

ACTIVE。Issue #113の1 user + 1 character実験経路を定める。

## 決定

LiveKit Cloudではなくself-host LiveKitを採用する。Roomとvoice sessionは1対1とし、Room名は`voice-{session_id}`、identityはuserが`user-{session_id}`、characterが`character-{character_id}-{session_id}`とする。Room SID、Participant SID、Track IDとmetadataのmappingはtransport adapter内のみに保持し、Conversation Coreへ渡さない。

character runtimeはFastAPI process内の独立`asyncio` taskとする。session、outbox、mapping、音声byteは永続化せず、process restart後は旧sessionではなく新sessionを開始する。

Core eventは既存schemaで検証し、payloadを単一application topicで配送する。ACKは全control eventを対象とし、session・方向ごとのoutboxを256 eventかつ1 MiBへ制限する。未ACKは1秒、2秒、4秒で再送し、尽きた場合はtransport unavailableとする。再接続時はauthoritative stateと確定済みterminal outcomeだけを再同期する。

`/voice/livekit`は隔離された実験経路であり、既存WebSocket音声UIとdefault transportは変更しない。

## 運用制約

devは7880/TCP、7881/TCP、7882/UDP、dogfoodは17880/TCP、17881/TCP、17882/UDPを使う。host networkと単一UDP muxを使い、TURN、Redis、TLS、固定`node_ip`は初期範囲に含めない。

実LiveKit Backend/Browser suiteはUDP・WebRTCと実serverを必要とするためCI外とし、`npm run test:integration:livekit`で明示実行する。

実行前に`LIVEKIT_TEST_FRONTEND_URL`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`を設定する。Backend pytestとPlaywright Chromiumの実サービス結合を同じrepository taskで順に実行する。テスト層とCI除外理由の正本は`docs/testing-policy.md`とする。

Frontendの`livekit-client`はBackend側SDKおよびself-host serverとの接続互換性を固定して検証するため、完全版`2.22.1`へ固定する。更新時は実LiveKit Backend/Browser suiteを通してから版を変更する。

将来のモバイル対応ではHTTPSだけでなく、WebRTC直接到達性、tailnet IP広告、UDP到達性、Safari secure contextを別途満たす。
