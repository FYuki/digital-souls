# Factの行為者判定評価

promptfooを使い、本番の `ThreadEpisodeExtractor._ground_content` を実LLMへ接続する。
Factの対象行為だけを固定した下書きを渡し、出典から行為者を読み取る段階を評価する。
候補選定、Episode分割、privacyによる保存拒否、DB保存、検索・応答はこの正解率の対象外。
保存拒否で人物判定の評価ができなくなることを避け、行為者判定自体と総合受入を区別する。

## 正解と合格条件

合成会話100件を用い、行為者の集合が固定正解と一致した件数が90件以上なら合格。
一人称、発言をまたぐユーザーの体験、assistantの二人称による語り直し、
assistant自身の体験、名前付き第三者、第三者への指示語、引用内の一人称・二人称、
行為者不明、複数行為者の10分類を各10件用意する。

- 正解は `expected_json` に保持し、LLMに渡す `input_json` へ含めない。
- whoのACTORを集合として照合する。既知の当事者はIDと名前、第三者はnull IDと事前定義した名前表記で照合する。
- 余分な行為者、人物の重複、TOPIC等への役割誤分類、人物IDの取り違えも不正解。
- 不明な行為者を特定の人物で補完しない。期待するACTORは空集合。
- 推論timeout・不正出力も100件の分母に残す。欠落・重複caseのあるrunは合格にしない。
- キャッシュを使わず、並列数1で全件を実推論する。別LLMを採点者に使わない。
- 分類別正解数と時間p50/p95も報告するが、今回合意した合格基準は全100件の正解率90%以上。

これは固定合成ケースでの行為者判定の評価であり、実運用分布や#344全体の合格率ではない。
失敗例を見てpromptを修正した場合は、同じケースの再評価であることを明示する。

## 実行

リポジトリルートの `npm ci` で導入したpromptfooを使う。Pythonはbackend依存を導入したvenvをPATHへ通す。
ローカルOllamaのgemma4:e4bを事前に導入しておき、開発環境で実行する。
共通Inference設定に必要な未指定Targetだけローカル値を補う。実行前に設定とモデルdigestを検証し、
準備失敗を100件のモデル精度に混ぜない。評価中の推論失敗は不正解として数える。

```bash
PATH="$PWD/backend/.venv/bin:$PATH" \
INFERENCE_TARGET_MEMORY_EXTRACTION=ollama/gemma4:e4b \
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS=32768 \
INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=4096 \
INFERENCE_TARGET_MEMORY_EXTRACTION_TIMEOUT_SECONDS=180 \
INFERENCE_TARGET_MEMORY_EXTRACTION_OPTIONS_JSON='{"temperature":0,"think":false}' \
MEMORY_FORMATION_LLM_TIMEOUT_SECONDS=180 \
MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS=400 \
npm run eval:episodic-actor
```

既存devとのモデル競合を避けて測定する。アプリ・dogfoodの停止やデータ更新はこのコマンドでは行わない。
必要なら `-- --output-dir <空の出力先>` を指定する。出力先を省略した場合は専用tempディレクトリを作成し、終了後も保持する。

`manifest.json` に実行commitと入力・実装のhash、`results.json` にpromptfoo結果、
`summary.json` に正解率と分類別成績、`progress.jsonl` にcase単位の進捗、
`promptfoo.log` に実行ログを残す。出力人物、model digest、prompt version、推論予算と時間も合成試験の証跡として保持する。
`summary.json` の `passed` がfalse、またはrunが不完全ならコマンドは非0で終了する。


## 出力設計の比較

既定はproduction。評価専用の改善案は次のコマンドで測定する。

~~~bash
npm run eval:episodic-actor -- --design compact --output-dir /tmp/unique-compact-actor
~~~

行為者の固定正解と90%の基準は共通。設計hashを記録し、異なる設計のrunを混ぜて合格にしない。
compactの人物schemaは名前と既知IDの対応、FactのACTOR役割を制約する。
誰が行為者かはLLMが本文から判断し、人物を特定できない場合の空配列も許す。
アプリの実行経路は変更しない。

[比較結果・反復確認・検証境界](../../../docs/episodic-quality-iterations-2026-09.md)を参照する。
