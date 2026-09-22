# 高速化候補の休止100独立Session（2026-09-19）

固定休止100件は完走したが、**VAD境界のcoverage不足で全体は未合格**。
誤分割0/100、最終STT入力PCMの両端照合100/100、VAD境界の照合90/100だった。
成功した90件だけで冒頭・終了境界を全件受入済みとはしない。

## 固定条件

- run: 424-candidate-pause-full-0919-01
- BE/FE・runner: 47e177c4dfaf2e7a0b31a90c39b4a2c5d05e558f
- TTS: 0b97ed943aa660877dccc24b8df25cae6eec1549、Graph256・参照latent再利用有効
- 専用Ollama 0.34.2とIrodori、integration-irodori-cuda-graph-pcm
- 採用声miori-b3-4221、B3 caption、seed 4221、40 steps、speed 1.02、BF16、compile無効
- 固定v2 pause全100件をmanifest順で実行。200/400/600msの文中無音、別Session、形成/統合停止、thinking無効
- 別cohortのpilot3件を合算しない。試行の追加・差替え・除外は0。開始前にplanと全fixture hashを固定した。
- 準備1回ごとに通常入力で先行応答を開始してから休止fixtureを流す既存方式。TTFAの空状態100件とは異なる試験。

[事前plan](../artifacts/voice-pause-formal-20260919/plan.json)、
[全件証跡](../artifacts/voice-pause-formal-20260919/evidence.json)、
[公開hash manifest](../artifacts/voice-pause-formal-20260919/manifest.json)に再現条件を残す。
実image IDは起動後の所有コンテナから確認した。測定中は文書・Git・ソース確認を行い、
追加GPU推論やローカルテストは重ねていない。完全無負荷のhostとは主張しない。

## 正式集計

| 項目 | 結果 |
|---|---:|
| 予定 / 記録 / 独立Session | 100 / 100 / 100 |
| 音声試行成功 | 100 / 100 |
| 誤分割の観測 / 未知 | 100 / 0 |
| 誤分割 | 0 / 100 |
| 最終STT入力の対応付け / 両端照合 | 100 / 100 |
| STT PCM欠測 | 0 |
| VAD境界の照合 / 欠測 | 90 / 10 |
| Session終了 / 音声監査終了 | 100 / 100 |
| 所有BE/FE回収・native SDK | 確認済み |

[PCM reporter](../artifacts/voice-pause-formal-20260919/pcm-report.json)はexit 0。
最小edge相関は0.88793。これは入力の両端coverageで、内部全区間の連続性・認識内容・聴感を保証しない。
部分認識などの他STT要求100件は最終要求と区別した。

[VAD reporter](../artifacts/voice-pause-formal-20260919/vad-report.json)はexit 1。
既存の計測式・閾値を変更せず、coverage gateだけがfalseだった。
照合90件における冒頭欠落・早期終了・境界不確定は0だが、欠測10件の品質は未確定。
終了境界の保守側p95は751.66ms（90件のみ）。全100件のp95や通常TTFAではない。

## 欠測10件の切り分け

[匿名の最終STT入力診断](../artifacts/voice-pause-formal-20260919/missing-diagnostic.json)に、
各件の二つのanchorと既存v4端部照合を保存した。

- 12/20/31/86/90/93/94/97番：前後anchorのずれ量が一致しない。
  差は40〜359 samples（16kHzで2.5〜22.44ms）。
- 55/57番：二つのanchorの一方を一意に確定できない。
- いずれも最終STT入力とfixtureの両端照合は成功しているが、現行VAD reporterが要求する
  全発話の共通offsetを証明できないため、境界coverageには算入しない。

原因が製品の収録経路か、非一様な時間差を扱えない観測方法かは、この結果だけでは確定しない。
両端成功を理由に現行の失敗を合格へ変更せず、追加観測・必要な測定基盤修正を別PRで扱う。

## 先行失敗と残条件

[先行pilot](voice-pause-pilot-20260919.md)の起動probe失敗・fixture未配置も保持する。
この正式runの成功で間欠的なBackend起動probeの原因解消を主張しない。
過去#358の誤分割2/100も別条件・別時点の証拠として残す。

空状態TTFAと固定記憶参照は別の固定版の結果。
相槌・割込、VADの欠測、実マイク・聴感、混在入力、更新/切り戻し、残レビューは継続する。
共有サービス・dogfoodの設定とデータを変更した証跡ではない。
