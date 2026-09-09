# 正解ラベル付き音声fixture v2

#150のVAD冒頭・終了境界、相槌、発話権取得、文中無音を診断する固定合成音声。音声生成は VOICEVOX:ずんだもん（ノーマル、speaker ID 3）。利用者の録音やdogfoodの会話は含めない。エンジンのversion応答は`latest`のため、不変なbuild識別子にはならない。保存済みWAVとSHA-256を再現の正本とする。

ラベルは判定器と独立した[fixture_cases_v2.json](../../../../scripts/voice_quality/fixture_cases_v2.json)で固定する。相槌10語句、take-turn 10語句に加え、前半・後半を別々に生成した10組の文を使用する。文の継続5組と「ええと」「あの」などの言い淀み5組を含む。v1の同じ語句の反復とは異なるが、自然な実声や100人の独立した発声を代表しない。

| cohort | 試行数 | 内容 |
|---|---:|---|
| backchannel | 100 | 10語句の開始位相・音量を変える |
| take_turn | 100 | 10語句の開始位相・音量を変える |
| pause | 100 | 10組の文の間へ200／400／600msの無音を入れ、開始位相・音量を変える |

正解境界は元音声の10ms RMS窓がPCM16値200を超える最初・最後の窓。SileroやWhisperの判定を正解に流用しない。元WAVの前後の余白を除いてから文中無音を挿入する。音素単位の人手注釈ではなく、境界の時間分解能は10msである。

リポジトリrootで実行する。

```bash
backend/.venv/bin/python scripts/voice_quality/fixtures.py validate \
  --root frontend/playwright/fixtures/voice-quality-v2 \
  --cases scripts/voice_quality/fixture_cases_v2.json
backend/.venv/bin/python scripts/voice_quality/fixtures.py materialize \
  --root frontend/playwright/fixtures/voice-quality-v2 \
  --cases scripts/voice_quality/fixture_cases_v2.json \
  --output frontend/test-results/vad-quality/fixtures-v2
node frontend/scripts/measure-labeled-vad.mjs \
  frontend/test-results/vad-quality/labeled-v2-legacy.json legacy \
  frontend/playwright/fixtures/voice-quality-v2 \
  frontend/test-results/vad-quality/fixtures-v2
node frontend/scripts/measure-utterance-detector.mjs \
  frontend/test-results/vad-quality/labeled-v2-legacy.json \
  frontend/test-results/vad-quality/utterance-detector-v2.json
node frontend/scripts/measure-utterance-background.mjs \
  frontend/test-results/vad-quality/utterance-background.json
node frontend/scripts/measure-utterance-continuous-background.mjs
```

`validate`はsource hash・独立ラベル・前半／後半の組み合わせ・正解境界・全300件のrecipeを検証する。生成済みversionへの再生成は拒否する。展開音声と診断結果はignoredの`frontend/test-results/`へ保存する。

保存済みSilero確率とPCMによる診断はBrowser実時間の測定ではない。非発声120件も固定合成音による診断であり、実環境の誤起動率へ一般化しない。これらからBrowserの誤cancel率や旧応答のstale提示、ネットワーク復旧の合格を判断しない。
