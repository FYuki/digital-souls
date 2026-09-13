# Irodori-TTS実GPU検証

#329 / #330の固定設定を用いた検証記録。性能・音質の受入とモデルの最終採用は未完了。

## 固定条件

- モデル: Aratako/Irodori-TTS-v4.1-Small、revision 2b28324dc263ed5e6638b3cf3dd94c82ead07b4b、非量子化CUDA/BF16。
- codec: Aratako/Semantic-DACVAE-Japanese-32dim、revision 47376ee24834d7a05a48ebabfe3cde29b3c5e214。
- 参照音声: miori-b3-4221、B-3 Caption、seed 4221、40 steps、speed 1.0。正本はcharacters/miori/assets/voice/miori-b3-4221.jsonのreference_synthesis_request。
- 共有サービス: Ubuntu-dogfood、UID/GID 10001:10001、127.0.0.1:50024。通常設定は待機8件、待機10秒、推論30秒、準備300秒。
- サービスコード: 416cd82。実機image IDはsha256:16191101d83b43a94ad8834b4f5ef5a8d45a8136282dec1e53239a98c87b3f12。同一採用revisionの保存済み標準ベースから構築し、非rootで実行した。CI配布imageと同一digestとは扱わない。
- 比較用人格・固定入力・STT/LLM・LiveKit・Frontend/Backend/ready gateの専用ポートは同一。VOICEVOX側は既存CCVのspeaker_id 14を使用する。比較中もIrodoriを常駐させ、モデルのGPU常駐条件を維持する。
- Irodori計測commit: df57441d44e377d47e9a23fdb250d8bb82151c67。VOICEVOX比較commit: e4cb627a1f6cf6e03e9c2f697b3ab96174319d7d。通常実装は共通で、人格はTTS設定以外が一致する。
- 各正式測定は5 warmup＋100独立Session。固定PCMの正解発話終了境界から、AudioWorkletとbrowser出力時計で確認した最初の再生までを測る。物理スピーカーの音響時刻ではない。
- RAGは無効、会話・記憶DBは試行ごとに空を確認する。モデルの最終音質や通常dogfood全条件の証明とは区別する。

## 共有サービスの準備と実合成

再起動後のモデル読込・warmupは20.333秒。固定voice IDで4.88秒のWAVを生成し、HTTP要求から受取まで1.167秒だった。単発値であり準備時間のp95や通常会話TTFAではない。選定原本のSHA256は維持した。

WSLのsystemdサービスだけではdistributionのアイドル終了を防げないことを実機で確認した。既存dogfood起動手順と同じflock付きkeepaliveを使用し、所有ユーザーdigital-soulsのロック競合終了コード75を確認した。dev/testによる共有サービスの起動・停止は行わない。

## 通常設定の正式測定

| 条件 | 試行 | 失敗 | TTFA欠測 | TTFA p50 | TTFA p95 | 先頭TTS生成 p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Irodori・compile無効 | 100 | 0 | 0 | 2501.1ms | 2708.4ms | 1103.4 / 1284.9ms |
| VOICEVOX | 100 | 0 | 0 | 1815.8ms | 1905.6ms | 415.2 / 466.4ms |

VOICEVOXは既存のTTFA p95 2000ms目標を満たす。Irodoriは未達で、p95はVOICEVOXより802.8ms長い。再生gap合計のp50は0ms、p95は101.3ms、単一gap最大値のp95は91.1ms。合成成功・応答完了を音質合格として扱わない。

匿名集計: [Irodori通常設定100試行](validation/irodori-329/irodori-uncompiled-controlled.json)、[VOICEVOX比較100試行](validation/irodori-329/voicevox-controlled.json)。実再生、transcript一致、Session終了、初期状態、SDK実ロード、相関とschemaを検証した。TTFAの欠測は0件だが、資源サンプルには起動前3件と観測失敗1件があり、0使用量へ補完していない。

## 共有サービスの優先処理とHTTP取消

同じ固定モデルを使用する実GPU要求で、実行中devを中断せず、待機dogfood、待機devの順に処理することを確認した。待機中のHTTP取消はキューから削除された。実行中のHTTP取消直後はactive=1を保持し、実際の推論が終わってから次のdogfood要求が成功した。最終状態はactive=0、pending=0だった。HTTP切断をGPU処理停止として扱わない。

記録: [実GPU優先処理・HTTP取消](validation/irodori-329/real-priority-cancel.json)。この検証はworkerの強制停止・再起動を含まない。

## 実GPU workerの期限と回復

処理中active=1を確認した後、共有サービス自身のGPU workerだけをSIGSTOPで一時停止した。通常の推論上限30秒により約30.670秒でHTTP 504 / tts_inference_timeoutが返り、旧workerの終了を確認した。readyは一時503となり、自動再準備後に次の実合成が成功した（3.48秒音声、要求から受取0.903秒）。回復後のactive/pendingはともに0。

