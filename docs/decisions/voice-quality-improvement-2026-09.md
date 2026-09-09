# LiveKit音声品質改善の測定条件

## 背景と決定

Epic #150はTTFAの改善と未実装の品質指標の実測を扱う。2026-09-06に次の方針を採用した。

- 現在のdogfoodのハードウェア、LLM、Whisper、VOICEVOXを固定し、改修前の状態を再測定する。
- 最初の最適化では人格、会話履歴、記憶contextの内容を維持する。トークン数の計測要求も含めて遅延を分解してから処理を改善する。
- 相槌・take-turn・VAD境界は正解ラベル付き合成音声で再現可能にし、実声の自然さはdogfoodで別途評価する。
- first playbackはブラウザ音声出力処理へ最初の応答サンプルが渡った時点とする。スピーカーの物理出音やマイクによる外部録音は今回の測定範囲に含めない。
- 実装は専用worktreeで行い、dev/testの測定data rootと実dogfoodの会話・記憶を分離する。

## 計測の意味

HTTP要求開始、HTTP response header受信、最初の本文token受信はBackendのmonotonic clockで別々に観測する。HTTP header受信をprovider内部の受付時刻や生成開始時刻と同一視しない。

Ollamaが応答するload／prompt evaluation／generation時間はprovider報告のdurationとして扱う。totalから既知のdurationを引いた残差をqueue待ち時間と断定しない。取得できない内部時刻は理由付き欠測とし、必要に応じてprovider側の計測を追加する。

本文、prompt、音声、秘密値を診断ログへ追加しない。応答ごとの数値観測はsession／utterance／responseに相関し、公開artifactから逆引き可能なIDを除く。

## 実装と検証の順序

1. A: prompt準備、token計測、Inference側の待ち、HTTP要求、first token、Ollama durationを分解し、現構成の改修前値を取得する。
2. B: client受信と再生、VAD元時刻、割り込みutteranceと旧responseの相関、cancelの分母を修正する。
3. A: 測定で確認したボトルネックを、応答内容とprivacy／memory境界を維持して改善する。
4. C: VAD正解境界、600ms以下の文中無音、相槌・take-turn各100試行以上、stale生成／受信と提示を測定する。
5. D: localhost WebRTCに作用するnetwork障害注入、control/audio復旧、重複再生、underrun／gap、resource、手動操作を測定する。
6. E: 同一fixture・同一初期状態、warm-up 5回除外、独立session／conversation 100試行の通常応答を再測定し、schema・匿名性・baseline比較を検証する。

品質閾値と完了条件は#150および`docs/voice-quality-measurement.md`を維持する。自動テストの成功、readiness、部分的な改善をEpicの実接続受入完了とは扱わない。WebSocket baselineは凍結を維持する。
