# LiveKit再接続SDKの固定ソースビルド（#150）

## 判断と根拠

Python SDK 1.1.16 / FFI 0.12.73の実通信断で、SDKの試行間に約5.13秒の待機を観測した。
同じ上流ソースの再試行間隔を変更した独立3試行では、制御・音声が2.35〜2.71秒で復旧した。
[実測と制約](../livekit-voice-quality-pilot-2026-09-06.md)を根拠に、Backendコンテナへ同じ変更を組み込んだ候補を用意する。
この判断だけで#150の受け入れを完了としない。固定した候補版で独立100件の再測定と他の必須検証を行う。

## 配布と更新

現在のimage workflowの配布対象はLinux amd64である。Backend Dockerfileで公式の固定ソースを取得し、
初期10秒の再試行間隔を250〜500msへ変更するpatchを適用する。以後は従来の最大7秒backoffを維持する。
新規試行の上限は64回・開始から60秒。進行中の試行のtimeout、認証拒否、serverによる終了、明示closeはSDKの既存処理に従う。

Python SDKは引き続き`livekit==1.1.16`とし、FFIだけを`/opt/digital-souls/livekit`へ同梱する。
`LIVEKIT_LIB_PATH`でこのライブラリを選ぶ。ホストのvenvは変更しない。
コンパイラやビルド用ソースは最終イメージへ含めず、上流・WebRTC・libyuv・protocolのライセンスを保存する。
`build.json`にはPython SDK/FFIの版、上流commit、patchと実ライブラリのSHA-256、対象platform、再接続policyを記録する。
ビルドの最後にPython SDKとの版の対応とFFI初期化を確認する。

今後SDKを更新する際は、固定ソース・submodule・Python版・FFI版の対応を同時に更新し、
再接続policyが上流で設定可能になった場合はpatchを除去できるか確認する。
同じ障害注入と100件の受け入れで互換性を確認し、測定の版を更新する。
今回のビルドはCUDA動画codecを含まない。対象はBackendの音声接続であり、
他platformやRTCのGPU動画codecへ適用する場合は、別途そのビルドと実機能を確認する必要がある。

## 測定と版の照合

正式runnerは測定commitごとの専用Backend・Frontend imageタグを使い、他worktreeのdevタグと競合させない。
所有するtest Backendだけを対象に、同梱build.jsonと実ライブラリのSHA-256、
Python package版、稼働中processの実行可能mappingのpath・inodeを照合する。
この検査用process自身のmappingを根拠にせず、既に稼働するprocessのmappingを確認する。
固定ソース・patchと一致した場合だけ、各runへ`native-sdk.json`を保存する。
process ID、container ID、path、設定値、認証情報を匿名SDK証跡へ転記しない。

再接続cohortはこの証跡を必須とし、同じ集計へ異なるSDK版・patch・実ライブラリハッシュを混在させない。
証跡のない過去のrawは、明示的な旧集計経路でのみ再集計できる。既存artifactを後付けのSDK証跡で書き換えない。


## ブラウザ側の猶予時計

実接続診断でwall clockの約1.78秒の巻き戻りを観測したため、ブラウザの再接続policyは
SDKから渡される`Date.now()`差分に猶予を依存させない。最初の再試行から`performance.now()`で測り、
初期10秒の250〜500ms、後半1,000〜1,500ms、40回・60秒上限を維持する。
新しい障害のretryCount 0で起点を再設定する。SDKの認証拒否や明示終了を再試行へ変更しない。
