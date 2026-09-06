# 音声品質の診断コマンド

これらは#150の診断用。各cohortの全stack受け入れ、通常100試行、実声dogfoodの完了は別途検証する。

## 独立data rootでの実音声pilot

専用worktreeのrootから、既存の推論サービスの設定ファイルを明示して実行する。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id residency-01 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --trials 3 \
  --disable-thinking
```

`--run-id`は毎回新しくする。`frontend/test-results/livekit-quality/runs/<run-id>/`が存在すれば拒否し、失敗した試行のDB・traceも上書きしない。外部推論サービスの設定だけを指定ファイルから読み、dogfoodのdata rootは引き継がない。LiveKitの鍵は既定では`infra/livekit/.env`から読む。test用LiveKitは`127.0.0.1:7880`、テストのFrontend／orchestratorは5173／4174を使用するため、起動前に競合を確認する。

`--disable-thinking`はpilot内だけで`think:false`を指定する実験条件である。人格・履歴・記憶を削らないが、元設定と同じ応答品質が保証されたという意味ではない。指定しなければ既存の生成optionsを維持する。共有Ollama、Whisper、VOICEVOXの再起動や設定変更は行わない。

runnerは準備1回と指定回数の測定を行い、実際のBrowser→LiveKit→Whisper→LLM→VOICEVOX経路でtranscript一致・応答完了・session終了を確認する。応答sourceとreceive／decode／playbackの対応が未実装の場合、理由付き欠測を残す。このpilotをcontrolled受入へ変更して扱わない。

## 音声源の正解境界を直接観測するpilot

`--scheduled-fixture`を追加すると、マイクの準備・publish完了後に固定WAVのPCMをAudioWorkletからMediaStreamへ流す。開始前にfixtureを消費しない。各境界のPCMを出すprocess呼び出しより前にmainから送ったping時刻と、通知をmainが受け取った時刻で境界を囲む。clock名はclient側の`performance.now()`で統一する。正解境界をVAD判定や`getUserMedia`完了時刻から作らない。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id source-clock-01 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --trials 3 --disable-thinking --scheduled-fixture
```

manifestの`fixture_clock_bounds`にはsource開始、正解発話開始、正解発話末尾の`lowerMs`／`upperMs`と元sample位置を保存する。上下限の幅が20msを超えた場合や欠測・逆転があれば試行は失敗する。遅延を単一値へ変換する必要がある場合は、末尾の下限を起点として遅延を過小評価しない。境界後に高頻度pingを停止する。

これは合成PCMの生成時刻の観測であり、物理マイク・スピーカーの時刻ではない。通常のfake microphoneとは入力供給方法が異なるため、凍結済みWebSocket baselineと同じ方法で計測したとは扱わない。WAVのhashとsample列は維持し、診断方法をmanifestへ明示する。応答音声のsource相関や全cohortの受け入れを完了したという意味ではない。

## 共有推論環境の数値観測

`inference-runtime.jsonl`へOllamaのロード済みモデル状態を約0.5秒おき、GPU使用量を約5秒おきに保存する。APIやコマンドの処理時間により間隔は延びる。観測失敗は欠測理由で残し、0使用量へ補完しない。

- 保存対象: ロード済みモデル数、対象モデルの有無、context token数、モデルのRAM／VRAM bytes、GPU使用率と使用／総memory bytes。
- 保存しない情報: model名・digest、endpoint、秘密値、prompt、transcript、任意のprovider応答文字列。
- GPUはhost全体の値であり、この会話だけの使用量ではない。BackendプロセスのRSS、transport別帯域、packet lossの代用にはしない。
- 時刻はobserver自身のmonotonic clock。BackendコンテナのtraceやBrowserのclockと同期したとは仮定しない。API呼び出しの開始・完了を両方記録する。
- 約0.5秒の離散観測なので、サンプル間に発生したcontext切り替えやqueue待ちを直接証明できない。

Ollamaの`load_duration`はprovider報告値として扱う。v0.32.5の[ChatHandlerとschedulerの実装](https://github.com/ollama/ollama/blob/v0.32.5/server/routes.go)では、handler開始からschedulerがrunnerを返すまでの時間が含まれる。重みを読み直した時間や純粋なqueue待ちへ分離した数値ではない。

## 正解ラベル付きfixture

[v1](../../frontend/playwright/fixtures/voice-quality-v1/README.md)と[v2](../../frontend/playwright/fixtures/voice-quality-v2/README.md)に、独立したラベル、音声生成クレジット、WAV hash、正解境界、展開・診断手順を記載する。`fixtures.py`と`stt_turn_diagnostic.py`は`--cases`で対応する固定ラベル定義を選択できる。
