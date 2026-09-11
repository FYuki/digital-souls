# 音声・テキスト併用Conversation Session契約（2026-09）

## 状態・適用範囲

**ACTIVE**。#319の決定事項と#320の設計を定める。2026-09-11時点では設計を確定した段階であり、runtimeへの適用・受入は#321〜#327で行う。

同じConversation Sessionへ音声とテキストを投入し、回答をテキストとTTSで返す。既存のsession / response / playback lifecycle、音声の相槌判定、privacy、履歴保存の正本は維持する。本ADRはテキスト入力、受理結果、入力抑止、入力source、および送信先の規則について既存の[音声契約](voice-session-contract-2026-08.md)と[LiveKit契約](livekit-transport-2026-08.md)より優先する。

## 確認済みの現状と変更理由

- `frontend/src/App.svelte`は通常テキスト送信時に音声sessionを終了する。#319では同じスレッドのsessionを継続する。
- `backend/app/livekit_transport/coordinator.py`のACKは、control eventを通知先へ渡した後に返す。通知先の非同期処理や保存が成功した証拠ではない。
- `backend/app/conversation_core/session.py`はSTT済み発話をpendingに保持し、応答開始時にまとめて消費する。直接テキストをSTT由来に偽装するとsourceと入力の終端が曖昧になる。
- 音声の相槌・take-turnはBEがSTT結果から判定する。#319作成時のFE判定移管の記載は仕様整理不備であり、移管しない。
- #279の表示スレッド/session分離と#291のthread revision/抽出予約は外部依存である。設計上の接続点と実際の統合成功を区別する。

## 共通User Input

Coreは`UserInput`を境界とし、`input_id`、`source`（`speech` / `text`）、本文、必要な応答判断を保持する。

- SpeechはSTTと既存の音声判定後に合流する。`input_id`は`utterance_id`と対応する。
- Textは`user_text_submitted`から合流する。`input_id`はその送信の`event_id`と対応し、STTと音声相槌分類を通さず明示的なUser Turnとして扱う。
- `utterance_finalized`は音声専用のまま維持する。テキストを偽のutteranceとして外部へ通知しない。
- response開始時の`source_inputs`は、消費した入力の`input_id`と`source`を順序付きで表す。`source_utterance_ids`を残す箇所はSpeechだけの派生値とし、TextのIDを混入させない。
- pendingの確定済みSpeechを後続入力とまとめる既存規則は維持する。テキスト優先で破棄するのは先行する未確定音声であり、確定・保存済み履歴を削除しない。
- 同じsessionに紐付くconversation、character、Memory / Tool / Screen Perception文脈、privacy境界を共用する。LiveKitのRoom/Track IDはCoreへ持ち込まない。

## protocolと互換性

SSOTは既存の`contracts/voice-session/voice-session.schema.json`とする。`source_inputs`を含む新契約はCore protocol `1.1`として実装し、FE/BEを同時に更新する。旧`1.0`はbootstrapで拒否し、Roomや会話turnを作成しない。既存WebSocketのbaseline契約は変更しない。LiveKit private frameは独立したtransport契約のため、変更しないframeのversionは`1.0`を維持する。

すべてのCore eventに既存の`protocol_version`、`event_id`、`session_id`、`monotonic_timestamp_ms`を付ける。送信元participantは認証済みsessionのuserと照合し、本文中のIDで別sessionや別conversationを指定できない。

| event | 決定権 | 固有payloadと意味 |
|---|---|---|
| `user_text_submitted` | FEの送信操作 | `speaker`、`text`。`event_id`がその送信の冪等性キー |
| `user_input_result_requested` | FE | `speaker`、`input_event_id`。元の送信を再実行せず、結果を照合する |
| `user_input_result` | BE | `input_event_id`、`status`、必要な`response_id` / `error_code`。受理結果の正本 |
| `audio_input_suppression_changed` | FE | `suppressed`、`reason=text_focus`。manual muteと独立した音声入力抑止の変更 |
| `response_started` | BE | 既存情報と`source_inputs`。Speech/Textの入力を一度だけ消費した応答 |

