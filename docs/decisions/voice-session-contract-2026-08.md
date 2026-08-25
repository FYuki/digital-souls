# 双方向音声セッション契約と JSON Schema SSOT (2026-08)

## 状態

**ACTIVE**。現行範囲は Wave 3 の transport 非依存 Conversation Core が扱う音声セッション契約と、JSON Schema から FE/BE 型を生成する試験導入に限定する。実動作、LiveKit 認証、VAD、streaming LLM/TTS は後続 Issue とその ADR で定める。

## 背景

現行の `backend/app/routers/ws.py` はターン型 WebSocket であり、Issue #17 の baseline 測定対象でもある。継続 listening、barge-in、再接続をこの経路へ直接足すと、状態モデルと測定条件を同時に変えてしまう。一方、FE/BE 間の既存会話契約は手書きで二重管理され、音声セッション向けの型生成・runtime validation・再生成差分検知がない。

## 決定事項

### 1. 独立した lifecycle

session、user utterance、assistant response、client playback を別 lifecycle とする。VAD の `speech_stopped`、STT 確定の `utterance_finalized`、Backend の `should_response`、`response_started` はそれぞれ独立した decision point である。

| lifecycle | 状態 | 許可する主な遷移 |
|---|---|---|
| session | disconnected / listening / muted / closed | disconnected → listening、listening ⇄ muted、listening・muted → disconnected、各非終端 → closed |
| utterance | speaking / stopped / finalized / pending / consumed / discarded | speaking → stopped → finalized、finalized → pending または consumed、speaking・stopped → discarded、pending → consumed |
| response | generating / completed / interrupted / failed / privacy_skipped | generating → completed / interrupted / failed / privacy_skipped |
| playback | idle / playing / stopped / completed / decode_failed | idle → playing、playing → stopped / completed / decode_failed |

`should_response=false` の finalized utterance は pending に保持する。後続 utterance で `should_response=true` になったとき、Backend は pending 群を同じ `source_utterance_ids` に含め、一度だけ consumed とする。

### 2. 所有者と event 一覧

Frontend は mic/VAD と実再生の事実、Backend は session 判定、STT 確定、generation、cancel 確定、永続化を所有する。`response_cancel_requested` は要求であり、終端は Backend の `response_cancelled` だけが確定する。

| event | owner | 意味 |
|---|---|---|
| `session_start_requested` | Frontend | version と再接続猶予パラメータを伴う開始要求 |
| `session_started` | Backend | session 確立 |
| `session_muted` / `session_resumed` | Frontend | mic 送信停止／再開の事実 |
| `session_ended` | Backend | session の終端確定 |
| `session_disconnected` | Frontend | transport 切断の検知 |
| `session_reconnect_requested` | Frontend | 同じ session への再接続要求 |
| `session_reconnected` | Backend | 猶予時間内の再接続確定 |
| `speech_started` | Frontend | VAD による発話開始。再生は Backend を待たず停止できる |
| `utterance_audio_chunk` | Frontend | media track 上の chunk と同じ `sequence` を持つ delivery control |
| `speech_stopped` | Frontend | VAD による発話停止。応答開始を意味しない |
| `utterance_finalized` | Backend | privacy 判定後の STT 確定テキストと `should_response` |
| `utterance_pending` | Backend | 応答見送り発話の保持 |
| `utterance_discarded` | Backend | privacy、切断、session 終了、無効音声による終端 |
| `response_started` | Backend | source utterance 群を一度だけ消費して generation 開始 |
| `response_delta` | Backend | 生成テキストの増分と text range |
| `response_audio_chunk` | Backend | audio media の `sequence` と生成本文の text range の対応 |
| `response_completed` | Backend | generation 完了。再生完了とは別 |
| `response_cancel_requested` | Frontend | barge-in 等による cancel 要求 |
| `response_cancelled` | Backend | response の cancel 終端確定 |
| `response_failed` | Backend | generation 失敗と回復可否 |
| `playback_started` | Frontend | 指定 chunk の実再生開始 |
| `playback_stopped` | Frontend | 停止理由と最後に再生完了した `sequence` |
| `playback_completed` | Frontend | 全 chunk の実再生完了 |
| `playback_decode_failed` | Frontend | 指定 chunk の decode 失敗 |
| `error` | 検出側 | recoverable / terminal と利用者向け状態の対応 |
| `observation` | 計測点の owner | 本文を持たない metadata-only 計測 |

### 3. 順序、重複、欠落、終端、ack、backpressure

- `event_id` は event の冪等性キーである。同じ `event_id` の再受信は ack 済みとして副作用を再実行しない。同じ ID で payload が異なる場合は terminal protocol error とする。
- lifecycle 内では owner が `monotonic_timestamp_ms` と `sequence` の昇順で送る。異なる lifecycle 間の全順序は保証しない。
- audio chunk の `sequence` は response または utterance ごとに 1 から始まり欠番を許さない。重複 chunk は同じ lifecycle 内で破棄し、異なる payload の同一 sequence は protocol error とする。
- control event の受理は `event_id` の受信 ack を意味する。音声 chunk ごとの個別 ack event は設けず、最後に連続して処理した sequence を `speech_stopped`、`playback_stopped`、`playback_completed` で累積報告する。
- 欠落を検出した受信側は、それ以降の非連続 chunk を再生済み・保存済みとして進めない。音声 byte の再送復元は行わない。
- transport は未 ack の control event と未消費 media に上限を設け、上限時は producer を待機させる。上限値は transport 実装 Issue で定める。会話契約は chunk の間引きや並べ替えを fallback として認めない。
- terminal event は response では `completed` / `cancelled` / `failed`、utterance では consumed 相当の `response_started` または `discarded`、session では `ended` である。終端後に同じ lifecycle ID で届いた delta、audio、完了、cancel は遅延 event として決定論的に破棄する。
- cancel と完了が競合した場合、Backend が先に受理して終端記録した event が winner となる。後着 event は状態も永続化結果も変更しない。Frontend の再生停止はこの winner 決定を待たない。

