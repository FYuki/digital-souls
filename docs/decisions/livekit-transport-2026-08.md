# LiveKit transport基盤

## 状態

ACTIVE。Issue #113で実装した1 user + 1 characterのLiveKit transport基盤と、
Wave 3でLiveKitを正式な音声経路として拡張する方針を定める。

## 決定

LiveKit Cloudではなくself-host LiveKitを採用する。Roomとvoice sessionは1対1とし、Room名は`voice-{session_id}`、identityはuserが`user-{session_id}`、characterが`character-{character_id}-{session_id}`とする。Room SID、Participant SID、Track IDとmetadataのmappingはtransport adapter内のみに保持し、Conversation Coreへ渡さない。

character runtimeはFastAPI process内の独立`asyncio` taskとする。session、outbox、mapping、音声byteは永続化せず、process restart後は旧sessionではなく新sessionを開始する。

Core eventは既存schemaで検証し、payloadを単一application topicで配送する。ACKは全control eventを対象とし、session・方向ごとのoutboxを256 eventかつ1 MiBへ制限する。未ACKは1秒、2秒、4秒で再送し、尽きた場合はtransport unavailableとする。再接続時はauthoritative stateと確定済みterminal outcomeだけを再同期する。

Issue #113で追加した`/voice/livekit`は基盤検証用の一時入口である。Wave 3の後続実装は
この経路だけに機能を積み上げるのではなく、通常の会話・conversation UIへLiveKit sessionを
直接統合する。既存WebSocket音声pipelineは移行前baselineとして凍結し、継続listening、
streaming、barge-in、再接続等のWave 3機能を追加しない。完成後に別工程でdefault transportを
切り替える計画は設けず、Frontend統合の時点からLiveKitを正式経路として扱う。

通常UIはsession、input、response、playbackを独立表示する。`/voice/livekit`はdev serverとLiveKit integrationだけで利用し、production buildでは製品入口として公開しない。音声session中にtextを送信した場合は、active音声responseと再生を停止して音声sessionを終了した後、既存HTTP text chat経路で送る。同一LiveKit session内のtyped textは将来範囲とする。

## 応答生成と出力完了の境界

LiveKitのLLM／TTS生成終了だけでは、Coreの応答をCOMPLETEDにしない。transport非依存の`ResponseCompletionPort`を挟み、LiveKit adapterが残りPCMを送出して総sample数をブラウザへ通知し、全packetの出力時計通過を確認してからCoreを完了させる。CoreへRoom SID・Track ID・PCM配送方式は渡さない。この待機中も応答はIN_PROGRESSで、cancel・disconnect・session終了により処理を中断できる。

`playback_completed`の途中prefix通知は維持し、全出力確認だけに`response_finished: true`を付ける。全出力時は同じ応答の連続したlogical metadataの総sample数と送信元input sample数を照合する。途中prefixが既に最終sequenceに達していても全出力通知を別eventとして一度送り、通常のcontrol outboxでACK・再送を扱う。Backendは待機中の応答IDと最終sequenceが一致する全出力確認だけを受理する。

残りPCMの送出後、全出力確認が10秒以内に来なければ応答を失敗させ、待機を解放する。生成中・配送中・確認待ち中のcancelはCoreの通常のキャンセルとし、COMPLETEDからCANCELLEDへのterminal状態の書換えは行わない。遅れて届く`playback_stopped`も対象response IDに限定して音声を止める。この契約を使うFrontendとBackendは同じ変更を含む版を組み合わせる。

## 運用制約

devは7880/TCP、7881/TCP、7882/UDP、dogfoodは17880/TCP、17881/TCP、17882/UDPを使う。host networkと単一UDP muxを使い、TURN、Redis、TLS、固定`node_ip`は初期範囲に含めない。

実LiveKit Backend/Browser suiteはUDP・WebRTCと実serverを必要とするためCI外とし、`npm run test:integration:livekit`で明示実行する。

実行前に`LIVEKIT_TEST_BACKEND_URL`、`LIVEKIT_TEST_FRONTEND_URL`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`を設定する。Backend pytestとPlaywright Chromiumの実サービス結合を同じrepository taskで順に実行する。テスト層とCI除外理由の正本は`docs/testing-policy.md`とする。

Frontendの`livekit-client`はBackend側SDKおよびself-host serverとの接続互換性を固定して検証するため、完全版`2.22.1`へ固定する。更新時は実LiveKit Backend/Browser suiteを通してから版を変更する。

将来のモバイル対応ではHTTPSだけでなく、WebRTC直接到達性、tailnet IP広告、UDP到達性、Safari secure contextを別途満たす。
