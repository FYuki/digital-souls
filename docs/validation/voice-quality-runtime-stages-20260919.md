# 音声応答のruntime工程診断（2026-09-19）

[前回の実会話診断](voice-quality-350-423-424-live-20260919.md)で空状態1件が約2.98秒となったため、
採用設定を保ったまま待ち時間を分解した。以下の単体診断を正式100試行の分母へ加えない。

## Irodoriの工程時間

共有TTSと同じimageを専用の一時コンテナで実行した。共有サービス自体は変更していない。
モデルcacheと登録済みvoiceは読取専用、ネットワークは無効、CCVのvoice設定だけをstdinから入力した。
声はmiori-b3-4221、seed 4221、40 steps、speed 1.02、compile off。
上流の通常create_speechを通し、synthesizeの既存stage_timingsだけを取得した。
音声本文・外部runtimeログ・実会話データは公開していない。

- [初回の準備失敗](../artifacts/voice-quality-irodori-stage-0919-01.json)はIncompleteSnapshotError。
  製品workerが取得する一部ファイルと診断の要求範囲が異なっていた。
  allow_patternsを製品と合わせて再実行した。元の失敗を上書きしていない。
- [修正後の診断](../artifacts/voice-quality-irodori-stage-0919-02.json)はwarmup 1件と固定短文3件が成功。
  初回準備込みは13,755.39ms。下表はその後の3件であり、初回を高速値へ差し替えていない。
- 実行後に所有する一時コンテナを削除し、共有TTSの継続稼働を確認した。
  実行image、script hash、モデル・codec revision、CCV設定hash、WAV hashを記録した。

| 固定文の順番 | API関数全体 | 参照音声の準備 | 長さ予測 | 音声生成 | 復号 |
|---|---:|---:|---:|---:|---:|
| 1 | 839.14ms | 56.00ms | 32.71ms | 710.13ms | 32.44ms |
| 2 | 858.57ms | 68.73ms | 33.08ms | 723.58ms | 26.73ms |
| 3 | 825.34ms | 60.17ms | 32.44ms | 700.78ms | 25.35ms |

共有HTTP queue、STT、LLM、LiveKit、ブラウザ出力はこの表に含まない。
今回の短文では生成が支配的であり、参照準備の省略だけで約1秒を短縮できるという証拠はない。
stepsや採用音声を変えて速度達成に置き換えない。

## Ollama公式版の比較

