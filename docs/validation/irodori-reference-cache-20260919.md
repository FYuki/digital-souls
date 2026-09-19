# Irodori参照音声latent再利用の対比較（2026-09-19）

## 結果と判定

実装PRは[#450](https://github.com/FYuki/digital-souls/pull/450)。
固定した候補イメージで、合成APIの中央値は612.28msから550.52msへ約10.1%短縮した。
15組中13組で改善し、組ごとの差の中央値は−53.71ms。これは合成API単体の診断であり、
発話終了からブラウザ再生までの100独立試行の合格、音質受入を意味しない。

前段のGraph256正式100試行はp95 2001.30msでStage 1未達。
[#449](https://github.com/FYuki/digital-souls/pull/449)の証跡を保持し、
今回の最適化を追加した条件は別cohortで測る。閾値・試行選別・声の変更で合格へ置き換えない。
記憶参照のStage 2はStage 1合格後に実施し、形成影響の調査は含めない。

## 固定条件と対象

- 製品commit: 0b97ed943aa660877dccc24b8df25cae6eec1549
- 候補image: sha256:d8279a7a43adc105ebe14b4bd3620fe2839c44966ff756c8c93e5dde9fba916c
- 固定upstream: Irodori 8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71、
  server 841fb7c6ec57729c56b9b75c0ef2562249b13a10
- Model 2b28324dc263ed5e6638b3cf3dd94c82ead07b4b、codec 47376ee24834d7a05a48ebabfe3cde29b3c5e214
- RTX 4070 Ti SUPER、BF16、非量子化、compile無効、CUDA Graph256は比較両側で有効。
- miori-b3-4221、採用B3 caption、seed 4221、40 steps、speed 1.02、
  duration推定とsampling scheduleは変更しない。
- 準備1回、固定5文の完全一致検査、5文×3回×ON/OFFの15組を実行。
  反復ごとにON/OFFの順を交替し、各runの全36要求を保存した。
- 共有モデル・声はread-only mountし、専用の一時GPUコンテナで実行した。
  共有Irodori、Ollama、Whisper、VOICEVOXの設定・再起動・配備は変更しない。

## 案ごとの結果

各行は別プロセスの診断であり、行を跨ぐ差を同一条件の改善率にしない。

| 案 | baseline中央値 | 候補中央値 | 組ごとの時間差中央値 | 改善した組 |
|---|---:|---:|---:|---:|
| 同一要求内の条件encoder再利用prototype | 515.56ms | 534.04ms | −21.94ms | 11/15 |
| 参照latent再利用prototype | 514.05ms | 451.55ms | −56.72ms | 15/15 |
| 参照latent再利用の製品実装 | 612.28ms | 550.52ms | −53.71ms | 13/15 |

条件encoderの重複除去では組ごとの差に改善があったが、各群の中央値は逆転した。
参照latent再利用より利得が小さく変動もあるため、追加の差替えを増やさず今回は採用しない。
製品実装の参照準備中央値は53.78msから0.91msへ減った。
時間差の全部を参照処理だけに帰属せず、各段階の時間も原記録に保持する。

## 一致性と品質の限界

5文すべてで、製品cacheから返した参照latent/maskと毎回再計算したtensorの完全一致を確認した。
警告等のmessagesも一致し、全36要求でGraphは2形状をcaptureした。
採用候補の20回の再利用要求（完全一致検査5回＋本計測15回）はすべてhit、
初回準備はmissから保存した。

全要求で音声frame数は最初の同文と一致した。ただし合成PCM全体は完全一致ではない。
製品診断の最初の同文との最大絶対差はPCM16で256、最大RMS差は17.04。
ON/OFF両側の反復でも差があるため、この数値だけから音質の同一性を認定しない。
実音声の試聴、発音、間合い、実マイク、失敗後の次会話等は別途必要である。

## 再現と証跡

[製品プローブ](../../scripts/voice_quality/probe_irodori_reference_cache.py)を、
上記候補イメージの/app/.venv/bin/pythonで実行する。標準入力には固定CCVの
data.extensions.digital_souls.tts_configだけを渡す。参照WAVと固定モデルcacheをread-only mountし、
専用GPUコンテナ、network none、read-only rootfs、/tmpのtmpfsで実施する。
baseline側もGraph256を使用し、製品の参照cache呼出だけをON/OFFする。

[manifest](../artifacts/irodori-reference-cache-20260919/manifest.json)に全原記録とプローブのhashを保存した。
prototypeのソースも同じartifactディレクトリに保持する。公開JSONは固定文のhash・sample数・時間・
構成を含み、音声本文、会話ID、credential、プロバイダpayloadは含まない。

## 実ブラウザ診断

候補TTSは専用port 50026、Ollama 0.34.2は専用port 11534を使い、
BE/FEと測定境界は93c8aa7dd569f991e872de5ac75502e8e2111ccbに固定した。
空状態・形成/統合無効・準備5回＋独立3試行の全8件で実ブラウザ再生とSession終了を確認した。
計測3件の保守側TTFAは1778.83ms、1766.43ms、1728.83ms。
全8件でexpectedSamplesとrenderedSamplesが一致し、gapSamplesは0、native SDKはverified。
準備時間はTTFAから分離し、初回の約7.95秒も[原記録](../artifacts/irodori-reference-cache-20260919/browser-small.json)に保持した。

runnerはmeasured=3の場合、正式reporterを実行せず終了1を返す。
summaryのexisting_reporter_exit=1はその初期値であり、このcohortの集計処理が失敗した記録ではない。
100試行用reporterの起動条件は[#448](https://github.com/FYuki/digital-souls/pull/448)で別途修正中。
今回のhost runnerにはPYTHONPATH=backendを明示し、固定済みBE/FEと測定コードは変更していない。

この診断後、同じ候補条件で350-irodori-reference-full-0919-01（準備5回＋100独立試行）を
事前登録して完走した。[正式結果](irodori-reference-formal-20260919.md)はp95 1967.65ms、測定100/100成功で第1段階達成。本書の小規模診断と正式分母は混ぜない。
