# 下り遅延 d の仮置き値と見直し条件（Issue #540）

[再生済み範囲推定ADR](../decisions/voice-playback-estimation-speech-services-2026-09.md)の補足決定に従い、
`DS_VOICE_DOWNLINK_DELAY_MS`の初期値を **300ms** に仮置きした。
これは実接続計測で確定した値ではなく、FE観測との差分計測が集まるまでの暫定値である。

## 根拠と境界

- 値は Backend 環境変数1本のみを正本とし、Sessionごとの較正は行わない。
- 推定再生済み範囲（停止確定時のprefix）と出力完了判定（送出完了+d）で同一値を使う。
- FEの`playback_completed`/`playback_stopped`/`output_stop_confirmed`は
  推定を上書きしない差分観測として記録される
  （measurement `fe_completed_observed`/`fe_stopped_observed`/`playback_estimate_delta_*`/`output_stop_confirmed`）。
- `last_played_audio_sequence`はvoice-session schemaとlivekit-transport schemaでoptionalに変更し、
  FEは送ってもよいが省略も許容する。

## 見直し方法

差分観測`playback_estimate_delta_{kind}`を集計し、推定と実測の系統的なずれが
示された場合に`DS_VOICE_DOWNLINK_DELAY_MS`の既定値（`DEFAULT_VOICE_DOWNLINK_DELAY_MS`）を見直す。
再計測は実接続（dogfoodまたは実LiveKit経路）で行い、モックE2Eの結果は受入証跡にしない。

## 未実施の確認

- 実接続でのd妥当性計測: 未実施（実LiveKit接続と外部推論serviceが必要。
  音声入力は固定音声fixtureを使用し、物理マイクは不要）。
- 単体・結合テストでは送出位置・decision時刻減算・schema optional化・観測転送を確認済み。

## #541での追試（2026-09-29）

独立dev Compose projectでLiveKit起動・HTTP 200と、固定PCMの実WebRTC送受信診断を確認した。
ただし、通常会話の`test:integration:voice`は共有Ollamaのreadinessで停止し、
`playback_estimate_delta_*`は**観測0件・NOT_RUN**。VOICEVOX／WhisperもGET接続不可だった。
独立PCM診断には会話経路の推定値がないため、過大推定0件の受入へ代用しない。

既定300msを変更する根拠は得られておらず、暫定値を維持する。過大推定件数・短い側の分布は欠測。
実行条件・再実行コマンド・種別ごとの分母を含む集計方法は
[#541受入記録](issue-3-acceptance-20260929.md)を参照する。
`value = FE − BE`の負値が過大推定であり、観測がない場合は合格へ変換しない。
