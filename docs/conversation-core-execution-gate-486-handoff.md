# #486 Conversation Core / Execution Gate 引継ぎ

## 実装境界

Conversation Coreは`ConversationCoreSession`を状態の正本として維持する。`pipeline.py`はLLMからTTSまでの実行、`terminal_effects.py`は終端の保存・配送・保留入力開始、`task_tracker.py`はstage/effect taskの取消と終了待ちを担う。lock、generation、response/inputのmap、終端状態の決定はSessionに残した。

Execution Gateは`result_classification.py`でnative responseと`MCPFailure`を一つの分類結果へ変換し、`execution.py`で正規化済み入力と実行準備を表す。認可、承認、journal claim、dispatch直前guard、settlementはGateが引き続き所有する。

Tool Useは`decision_loop.py`へ候補、判断、入力待ち、確認再開、結果採用を移した。`ToolService`のconversation単位run、timeout、status、`ToolMaterial`、confirmation public契約は維持し、loopからsourceやjournalを直接呼ばない。

## 変更契約と証拠

- `C486-01` / `C486-02`: [Conversation Core検証記録](validation/conversation-core-486-20260924.md)
- `C486-03` / `C486-04` / `C486-05`: [Execution Gate / Tool Use検証記録](validation/execution-gate-tool-use-486-20260924.md)
- 公開API、schema、DB、model、prompt、音声設定、認可policy、保存規則、retry上限は変更していない。

## 残課題

Frontend依存は作業用worktreeへ既存lockfileどおり`npm ci`で導入し、build、check、Frontend unit/module、Backend VAD parityを実行済みである。実Ollama、実Whisper、実MCP、実ToolServiceもtest識別・一時dataで確認済みである。

実LiveKit transportは、既存dev LiveKitを停止・変更せず、一時Backend（port `18000` / `18001`、`DS_ENVIRONMENT_ID=test`、`PYTHON_DOTENV_DISABLED=1`、一時data root）で実行した。Python `3.12.3`、`livekit` `1.1.16`、`livekit-api` `1.2.0`、`livekit-protocol` `1.1.26`、`httpx2` `2.13.0`を使用し、既存server imageは`livekit/livekit-server:v1.9.7`だった。token endpoint処理中にBackendが`Nvidia Decoder is supported.`後`terminate called without an active exception`でnative abortし、単独caseではclient側に`httpx.RemoteProtocolError: Server disconnected without sending a response`が観測された。証拠ログは`/tmp/takt-c486-livekit-backend.gxDOeW.log`と`/tmp/takt-c486-livekit-single-backend.QWYakm.log`である。保存ログに終了コードはなく未確認とし、実マイク・聴感は`NOT_RUN`である。詳細なコマンドと条件は[Conversation Core検証記録](validation/conversation-core-486-20260924.md)に記録した。

変更前比較は未実施である。`backend/app/livekit_transport/`と対象integration testは基点と差分がなく、専用test LiveKit設定・独立した基点実行資源がないため、同じnative abortを無条件に繰り返していない。変更起因か環境・native SDK起因かは未判定である。

固定条件の基点前後性能比較は`INCONCLUSIVE`である。`run_pilot.py`は未コミットworktreeを`ValueError: measurement requires a committed worktree`として拒否し、commitはこのstepで禁止されている。さらに比較用の専用`infra/livekit/.env`が存在しないため、共有LiveKit設定を流用して比較測定を進めていない。過去の成功記録やmock結果を今回の証拠へ読み替えていない。

Backend module全体は`2006 passed, 1 failed, 16 warnings`。唯一の失敗は環境起動testのDocker buildx activity pathがread-onlyという準備失敗で、対象test・関連環境コードは基点と同一であり、#484にも同じ制約が記録されている。共有Docker設定の変更や既存containerの停止・削除は行っていない。

深い入れ子JSONのTool引数は、基点`9a08fdebb2a320c5398be70fb5259d20c8759521`と同じ経路で`backend/app/tool_use/projection.py:99`の`RecursionError`を再現した。これは#486の抽出対象外の既存不具合であり、今回`invalid_decision`へ変換していない。修正する場合は、今回のCore/Gate/Tool内部抽出とは別責務として扱う。
