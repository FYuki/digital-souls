# 音声品質の障害注入環境

#150の再接続検証専用。通常devの7880番台、dogfoodの17880番台とは別の19880番台を使う。containerは専用Docker bridgeへ接続し、hostへ公開するportをloopbackに限定する。通常dev・dogfoodへこの設定を反映しない。

LiveKit v1.9.7の[公式設定例](https://github.com/livekit/livekit/blob/v1.9.7/config-sample.yaml)に従い、`use_external_ip: false`と`node_ip: 127.0.0.1`を指定する。BrowserとBackendが同じ開発hostから接続する検証用の構成であり、別端末からの接続を想定しない。

## 起動と障害注入

専用のAPI key/secretを`LIVEKIT_KEYS`へ設定し、秘密値をcommand logやartifactへ保存しない。通常dev・dogfoodとは別のCompose project名を指定する。

```bash
docker compose -p ds-voice-quality-fault -f infra/voice-quality/compose.yaml up -d
python3 scripts/voice_quality/network_fault.py \
  --container ds-voice-quality-fault-livekit-1 --duration-ms 2000
```

スクリプトは専用purpose labelとtest labelを持つ稼働container、専用labelを持つbridge、対象containerだけが接続されていることを確認する。host networkや共有networkは拒否する。対象の接続だけを切り離し、`finally`で元のnetwork・IPへ戻す。host全体のfirewall変更やサービス再起動は行わない。

## 計測境界

出力は切断、link復旧、signaling TCPへの接続成功という別々のeventである。timestampは障害runnerのmonotonic clockであり、Browserの`performance.now()`と直接減算しない。

TCP接続成功はcontrolとaudio双方の利用可能を意味しない。再接続合否には、実control往復と実音声frameの復旧を別に観測し、network復旧時刻とのclock対応を確認する必要がある。新規TCP接続だけの結果をreconnectの成功率や復旧latencyへ流用しない。

終了時はこのprojectだけを片付ける。

```bash
docker compose -p ds-voice-quality-fault -f infra/voice-quality/compose.yaml down
```

## 2026-09-07の実WebRTC smoke

Python LiveKit SDKの2 participantで合成toneと制御probeを送受信し、専用bridgeを2秒切断した。BrowserとConversation Coreを含む品質受入ではなく、障害注入が実mediaへ作用することの確認である。

調整中に、最初の試行はtimeout、次の試行は通信復旧後のAudioStream cleanupでSDKが異常終了し、その次は復旧後の単発制御probe待ちでtimeoutした。これらを成功へ読み替えない。再接続で作り直された全streamを閉じ、疎通確認のprobeを繰り返す検証コードへ修正した。

最後の診断試行は終了処理まで正常終了した。切断前の有音受信は75frame、切断中央区間の有音受信は0frame。link復旧から最初の有音受信は3,265.5ms、制御probe受信は3,298.3msだった。これは1回の診断値であり、reconnect成功率99%やp95 3,000ms以下の証明ではない。制御・音声の利用可能時刻とnetwork復旧を分け、障害注入用runnerと測定対象のclockを明示する必要も残る。
