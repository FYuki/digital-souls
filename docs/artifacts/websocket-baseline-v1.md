# WebSocket音声品質baseline v1

- warm-up: 5
- 測定trial: 100
- 成功: 100
- 失敗: 0
- 除外: 0

| 指標 | status | p50 | p95 | 成功 / trial |
|---|---|---:|---:|---:|
| utterance_finalized | measured | 0.300 | 0.505 | 100 / 100 |
| response_decision | measured | 0.300 | 0.505 | 100 / 100 |
| stt_start_latency | measured | 0.908 | 1.249 | 100 / 100 |
| stt_processing | measured | 2064.935 | 2170.696 | 100 / 100 |
| llm_start_latency | measured | 0.162 | 0.226 | 100 / 100 |
| first_text_latency | measured | 8914.315 | 12335.077 | 100 / 100 |
| llm_completion | measured | 8914.450 | 12335.370 | 100 / 100 |
| tts_start_latency | measured | 0.543 | 0.709 | 100 / 100 |
| first_audio_generation | measured | 2875.809 | 4023.346 | 100 / 100 |
| client_playback_latency | measured | 24.600 | 31.200 | 100 / 100 |
| ttfa | measured | 15424.550 | 19896.150 | 100 / 100 |
| local_playback_stop | not_applicable | - | - | 0 / 100 |
| turn_decision | not_applicable | - | - | 0 / 100 |
| cancel_after_decision | not_applicable | - | - | 0 / 100 |
| barge_in_cancel_total | not_applicable | - | - | 0 / 100 |
| vad_leading_boundary | measured | 164.000 | 179.705 | 100 / 100 |
| vad_trailing_boundary | measured | 1474.250 | 1489.700 | 100 / 100 |
| stale_output | not_applicable | - | - | 0 / 100 |
| reconnect | not_applicable | - | - | 0 / 100 |
| playback_continuity | not_applicable | - | - | 0 / 100 |
| processing_failure | measured | 0.000 | 0.000 | 100 / 100 |
| manual_operations | measured | 0.000 | 0.000 | 100 / 100 |

p50は診断値であり、latencyの比較・合否にはp95を使用する。
