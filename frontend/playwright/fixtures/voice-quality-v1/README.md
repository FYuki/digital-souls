# 正解ラベル付き音声fixture v1

#150の相槌・発話権取得・無音境界を診断する合成音声。利用者の会話やdogfoodデータを含まない。音声生成: VOICEVOX:ずんだもん（ノーマル、speaker ID 3）。エンジンが返したversionは`latest`であり、不変なbuild識別子ではない。再現の正本は保存済みWAVとSHA-256で、再合成した別音声を同じ証跡へ置き換えない。

ラベルと本文・意図は[`fixture_cases.json`](../../../../scripts/voice_quality/fixture_cases.json)で、判定実装より先に固定する。各cohortは10語句×10種類の開始位相・音量条件で100試行。100人の独立した実発声や実声の自然さを代表しない。

- `backchannel`: 相手の説明継続を求める短い受容・共感。
- `take_turn`: 説明の停止・訂正・質問で発話権を要求する。相槌に続けて質問する語句も含める。
- `pause`: 同じ語句を2回つなぎ、200／400／600msの既知の無音を挿入する。VAD連結の診断用であり、自然な文中の言い淀みを網羅しない。

正解境界は元WAVの10ms RMS窓がPCM16値200を超える最初・最後の窓。SileroやWhisperの判定を正解ラベルへ流用しない。境界の時間分解能は10msであり、人間による音素境界の注釈とは異なる。元音声の区間を切り出してから無音を挿入するため、設定した無音長と保存WAVの余白を混同しない。

リポジトリrootで次を実行する。

```bash
backend/.venv/bin/python scripts/voice_quality/fixtures.py validate
backend/.venv/bin/python scripts/voice_quality/fixtures.py materialize --output frontend/test-results/vad-quality/fixtures
node frontend/scripts/measure-labeled-vad.mjs
backend/.venv/bin/python scripts/voice_quality/stt_turn_diagnostic.py
```

`validate`は語句ラベル、source hash、正解境界、全300件のrecipe・分母を確認する。生成済みのversionへ`generate`を再実行すると失敗する。展開した300個のWAVと診断結果はignoredの`frontend/test-results/`に置く。公開aggregateには音声本文・transcript・逆引き可能なsession IDを含めない。

Silero診断は実モデルを使用するが、Browser実時間やLiveKitを含まない。Whisper診断は観測済みVAD区間を実WhisperとCoreの判定関数へ渡すが、旧responseのcancel、再生停止、stale提示を検証しない。いずれも全stack受け入れの代用にはしない。
