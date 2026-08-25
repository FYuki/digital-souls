# 音声会話品質の計測

## 目的

現行 WebSocket pipeline と Wave 3 LiveKit pipeline を同じ定義で比較する。計測は `automated_test`、`controlled_baseline`、`dogfood` を別run・別artifactとし、混在させない。schemaは [voice-quality-artifact-v1.schema.json](schemas/voice-quality-artifact-v1.schema.json) である。

## trace と clock

生traceは `event_id`、`session_id`、`utterance_id`、`response_id`、event名、stage、outcome、reason code、timestamp、clock domain、単位、数値観測値だけを allow-list で書き出す。prompt、transcript、音声本文、音声payload、exception詳細は書き出さない。server は `server_monotonic` / ns、client は `client_monotonic` / msを使い、異なる clock domain の値を直接減算しない。fixtureの発話開始・終了はfixture開始時とsample数からclient clock上の時刻へ変換する。

## 指標カタログ

| 指標 | 開始 | 終了 | clock / 単位 |
|---|---|---|---|
| utterance finalized | VAD speech end | utterance確定 | 同一clock / ms |
| response decision | VAD speech end | response開始decision | 同一clock / ms |
| STT開始 | user audio受信 | STT開始 | server monotonic / ms |
| STT処理 | STT開始 | STT完了 | server monotonic / ms |
| LLM開始 | STT完了 | LLM開始 | server monotonic / ms |
| first text | STT完了 | first text delta | server monotonic / ms |
| LLM完了 | LLM開始 | LLM完了 | server monotonic / ms |
| TTS開始 | first synthesizable segment | TTS開始 | server monotonic / ms |
| first audio | first synthesizable segment | first audio publish | server monotonic / ms |
| client playback | audio/track受信 | first playback | client monotonic / ms |
| TTFA | 利用者発話終了 | first AI playback | client monotonic / ms |
| local playback stop | speech start | local stop | client monotonic / ms |
| turn decision | speech start | backchannel/take_turn確定 | 同一clock / ms |
| cancel after decision | take_turn確定 | server cancel確定 | server monotonic / ms |
| barge-in cancel total | speech start | server cancel確定 | 同一clock / ms |
| VAD冒頭・終了境界 | fixture正解境界 | 実際の取込・確定 | fixture/client / ms・発生率 |
| stale出力 | cancel確定 | 旧responseの生成・受信・提示 | 件数 |
| reconnect | network疎通回復 | control/audioの双方利用可 | 同一clock / ms・成功率 |
| playback継続性 | 予定playout | 実frame供給・buffer | ms・件数・比率 |
| resource | run開始 | run終了 | CPU、memory、送受信量、network条件 |
| 処理失敗 | 応答対象utterance | stage outcome | 件数・失敗率 |
| 手動操作 | session開始 | 通常会話終了 | 回数 |

現行WebSocketで理由付き `not_applicable` とするのは、`local_playback_stop`、`turn_decision`、`cancel_after_decision`、`barge_in_cancel_total`、`stale_output`、`reconnect`、`playback_continuity` の7指標である。実装済み計測点のeventが来ない場合は `missing`、その指標に関係するstageが失敗を報告した場合は `failed` である。正常cancel、privacy skip、利用者終了、障害注入は関係する指標で `excluded` とし、失敗率の分子・分母に入れない。stage outcomeはSTT、LLM、TTS、transport、playbackをそれぞれのeventから選び、response全体の結果を一括投影しない。

## 集計と合否

p50とp95は Hyndman-Fan Type 7 で計算する。p50は診断値、latencyの合否はp95で判定する。rateはbasis point（`100 = 1%`）で、artifactに試行数、成功、失敗、missing、not applicable、除外、率の分母を残す。

LiveKitの絶対p95上限は TTFA 2000ms、local stop 150ms、turn decision 300ms、decision後cancel 200ms、全cancel 500ms、utterance確定 800msである。比較可能な同一区間では `WebSocket p95 + max(10%, 50ms)` も同時に満たす。TTFA p50 1000msは改善目安であり合否を変えない。CPU、memory、bandwidth、packet loss、network条件も診断値であり合否に使わない。

## controlled WebSocket baseline

Ubuntu-dev の `integration-voice` Profileで Backend、Ollama、Whisper、VOICEVOX、Chromium を起動し、日本語固定fixture `speech-v2`・同一初期状態でwarm-up 5回を行った後、独立session・独立conversationで100回を測定する。fixtureは日本語の単一発話とVAD確定用の後続無音を含み、正解transcriptは実際の発話内容と一致する。外部の Ollama と VOICEVOX を起動してから、repository rootで `npm run baseline:websocket` を実行する。runnerはwarm-up前にfixture version、WAVのSHA-256・sample rate、発話境界、期待transcriptを検証する。各試行では画面に確定した利用者transcriptをUnicode NFKC正規化、前後空白除去、連続空白圧縮して期待値と比較し、不一致なら停止する。通常の `test:integration:voice` とは別の `frontend/test-results/controlled-baseline/` を一時data rootとし、trial manifestにはfixture version、hash、transcript一致結果だけを残す。warm-upはaggregateに入れない。finalizerは回数・独立ID・fixture identity・全試行のtranscript一致・初期状態を再検証し、schema検証と再帰的な匿名性検査を通したaggregateだけを `docs/artifacts/websocket-baseline-v1.json` へ保存する。LiveKit実装後はtransportだけを `livekit` とし、同じfixture、回数、schema、目標値で再実行する。

## 保存と削除

dogfood生traceはリポジトリ外の `DS_DATA_DIR/voice-metrics/raw/` へ保存し、7日を超えたファイルを起動時に削除する。生traceはGit、会話履歴、テスト成果物、dogfood backupの対象にしない。長期保存aggregateからはevent・session・utterance・response IDを除く。リポジトリ内へ誤出力した `voice-metrics/raw/` は `.gitignore` で追跡対象外にする。
