# 高速化Irodori候補の休止pilot（2026-09-19）

[測定基盤PR #457](https://github.com/FYuki/digital-souls/pull/457)で、専用Ollama 0.34.2と
高速化Irodoriを使う試験へ、既存Whisper入力PCM観測を接続した。製品のVAD判定は変更していない。
本書はその3件の実接続診断と、先行2回の失敗を保存する。100件の正式受入とは区別する。

## 固定条件と結果

- BE/FE・runner: 47e177c4dfaf2e7a0b31a90c39b4a2c5d05e558f
- TTS: 0b97ed943aa660877dccc24b8df25cae6eec1549、CUDA Graph256・参照latent再利用有効
- miori-b3-4221、B3 caption、seed 4221、40 steps、speed 1.02、BF16、compile無効
- integration-irodori-cuda-graph-pcm、候補Ollama 11534、候補TTS 50026、
  専用PCM proxy 50023から共有Whisper 50022へ同じbytesを転送
- 固定v2 pause fixtureの33・36・39番。すべて600ms休止。少数の再現診断として事前選択した。
  旧#358の36・39番で誤分割があったため、その前後を確認する目的である。
- 各fixtureは別Session。記憶形成・統合停止、thinking無効。モデル・声・共有設定を変更していない。
  固定音声による実STT/LLM/TTS/LiveKit/ブラウザ試験であり、人の実マイク試験ではない。

| run末尾 | 結果 | 分母の扱い |
|---|---|---|
| pilot-0919-01 | 全用途のBackend起動probeがunavailable。専用アプリを回収 | 計画3、音声入力NOT_RUN |
| pilot-0919-02 | Backend起動成功。作業用worktreeに展開WAVがなく、入力前に停止 | 計画3、音声入力NOT_RUN |
| pilot-0919-03 | 固定WAVをmanifestのhashで検証・配置後、3/3完了 | 成功診断3件。先行失敗を置換しない |

01の後、同じBackend imageを使う読み取り専用の一時コンテナからOllamaAdapter.probeを行い、
gemma4:e4bとnomic-embed-textのモデル情報取得は成功した。02以降の起動成功だけで、
間欠的な起動probe失敗の原因が解消したとは判断しない。

03はVADの発話確定・終了・finalizedが各1件、誤分割0/3、冒頭欠落判定0、早期終了判定0、
境界照合3/3、欠測0。終了境界の保守側p95は763.56ms。
実STT入力のfinal要求・両端照合も3/3、欠測0、最小edge相関0.9500。
[PCM report](../artifacts/voice-pause-pilot-20260919/pcm-report.json)と
[VAD report](../artifacts/voice-pause-pilot-20260919/vad-report.json)は既存reporterの原出力で、
100件未満のgateだけがfalseのため、正式合格にはしていない。

全3件でSession終了、音声監査終了、native SDK verifiedを確認した。
全3runの所有BE/FEは回収確認済み。入力PCMの両端照合は内部全区間の連続性や聴感品質の証明ではない。
初回マイク準備約7.99秒もTTFAと混同せず原記録へ保存した。

## 過去の誤分割に対する限定診断

旧#358のafter-pauseは100件中36・39番で2分割を記録している。今回の少数成功で
過去の失敗を撤回せず、製品VADを修正済みとも説明しない。

先行して、同じfixture 33・36・39をCPUの実Silero/二次VADモデルへ10位相ずつ入力した
[30ケースの診断](../artifacts/voice-pause-pilot-20260919/source-model-replay.json)でも分割を再現しなかった。
これは元の合成PCMを直接入力する試験であり、RTCで復号した実入力PCMの再現ではない。
モデルのhash、位相、frame診断を残す。

## 証跡と継続条件

[匿名証跡](../artifacts/voice-pause-pilot-20260919/evidence.json)に実image ID、
準備失敗、VAD件数、Session終了、各元記録のhashを保存した。
[manifest](../artifacts/voice-pause-pilot-20260919/manifest.json)で公開ファイルのhashも照合できる。
元のrunner log、native記録、PCM観測、trace、trial manifestはignoredのローカルrunに保持する。
会話ID、本文、認証情報、音声bytesは公開artifactへ入れない。

正式cohort 424-candidate-pause-full-0919-01は同じ固定版・全100fixture順で事前登録して開始した。
本書作成時点では実行中。全件と欠測を保持し、誤分割1%以下・VAD/PCM coverageを終了後に判定する。
同時に追加のGPU推論を行わず、文書/Git作業を実施した。完全無負荷のhostとは主張しない。

相槌・割込、実マイク・聴感、移行/切り戻し、残レビューなどは別の残条件。
空状態TTFA達成と記憶参照の計測完了をもってこれらを省略しない。


正式100件は完走した。[正式結果](voice-pause-formal-20260919.md)は誤分割0/100、
PCM両端100/100、VAD境界90/100でcoverage不足のため未合格。
本pilot作成時点の実行中記録と先行失敗は履歴として保持する。