本文は空白だけの入力を拒否し、既存の通常chatと整合する入力長・privacy検証を行う。上限を超える入力を切り詰めて別の内容として受理しない。イベント名とschemaの具体化はこの契約を実装する#321で行う。

### 受理・配送・回答の分離

| 状態 | 意味 | FEの扱い |
|---|---|---|
| 配送ACK | transportがeventを受け取った | 本文を消したり、送信成功を確定したりしない |
| `processing` | BEが入力を処理中で、受理結果は未確定 | 送信中。切断すれば送信確認中へ移る |
| `accepted` | BEが共通入力と対応する応答処理を引き受けた | 送信成功。回答完了・TTS再生成功とは別 |
| `rejected` | 検証・session状態等により受理しなかった | 送信失敗。本文とfocusを保持する |
| `not_received` | 照合時に未受理を確定し、元の送信を閉じた | 本文を保持し、ユーザーが再送できる |

受理成立後にLLM/TTSが失敗しても、入力を未受理へ戻さない。回答の失敗は既存response lifecycleで通知する。privacyで本文を保存しない場合も、本文を複製しない識別子と処理結果で送信の受理状態を区別する。

### 再送・照合の競合

1. `(session_id, input_event_id)`と同じpayloadに対する受付・結果更新を直列化する。受付を記録する前に非同期生成を起動しない。
2. 同じID/同じpayloadの再送は同じ結果へ対応付け、User Turn、回答、保存後の副作用を重複させない。同じID/異なるpayloadは既存のterminal protocol errorとする。
3. 切断後、FEは本文と元のIDを保持し、「送信確認中」を表示する。別の新規入力として自動送信しない。
4. 再接続後は結果照合eventを送る。`processing`は未受理と扱わず、確定結果を待つ。受理済みなら送信成功、未受理ならユーザーが明示的に再送できる状態へ戻す。
5. 照合が元の送信より先に処理されて未受理と確定した場合、そのIDの未受理終端を記録する。後着した元の送信を新規受理してはならない。ユーザーの明示的な再送は新しいIDを使う。
6. 結果は再接続可能なsessionの存続中保持し、確定済み結果や未受理終端を都合よく追い出して再実行可能にしない。既存の容量上限に達した場合は明示的なerrorを返し、黙って重複防止を無効化しない。
7. session消失やBackend再起動などで結果を証明できない場合、受理結果不明を未受理と断定しない。本文と結果不明状態を保持し、自動再送・自動成功表示をしない。既存の再接続猶予とsession終了の規則は緩和しない。

この結果管理は会話本文の第二の履歴ではない。履歴の表示・文脈・記憶へ渡す本文は共通Conversation Historyを正本とする。

## テキスト優先とresponse中断

同じVoice Sessionへのテキストsubmitを新しいUser Turnとして扱う。FEは対象responseのlocal playbackを即時停止し、BEへcancelを要求する。BEのcancel成立通知を待ってから止める実装にはしない。BEの終端は既存のcancel/complete競合規則で確定する。

BEはテキストsubmitを処理する境界で先行する未確定音声を無効化し、STT完了を待たずTextを処理する。capture、STT待機、実行中STT、冒頭preview判定には有効性境界を適用し、後着結果から`turn_decision`や回答を開始しない。共有STTサービス自体の停止は要求しない。

旧responseのtext/audioと確定通知はresponse IDと世代で区別し、新しい回答として表示・再生しない。現在の回答生成と音声出力の停止・中断履歴保存は、既存の実再生済みprefix契約を維持する。

入力欄へのfocusだけでは回答を中断しない。音声での相槌・take-turnはBEの現行判定に従う。

## focusとミュート

