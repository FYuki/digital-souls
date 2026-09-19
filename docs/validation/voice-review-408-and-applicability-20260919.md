# #408の残レビューと受入証跡の適用範囲（2026-09-19）

## レビューの基点

#408の実差分はmerge commit ec836d2875692d10e0c88bf0a6b4fd0f3d114d92の第1親から176ファイル。
これは今回の変更前main e51bce16f83c156ffb53db0c3ded67c40cde5001に含まれる。
当時のCodeRabbit未実行・上限によるskipをレビュー済みへ書き換えない。
今回のmain向けPR #460のCodeRabbit分割レビューとは別に、次の単位で残レビューを行った。

## 確認範囲

| 単位 | 確認した内容・結果 |
|---|---|
| 音声入力・欠落検証 | detector/models/pipeline/session、microphone_frames/integrity、productionのbridge/readerは[先行レビュー](voice-quality-350-423-424-status.md)で確認。終了待ち取消が後続STTを止める不具合と新世代preroll消失を#435で修正。 |
| 契約・参加者・入力許可 | #408のcoordinator、delivery、text_input、router、FE room/client/control/private-contract/audio-probe、voice-sessionの差分を確認。FEからの発話境界注入を拒否し、Session・参加者・track・revisionの一致を必要とする。旧版は資源作成前に拒否する。 |
| 起動・取消・UI | #408のensureSession/openInputGate、AudioRecorder、Appの差分と、その後の取消修正を照合。失敗時の資源所有、遅いACKの無視、手動mute維持を確認。開始取消の実ブラウザ証跡は[別記録](voice-microphone-cancel-20260919.md)。 |
| 生成型・schema・資産・環境 | JSONを構造比較し整形差分を除外。Core/private protocol 2.0、sample位置順序、safe integerと必須世代を照合。同梱Silero 1,807,522 bytes / a35ebf52…、libfvad 15,331 bytes / 3fadafc9…が資産READMEのSHA256と一致。固定依存、dev専用Whisper接続、Docker build時FFI解放を確認。 |
| 計測集計 | VAD/PCM/割り込みreporterとcohort検証は先行レビュー済み。fixture番号指定の選択順不一致を#437で修正。run_pilot、履歴Repository往復、BE境界観測、割り込みprobe、ブラウザ診断と実接続specの差分も確認。通知受信時刻を音声開始へ置き換えず、欠測・上限超過を成功に補完しない。 |
| 証跡の解釈 | #408のJSON証跡を読み取り、FAIL・途中run・限定smoke・Repository往復を全受入の合格に拡張していないことを確認。旧履歴試験はSQLite/Repositoryの範囲。実アプリの更新・切り戻しは[今回の別試験](voice-application-rollback-20260919.md)で補う。 |
| Whisper | #408の有限なno_speech_probによるsegment選択と起動時推論の非公開を確認。今回の人の確認は共有50022を使用しており、50025の修正検証には数えない。クリック音はマイク位置で解消したため追加修正・接続先変更を行わない。 |

このレビューは、実行経路・契約・試験・証跡を上記の単位で照合したもの。
176ファイル全行へのCodeRabbit承認や、全ての過去測定を再実行したという主張ではない。
既知の修正を除き、この残レビューから追加の重要な製品不具合は見つからなかった。

## 関連テスト

2026-09-19、f3fd6a09f750dd6d003a0726ab8ec8ba5d723b27で以下を実行。

- Backend: bootstrap API、text delivery、voice-session契約、delivery/lifecycle、private契約、資産、Whisper worker。363 passed / 1 skipped。既存Starlette非推奨警告1件。
- Frontend: input authority、AudioRecorder-backend、voice-session/private契約、voice-session controller、BE VAD診断、割り込みprobe。7ファイル136 passed。
- Frontend初回はworktreeにnode_modulesがなくvitest未検出。既存の固定依存を接続後に上記全体を再実行し成功した。
- 先行の入力bridge修正196件、fixture選択修正283件、#473の計測補正305件は各PRの記録を参照する。

これらは関連unit/moduleであり、新しい人の実マイク受入や実サービス速度測定ではない。

## 最終候補への証跡の適用

照合先はEpic 90e9bc091bcf615979bc41e2ee749d022788cb65。未実測の版のp95保証には読み替えない。

| 根拠の版 | 照合結果 |
|---|---|
| 正式100回 BE/FE 93c8aa7 + TTS 0b97ed9 | TTS実装は0b97ed9から照合先まで差分なし。BE/FEの通常応答後の処理に関係する差分は障害時再同期、取消資源回収、reader失敗理由の診断。その他は試験Profile・記憶参照検証・起動準備・集計。正常な独立応答のモデル・合成条件・推論本文経路を変更していないため、全100回を再実行しない。 |
| 人の確認 3409662 | その後の製品差分は準備処理ごとの期限、終了済みSessionへの遅延bind防止、復元意図の確認順、旧録音器close待機解消。マイク位置調整を条件とした人の確認を保持する。一般的な雑音耐性は未保証。 |
| 実準備失敗・手動再試行／実アプリ往復 f5e8228 | その後は集計精度補正と旧録音器closeの取消待機解消。LiveKit継続入力・履歴保存・privacyの正常経路やDB schemaを変更していない。 |
| 再接続100回 c0f8a5a | その後のroom再接続処理・入力integrityコードに差分なし。準備・取消修正や計測専用差分を区別し、後続会話復帰の証跡を保持する。 |
| TTS故障 3f8d53a + TTS 0b97ed9 | TTS実装は同一。生成中／再生中故障の2ケース、同一Sessionでの後続会話復帰を保持する。後続修正は準備期限・マイク開始取消・診断追加であり、故障復旧判定の置換ではない。 |

空状態p95は[時計精度補正](voice-clock-precision-20260919.md)後1968.00ms（100件）。
固定記憶参照20件は2338.78msで、合否条件には含めない。
休止VADの10件欠測、旧07b8への意味記憶schema切り戻しFAIL、相槌・割り込み各100件率の未保証は保持する。

## 残るmain向け確認

#461・#462・#463のCodeRabbitレビュー後に入った差分は、別の最終分割レビューで確認する。
その結果・必要修正と、最終headのCIおよびContainer imagesが揃うまでは
#460をマージ可能として扱わない。mainへのマージ操作はユーザーが行う。