記録: [実worker期限・再起動・次要求](validation/irodori-329/real-worker-deadline.json)。実行中HTTP取消との違いを実プロセスで確認した。強制停止の対象は所有コンテナのworkerだけで、他のGPU処理は対象にしていない。

## 実会話・割込・再接続

通常設定で次の小規模診断を完了した。100試行の性能測定や、再接続成功率の統計的受入とは区別する。

| 診断 | 実行数 | 確認結果 |
|---|---:|---|
| 同一Session連続会話 | 3発話 | 同じSession、異なるresponse、transcript一致、各音声track対応、Session終了 |
| 発話権を取る割込（pilot） | 3独立Session | 実再生中の固定音声入力、take_turn判定、旧応答取消、音声graph解放、Session終了 |
| 無発話の終了・切断 | 2Session | 正常終了とbrowser切断後の再接続猶予終了をnative記録で確認 |
| 専用LiveKit bridgeの2秒切断 | 1Session | 制御と実音声の回復、復旧後の次発話、Session終了 |

割込後の旧音声提示、受信音声、本文提示・受信、Coreが受け取るprovider結果は、観測した3件の全境界で遅延結果0だった。出力停止の再検証も3件確認済み。ただし既存reporterの最低100試行を満たさないため、このpilot単独の正式な割込率・stale受入は未合格のまま記録する。後述の独立100試行を正式な追加検証とする。

ネットワーク復旧から制御回復までの上限値は360.2ms、実音声回復まで2553.6ms（1回）。重複出力区間0、packet証拠欠測0、出力経路失敗0。ネットワーク切断が制御へ作用したことを確認し、単なるTCP疎通や後続発話だけを回復の代用にしていない。診断は共有TTSを停止せず、専用ラベル・独立bridge・loopback公開ポートを確認したLiveKitだけへ作用させた。再接続成功率99%の証明ではない。

実行revisionは連続会話・割込・終了が9922b9b、ネットワーク障害がdbc620e（診断Profile追加8fc2ce8を含む）。[実スタック診断集計](validation/irodori-329/real-stack-diagnostics.json)、[割込集計](validation/irodori-329/take-turn-report.json)、[旧応答停止監査](validation/irodori-329/stale-output-report.json)に匿名値と元証跡hashを保存した。

割込run 01は展開済み固定WAV不足で音声入力前に失敗。保存済み原音声・全300件のrecipeとhashを検証して展開し、run 02で測定した。ネットワークrun 01は専用LiveKitのキー区切り構文により起動前に停止し、専用環境ファイルの空白を補正してrun 02で測定した。これらの前提失敗は実応答成功数に含めない。

## 割込・旧応答停止の正式100試行

2026-09-14 JSTに、同じ通常設定・選定音声・計測revision dbc620eで、固定ラベル付きtake_turnの100独立Sessionを追加測定した。run IDはirodori-329-take-turn-100-01。実再生との重なり、take_turn判定、取消、Session終了を全100件で確認し、失敗・欠測は0。10語句の位相と音量を変えた既存fixtureであり、100人の実発声を代表しない。

| 指標 | 境界 | p50 | p95 | 既存p95上限 |
|---|---|---:|---:|---:|
| local_playback_stop | clientのspeech_started→local_playback_stopped | 1063.0ms | 1890.1ms | 3000ms |
| turn_decision | clientのspeech_started→turn_decision_client | 1063.0ms | 1890.1ms | 3000ms |
| cancel_after_decision | serverのtake_turn_decision→server_cancelled | 26.8ms | 31.6ms | 200ms |
| barge_in_cancel_total | clientのspeech_started→server_cancelled_client | 1106.5ms | 1923.3ms | 3500ms |

旧応答のブラウザ出力は、post-gainの実出力時計と保存した停止窓を再検証した。旧音声提示、受信本文、live/history本文提示、Coreのprovider結果受信は全100件で0。出力停止証拠100件、未確認・欠測0で、既存reporterの最低100件・coverage・全境界観測・stale提示の判定をすべて満たした。

受信音声は2件で取消時刻の境界と重なり、合計で最大2packet／1920sampleが取消後受信の可能性として残る。確実な取消後受信は0で、これらも旧音声として再生されていない。受信と提示の境界を混同せず、受信packetすべてが確実に0だったとは扱わない。

[正式割込100試行](validation/irodori-329/take-turn-100-report.json)と[正式旧応答停止監査](validation/irodori-329/stale-output-100-report.json)に、匿名集計・元manifest/trace/fixture/reporterのhashを保存した。先の3件pilotのファイルは変更していない。

## 実ブラウザ出力の試聴用録音

同じ選定設定を用い、実Backend／Whisper／LLM／Irodori／LiveKitの応答「こんにちは。今日も穏やかな時間ですね。」を、ブラウザのpost-gain最終出力から分岐録音した。最初の待ち時間を含む14.16秒、48kHz monoの試聴用WAVを作成し、元WebMも保持した。録音時の追加Opus符号化を経るため、無加工の合成WAVや正式性能測定とは区別する。