- 表示中スレッドと音声sessionが一致する場合、その入力欄のfocus中だけ音声をSTT / Conversationへ流さない。FEのcapture/VAD送出制御とBEの受信抑止を整合させ、抑止中の音声をblur後にまとめて処理しない。
- focusはmanual muteを変更しない。manual mute、focusによる抑止、#279のスレッド切り替えミュートを独立した理由として保持する。
- 送信成功時は当該入力欄のfocusを外す。manual muteやスレッド切り替えミュートなど、別の理由がなければ音声入力を再開する。失敗時は本文とfocusを保持する。
- Enterとクリックで同じ条件を満たす。IME composition中のEnterをsubmitにしない。
- 非同期結果は送信元のスレッド・入力欄へ対応付ける。送信後に別スレッドや別入力欄へ移動していても、その欄のfocusやマイク状態を誤操作しない。
- 接続、回答生成、TTS再生、BEイベント受信、履歴更新はfocus抑止中も継続する。

## スレッドと共通client

送信先は入力欄が紐付く表示中スレッドとする。音声sessionの所属スレッドと一致すればVoice Session protocolへ送り、一致するsessionがなければ既存`/chat`へ送る。`/chat`にvoice session IDを追加して経路を擬似接続しない。

#279によりAの音声sessionを維持してBを表示している場合、Bへのテキスト送信でAの回答・入力を中断/破棄せず、sessionを終了せず、マイクを再開しない。Aへ戻っても切り替えミュートは明示操作まで保持する。通常chatを暗黙の音声session開始や常時TTSへ変更しない。

protocol client、受理結果管理、User Input、response/cancel、抑止理由、BEイベントからの状態投影、履歴反映はWeb画面に閉じない共通層へ置く。FEはローカルdevice/transportの事実を所有し、生成開始/完了・Tool処理・TTS生成・cancel成立を独自推測で確定しない。Webと#317は同じclient契約を利用し、Desktop shell、直近3メッセージ表示、OS統合は#317へ委ねる。

## 履歴と外部依存

Speech/Textとも同じConversation Coreと共通Conversation History保存境界を通す。user / character turn、privacy、character分離、再送の冪等性を維持する。#291のthread revision更新と抽出予約を、この保存境界から利用する。抽出器、Episode schema、重複判定を#319で再実装しない。

#291が未統合の間は接続点の基礎実装を進められるが、thread revision/予約の検証は未完了として残す。#279のsession維持・マイク制御が未統合の間も、A/B切り替えの実受入を完了扱いにしない。

## 実装マイルストーンと検証対応

| Issue | 成果 | この段階で確認する境界 |
|---|---|---|
| #320 | 本ADRと既存ADRの優先関係 | 合意事項、source、受理/照合、責務、外部依存の対応 |
| #321 | schema/生成型/validation、Backend受付・結果照合 | 不正入力、version不一致、同一ID競合、ACK欠落、再送、照合先行と遅着 |
| #322 | 共通User Input・文脈・履歴 | Speech/Textの入力source、実SQLite、二重登録防止、#291のrevision/予約 |
| #323 | 共通clientと状態投影 | BE結果とlocal観測、確認中・再接続、スレッド対応、shell非依存 |
| #324 | Web入力欄の送信経路 | 同一/別スレッド、通常chat、TTS、IME、送信結果UI、#279統合 |
| #325 | focus・ミュート制御 | クリック/Enter、成功/失敗、focusのみで停止しない、#279のミュート保持 |
| #326 | text優先・割り込み | 実行中STT/previewの遅延結果、cancel/complete競合、生成中/再生中、連続submit |
| #327 | 全体の実接続受入と#317への引き継ぎ | 実BE/LLM/STT/TTS/LiveKit/ブラウザ、混在履歴、回帰、#279/#291接続 |

schemaの正常/不正共有fixtureとCore/clientの競合テストを分ける。fixtureのvalidation成功だけを、保存・再送・割り込みの動作証明にしない。各子Issueのunit/module回帰を先に完了し、実サービスが必要な条件は#327で証跡を揃える。

`docs/testing-policy.md`に従い、transport用`test:integration:livekit`と、実LLM/STT/TTSを通る通常会話用`test:integration:voice`を区別する。テストは独立dev/test data rootで行い、dogfoodの会話データを利用しない。#150の性能目標は変更しない。
