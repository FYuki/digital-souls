# Irodori CUDA Graph候補の実ブラウザ診断（2026-09-19）

[実装 #442](https://github.com/FYuki/digital-souls/pull/442)と
[TTS単体の証跡 #443](https://github.com/FYuki/digital-souls/pull/443)は、対象headの全CI成功後にEpicへ統合した。
この文書は専用Ollama 0.34.2とCUDA Graph TTSを組み合わせた実ブラウザ結果であり、
空状態の独立100試行、TTFA p95≤2000ms、人の実マイク・聴感の受入ではない。

## 固定した構成

[計測Profile #444](https://github.com/FYuki/digital-souls/pull/444)を追加し、
integration-irodori-cuda-graph をOllama 11534 / Irodori 50026へ固定した。
両サービスはexternalで、runnerは共有サービスの起動・更新・停止を行わない。
Frontend/Backend/ready gateは18573/18500/18574、試行ごとに新規test data rootを使用する。
形成・統合schedulerは停止し、空の会話履歴・RAG無効を初期状態receiptとhashで確認した。

- 計測版: 93c8aa7dd569f991e872de5ac75502e8e2111ccb。
- TTS実装: f7f43196ad904b30fdc7d5b6c1d4d357aba5f152、
  image sha256:6c272aa27079cdaf7948bbfb07e66c748b5c8b9d1fd925e562ef95ec63b278cf。
- Ollama候補: 0.34.2、同じgemma4:e4b、chatのcontext 8192、thinking無効。
- 採用声miori-b3-4221、B3 caption、seed 4221、speed 1.02、40 steps、BF16、torch.compile無効。
- 専用TTSは実準備合成成功を確認。/versionのcudaGraphRequestedは起動要求値で、
  各forwardのGraph使用を証明する値ではない。最終TTS単体のGraph使用回数は#443を参照する。

[匿名集計と元証跡hash](../artifacts/voice-quality-irodori-graph-browser-20260919.json)に、
全run、初期状態、工程時間、準備、再生完了、回収確認を保存した。
会話原文・session等の識別子・秘密値を転記しない。設定の秘密値はmemfd経由でrunnerへ渡した。

## 初回の失敗と修正

| run | 結果 |
|---|---|
| 350-irodori-graph-0919-01 | 会話開始前に候補Ollama /api/version が10秒でtimeout。会話試行なし。同じcontainer ID・稼働状態と、その後のAPI正常応答を確認し、再起動しなかった |
| 350-irodori-graph-0919-02 | 旧計測版9f5825eのFrontend計測URL読取が新Profileを拒否。音声fixture未供給。native SDK確認も早期終了で欠測。専用BE/FEの回収を確認 |
| 350-irodori-graph-0919-03 | Frontend修正版93c8aa7で独立1試行成功。実再生完了・gap 0・Session終了を確認 |

#444の初期局所検証はBackend 786件、Frontend module 147件、mypy 364 source files成功。
初回に見つかったrunnerのTTS観測先設定とテストimportの不足3件は、修正後に関連全件を再実行した。
その後の実接続でFrontendのURL読取漏れを検出し、実読取とdogfood利用拒否を確かめる回帰テストを追加。
修正した2ファイルを含むFrontend module 31件が成功し、新しい固定版のBE/FEイメージを再buildした。
局所テストだけで実ブラウザ接続済みとは判断しなかった。

## 独立1試行と小規模cohort

単独03はTTFA **2035.43〜2037.13ms**、準備7974.4ms、全文456000 samples・475 packets・gap 0。
準備時間はTTFAへ混ぜない。下限・上限はfixtureの発話終了時刻の観測幅による。

続く 350-irodori-graph-small-0919-01 は、事前登録した準備5件＋独立測定3件。
全8件成功、欠測0、全件gap 0、形成・統合停止、data root独立、所有BE/FE回収を確認した。
小規模runnerの終了値1は正式100件reporter未実行の仕様であり、8件の実行失敗を意味しない。
正式100件の分母に、この診断や過去の成功試行を混ぜない。

| 測定番号 | TTFA範囲 ms | STT ms | prompt準備 ms | LLM要求→first token ms | 先頭TTS ms |
|---|---:|---:|---:|---:|---:|
| 006 | 2115.83〜2117.63 | 157.16 | 181.42 | 40.17 | 790.59 |
| 007 | 2167.43〜2169.13 | 170.37 | 115.75 | 43.36 | 952.00 |
| 008 | 1949.43〜1951.23 | 131.24 | 146.75 | 35.99 | 699.18 |

工程値はserver同一時計の差分。TTFAはブラウザ時計の実再生観測であり、
工程値を足したものや小標本のp95を正式な受入値として扱わない。
測定側の準備は3件ともreadyで、中央値1380.5ms、最大1382.3msだった。
3件中2件が2000msを超えるため、速度受入は未達のまま。

cohort終了後に専用TTSだけを停止・削除し、共有TTSが稼働していることを確認した。
共有Ollama/TTSのimage・設定・モデル・dogfoodデータは変更していない。

## 出力コピー省略の限定比較

現在の独立CFGでは各step内で出力を消費するため、次のforward前に出力を複製しない候補を
専用コンテナへ一時適用した。独立CFGであることを実行時にassertし、
声・step・schedule・精度・durationを変更せず、同じ5文×3回を現行Graphと対比較した。

[匿名結果](../artifacts/irodori-graph-output-reuse-20260919.json)では、
現行Graph中央値636.28msに対してコピー省略622.03ms（約14msの差）。
全件音声長は一致したが、小標本で効果が小さく、現状の2秒超過を解消する証拠にはならない。
joint/alternating CFGで必要な出力保護を製品から削除せず、この変更は採用していない。
約41%短縮だった初期試作との違いを、このコピーだけの影響とは説明できない。

## 継続する受入

正式な空状態100試行はNOT_RUN。記憶参照の第2段階もNOT_RUNで、
第1段階達成後の計測のみとし、受入条件に含めない。記憶形成の影響調査は行わない。
先頭TTSのばらつきと追加最適化を調べつつ、既存の相槌・文中休止・再接続・連続応答gap、
実マイク/聴感、一括更新・rollback、残レビューの条件を維持する。
