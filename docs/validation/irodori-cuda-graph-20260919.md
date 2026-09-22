# Irodori CUDA Graphの固定条件による高速化診断（2026-09-19）

実装は[PR #442](https://github.com/FYuki/digital-souls/pull/442)、測定証跡はこの文書と
[manifest](../artifacts/irodori-cuda-graph-20260919/manifest.json)に分離する。
実装commitは f7f43196ad904b30fdc7d5b6c1d4d357aba5f152。
共有TTS/Ollamaの設定・配備を変更せず、専用の一時GPUコンテナで検証した。
共有モデルと登録音声はread-only mountとし、候補終了後に所有コンテナだけを回収した。

## 採用する最適化と固定条件

ユーザー提示の[note記事](https://note.com/rotejin/n/n6f46515de2ac)と
[Zenn記事](https://zenn.dev/yoshitetsu/articles/e616831f96b44a)を参考に、
演算起動の負荷削減を優先した。後者のMPS・旧500Mモデル・step数変更の比較値を、
現行CUDA/v4.1の期待値として転用しない。記事の数値は今回の受入証拠ではない。

RF forwardを要求内CUDA Graphで再利用する。
[PyTorchのCUDA Graph条件](https://docs.pytorch.org/docs/2.14/notes/cuda.html#cuda-graphs)に従い、
side streamでwarmupし、静的な入出力領域を確保する。
[timestep埋込みの固定source](https://github.com/Aratako/Irodori-TTS/blob/8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71/irodori_tts/model.py#L45)
内のCPUからCUDAへの定数転送はcaptureできないため、同じCUDA演算順で求めた定数を要求内に保持する。
全入力は各forwardで更新し、CFGが同形状の複数出力を参照しても上書きされないよう出力を独立させる。

- RTX 4070 Ti SUPER、CUDA、BF16、非量子化。torch.compileは無効。
- Irodori code 8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71、
  model 2b28324dc263ed5e6638b3cf3dd94c82ead07b4b、
  codec 47376ee24834d7a05a48ebabfe3cde29b3c5e214。
- miori-b3-4221、採用B3 caption、seed 4221、speed 1.02、40 steps、chunking無効。
  duration推定と音声の末尾処理を変更しない。
- 空きVRAM確認・最大2形状・入力64MiB上限はcapture前の条件で、一時メモリ使用量の厳密な上限ではない。
- 候補image: sha256:6c272aa27079cdaf7948bbfb07e66c748b5c8b9d1fd925e562ef95ec63b278cf。
  base image: sha256:16191101d83b43a94ad8834b4f5ef5a8d45a8136282dec1e53239a98c87b3f12。

## 最終イメージの対比較

[再現用probe](../../scripts/voice_quality/probe_irodori_cuda_graph.py)で、
準備1回を分離し、固定5文×3反復×2方式の30要求を実行した。
方式の順番は反復ごとに入れ替えた。最終結果は
[image-01](../artifacts/irodori-cuda-graph-20260919/irodori-graph-0919-image-01.json)を正とする。

方式別中央値は **824.13ms → 623.26ms（24.4%短縮）**。
これはサーバー内create_speech呼出しからWAV完成までの時間であり、
発話終了からブラウザ再生開始までのTTFAではない。
Graph側15要求は各40 forward・1 captureを記録した。

| 条件（各方式3回） | WAV長 秒 | 通常中央値 ms | Graph中央値 ms |
|---|---:|---:|---:|
| 固定文1 | 1.52 | 798.3 | 618.6 |
| 固定文2 | 2.60 | 803.6 | 618.4 |
| 固定文3 | 2.40 | 806.0 | 602.6 |
| 固定文4 | 6.28 | 836.2 | 643.6 |
| 固定文5 | 11.36 | 903.4 | 756.2 |

全30要求で対応する音声のframe数が一致した。短文3種のPCMはbit一致ではなく、
通常版の反復でもPCM16のRMS差12.86〜18.08、Graph版でも12.70〜17.66を記録した。
長文2種はこの比較でPCM一致だった。数値差が小さいことを聴感同等性の証拠とはしない。
人の試聴、ノイズ・声質・読み・抑揚・間合いの受入は未実施。

同じ形状の条件tensorを同じstorage上で変更した場合の結果更新、
次のforward後も前の出力が維持されること、3形状目で通常forwardへ戻ることを、
合成前の実GPUチェックで確認した。
VRAM値はprocess内の累積/時点値であり、独立したpeak比較ではない。
VRAM削減効果は主張しない。GPU共有条件の統制済み100件や同時負荷試験ではない。

## 試作・失敗の保存

以下は最終結果へ混ぜず、失敗・途中結果も保存した。

| run | 結果と位置付け |
|---|---|
| schedule-01 | scheduleのCPU同期集約。明確な速度差がなく不採用 |
| graph-01 | capture失敗。初版probeの制約で途中trial欠測 |
| graph-02 | capture失敗。準備と直前baselineの2行を保持 |
| graph-03 | capture内の定数tensor転送が原因と特定。途中2行と失敗stackを保持 |
| graph-04 | 試作版9対比較。約817→478ms。ただし実装版の出力保護等を含まず、採用結果にしない |
| product-01 | 実装版の初回15対比較。約816→621ms |
| product-02 | 容量検査を初回形状に限定。約818→616ms |
| image-01 | 固定イメージ・再現用probeによる最終15対比較。上記を参照 |

prototype/product測定は旧固定イメージ内へ候補moduleを注入したローカル診断。
base_revisionだけで実装済みとせず、script hashで識別する。
最終image-01とサービス試験はcommit付き候補イメージを使用した。

イメージbuildも3回失敗した。1〜2回目はstdin tarの識別、3回目はBuildKitが
ローカルimage IDをDocker Hub名として解釈したことによる失敗。
専用一時ディレクトリと、内容IDを確認した専用local tagで4回目に成功した。
共有サービスのコンテナ・永続設定は変更していない。

## サービス起動・異常後の復旧

[service-01](../artifacts/irodori-cuda-graph-20260919/irodori-graph-service-0919-01.json)では
専用loopback port 50026のHTTPサービスを起動した。

- 起動全体12.996秒、worker準備11.963秒。通常の合成時間から分離した。
- 正常合成2回: 655.65ms / 772.65ms、48kHz PCM16 WAV。
- 専用workerだけを合成中にSIGKILL。1要求が502 / tts_worker_failedで終了。
- 約11.962秒で再準備し、後続2回の実合成が698.95ms / 670.03msで成功。
- 所有候補コンテナを回収し、共有TTSが稼働中であることを確認した。

これは実worker停止の復旧試験であり、実CUDA OOM・capture例外・あらゆる障害の一括受入ではない。
CPU単体では設定の既定OFF/不正値拒否、要求間のGraph非共有、例外時の差替え復元、
capture前の容量・空きVRAM・未知入力・形状上限を確認した。
関連48件、irodori_serviceのmypy、diffチェックが成功した。

## 再実行と残条件

候補イメージの一時コンテナへ、上記probeを標準入力やread-only mountで渡し、
stdinに採用CCVのtts_configだけを与える。stdoutは匿名JSONとし、ネイティブログと音声本文を保存しない。
HF_HUB_OFFLINE / TRANSFORMERS_OFFLINEを設定し、既存cacheとvoicesはread-only、
書込み先は専用tmpfsのみとする。既存の共有コンテナ内でprobeを実行しない。

DS_IRODORI_CUDA_GRAPHは既定OFFのまま。これは#350/#423/#424の性能受入完了ではない。
既存Ollama候補との組合せによる実ブラウザ小規模診断、空状態の独立100試行とTTFA p95≤2000ms、
実マイク・聴感・他の品質条件を継続する。記憶参照はその後の計測のみで受入条件に含めず、
記憶形成の影響調査を追加しない。