上流の[GGUF metadata抽出修正](https://github.com/ollama/ollama/commit/ea8d65004affdae6464af1c73a55eb34f5a67eb1)を確認し、
共有0.32.5と専用0.34.2を同一モデルdigest・num_ctx 8192・num_predict 1・think false・temperature 0で比較した。
候補imageのdigestはsha256:da6e0dc5651df159e45686fd663c4dbe1624a52c44d7280eeac1551d8f865532。
専用サービスはlocalhost:11534。共有のblobs/manifestsは読取専用で参照し、
候補版が作るmetadataはコンテナ内に保存する。共有版のバイナリ・設定・モデルを更新していない。

[全10件の結果](../artifacts/voice-quality-ollama-0342-comparison-0919-01.json)では、
両版とも入力token数11で、各5件が成功した。

| 版 | 初回の全時間 | 2件目以降の全時間 | 2件目以降のload_duration |
|---|---:|---|---|
| 0.32.5 | 11,762.85ms | 292.84 / 285.08 / 287.43 / 299.88ms | 253.32〜261.05ms |
| 0.34.2 | 38,083.48ms | 1,219.44 / 39.96 / 32.38 / 33.70ms | 0.63〜1.22ms |

候補版の最初の2件の遅さを除外しない。新旧の同時常駐時はGPU使用量が約15GBで、
候補版の初回にloadとprompt evaluationの長い時間を観測した。
常駐後のload_durationは短縮したが、単体5件から実会話p95の達成や共有版更新の妥当性を確定しない。
共有モデルが自然に解放された状態で、既存の実会話runnerによる追加診断を行う。

## 候補版実会話の設定不一致（350-ollama-0342-0919-01）

この試行は候補版0.34.2の性能評価には使用しない。
inference envのOLLAMA_BASE_URLを11534に変えたが、integration-irodori Profileの固定11434が解決時に優先された。
resolved-profile.jsonのdependenciesとderivedEnvironmentがともに共有11434を示す。
candidate-provider.jsonは候補API自体のversion確認に限られ、Backendがその候補を使った証明ではない。
元の記録は変更せずcandidate-disposition.jsonを追加した。

実際の共有経路では1試行成功、TTFA区間3224.47–3226.27ms、gap 0ms、全文再生完了だった。
準備時間4961msはTTFAから分離する。Stage 1の100試行受入はNOT_RUNのままである。
候補専用Profileと接続先境界のテストを追加してから、新しいrun IDで測定する。
[匿名の設定不一致記録](../artifacts/voice-quality-ollama-0342-misroute-0919-01.json)を参照。

## 候補Profileの初期状態検査失敗（350-ollama-0342-0919-02）

専用Profileを追加した版8489b2fでは、resolved-profile.jsonの接続先は11534へ一致し、
共有11434のモデルが試行前後とも未ロードであることを確認した。
ただし初期状態を検査するvoice_quality_stateの許可Profileに追加漏れがあり、発話入力前に停止した。
1件予定・0件成功・1件失敗、TTFAは欠測。短時間で終了したためnative SDKも採取できなかった。
[準備失敗の記録](../artifacts/voice-quality-ollama-0342-preparation-0919-02.json)を保持し、
状態検査・リソース観測・集計の候補Profile対応を追加して再試行する。

## 候補APIの事前接続timeout（350-ollama-0342-0919-03）

次の試行はrunner起動前、候補/api/versionの5秒timeoutで停止した。
コンテナはrunning・OOMなしで、追加の読み取り照会では0.34.2が応答した。
[事前接続失敗](../artifacts/voice-quality-ollama-0342-preflight-0919-03.json)を記録し、
サービス再起動やtimeout緩和をせず、別ID04で再試行した。

## 候補版の実会話1試行（350-ollama-0342-0919-04）

[候補専用Profileの修正](https://github.com/FYuki/digital-souls/pull/440)を含む880f61fで、
1件予定・1件成功・再試行なし。resolved-profile.jsonの依存先と派生envがともに11534へ一致し、
共有11434のモデルが試行前後とも未ロードであることを確認した。
空状態hashは前回と同じ62d55bf7b16253914146f5ea466ee6a65b129056426129c2e8421b9cc87353e9。
形成・統合schedulerは無効、履歴・記憶行数は0、RAG無効を検査した。

発話終了から実ブラウザ再生までのTTFAは**2441.63–2443.33ms**。
準備時間は21013.30msで別計上する。
全文254400 frames（入力253440＋codec padding960、48kHz）、265 packetを再生し、
gap 0、観測欠測0、transcript一致とsession終了を確認した。native SDKの実ロード証跡もverifiedだった。

| 工程 | 実測 |
|---|---:|
| STT | 146.95ms |
| prompt準備 | 154.16ms |
| LLM request→first token | 167.22ms |
| 最初のTTS合成 | 980.32ms |

共有版の独立1件2975.83–2977.43msより短いが、単一試行同士の比較でありp95改善量は確定しない。
候補版でも2秒を超えており、正式100試行の受入はNOT_RUN、Stage 2もNOT_RUNのまま。
候補版への共有環境更新を承認済み・完了とは扱わない。
元の誤接続01、準備失敗02、事前接続失敗03を含めて保持する。
[匿名の実会話証跡](../artifacts/voice-quality-ollama-0342-pilot-0919-04.json)を参照。
