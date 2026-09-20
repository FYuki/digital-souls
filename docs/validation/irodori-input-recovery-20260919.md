# 音声異常後の入力再認可とTTS障害復旧（2026-09-19）

修正後の固定版で、生成中・先頭再生後の両ケースについて、
専用TTSの実worker停止から **同じSessionの次の音声応答まで復旧** を確認した。
[先行診断の未達](irodori-worker-recovery-20260919.md)を削除せず、
原因診断3run、準備失敗1run、修正後成功1runの[全記録](../artifacts/irodori-input-recovery-20260919/evidence.json)を保存した。
[原ファイルと試験コードのhash](../artifacts/irodori-input-recovery-20260919/manifest.json)も保持する。

製品修正は[PR #453](https://github.com/FYuki/digital-souls/pull/453)、
試験基盤は[PR #452](https://github.com/FYuki/digital-souls/pull/452)、測定証跡は#451で分離した。
#453は#452上の差分。CIが開始できないため両PRとも未統合である。

## 原因と変更

再生中のTTS故障後、応答音声のRTP timestamp overlapを契機に再同期が起きる。
この経路はBackendのSession世代を進め、マイクreaderを交換する一方、
Frontendへ入力再認可が必要な状態を通知していなかった。
Frontendが旧認可を保持したまま旧readerのaudio_input_unavailableを適用し、
入力停止から次会話へ戻れない状態になっていた。

診断02でreaderの終了が取消と分かり、03でmicrophone_track_replacedに絞り込んだ。
PCM clock・VAD resetの失敗と区別し、本文・PCMを含めない原因コードを保存した。
RTP gap/overlap/ragged gapの3条件で、修正前に復旧状態の通知を欠くことを既存unitで再現した。

同じ経路にも既存の復旧状態管理を適用する。
再同期要求前に旧入力認可を無効化し、権威状態と制御往復を確認してから新trackを認可する。
ネットワーク自体の再接続を待つ必要がない経路であることを明示し、
既に再接続中なら現在の同期要求を再利用する。
後着の古い入力停止通知を新認可へ適用しない既存の世代照合は維持する。

## 全runと固定条件

cohortではなく、故障注入の機能診断である。成功率100件や正式TTFAの分母へ混ぜない。

| run末尾 | revision | 生成中 | 先頭再生後 |
|---|---|---|---|
| 01 | 10b6797 | 成功 | 次音声応答timeout。reader通常log未出力 |
| 02 | 4097696 | 成功 | 次音声応答timeout。取消によるreader終了を記録 |
| 03 | c38c3c9 | 成功 | 次音声応答timeout。reader交換と確定 |
| 04 | 3f8d53a | NOT_RUN | NOT_RUN |
| 05 | 3f8d53a | 成功 | 成功 |

各runは424-input-recovery-0919-XX。04は候補Ollamaの全Targetのstartup probe unavailableにより
Backend準備が失敗した。別途モデル一覧APIの500も観測し、到達性回復後に05を開始した。
API失敗の根本原因まで解消したとは主張しない。自動試験内のretryは0で、失敗runを残した。

最新成功版は3f8d53ad08eec430ff724deec06df1ef9a441002。
アプリ基準93c8aa7dd569f991e872de5ac75502e8e2111ccbに対し、Backendの診断とFrontendの再同期処理を変更。
BE/FEは修正版からbuildし、実行中の両image IDを保存した。
実native SDKはPython 1.1.16 / FFI 0.12.73、
source 63128d01d955d9d8967544f46cff64a361232bf6と既存patch/libraryのhashを照合した。

TTS候補は0b97ed943aa660877dccc24b8df25cae6eec1549、
image sha256:d8279a7a43adc105ebe14b4bd3620fe2839c44966ff756c8c93e5dde9fba916c。
Graph256・参照latent cache ON、compile OFF、miori-b3-4221、B3 caption、
seed4221、40steps、speed1.02、BF16を保持。専用port 50026、専用recovery ownerの単一workerに限定した。
共有サービスやdogfoodの設定・保存データは変更していない。

## 最新runの確認範囲

| 指標 | 生成中 | 先頭再生後 |
|---|---:|---:|
| worker active観測 | 1 | 1 |
| 同じSession数 | 1 | 1 |
| 後続のexpected / rendered samples | 275520 / 275520 | 204480 / 204480 |
| 後続gap samples | 0 | 0 |
| 旧応答の終端 | response_failedのみ | response_failedのみ |
| 旧応答の再生完了記録 | なし | なし |
| transport failure | 0 | 0 |

固定text要求に対して生成中／先頭出力後を区別してSIGTERMを注入し、
失敗表示とSession存続、TTS再準備後のspeech-v2→実STT→LLM→TTS→ブラウザ再生完了を確認した。
TTS readinessだけで回復と判断していない。追加のマイク再開操作は行わない。

先頭再生後はtimestamp_overlapを実測し、audio_input_openedが3回記録された。
旧認可に対するaudio_input_unavailableはrawのCore観測へ残っているが、
UIのマイク状態は初期off→standbyを維持し、同じSessionの次の音声が完走した。
raw errorを削除したり、全イベントにerrorがなかったとは表現しない。

旧出力は失敗通知時点のrender上限frame219392以降で非ゼロsample0、
無音終端220288、実出力時計220544、監査node切断を照合した。
後続応答完了まで旧nodeの非ゼロ出力は0。失敗通知直後の停止遅延0msや、
node破棄後の1秒無音観測を意味しない。

全5runで所有Frontend/Backendのteardown完了を確認。
Backend関連38件、runtime66件、Frontend room/session130件、npm run check、
mypy app305 source filesの局所検証も成功した。
当初のreader mockが新しい理由引数に対応していない4件の失敗は、終了理由の照合を加えて修正した。
実測とは別の検証層として扱う。

## 残条件

本結果はTTS故障後の次会話復旧2ケースの確認である。
実マイク・聴感、固定記憶参照の第2段階、正式な再接続/相槌/休止の分母、
機能競合、一括更新/切り戻し、未完了レビューなどの全受入は継続する。
[空状態の正式速度結果](irodori-reference-formal-20260919.md)は93c8aa7＋TTS0b97の証拠として保持し、
今回の修正版で100件を再測定済みとは扱わない。
GitHub CIは課金・利用上限による開始前失敗が続いており、ローカル検証をCI成功へ代用しない。
