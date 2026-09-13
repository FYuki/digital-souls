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
| 発話権を取る割込 | 3独立Session | 実再生中の固定音声入力、take_turn判定、旧応答取消、音声graph解放、Session終了 |
| 無発話の終了・切断 | 2Session | 正常終了とbrowser切断後の再接続猶予終了をnative記録で確認 |
| 専用LiveKit bridgeの2秒切断 | 1Session | 制御と実音声の回復、復旧後の次発話、Session終了 |

割込後の旧音声提示、受信音声、本文提示・受信、Coreが受け取るprovider結果は、観測した3件の全境界で遅延結果0だった。出力停止の再検証も3件確認済み。ただし既存reporterの最低100試行を満たさないため、正式な割込率・stale受入は未合格のまま記録する。

ネットワーク復旧から制御回復までの上限値は360.2ms、実音声回復まで2553.6ms（1回）。重複出力区間0、packet証拠欠測0、出力経路失敗0。ネットワーク切断が制御へ作用したことを確認し、単なるTCP疎通や後続発話だけを回復の代用にしていない。診断は共有TTSを停止せず、専用ラベル・独立bridge・loopback公開ポートを確認したLiveKitだけへ作用させた。再接続成功率99%の証明ではない。

実行revisionは連続会話・割込・終了が9922b9b、ネットワーク障害がdbc620e（診断Profile追加8fc2ce8を含む）。[実スタック診断集計](validation/irodori-329/real-stack-diagnostics.json)、[割込集計](validation/irodori-329/take-turn-report.json)、[旧応答停止監査](validation/irodori-329/stale-output-report.json)に匿名値と元証跡hashを保存した。

割込run 01は展開済み固定WAV不足で音声入力前に失敗。保存済み原音声・全300件のrecipeとhashを検証して展開し、run 02で測定した。ネットワークrun 01は専用LiveKitのキー区切り構文により起動前に停止し、専用環境ファイルの空白を補正してrun 02で測定した。これらの前提失敗は実応答成功数に含めない。

## compile追加実験

同じモデル・声・40 stepsのままIRODORI_COMPILE_MODEL=true、IRODORI_COMPILE_DYNAMIC=true、compile thread 1、専用cacheで試した。初回warmupがtts_preparation_failedとなり、実合成・性能評価には到達していない。準備上限はこの実験だけ900秒とした。通常のcompile無効設定へ復帰済み。詳細診断は共有サービスの最長15分停止を伴うため自動承認が拒否し、ユーザー確認待ち。高速化成功・正式採用として扱わない。

記録: [compile初回準備失敗](validation/irodori-329/compile-dynamic-startup-failure.json)。

## 未完了

性能目標未達、compile準備失敗の原因確認、区間間の試聴とユーザーのモデル最終判断を残す。小規模の実接続診断を既存の100試行品質受入の代用にはしない。閾値の自動緩和、mainマージ、#329/#330のクローズは行っていない。
