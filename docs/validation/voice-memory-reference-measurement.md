# 固定記憶参照の追加計測

第1段階の空状態100試行とは別の第2段階。速度や参照件数の合格閾値は置かない。
基点は第1段階のBE/FEと同じ93c8aa7。製品経路を変更せず、既存の独立音声測定へ明示指定の分岐を追加する。

## 固定条件

- fixture「synthetic-greeting-preferences-v1」。挨拶に関する架空のUSER_PREFERENCE 3件。
  本文はbackend/app/voice_quality_reference_state.pyへ固定し、本文・集合・正本行のhashを保存する。
  実ユーザーやdogfoodの記憶を使用しない。記憶の抽出・形成を試すfixtureではない。
- speech-v2の入力、CHATモデル・options、Irodoriの採用声・seed4221・40steps・speed1.02・BF16を第1段階と揃える。
- 各試行は新規data root、空の会話履歴。記憶形成・統合の無効receiptを検査する。
- integration-irodori-memory-referenceは専用Ollama11534／TTS50026、Chroma in_process。
  共有サービスの設定・データは変更しない。
- 記憶は新規rootだけに登録する。再実行・上書き・別root・symlinkは拒否する。
  正本の登録後、既存index workerが実embeddingで派生indexを構築する。
  index処理の失敗と120秒の個別timeoutを検出する。Session準備全体の上限ではない。
- 検索した記憶が実際の応答プロンプトへ入ったことをmemory_response_dependenciesの記憶ID・版と照合する。
  未参照0件、実行失敗、観測欠測を区別し、成功例だけに差し替えない。
- 発話終了→実ブラウザ再生の測定境界と時計区間、sample/gap照合、Session終了・teardownを既存基盤から維持する。

## 実行

最初に独立1試行で接続・正本・index・実参照の観測を検証する。これは正式比較の分母に入れない。
その後、準備5試行＋測定20独立試行を事前登録する予定。件数はユーザーから委任された範囲で選び、
100件の空状態受入とは別の探索的な分布比較とする。失敗・欠測があっても分母20を保持する。
cohort開始前にコード・image・設定・集合・版・試行計画を固定する。

単独診断の入口:

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --profile integration-irodori-memory-reference \
  --run-id 350-memory-reference-pilot-0919-01 \
  --inference-env /path/to/private-inference.env \
  --livekit-env /path/to/private-livekit.env \
  --trials 1 --scheduled-fixture --isolated-normal-phase measured \
  --disable-memory-formation --memory-reference --disable-thinking
```

秘密値をIssueやログへ載せない。上記は入口であり、サービスidentity／revisionのpreflightと
resource/native SDK、実行中image、後始末確認を伴うhost runnerから実行する。

## 結果の扱い

全予定数、準備・応答の成功／失敗／欠測、参照あり／未参照、p50/p95、空状態との差を報告する。
2,000msの合否、記憶形成・長履歴の性能、実マイク・聴感へ拡張しない。
計測用コードと実測証跡は別PRに保存する。現時点では実サービスの追加計測は未実施。