ローカル試聴専用revision e9d7ebd、run ID irodori-329-listening-01。WAV SHA256はe8c6fd6ab0072a664b600d10cd221ff17e8269e8afe587fe957000d6d0254ed6、元WebMは16c536d190cdf8fa5dce89f3be4ae77d9d1bcdef331128cfd4c1d919b6577d20。録音経路の追加は製品コードに含めない。区間間の自然さ・音質のユーザー受入は未完了。

## compile追加実験

同じモデル・声・40 stepsのままIRODORI_COMPILE_MODEL=true、IRODORI_COMPILE_DYNAMIC=true、compile thread 1、専用cacheで試した。初回warmupがtts_preparation_failedとなり、実合成・性能評価には到達していない。準備上限はこの実験だけ900秒とした。通常のcompile無効設定へ復帰済み。当初は共有サービスの最長15分停止について自動承認が拒否したが、2026-09-14 JSTにユーザーが高速化診断を許可し、以下の追加診断を実施した。高速化成功・正式採用として扱わない。

記録: [compile初回準備失敗](validation/irodori-329/compile-dynamic-startup-failure.json)。

## コンパイル失敗の詳細診断（2026-09-14 JST）

同じimage・モデルrevision・参照声・40 steps・CUDA/BF16を使い、共有サービスを一時停止して上流の通常合成関数へ固定文を渡した。会話データは使用せず、通常workerが抑制する詳細例外を診断専用ログに記録した。PyTorch 2.10.0+cu128、Triton 3.6.0、SymPy 1.14.0。

初回診断ではwarmup開始から123.241秒でInductorErrorとなった。失敗経路はforward_with_encoded_conditions → CUDAコード生成 → extract_normalized_read_writes → SymPy Expr.is_constant → torch.utils._sympy.functions.Mod.eval。定数判定が負数を代入し、非負値を前提とするMod.evalのassertionに到達した。同じ経路の報告が[PyTorch #170550](https://github.com/pytorch/pytorch/issues/170550)にある。OOMやコンパイラ実行ファイル不足を原因とする例外ではない。

この診断のサービス停止から通常設定の復帰まで147.224秒。復帰後、dogfood側から固定文を要求し、HTTP 200、非無音の48kHz mono WAV（4.88秒、要求から受取0.937秒）を確認した。WAV SHA256はdd50e907a48057c12cbf1d9efbbed6f0c8ec7b5edb2e09139ab581b9628c1e8aで、以前の通常合成と一致した。

同じ診断内の追試では、PyTorchに存在する環境変数TORCHINDUCTOR_COALESCE_TILING_ANALYSIS=0だけを追加し、compile自体とdynamic指定は維持した。ライブラリの更新やソースへのパッチは行っていない。この設定は失敗したメモリアクセス解析を無効化するためのもので、通常サービスへの採用設定ではない。

| 固定文の条件 | 上流通常API関数の所要時間 | 結果 |
|---|---:|---|
| モデル読込とcompileを含むwarmup | 266.875秒 | 非無音WAV、4.40秒 |
| 初めての別文 | 112.884秒 | 非無音WAV、4.84秒 |
| 上と同じ文の繰り返し | 0.539秒 | 同じWAV SHA256、4.84秒 |

別文は「おかえりなさい。今日は、どんな一日でしたか。」。2回のWAV SHA256は980c6ba103b5e4cb3e5d547cfc08fca7c8e52ef665940426de153e43e6e3ce1cで一致した。通常設定の4.88秒WAVとはhash・長さが異なるため、同じ音声品質が保証されたとは扱わない。

直接関数呼び出しによる診断であり、共有workerの30秒期限・STT/LLM/LiveKit/ブラウザを通る正式TTFAではない。初めての別文112.884秒は通常workerの30秒期限を超える。同じ文の単発0.539秒だけを根拠に採用せず、compileは無効のままとする。異なる入力での遅延は再コンパイル等の可能性があるが、guard/recompileログは今回取得しておらず、具体的な再コンパイル条件は未確定。今後はその条件と入力長への依存を確認し、固定文だけの事前準備を任意の会話への保証にしない。

追試後も通常設定へ復帰し、dogfoodから4.88秒のWAVを0.932秒で実合成した。hashは追試前の通常設定と一致。2回の停止時間は147.224秒＋404.238秒＝551.462秒（約9分11秒）で、合計15分以内。診断コンテナを終了し、元のimage・設定へ戻した。

[固定条件の詳細診断と復帰記録](validation/irodori-329/compile-approved-diagnostic.json)に、2回の結果・生ログと診断スクリプトのSHA256を記録した。生ログは診断所有ディレクトリに保持し、会話本文や設定ファイル全体をリポジトリへ追加していない。

## 未完了

性能目標未達、compile回避条件での未知入力の準備遅延と実会話性能・品質確認、区間間の試聴とユーザーのモデル最終判断を残す。小規模の実接続診断を既存の100試行品質受入の代用にはしない。閾値の自動緩和、mainマージ、#329/#330のクローズは行っていない。
