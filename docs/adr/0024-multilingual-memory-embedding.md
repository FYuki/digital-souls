# ADR 0024: 記憶検索の embedding を多言語モデル bge-m3 へ切り替え、閾値を0.52にする

Status: Accepted

日付: 2026-10-10

Accepted日: 2026-10-10

根拠: 2026-10-10 のユーザー決定（[Issue #131 のコメント](https://github.com/FYuki/digital-souls-core/issues/131)の1〜4）。
前提は[Epic #124 の Q1〜Q5](https://github.com/FYuki/digital-souls-core/issues/124)と、
[Issue #130 の候補比較](../evidence/2026-10-10-semantic-embedding-candidates.md)です。

## 背景

[ADR 0023](0023-semantic-evaluation-contract.md)の62ケースによる実モデル評価で、nomic-embed-text-v1.5 f16（接頭辞なし）は
検索・回答とも FAIL でした。日英 0/10、無関係 0/10、閾値 0/1 です（[証跡](../evidence/2026-10-09-semantic-real-model-evaluation-top5.md)）。

Q1・Q2 に従い、nomic のタスク接頭辞の有無と多言語 embedding モデルを比べました。Q3 に従い、比較には
合否判定用の62ケースとは別の[調整用の合成89件](../../evals/semantic/tuning/README.md)を使い、62ケースでは候補を評価していません。
候補20・最大5・同等帯0.002・タイブレーク・合否基準は Q4 により変えていません。

閾値0.54（当時の本番値）での調整用ケースの結果（各候補3回、3回とも同じ）は次のとおりです。

| 候補 | synonym /10 | paraphrase /10 | cross_language /24 | unrelated /22 | threshold /12 |
| --- | --- | --- | --- | --- | --- |
| nomic v1.5 f16（接頭辞なし、従来） | 9 | 8 | 0 | 0 | 6 |
| nomic v1.5 f16（search_query / search_document） | 9 | 8 | 0 | 0 | 6 |
| nomic v2 MoE f16（search_query / search_document） | 5 | 9 | 0 | 18 | 9 |
| bge-m3 Q8_0（接頭辞なし、CLS） | 10 | 10 | 20 | 12 | 6 |
| multilingual-e5-large Q8_0（query / passage） | 10 | 10 | 24 | 0 | 6 |

閾値を0.40〜0.80で掃引しても、全分類90%と必須ゲート違反0を同時に満たす候補・閾値はありませんでした。
bge-m3 の掃引では次のとおりです（unrelated は話題が遠い型12件・近い型10件の合計）。

| bge-m3 の閾値 | synonym /10 | paraphrase /10 | cross_language /24 | unrelated /22（遠い型 /12） | threshold /12 |
| --- | --- | --- | --- | --- | --- |
| 0.52 | 10 | 10 | 24 | 8（8） | 6 |
| 0.53 | 10 | 10 | 21 | 10（10） | 6 |
| 0.54 | 10 | 10 | 20 | 12（12） | 6 |

## 選択肢

1. **nomic v1.5 を維持し、接頭辞・閾値だけを変える。** 接頭辞を付けても日英は改善せず（0.54で0/24、最良でも21/24）、
   日英と無関係を同時に満たす閾値がありません。
2. **bge-m3 へ切り替える。** 閾値0.54では日英20/24・話題が遠い無関係12/12、0.52では日英24/24・話題が遠い無関係8/12です。
3. **multilingual-e5-large へ切り替え、閾値を0.60〜0.61へ上げる。** 正解の取りこぼしはありませんが、
   無関係は0/22で、該当なしの質問に記憶を返します。
4. **nomic v2 MoE へ切り替える。** 無関係は18/22ですが、0.54で同義語5/10・日英0/24です。

監督の提案（Proposed 時点）は選択肢2の閾値0.54でした。ユーザーは選択肢2を採用し、閾値は日英の取りこぼしをなくす0.52を選びました。

## 決定

- 記憶検索の embedding モデルを **bge-m3**（BAAI、MIT）の GGUF Q8_0 にします（ユーザー決定1）。
  - ファイル：`ggml-org/bge-m3-Q8_0-GGUF` revision `9eba04c5d75ba5a1595e45de734d36bef4e5cb98` の `bge-m3-q8_0.gguf`、
    SHA-256 `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`、634,553,760 bytes。
  - llama.cpp は既存と同じ固定イメージを使い、pooling は `cls`、次元は1024です。
  - 接頭辞は付けません（モデルカードの推奨どおり）。profile へ接頭辞の設定は追加しません。
- relevance 閾値を **0.54 から 0.52 へ変更** します（ユーザー決定2）。本番の `RetrievalPolicy` の既定値を0.52にします。
- 候補20・最大5・同等帯0.002・タイブレーク（`last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC`）・
  relevance の式 `1/(1+sqrt(二乗L2))`・合否基準（上位5件に1位にあるべき記憶が含まれる）は変えません（Q4）。
- 0.52 は bge-m3 の relevance 空間での値です。embedding モデルを変える場合は閾値も再調整します。
- [ADR 0010](0010-in-process-memory-search.md)・[ADR 0018](0018-memory-retrieval-context.md)・[ADR 0022](0022-memory-retrieval-from-records.md)・
  [ADR 0023](0023-semantic-evaluation-contract.md)の閾値0.54の記述は、本文を書き換えず、冒頭の置換注記でこの ADR を参照します。
- 本番で embedding のベクトルは永続化していません（[ADR 0011](0011-local-memory-embedding.md)・ADR 0022）。モデルの切替でデータの移行は不要です。2026-10-10 に実装・schemaを照合して確認済みです。
  `MemoryRetrieval.search` はローカル変数 `vectors` を `rank_records` へ渡し、storeへの書込はありません。
  `MemoryRecordStore` の保存portと `postgres_record_schema.COLUMNS`、版6の `postgres_schema` に
  embedding/vector列・永続indexはなく、model/digestは検索guardだけの識別です。

## 影響

- `compose.llamacpp.yml` と設定文書に、bge-m3 の embedding サーバーの起動設定（モデルの SHA-256 確認、pooling、loopback の port）を追加・更新します。
  従来の nomic 用 embedding コンテナは手動作成のため、切替の手順も文書にします。
- `LocalEmbeddingProfile` の model・digest・dimensions（1024）の設定例と評価用 profile を更新します。adapter のコードは変えません。
- SPEC・[記憶API](../memory.md)・[意味検索評価手順](../memory-evaluation.md)の閾値を0.52に合わせます。閾値の既定値をUTで確認します。
- ユーザー承認済み Q3 例外として、62ケースの `below-threshold` / `below-threshold-record` の偽vectorだけを
  `[0.63, 0.7765951326141569, 0.0, 0.0]`（relevance約0.53757）から
  `[0.5565557545480253, 0.8308102623821387, 0.0, 0.0]`（relevance 0.515）へ変更しました。
  閾値0.52の直下で該当なしを検証する意図を保つためです。本文・gold・他の61ケース・
  この記録以外のvectorは不変で、この本文は他ケースと共有されていません。実モデル評価は偽vectorを使いません。
- 調整用89件も本文・goldを維持し、境界付近44記録の偽vectorだけを0.52前後へ追従しました。
  答えあり0.5201〜0.521、該当なし0.519〜0.5199、別候補0.51、unrelated-nearは0.51 / 0.50です。
- VRAM は embedding サーバーの読込みで約0.6 GB 増えます（nomic v1.5 は約0.55 GB）。

## 残る課題と後続

- 同じ対象の別属性を問う「近い型」の無関係質問は、どの候補・閾値でも正解のある質問と relevance が重なり、
  単一の embedding と閾値では分離できません。調整用ケースの bge-m3・0.52 では近い型 0/10、話題が遠い型も 8/12 です。
  対処（reranker、または回答の段階で「該当なし」と判断する方法）は**この Epic では扱わず、後続の Epic で扱います**（ユーザー決定3）。
- 62ケースの実モデル再評価は [Issue #132](https://github.com/FYuki/digital-souls-core/issues/132)で行います。
  **結果が FAIL でも、正確に記録すれば Epic #124 を完了とします**（ユーザー決定4）。FAIL を PASS とは書きません。
- 調整用ケース・NoMIRACL の先頭 passage・MKQA の先頭99ペアに過適合している可能性があります。
- F16 と Q8_0 の量子化の違いを分離していません。長い passage・多数の候補・同時リクエストでの VRAM と速度は未測定です。
