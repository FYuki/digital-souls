# 準備失敗の理由表示と手動再試行（2026-09-19）

固定候補f5e8228bで、専用devアプリのTTS readiness要求だけをローカルproxyから503にした。
共有TTSの停止・再起動・設定変更は行っていない。

1. 初回開始は503 / tts_not_ready / stage=ttsとなり、画面に音声合成の準備失敗と再試行の案内を表示した。
2. マイク表示はOFFとなり、マイクボタンから再試行できた。
3. proxyの失敗を解除して1秒待っても自動再開せず、要求IDの追加もなかった。
4. 利用者の再クリックで新しい開始要求を作成し、同じ画面でstandbyに到達した。
5. 固定speech-v2を実STT/LLM/TTS/LiveKitへ送り、実再生完了・履歴completed 1件・終了API 200を確認した。
   native SDKはverified、transport failureは0件だった。

準備全体に時間上限を加えていない。これはTTS readiness故障1ケースの実接続証拠であり、
全Providerの全エラーを実故障で網羅した保証ではない。他の理由コード・個別timeoutは関連unit/moduleで確認する。

[結果](../artifacts/voice-preparation-retry-20260919/result.json)と
[実行ソースのhash](../artifacts/voice-preparation-retry-20260919/manifest.json)を保存した。
元の試験は候補worktreeのfrontend/test-results/practical-preparation/<run>/harnessで実行した。
同梱specとconfigをその相対配置に置き、PREPARATION_ROOT、PREPARATION_REPORT、PREPARATION_ARTIFACTSを
新規の専用ディレクトリへ指定する。専用proxy 50526から共有TTS 50024へ転送し、
候補BackendだけのIRODORI_BASE_URLを50526へ向ける。fail-readinessファイルの有無で故障を切り替える。
終了時はEnvironment CLIで所有dev環境を停止し、所有proxyも終了した。実マイク候補18573は維持した。

## CodeRabbitのforceOff指摘の判断

#463の4053260569は、forceOff=trueならボタンを常時無効化し、開始を拒否する提案だった。
現在のAppはerror/endedの間もforceOffをtrueに保ち、利用者の次の開始操作で状態を切り替える。
そのまま常時無効化すると、この実確認で成功した手動再試行を妨げる。
選択なし・別会話との不一致・終了処理中は親のdisabledでも制御するため、この提案は採用しない。
これは雑音・クリック音への対策ではない。停止待ちの未解放という別指摘4053260567は#471で修正した。
