# Web・音声共通のCore呼出契約（2026-09）

## 状態・適用範囲

**ACTIVE（#3作業ブランチ）**。既存Web通常chatとLiveKit Speech/Textを同一Backendの会話準備へ接続する契約を定める。複数入口の目標と後続範囲は[複数入口ADR](multi-entry-character-core-2026-09.md)を優先する。実サービス・実マイクの受入状況は[検証記録](../validation/issue-3-core-boundary-20260926.md)を参照する。

## 呼出と所有

`backend/app/core_invocation.py`の`CoreInvocation`は、character、conversation、確定した本文、actor、出力先visibility、入力ID群、応答IDを持つ。`actor`は`owner / other / self`、platform、任意のexternal IDからなる。現行のWebと音声は`owner`・`private`だけを許す。`other / self`、external ID、`public`は契約に予約し、#516／#517／#518の権限・記憶規則が接続するまで呼出時に拒否する。

| 境界 | 現在の対応と所有 |
|---|---|
| Web通常chat | `/chat`から`ChatService.generate_reply_async`へ進み、`invoke_text(CoreInvocation)`を呼ぶ。受理後に`HistorySession`のturn IDを応答IDとし、全量生成・保存・失敗・形成予約を一度だけ行う。HTTP本文には独立した入力IDがないため、`input_ids`は空欄 |
| LiveKit Speech/Text | `ConversationCoreSession`が受理、入力ID、応答ID、generation、delta、割込・取消・終端を所有する。確定本文と既存`Response.source_inputs`／`response_id`を`CoreInvocation`へ写し、`ChatService.prepare_core_reply`へ渡す。`ConversationHistoryPersistenceAdapter`が開始・保存・実再生済みprefixを確定する |
| 共通判断 | `ChatService.prepare_core_reply`から既存のCard、PromptBuilder、History、Memory read、Inference Router、ToolServiceとExecution Gateを使う。第二の人格・記憶・権限・Target設定は作らない |
| 形成予約 | 保存済みの通常完了turnだけを`submit_completed_turn`へ渡す。HTTP側の保存直後と音声側の完了observerが各1回呼ぶ。画面由来と失敗／取消等は予約しない。既存workerの再試行・削除規則は維持する |

FEの表示結果と音声の実再生結果は、生成済み本文とは異なる。音声の再生済み範囲、遅延旧出力の拒否、再送・ACK、STT/TTS、発話区間検出、ターン判断、再生queueは`conversation_core`／`livekit_transport`側に残す。Core呼出契約にはLiveKit Room、Track、PCM、FEの型を入れない。Voice adapterを別の実装へ差し替える際も、確定入力と識別子、割込・取消、実再生結果を既存のsession／persistence境界へ対応させる。

## 依存と差替え点

`ChatService.invoke_text`はFEなしの内部clientから直接使える。#516のCLI・認証・能力宣言を先取りしない。活動型は将来`self`を用いて同じ呼出契約に接続するが、現行実装は拒否する。公開出力の記憶絞り込みは#518が実装する。

推論実行は既存Inference RouterのTarget／Provider Adapterへ委譲する。#422のRunner差替えでもprompt・入力上限・cancel・errorと出力をこの境界で保つ。MemoryはHistoryService／MemoryReadRepository／形成workerの契約を用い、SQLite正本とChroma派生indexを維持する。remote Memory実装・新しいネットワークhopは追加しない。

Toolの候補・確認再開は既存ToolServiceを使い、Capability Snapshot、Execution Gate、送信検査、取消後dispatch拒否、結果不明の扱いを維持する。入口側に独立した権限判定やTool loopを置かない。外部作用の実行直前検査はGateの契約による。

## 変更の範囲

この整理ではCard・prompt本文、モデル／音声設定、DB schema、会話と保存のライフサイクルを変更しない。実音声の回帰・性能・品質は固定fixtureと実サービスで別に確認する。実マイクと聴感は利用者の検証結果が出るまで`NOT_RUN`とする。
