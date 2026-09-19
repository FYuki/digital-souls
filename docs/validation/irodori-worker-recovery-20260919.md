# 専用Irodori workerの障害復旧診断（2026-09-19）

本記録時点の復旧条件は **未達**。後続の[入力再認可修正と別run](irodori-input-recovery-20260919.md)で2ケースの成功を確認した。生成中の停止では同じSessionの後続音声応答まで確認した。
先頭再生後の停止では旧音声の出力停止を確認したが、その後にaudio_input_unavailableが発生し、
次の音声応答を確認できなかった。空状態の[正式速度結果](irodori-reference-formal-20260919.md)とは別の条件である。

[4回の匿名証跡](../artifacts/irodori-worker-recovery-20260919/evidence.json)と
[試験コード・原ファイルhash](../artifacts/irodori-worker-recovery-20260919/manifest.json)を保存した。
実装した試験基盤は[PR #452](https://github.com/FYuki/digital-souls/pull/452)。
未達ケースを削除したり、成功側と合算して復旧受入合格へ変換しない。

## 条件と注入範囲

TTSは0b97ed943aa660877dccc24b8df25cae6eec1549、
image sha256:d8279a7a43adc105ebe14b4bd3620fe2839c44966ff756c8c93e5dde9fba916c。
Graph256と参照latent cacheを有効とし、固定CCVの声・seed・40steps・speed1.02・BF16を保持した。
共有TTSを故障対象にせず、専用のrecovery ownerとport 50026を用いた。

helperはimmutable image、完全container ID、所有label/name、host network、
port/起動引数を照合する。active=1を観測後に再照合し、
PID 1のuvicorn配下にある単一spawn workerにSIGTERMを送った。
共有Ollama/TTS/Whisper/VOICEVOXの設定・配備・再起動や保存データは操作していない。

最初の要求は固定した「星空の説明を二十文」のtext要求。
生成中と先頭実再生後の2ケースを分ける。active観測からSIGTERMまでの命令転送があるため、
特定のPCM sample再生と同時刻の故障注入とは主張しない。
次会話はspeech-v2を実STTへ送り、実LLM/TTS/LiveKit/ブラウザ出力を確認する。

アプリ本体のsourceは93c8aa7dd569f991e872de5ac75502e8e2111ccbと同じ。
後段runのimageは試験worktreeのrevisionでbuildし、
Frontendの単体試験ファイル以外にアプリ本体の差がないことを照合した。
最新runの実行image IDはevidenceのrunning_imagesに保存した。
旧image tagを固定しただけで実imageも同一だったとは扱わない。

## 全試行

| run末尾 | 試験版 | 生成中 | 先頭再生後 |
|---|---|---|---|
| 01 | 196d236 | NOT_RUN | NOT_RUN |
| 02 | 196d236 | 成功 | 1秒無音の監査条件で不成立 |
| 03 | 1c42bee | NOT_RUN | NOT_RUN |
| 04 | 1c42bee | 成功 | 旧出力停止確認後、次音声応答がtimeout |

01はBackend readiness timeout。根本原因は未確定で、後から03と同じ原因へ分類しない。
03は起動probeでmemory-consolidation/embeddingのunavailableが記録された。
同時期に専用Ollama 11534の読取りがtimeoutし、その後の到達性回復を確認した。
準備失敗を音声ケースの実行済み件数へ入れない。全runで所有Frontend/Backendのteardown完了を確認した。

02の監査は旧node破棄後まで1秒間の出力報告を要求していた。
破棄後の欠測を無音へ補完せず、04では無音interval、finished終端、実出力時計、node切断を照合した。
この試験側の修正と、製品の音声停止動作を区別する。

## 最新runの部分的に確認できた範囲

生成中は実worker停止、応答失敗表示、Session存続、再準備後の固定音声のSTT、
同じSessionの次応答完了とsample数保存・gap 0を確認した。
旧応答の終端はresponse_failedのみで、旧応答の再生完了は記録されなかった。

先頭再生後は失敗通知のrender上限frame 239104以降に非ゼロsample 0。
連続した無音終端は239872、実出力時計は240000まで通過し、監査nodeの出力接続が切断された。
欠測理由はnullで、この終端停止確認のassertionを通過した。
ただし失敗直後の停止遅延0msや、破棄後1秒間の実測無音を意味しない。

続いてaudio_input_unavailableが通知され、UIは入力停止・Session終了・再開案内になった。
後続のspeech_started/utterance_finalized/response_startedは観測されず、90秒待機で失敗した。
TTSのhealth readyだけを次会話回復と扱わない。マイクreaderの終了、PCM clock検査、
入力resetのどれが直接原因かはまだ確定していないため、原因調査・修正後の別runが必要である。

本診断は固定音声の実接続であり、実マイク・聴感、再接続100件、相槌、休止、
更新/切り戻し、全体受入を代替しない。GitHub CIの課金・利用上限の問題も未解消で、PRは未統合。