### 4. chunk と本文範囲

`response_audio_chunk.sequence` と `text_range` が音声 media と生成本文を結び付ける。`text_range` は Unicode code point 単位の半開区間 `[start, end)` で、sequence 1 から連続する範囲だけを再生済み prefix とみなす。`playback_stopped.last_played_sequence=N` のとき、Backend は 1..N の連続 chunk に対応する末尾までを算出し、その本文だけを privacy sanitizer に通して `interrupted` turn へ保存する。欠番後の範囲、生成本文を越える範囲、重複 sequence は保存しない。

音声 byte は user utterance、合成音声とも永続化せず、session 終了時に破棄する。履歴と記憶へ渡すのは privacy 判定済みの STT 確定テキストと生成テキストだけである。privacy skip は既存どおり `privacy_skipped` で永続化し、音声契約では `utterance_discarded(reason=privacy)` と表す。

### 5. 再接続

猶予時間内なら `session_id` と conversation を維持する。ただし進行中 utterance は `discarded(reason=disconnect)`、進行中 response は実再生範囲まで `interrupted` として終端し、音声を再送して途中状態を復元しない。猶予時間超過後は旧 session を `session_ended(reason=reconnect_timeout)` とし、新しい `session_id` で開始する。猶予時間の具体値は `session_start_requested.reconnect_grace_ms` で渡す。

### 6. error と protocol version

`protocol_version` は現在 `1.0` の完全一致だけを受理する。境界 validation は unknown を JSON Schema で検証した後に生成型へ変換する。非互換 version の開始要求では session、conversation turn、media resource を作らず、terminal `error` に変換する。recoverable error は利用者状態を listening / muted / reconnecting のいずれかに保ち、terminal error は ended または error とする。

### 7. observation と baseline

`observation` は `session_id`、任意の `utterance_id` / `response_id`、計測点、単調増加時刻だけを持つ。本文、音声、STT 結果は schema の `additionalProperties: false` により拒否する。計測点は `speech_stopped`、`utterance_finalized`、`response_started`、`first_audio_out`、`playback_started` の5点である。

Issue #17 の現行 WebSocket baseline と比較する共通指標は「発話終了 → 初音」とする。新経路では `speech_stopped` から `first_audio_out` を測り、STT 確定・応答判断などの中間区間は追加情報として別表示する。既存 `ws.py` の計測条件は変更しない。

### 8. LiveKit mapping と境界

| Conversation Core | LiveKit | mapping 境界 |
|---|---|---|
| voice session | Room 接続 | AudioTransport adapter が関連付け、Room SID を Core ID にしない |
| speaker `participant_id` | Participant identity / SID | adapter の session 内 map。Conversation Core の participant ID を正本とする |
| utterance audio | user AudioTrack | media は track、開始・停止・確定は data event |
| response audio | character AudioTrack | chunk `sequence` は track frame と data event の adapter metadata で対応 |
| lifecycle event | reliable data event | JSON Schema validation 後だけ Core へ渡す |
| observation | data event または local metric | 本文・音声を mapping しない |

LiveKit の Room、Participant、Track SID、codec、token を Conversation Core の event schema や永続モデルへ入れない。LiveKit 固有の再送、接続、track 購読は AudioTransport adapter が吸収する。

### 9. schema、生成物、generator、CI

SSOT は `contracts/voice-session/voice-session.schema.json` とする。共有 fixture も同ディレクトリに置き、BE/FE が同じファイルを読む。

generator は quicktype 23.2.6 を選ぶ。Draft 2020-12 JSON Schema から TypeScript と Python Pydantic 型を同じ CLI で生成でき、生成結果が決定的で、既存の npm 実行入口へ統合できるためである。生成物は `frontend/src/lib/voice-session/generated.ts` と `backend/app/voice_session/generated.py` に置く。`npm run generate:voice-session` が双方を再生成する。

Frontend は Ajv 2020、Backend は `jsonschema.Draft202012Validator` で外部入力を検証し、成功後だけ生成型へ変換する。CI は依存 install 後に `npm run check:voice-session-generated` を実行し、再生成後の2生成物に差分があれば失敗する。

### 10. 段階移行

1. 並存: 現行 WebSocket を default と baseline 対象のまま維持し、新契約を使う LiveKit 経路を別入口で実装・評価する。
2. default 化: 後続 Issue の受入と baseline 比較を完了後、新経路を default にする。非互換 version は新 session を確立せず terminal error とする。
3. 旧経路削除: default 化後に明示した削除 Issue で、現行 WebSocket と既存 FE 契約を削除する。本 Issue では変更しない。

## 結果

- 後続 Issue は transport の実装詳細と独立して lifecycle、cancel、再接続、履歴範囲を実装できる。
- FE/BE の型、runtime validation、fixture、CI 差分検知が同じ schema を正本とする。
- 中断履歴は生成全文ではなく利用者が実際に聞いた連続 prefix に一致する。
- 現行 WebSocket baseline は変更されず、新旧の「発話終了 → 初音」を比較できる。
