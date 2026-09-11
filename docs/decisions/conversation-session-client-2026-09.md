# Conversation Session共通clientの利用境界（2026-09）

#319 / #323のclient実装。動作契約は[音声・テキスト併用Conversation Session契約](conversation-session-text-input-2026-09.md)を正本とする。Web入力欄への配線は#324、focus/muteのdevice接続は#325、テキスト優先の横断統合は#326で行う。

## 接続と送信

`frontend/src/livekit/voice-session.ts`の`LiveKitVoiceSessionController`をWebとDesktop #317の共通入口とする。`VoiceSessionDependencies`からtransportと時計・ID生成を注入できる。DOMやTauri APIをcontrollerへ持ち込まない。

- `ensureSession(context)`は明示的な音声session開始に使う。`context`はcharacterとconversationの組で、表示選択から独立したsession所属を保持する。
- `matchesContext(context)`で表示中スレッドとの一致を判定する。一致しない場合の通常`/chat`送信はshellが行う。
- `canSubmitText(context)`は接続状態・送信元・未確認の先行入力を確認する。生成中や再生中でも送信可能とする。
- `submitText(context, text)`のPromiseは送信IDを返す。**Promiseの完了は入力の受理成功ではない**。`snapshot.textSubmissions`の該当IDが`accepted`になったときだけ送信成功として扱う。
- 本文・session ID・送信元スレッドを`TextSubmissionTracker`で保持する。配送失敗や通知未着は`confirming`とし、5秒間隔の結果照合を行う。切断中は照合を止め、同じsessionの再接続後に結果だけを照合する。
- `not_received` / `rejected`が確定した入力は明示的な再送が可能になる。再送操作は新しいIDを使う。`processing`や切断を未受理へ読み替えない。
- session終了・Backend消失時も未確認の本文を保持する。同じスレッドの未確認入力を新しいsessionへ自動投入しない。確定結果のないまま成功・失敗を断定しない。

## 状態と履歴表示

snapshotはsessionの接続状態、入力、response、playback、テキスト送信結果を分離する。送信操作だけで生成開始・回答完了を確定しない。

Core eventの通知callbackは`(event, context)`を受け取る。表示中のスレッドではなく、通知に付随するsession所属の`context`へ履歴更新・errorを対応付ける。

`VoiceHistoryProjection`は共有protocolの`source_inputs`、STT確定文、送信本文から表示用のuser/character turnを作る。response ID・session ID・スレッドの対応とtext sequenceを検証し、重複・終端後の遅着deltaを反映しない。通常の保存済み履歴はBackendから取得し、この一時投影を第二の履歴正本にしない。sessionを解放したら`releaseSession`で投影の本文を解放する。

## 入力抑止と中断

`InputSuppressionPolicy`はmanual、text focus、thread switchを独立した理由として保持する。focus解除ではmanual/thread switchを消さない。スレッド切り替え時に`switchThread()`、元スレッドでの明示再開時だけ`resumeExplicitly()`を使う。device・VAD・BE抑止イベントへの反映は#325の責務とする。

`interruptResponse(context)`は対象スレッドを確認して即時local playback stopを行い、再生済みprefixとcancel要求を送る。BEの取消通知を受けるまで生成完了と扱わない。音声の相槌・take-turn判定はBEのまま維持する。`submitText`との一体化と遅延STT破棄は以下のM7境界で扱う。

## 検証範囲

clientのunit検証では配送/受理の分離、publish未完了時の照合、切断・結果不明・再送、スレッド誤操作防止、BE状態とlocal stopの境界を確認する。共有schemaと実client部品を接続するmodule検証では混在入力の履歴投影、重複・遅着、mute理由の保持を確認する。実LiveKit/LLM/STT/TTS/ブラウザでの受入は#327で別途行う。

## focusによる入力抑止の接続（M6）

`setTextInputFocused(context, focused)` は一致するsessionの音声入力だけを抑止する。
microphone trackを直ちに無音化し、VAD境界をリセットし、BEへ
`audio_input_suppression_changed(reason=text_focus)` を送る。focusは通常の
`session_muted` や利用者のmute試行数に加算しない。blur時には抑止解除の通知完了を
待って再開する。短時間のfocus/blurが重なった場合は最新の状態だけを反映する。

`muteMicrophone()` はmanual、`muteForThreadSwitch()` はthread_switchを保持する。
両者はfocus解除・受理成功では解除されず、`resumeMicrophone(stream)` の明示操作で
解除する。マイク操作は直列化し、遅いpublish完了で新しいmuteを上書きしない。
再接続時は現在のfocus状態を再通知し、接続中に変更された抑止状態をBEへ戻す。

Web入力欄は受理確定まで本文とfocusを保持し、Enter/クリックとも成功時にblurする。
送信ボタンのpointer/mouse押下ではfocusを奪わない。別スレッドのfocus操作は
元sessionへ適用しない。UIの「テキスト入力中」は一時抑止、「ミュート」は持続muteを表す。

BEはfocusとmanualの入力ゲートを独立保持し、抑止中の音声frame/prerollを
STTへ渡さない。抑止時に未終了のcaptureは `input_suppressed` として破棄し、
先行STT previewにはcaptureの有効性を渡して、遅延結果による回答停止を防ぐ。
focus以前にVADで終了した音声や処理中の確定入力はfocus操作だけでは取り消さない。
text submit時に先行する未確定入力をすべて破棄する規則は以下のM7境界で扱う。

unit/module検証は実サービス・ブラウザ音声受入の代わりにしない。#279との全体統合と
実LiveKit/STT/LLM/TTSを用いた混在会話はM8の完了条件として残す。

## text submitによる割り込みと音声入力の世代（M7）

clientは新しいtextの送信IDを予約してから、旧回答を即時local stopし、
`playback_stopped`、`response_cancel_requested`、`user_text_submitted`を送る。
再生停止通知のACKを待ってtextやcancel要求を止めない。旧responseのdelta、開始イベント、
再生観測は取消対象のresponse IDで除外する。取消の成立・終端履歴はBE通知を正本とする。

BEの`TextInputReceiver`が新規受付と判定した入力だけが、production bridgeの
未確定capture、media tail待機、待機STT、prerollを破棄する。Coreは音声入力の世代を
進め、処理中STTやpreviewの結果を無効化する。providerの終了を待つことなくtextを
共通User Input queueへ入れ、旧responseをcancelして次の回答を開始する。
無効化された音声は`utterance_discarded(reason=text_priority)`で示す。

音声の世代はSTT開始前、認識後、turn決定の配送、cancelの状態変更、User Input受理で
確認する。古いSTTの成功・失敗やpreviewが遅着しても新しい入力・回答を開始／取消しない。
bridgeも処理世代を持ち、古いtaskの完了通知で新しいSTTの実行中状態を解除しない。
再接続時の照合・同じevent IDの再送は受理台帳が処理し、音声破棄やcancelを再実行しない。

すでに確定した音声入力は共通queueに保持し、textと同じ順序で次のUser Turnへ渡す。
すでに保存した履歴も保持する。別スレッドへのHTTP送信にはこの処理を適用しない。
Core/bridge/受理台帳を接続したmodule検証とmocked browser検証を行うが、実サービスを
用いた混在会話・履歴更新・音声再生の最終受入はM8で確認する。
