# Wave 3 LiveKit音声会話の受入

## 目的と責務

Issue #111の自動受入と、Issue #112の利用者によるdogfood受入を分離する。自動testはcontract、race、異常注入を決定論的に再現する。実マイク、実speaker、室内反響、主観的な間合いはmockで合格扱いにせず、利用者がUbuntu-dogfoodで確認する。

`/voice/livekit`はtransport基盤の開発・integration用入口であり、製品受入には使わない。通常の`/`にあるconversation UIだけをWave 3の正式入口とする。

## Epic受入シナリオと証跡

| # | 利用者シナリオ | CI内の決定論的証跡 | 実接続またはdogfoodの証跡 |
|---|---|---|---|
| 1 | 追加操作なしで3往復以上 | mocked E2E「通常UIの同一sessionで追加操作なしに3往復を履歴へ確定する」 | `test:integration:voice`と#112で3往復 |
| 2 | 思考中・発話中にも入力 | controllerの生成中speech start、Conversation Coreのpending utterance test | #112で思考中と再生中を各1回 |
| 3 | speech startでlocal停止 | Roomのaudio graph即時停止、controllerのevent順序test、連続barge-in mocked E2E | `local_playback_stop` p95と#112の聴感 |
| 4 | 割り込み発話を冒頭から処理 | Core bridgeの発話別PCM所有・STT直列queue test | 実Whisperを含む`test:integration:voice` |
| 5 | cancel後の旧text/audioを混入しない | Appの遅延delta、Roomのresponse/generation gate、Core cancel race | #112で割り込み後の再開なしを確認 |
| 6 | 短い間・言い淀みを分割しない | AudioRecorder VAD fixtureとmedia tail test | 実マイクで短い間を含む発話 |
| 7 | 切断・STT・LLM・TTS障害後に復帰 | reconnect mocked E2E、Core stage異常、Whisper隔離、VOICEVOX lifecycle test | 実LiveKit／実音声integration |
| 8 | 中断turnを完成履歴・記憶にしない | Conversation History persistence、sanitizer、memory formation対象外test | #112後に履歴表示を確認 |
| 9 | mute・再開・終了 | controller unitと通常UIのmute・再開・終了mocked E2E。Room切断回数も検証する | #112で各操作とresource解放を確認 |
| 10 | 通常UIだけで成立 | mocked/integration voiceは`/`を使用し、production buildでは検証画面を公開しない | #112も`/`だけを使用 |

mocked E2Eの成功は実LiveKit、Whisper、Ollama、VOICEVOXの成功を意味しない。外部serviceを含む証跡は`integration-voice`、LiveKit transport単体は`integration-livekit`として別runにする。

## 決定論的な異常・境界matrix

| 境界 | 自動test |
|---|---|
| 短い間・言い淀み | `frontend/src/lib/audio/vad-policy.unit.test.ts`で1.3秒の無音を同一発話へ結合し、1.4秒の確定無音で一度だけ終端する |
| cancel／完了raceと遅延出力 | `test_first_terminal_wins_for_state_event_persistence_and_delivery`、`test_cancelled_generation_drops_late_text_and_audio_before_both_ports`、mocked E2Eのbarge-in scenario |
| STT timeout後の次発話 | `test_lock_wait_timeout_discards_owner_and_next_request_succeeds` |
| LLM／TTS／audio publish失敗後の次発話 | `test_automatic_llm_failure_terminates_once_and_starts_pending_response`、`test_automatic_tts_failure_terminates_once_and_starts_pending_response`、`test_delivery_failure_is_not_recorded_as_an_llm_failure` |
| 履歴・memory境界 | `test_should_sanitize_interrupted_partial_reply_before_persistence`と`test_core_starts_and_terminates_the_same_history_turn_once`。後者はcompletedだけをmemory formation observerへ渡す |
| reconnect・重複・resource解放 | mocked reconnect／mute・再開・終了E2E、track重複・再購読とaudio graph失敗後の復旧を含む`frontend/src/livekit-room.unit.test.ts`、`test_production_room_cleanup_survives_cancelled_stop`、`test_production_stop_all_retries_failed_room_cleanup` |

## 自動検証

リポジトリrootで次を実行する。

```text
npm run test:unit
npm run test:module
npm run test:e2e:mocked
npm run lint:python
npm run check
npm run build
```

capabilityがあるUbuntu-devでは次も実行する。

```text
npm run test:integration:voice
npm run test:integration:livekit
```

Playwrightのrun結果、trace、添付JSONはsuite固有の`frontend/test-results/`配下へ保存する。mockと実接続のartifactを同じ成功証跡へ統合しない。本文、transcript、音声payloadを長期保存する品質artifactへ入れない。

## #112 dogfood引き渡し項目

### 受入前セットアップ

#112を開始するrevisionはLiveKit dogfood serviceを含むため、通常deployではなく`infra/dogfood/README.md`の「経路②: bootstrap管理資材のin-place更新」で反映する。既存会話データの論理backupと`backup-verify`を成功させ、サービス停止後に、新しいLiveKit API key／secretを設定したmode `0600`の一時envでbootstrapする。続けてbootstrapと同じcommit SHAをdeployし、次をすべて確認してからブラウザ受入へ進む。

- `scripts/dogfood/status.sh`が成功し、`digital-souls-livekit.service`と`digital-souls-livekit` containerがactive／runningである。
- Profile readinessでOllama、VOICEVOX、Whisper、LiveKit、Backend、Frontendがreadyである。
- `/etc/digital-souls/livekit.yaml`と`livekit-backend.env`が`0640 root:digital-souls`であり、資格情報を端末、Issue、Gitへ転記していない。
- 対象commit、browser、LiveKit／Whisper／Ollama／VOICEVOXのversionだけを受入記録へ残せる状態である。

利用者はUbuntu-dogfoodで通常UI `/` から次を実施する。

1. 音声sessionを開始し、操作を挟まず日本語で3往復以上会話する。
2. 思考中とCharacter再生中にそれぞれ割り込み、local停止と新しい意図への応答を確認する。
3. 完成済み応答の再生中に割り込み、音声だけが止まり全文履歴が残ることを確認する。
4. 生成中応答へ割り込み、再生済みprefixだけが中断履歴となり、完成turnや長期記憶候補にならないことを確認する。
5. mute、再開、終了を行い、終了後に旧音声が再開しないことを確認する。
6. 一時切断から60秒以内に復帰し、旧responseの重複表示・再生がないことを確認する。
7. 回復不能時に「音声会話を再開」が表示され、conversation履歴を保った新sessionを開始できることを確認する。

結果には実施日時、commit、browser、LiveKit／Whisper／Ollama／VOICEVOXのversion、各項目のpass/fail、失敗理由コードだけを残す。実会話本文と音声はIssueやGitへ添付しない。#112と#106の最終closeは、この利用者受入後に行う。
