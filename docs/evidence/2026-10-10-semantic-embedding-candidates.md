# 接頭辞・多言語embedding・閾値の調整用比較

2026-10-10、[Issue #130](https://github.com/FYuki/digital-souls-core/issues/130) / Epic #124。
指定の `feature/130-embedding-candidates`、起点 `5b3340c9a6c50e8b047097e91d42c81c5379f4bd` で実装・測定しました。
**全5候補の閾値0.54はFAIL。掃引でも、全分類90%とランキングゲート違反0を同時に満たす閾値はありません。**
本番設定・LocalEmbedding/LocalEmbeddingProfileの既定・ランキング・SPEC・ADR・入力/goldは変更していません。
実モデルには調整用89件だけを使用しました。固定62件の実モデル評価は **NOT RUN（禁止）**。
推奨は後続検討の候補で、採用は #131 のユーザー判断です。push・PR作成・mergeは行っていません。

本文を含まない[report JSON](2026-10-10-semantic-embedding-candidates-report.json)に、各回の本番経路観測、
全ケースの全適格候補relevance、41閾値×全分類×3回、モデル取得情報、速度・VRAM・補助分布を記録しました。
query・保存文・外部質問/本文・vectorは出力していません。モデル/goldへ結果を反映していません。

## 再現条件と実装

本番 `MemoryRetrieval` → `MemoryContext` → dispatch guard と既存の独立relevance検算/採点を再利用します。
評価専用 `PrefixEmbedding` はbatch先頭にquery接頭辞、残りにdocument接頭辞を付与し、入力・順序・重複を保持します。
prefixとdelegate設定はEmbeddingSpaceの識別へ含め、途中の変更を本番guardで検出します。空batchの通信はありません。
閾値0.54の結果を `rank_records` による同じ候補snapshotの順位とケースごとに照合します。
掃引は取得したfresh vectorをメモリ内だけで使い、本番 `rank_records` の閾値だけを0.40〜0.80へ0.01刻みで変更します。
ランキングを再実装せず、候補20・最大5・同等帯0.002・最新ユーザー言及順・goldは保持します。
**掃引はランキング/品質のオフライン分析です。変更後のprivacy/context/dispatchの実接続ゲートを再受入した結果ではありません。**
基準を緩めず、relevant全包含・top-1包含・forbidden IDs・該当なしを別々に記録します。
本番で適格候補0件となるepoch_changeはembeddingを呼ばず、空relevanceと `embedding_called=false` を記録します。
それ以外の88件は各候補各回fresh batchを送り、3回で264呼出し・1056 texts。vector/結果cache・SDK retryはありません。
分類器providerは既存のsynthetic。`backend_identity_verified=false` を維持し、実分類器品質・モデル真正性の独立証明にはしません。

実モデル baseline の実行revision（全てdirty=false）:
| 候補 | baseline SHA | supplement SHA |
| --- | --- | --- |
| nomic v1.5 / なし | fc62447b95947f1151e68947943bf1eba9596efa | d71d8986cf8230223699bf928825ffe61a93bd62 |
| nomic v1.5 / search_* | fc62447b95947f1151e68947943bf1eba9596efa | d71d8986cf8230223699bf928825ffe61a93bd62 |
| nomic v2 MoE / search_* | d71d8986cf8230223699bf928825ffe61a93bd62 | d71d8986cf8230223699bf928825ffe61a93bd62 |
| bge-m3 / なし | d71d8986cf8230223699bf928825ffe61a93bd62 | d71d8986cf8230223699bf928825ffe61a93bd62 |
| multilingual-e5-large / query・passage | d71d8986cf8230223699bf928825ffe61a93bd62 | d71d8986cf8230223699bf928825ffe61a93bd62 |

前半nomic v1.5測定の後はsupplement専用のscanner事前除外と実行識別を追加しました。ランキング/接頭辞/本番経路計算は同じです。
コード3ファイルの最終SHA-256はreportの `execution_code_sha256`、入力/goldのhashは各baselineの `case_version` にあります。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH TMPDIR=/dev/shm
# 各候補を1つずつ起動し、git管理外のloopback/no-auth profileを指定する。
bash tools/with-test-postgres.sh timeout 1700 uv run --no-sync python \
  tools/evaluate-semantic-embedding-candidates.py --profile <candidate.local.json> \
  --prefix <none|nomic|e5> --runs 3 --output <candidate-report.json>
timeout 1700 uv run --no-sync python tools/evaluate-semantic-embedding-candidates.py \
  --profile <candidate.local.json> --prefix <none|nomic|e5> --supplements-only \
  --supplements /dev/shm/dsc-129-supplements-final --output <supplement-report.json>
```

CLIは調整用2ファイルへ固定し、`--cases`/`--expectations` は受け付けません。APIも89件・tuning ID集合以外を拒否します。
完走した品質/ゲートFAILの終了値は1、実行失敗は2。補助の終了値0は記述統計の完了であり、NOT RUNをPASSへ変えません。
各モデル・評価ごとに1700秒上限を設定し、1コマンド30分以内で分割しました。

## モデル出典・版・ライセンス・設定

取得先は `/home/asa/.local/share/digital-souls/models/`。新規3ファイルはHTTPS/固定revisionで取得し、
Hugging Face LFSのSHA-256・サイズとローカル計算の完全一致を確認後、partialから確定しました。
ファイルは非信頼データとして扱い、実行/モデル解析は固定llama.cppコンテナ内のみ。remote Pythonやモデル付属コードは実行していません。
v1.5は既存ファイルを再利用し、Ollama公式registry v1.5 manifestのmodel layerのdigest・サイズと照合しました。Ollamaは起動していません。
E5はcommunityのGGUF-my-repo変換配布です。元モデル/変換元と固定blobをカード・hashで確認しましたが、tensor同一性の独立検証はNOT RUNです。

### nomic-embed-text-v1.5.f16.gguf

| 項目 | 値 |
| --- | --- |
| 出典 | [固定blob](https://registry.ollama.ai/v2/library/nomic-embed-text/blobs/sha256:970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6) |
| revision | manifest-sha256:0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f |
| 量子化 | F16 |
| SHA-256 | 970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6 |
| bytes | 274290656 |
| カードのライセンス | Apache-2.0 |

### nomic-embed-text-v2-moe.f16.gguf

| 項目 | 値 |
| --- | --- |
| 出典 | [固定blob](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe-GGUF/resolve/ffbcf4c99e5d617dda10ec8c0e9f75754b0cbb80/nomic-embed-text-v2-moe.f16.gguf) |
| revision | ffbcf4c99e5d617dda10ec8c0e9f75754b0cbb80 |
| 量子化 | F16 |
| SHA-256 | a5db3381f2e514d3490a3a31fe70eb1a65e95016c85c6c2c23223b810806594f |
| bytes | 957680480 |
| カードのライセンス | apache-2.0 |

### bge-m3-q8_0.gguf

| 項目 | 値 |
| --- | --- |
| 出典 | [固定blob](https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/9eba04c5d75ba5a1595e45de734d36bef4e5cb98/bge-m3-q8_0.gguf) |
| revision | 9eba04c5d75ba5a1595e45de734d36bef4e5cb98 |
| 量子化 | Q8_0 |
| SHA-256 | aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173 |
| bytes | 634553760 |
| カードのライセンス | mit |

### multilingual-e5-large-q8_0.gguf

| 項目 | 値 |
| --- | --- |
| 出典 | [固定blob](https://huggingface.co/chris-code/multilingual-e5-large-Q8_0-GGUF/resolve/272a0975bd1a67886c0719767af84dfd2cbd9aa4/multilingual-e5-large-q8_0.gguf) |
| revision | 272a0975bd1a67886c0719767af84dfd2cbd9aa4 |
| 量子化 | Q8_0 |
| SHA-256 | 6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3be |
| bytes | 603097600 |
| カードのライセンス | mit |

| 元モデルカード（2026-10-10確認） | pooling / 次元 | 検索接頭辞 |
| --- | --- | --- |
| [nomic v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/blob/e9b6763023c676ca8431644204f50c2b100d9aab/README.md) | mean / 768 | search_query: / search_document: |
| [nomic v2 MoE](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe/blob/1066b6599d099fbb93dfcb64f9c37a7c9e503e85/README.md) | mean / 768 | search_query: / search_document: |
| [bge-m3](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/README.md) | CLS / 1024 | なし |
| [multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large/blob/3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3/README.md) | mean / 1024 | query: / passage: |

各接頭辞の末尾は半角スペースです。bge-m3のCLSは[固定pooling config](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/1_Pooling/config.json)でも確認しました。
v1.5既存blobと現在のHugging Face公式GGUFはhash/サイズが異なるため、同一ファイルと偽っていません。

## 閾値0.54の3回の分類品質・ゲート

各セルはquality_passed / total。3回とも分類・返却ID・gate判定が一致しました（score/所要時間の同一性は要求しません）。
| 分類 | nomic v1.5 / なし | nomic v1.5 / search_* | nomic v2 MoE / search_* | bge-m3 / なし | multilingual-e5-large / query・passage |
| --- | --- | --- | --- | --- | --- |
| synonym | 9/10 | 9/10 | 5/10 | 10/10 | 10/10 |
| paraphrase | 8/10 | 8/10 | 9/10 | 10/10 | 10/10 |
| cross_language | 0/24 | 0/24 | 0/24 | 20/24 | 24/24 |
| unrelated | 0/22 | 0/22 | 18/22 | 12/22 | 0/22 |
| threshold | 6/12 | 6/12 | 9/12 | 6/12 | 6/12 |
| private | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| excluded | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_source | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| deleted_memory | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_character | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_subject | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| binding_client | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| after_search | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| after_answer | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| epoch_change | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| equivalent_order | 1/1 | 1/1 | 0/1 | 1/1 | 1/1 |

| 集計 | nomic v1.5 / なし | nomic v1.5 / search_* | nomic v2 MoE / search_* | bge-m3 / なし | multilingual-e5-large / query・passage |
| --- | --- | --- | --- | --- | --- |
| 品質条件 / 89 | 34 | 34 | 51 | 69 | 61 |
| 全条件 / 89 | 28 | 28 | 47 | 63 | 55 |
| 各回合否 | FAIL | FAIL | FAIL | FAIL | FAIL |

ゲート違反件数（各回同じ、重複計上あり）:
| gate | nomic v1.5 / なし | nomic v1.5 / search_* | nomic v2 MoE / search_* | bge-m3 / なし | multilingual-e5-large / query・passage |
| --- | --- | --- | --- | --- | --- |
| forbidden_ids | 34 | 34 | 11 | 22 | 34 |
| threshold | 0 | 0 | 0 | 0 | 0 |
| verified_records | 0 | 0 | 0 | 0 | 0 |
| top_one | 27 | 27 | 31 | 4 | 0 |
| dispatch | 0 | 0 | 0 | 0 | 0 |
| context | 0 | 0 | 0 | 0 | 0 |
| no_match | 28 | 28 | 7 | 16 | 28 |

forbidden_idsには、近い無関係ケースや紛らわしい記録のgold禁止IDも含まれます。一律にprivacy漏洩と数えません。
private/excluded/削除出典・記憶/Binding/after_search/after_answer/epoch_changeの10分類は各候補1/1、全ゲート違反0。
nomic v2のequivalent_orderは正解の閾値落ちで品質/top_one FAIL。実モデルではfixtureの全順序一致ゲートを追加していません。

閾値0.54の方向・題材別品質（各回同じ）:

| 候補 | 英語query/日本語記憶 / 12 | 日本語query/英語記憶 / 12 | 日常 / 43 | 技術 / 35 |
| --- | --- | --- | --- | --- |
| nomic-v15-none | 0 | 0 | 12 | 11 |
| nomic-v15-prefix | 0 | 0 | 12 | 11 |
| nomic-v2 | 0 | 0 | 24 | 17 |
| bge-m3 | 11 | 9 | 33 | 25 |
| e5-large | 12 | 12 | 27 | 23 |

## 閾値の釣り合い（全41点）

数値は3回の正解件数。同じなら単一値、差があればmin–maxで表示します。
far/nearはunrelated該当なし、T+はthreshold答えあり、T−はthreshold該当なし。forbidden/top-1は全89件の違反数。
分母: synonym 10、paraphrase 10、cross 24、far 12、near 10、T+ 6、T− 6。全分類の完全な集計はreportにあります。
全候補で、全分類90%とforbidden/top-1/no_match違反0を同時に満たす範囲は **なし**。
下表の閾値を本番へ採用したものではありません。

### nomic v1.5 / なし

| 閾値 | synonym | paraphrase | cross | far | near | T+ | T− | forbidden | top-1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.41 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.42 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.43 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.44 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.45 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.46 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.47 | 9 | 8 | 17 | 0 | 0 | 6 | 0 | 34 | 10 |
| 0.48 | 9 | 8 | 5 | 0 | 0 | 6 | 0 | 34 | 22 |
| 0.49 | 9 | 8 | 2 | 0 | 0 | 6 | 0 | 34 | 25 |
| 0.50 | 9 | 8 | 1 | 0 | 0 | 6 | 0 | 34 | 26 |
| 0.51 | 9 | 8 | 1 | 0 | 0 | 6 | 0 | 34 | 26 |
| 0.52 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.53 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.54 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.55 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.56 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.57 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.58 | 9 | 8 | 0 | 4 | 0 | 6 | 0 | 30 | 27 |
| 0.59 | 8 | 7 | 0 | 6 | 0 | 6 | 0 | 28 | 29 |
| 0.60 | 8 | 6 | 0 | 9 | 0 | 6 | 0 | 25 | 30 |
| 0.61 | 8 | 6 | 0 | 10 | 0 | 6 | 1 | 23 | 30 |
| 0.62 | 7 | 6 | 0 | 11 | 0 | 6 | 1 | 22 | 31 |
| 0.63 | 7 | 4 | 0 | 11 | 3 | 6 | 2 | 18 | 33 |
| 0.64 | 6 | 1 | 0 | 12 | 4 | 6 | 2 | 16 | 37 |
| 0.65 | 5 | 1 | 0 | 12 | 5 | 4 | 2 | 14 | 41 |
| 0.66 | 2 | 1 | 0 | 12 | 6 | 3 | 2 | 11 | 45 |
| 0.67 | 1 | 1 | 0 | 12 | 8 | 3 | 4 | 6 | 46 |
| 0.68 | 0 | 1 | 0 | 12 | 8 | 2 | 4 | 6 | 48 |
| 0.69 | 0 | 0 | 0 | 12 | 9 | 2 | 4 | 4 | 49 |
| 0.70 | 0 | 0 | 0 | 12 | 9 | 2 | 6 | 2 | 49 |
| 0.71 | 0 | 0 | 0 | 12 | 9 | 1 | 6 | 1 | 51 |
| 0.72 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 52 |
| 0.73 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 52 |
| 0.74 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 56 |
| 0.75 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 58 |
| 0.76 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 58 |
| 0.77 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 58 |
| 0.78 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 58 |
| 0.79 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.80 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |

### nomic v1.5 / search_*

| 閾値 | synonym | paraphrase | cross | far | near | T+ | T− | forbidden | top-1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.41 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.42 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.43 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.44 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.45 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.46 | 9 | 8 | 21 | 0 | 0 | 6 | 0 | 34 | 6 |
| 0.47 | 9 | 8 | 18 | 0 | 0 | 6 | 0 | 34 | 9 |
| 0.48 | 9 | 8 | 13 | 0 | 0 | 6 | 0 | 34 | 14 |
| 0.49 | 9 | 8 | 2 | 0 | 0 | 6 | 0 | 34 | 25 |
| 0.50 | 9 | 8 | 1 | 0 | 0 | 6 | 0 | 34 | 26 |
| 0.51 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.52 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.53 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.54 | 9 | 8 | 0 | 0 | 0 | 6 | 0 | 34 | 27 |
| 0.55 | 8 | 8 | 0 | 4 | 0 | 6 | 0 | 30 | 28 |
| 0.56 | 8 | 8 | 0 | 9 | 0 | 6 | 0 | 25 | 28 |
| 0.57 | 7 | 6 | 0 | 10 | 3 | 6 | 1 | 20 | 31 |
| 0.58 | 4 | 3 | 0 | 11 | 3 | 6 | 1 | 19 | 38 |
| 0.59 | 4 | 1 | 0 | 12 | 6 | 5 | 3 | 11 | 41 |
| 0.60 | 2 | 1 | 0 | 12 | 8 | 4 | 4 | 6 | 44 |
| 0.61 | 0 | 0 | 0 | 12 | 9 | 3 | 5 | 3 | 48 |
| 0.62 | 0 | 0 | 0 | 12 | 10 | 2 | 5 | 2 | 49 |
| 0.63 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 1 | 50 |
| 0.64 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 0 | 53 |
| 0.65 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 56 |
| 0.66 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 57 |
| 0.67 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.68 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.69 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.70 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.71 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.72 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.73 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.74 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.75 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.76 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.77 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.78 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.79 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.80 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |

### nomic v2 MoE / search_*

| 閾値 | synonym | paraphrase | cross | far | near | T+ | T− | forbidden | top-1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.41 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.42 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.43 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.44 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.45 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.46 | 10 | 10 | 23 | 0 | 0 | 6 | 0 | 34 | 1 |
| 0.47 | 10 | 10 | 23 | 0 | 0 | 6 | 0 | 34 | 1 |
| 0.48 | 10 | 10 | 23 | 2 | 0 | 6 | 0 | 32 | 1 |
| 0.49 | 10 | 10 | 12 | 10 | 0 | 6 | 0 | 24 | 12 |
| 0.50 | 10 | 10 | 6 | 11 | 0 | 6 | 0 | 23 | 18 |
| 0.51 | 10 | 10 | 2 | 12 | 0 | 6 | 0 | 22 | 22 |
| 0.52 | 9 | 10 | 0 | 12 | 2 | 6 | 0 | 20 | 25 |
| 0.53 | 7 | 10 | 0 | 12 | 5 | 6 | 1 | 15 | 27 |
| 0.54 | 5 | 9 | 0 | 12 | 6 | 6 | 3 | 11 | 31 |
| 0.55 | 4 | 8 | 0 | 12 | 9 | 6 | 4 | 6 | 33 |
| 0.56 | 3 | 3 | 0 | 12 | 10 | 6 | 6 | 2 | 39 |
| 0.57 | 2 | 1 | 0 | 12 | 10 | 3 | 6 | 0 | 48 |
| 0.58 | 0 | 1 | 0 | 12 | 10 | 0 | 6 | 0 | 55 |
| 0.59 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.60 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.61 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.62 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.63 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.64 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.65 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.66 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.67 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.68 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.69 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.70 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.71 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.72 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.73 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.74 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.75 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.76 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.77 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.78 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.79 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.80 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |

### bge-m3 / なし

| 閾値 | synonym | paraphrase | cross | far | near | T+ | T− | forbidden | top-1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.41 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.42 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.43 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.44 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.45 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.46 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.47 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.48 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.49 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.50 | 10 | 10 | 24 | 1 | 0 | 6 | 0 | 33 | 0 |
| 0.51 | 10 | 10 | 24 | 2 | 0 | 6 | 0 | 32 | 0 |
| 0.52 | 10 | 10 | 24 | 8 | 0 | 6 | 0 | 26 | 0 |
| 0.53 | 10 | 10 | 21 | 10 | 0 | 6 | 0 | 24 | 3 |
| 0.54 | 10 | 10 | 20 | 12 | 0 | 6 | 0 | 22 | 4 |
| 0.55 | 10 | 10 | 16 | 12 | 1 | 6 | 0 | 21 | 8 |
| 0.56 | 10 | 10 | 10 | 12 | 1 | 6 | 1 | 20 | 14 |
| 0.57 | 10 | 9 | 3 | 12 | 3 | 6 | 3 | 16 | 22 |
| 0.58 | 9 | 8 | 1 | 12 | 5 | 6 | 3 | 13 | 26 |
| 0.59 | 8 | 7 | 1 | 12 | 8 | 6 | 3 | 8 | 29 |
| 0.60 | 5 | 7 | 0 | 12 | 9 | 6 | 5 | 5 | 33 |
| 0.61 | 4 | 4 | 0 | 12 | 10 | 6 | 5 | 3 | 37 |
| 0.62 | 3 | 3 | 0 | 12 | 10 | 6 | 5 | 1 | 42 |
| 0.63 | 2 | 2 | 0 | 12 | 10 | 3 | 6 | 0 | 48 |
| 0.64 | 1 | 1 | 0 | 12 | 10 | 3 | 6 | 0 | 52 |
| 0.65 | 0 | 1 | 0 | 12 | 10 | 3 | 6 | 0 | 54 |
| 0.66 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 0 | 56 |
| 0.67 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 0 | 56 |
| 0.68 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 0 | 58 |
| 0.69 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 59 |
| 0.70 | 0 | 0 | 0 | 12 | 10 | 1 | 6 | 0 | 59 |
| 0.71 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.72 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.73 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.74 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.75 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.76 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.77 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.78 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.79 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.80 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |

### multilingual-e5-large / query・passage

| 閾値 | synonym | paraphrase | cross | far | near | T+ | T− | forbidden | top-1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.41 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.42 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.43 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.44 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.45 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.46 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.47 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.48 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.49 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.50 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.51 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.52 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.53 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.54 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.55 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.56 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.57 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.58 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.59 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.60 | 10 | 10 | 24 | 0 | 0 | 6 | 0 | 34 | 0 |
| 0.61 | 10 | 10 | 22 | 0 | 0 | 6 | 0 | 34 | 2 |
| 0.62 | 10 | 10 | 17 | 6 | 0 | 6 | 0 | 28 | 7 |
| 0.63 | 10 | 10 | 5 | 11 | 0 | 6 | 0 | 23 | 19 |
| 0.64 | 10 | 10 | 0 | 12 | 0 | 6 | 0 | 22 | 24 |
| 0.65 | 10 | 10 | 0 | 12 | 2 | 6 | 1 | 19 | 24 |
| 0.66 | 10 | 10 | 0 | 12 | 3 | 6 | 3 | 16 | 24 |
| 0.67 | 7 | 10 | 0 | 12 | 8 | 6 | 5 | 8 | 27 |
| 0.68 | 6 | 7 | 0 | 12 | 10 | 6 | 6 | 3 | 32 |
| 0.69 | 3 | 6 | 0 | 12 | 10 | 6 | 6 | 2 | 36 |
| 0.70 | 1 | 4 | 0 | 12 | 10 | 6 | 6 | 1 | 40 |
| 0.71 | 1 | 0 | 0 | 12 | 10 | 5 | 6 | 0 | 49 |
| 0.72 | 0 | 0 | 0 | 12 | 10 | 2 | 6 | 0 | 53 |
| 0.73 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 58 |
| 0.74 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.75 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.76 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.77 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.78 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.79 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |
| 0.80 | 0 | 0 | 0 | 12 | 10 | 0 | 6 | 0 | 60 |

## ローカル補助: NoMIRACL ja / MKQA ja-en

[取得・版・ライセンス](../../evals/semantic/tuning/README.md#ローカル限定の外部補助データ)の既存ローカル変換を利用しました。再取得はNOT RUN（存在・hash確認済み）。
NoMIRACL: fixed devの関連50問+非関連50問、976 passage judgments（関連87、非関連889）。
title + 改行 + textの先頭200 Unicode文字、query先頭128文字を全候補へ同じ制限で使用。
長文モデルと512-tokenモデルを比較するための明示的な補助前処理で、本番や元ファイルの変更ではありません。
本文の先頭への切り詰めで関連箇所が失われる限界があり、全長passage評価とは区別します。
MKQA: 元100ペアのうち1ペアがraw質問のscanner failed（quote-leading非JSON）で **NOT RUN**。
scanner/LocalEmbeddingは変更せず、モデルやscoreを見る前の同じraw入力事前検査で全候補から同じペアを除外。
残る99ペアと全9702非ペアを両方向で比較しました。両側query接頭辞の対称embeddingなので方向間の分布/AUCは一致します。
no-prefixにも無理にprefixを足さず、拒否をfixtureへfallbackしていません。本文・質問文・対象IDは公開していません。
AUCはP(positive relevance > negative relevance)+0.5×同点。標本/文書長制限付きの記述統計で、品質PASSではありません。

| 候補 | NoMIRACL AUC | 関連 p05 / p50 / p95 | 非関連 p05 / p50 / p95 | MKQA AUC | ペア p05 / p50 / p95 | 非ペア p05 / p50 / p95 |
| --- | --- | --- | --- | --- | --- | --- |
| nomic v1.5 / なし | 0.588599 | 0.5242 / 0.5857 / 0.6353 | 0.5280 / 0.5719 / 0.6199 | 0.802386 | 0.4639 / 0.4866 / 0.6169 | 0.4568 / 0.4701 / 0.4877 |
| nomic v1.5 / search_* | 0.562675 | 0.5247 / 0.5779 / 0.6279 | 0.5308 / 0.5711 / 0.6136 | 0.795017 | 0.4679 / 0.4930 / 0.6415 | 0.4629 / 0.4760 / 0.4940 |
| nomic v2 MoE / search_* | 0.778416 | 0.4949 / 0.5306 / 0.5643 | 0.4690 / 0.5033 / 0.5407 | 0.998524 | 0.4874 / 0.5506 / 0.6864 | 0.4210 / 0.4328 / 0.4559 |
| bge-m3 / なし | 0.800758 | 0.5037 / 0.5503 / 0.5830 | 0.4857 / 0.5167 / 0.5545 | 0.998792 | 0.5327 / 0.6329 / 0.7695 | 0.4448 / 0.4586 / 0.4860 |
| multilingual-e5-large / query・passage | 0.826914 | 0.6214 / 0.6628 / 0.6954 | 0.5996 / 0.6302 / 0.6651 | 0.997775 | 0.5961 / 0.6622 / 0.7268 | 0.5300 / 0.5463 / 0.5706 |

MKQAは翻訳質問の近さであり、別属性の記憶へ誤一致しないことを証明しません。NoMIRACLのAUCが良くても同じCore閾値が適用できるとは限りません。

## 速度・VRAM・コンテナの終了

RTX 4070 Ti SUPER 16376 MiB、NVIDIA-SMI 590.57 / driver 591.86。各ロード前にnvidia-smiを確認し、他のモデル推論は見えませんでした。
表示等を含む基底VRAMは約3066〜3070 MiB。MiBはGPU全体のロード前/ロード後観測で、モデル専用のpeakや最大運用負荷ではありません。
embedの速度はSDK/HTTP/正規化済み応答検証を含む1056 texts / embed合計秒。ケース秒はDB setup/search/teardown/掃引等を含む全評価秒 / 267。
モデル全体は直列。CPU品質ゲート/別の使い捨てPG試験との重複があるため、厳密な性能ベンチマークの保証はしません。

| 候補 | embed texts/s | embed合計秒 | 評価全体秒 | 秒/case | VRAM before→loaded / 差 MiB | 停止後 MiB |
| --- | --- | --- | --- | --- | --- | --- |
| nomic v1.5 / なし | 61.93 | 17.05 | 67.10 | 0.251 | 3069→3620 / +551 | 3069 |
| nomic v1.5 / search_* | 61.60 | 17.14 | 66.67 | 0.250 | 3069→3620 / +551 | 3069 |
| nomic v2 MoE / search_* | 64.44 | 16.39 | 63.28 | 0.237 | 3069→3925 / +856 | 3070 |
| bge-m3 / なし | 51.37 | 20.56 | 72.21 | 0.270 | 3070→3669 / +599 | 3069 |
| multilingual-e5-large / query・passage | 53.03 | 19.91 | 73.52 | 0.275 | 3066→3665 / +599 | 3092 |

imageは全モデルで `ghcr.io/ggml-org/llama.cpp@sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
build 11347 / 0.5.0-dev / revision `5fc4f3c8c7103ffd0b7ff5ee4855bcc78a3ed5cd`、GNU 14.2.0 / Linux x86_64を確認。
共通server: embeddings、ctx/batch/ubatch=2048、parallel=1、gpu-layers=99、threads=8、モデルに合うpooling。
既存v1.5は18082、他3モデルは未使用の127.0.0.1:18083。コンテナ内0.0.0.0:8080の公開先はloopbackだけです。
一時コンテナはuser=現uid/gid、read-only、cap-drop ALL、no-new-privileges、pids=256、memory=12g、
64MiB noexec/nosuid tmpfs、model read-only bind、cache-ram=0、no-webui、log-disable。同時に複数モデルをロードしていません。
profileは0700のgit管理外directory / 0600 file、loopbackだけ、enabled明示、資格情報なし。

| モデル | 終了確認UTC | 終了状態 |
| --- | --- | --- |
| nomic v1.5 / なし | 2026-10-10T02:57:23Z | 既存embedding: docker stop後inspect=exited / docker ps -aでExited |
| nomic v2 MoE / search_* | 2026-10-10T02:58:50Z | 一時container: stop後exited→rm→docker ps -a不在 |
| bge-m3 / なし | 2026-10-10T03:01:14Z | 一時container: stop後exited→rm→docker ps -a不在 |
| multilingual-e5-large / query・passage | 2026-10-10T03:03:17Z | 一時container: stop後exited→rm→docker ps -a不在 |

gemma（18081）は一度も起動しておらず、終了時も既存containerがExitedです。使い捨てPGもrunner cleanupで削除し、core-pg-*不在を確認しました。
ダウンロード済みGGUFは許可されたモデルdirectoryに保持しました。

## 品質ゲート・開発途中のFAIL・未実施

| ゲート | 最終結果 |
| --- | --- |
| uv sync --locked / uv lock --check | PASS / PASS、80 packages、lock変更なし |
| ruff check / format --check（CI対象＋候補CLI） | PASS / PASS、120 files |
| mypy（同じ追加CLIをfilesへ追加） | PASS、120 source files |
| pytest -m ut | PASS、741件 |
| pytest -m it1 | PASS、590件 |
| uv build --no-build-isolation | PASS、sdist/wheel |
| hash付き依存install→wheel独立install/import | PASS |
| bash tools/test-postgres.sh | PASS、412件。候補fixture89×3も専用PGで確認 |
| evals/semantic npm ci --no-audit --no-fund | PASS、575 packages |
| node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs | PASS、59 registered tests、SKIP/TODO/cancelledなし |
| node tools/check-docs.mjs / git diff --check | PASS / PASS（証跡をstage後に確認） |
| shell bash -n / PostgreSQL Compose config | PASS / PASS |
| 偽embedding固定62×3 | PASS、各回62/62、全分類/ゲートPASS、183 calls、quality_evidence=false |
| 偽embedding調整89×3 | PASS、各回89/89、全分類/ゲートPASS、264 calls、quality_evidence=false |
| 禁止ファイルの起点との差分 | PASS、ケース/gold、SPEC/ADR、本番embedding/ranking/retrievalは空差分 |

API CIのruff対象とpyprojectのmypy filesへCLIを追加。製品/default動作を増やしていません。
markerのdeselectedは選択外でSKIPではありません。依存由来のdeprecation/type/npm警告はあります。
開発途中: 未実装moduleのUT collection ERROR（Red）、ruff長行/format、mypy型推論、
補助schemaのsource_id継承誤り（合成UT FAIL）、掃引集計のcounter初期化漏れ（実モデル試行exit 2）、
MKQA scanner failed（補助試行exit 2）がありました。全て最終修正/明示NOT RUN化後に必要なゲート/測定を再実行しました。
counter初期化を完全な41点集計UTで回帰し、scanner拒否を合成UTでprefixなし/ありの両方に確認します。
失敗・不完全な試行や初期commitの成績は最終比較reportへ含めていません。最終baselineは全5候補各3回完走、各回89件で実行errorなし。
NOT RUN: 固定62件の実モデル（禁止）、全長NoMIRACL、MKQA拒否1ペア、他量子化/他pooling/多言語モデル接頭辞なしの追加比較、
実回答モデル・実分類器品質・形成〜利用IT2/ST・本番採用・GitHub CI。実モデル品質FAILをゲートPASSで相殺しません。

## 推奨候補と残る論点

全分類・必須ゲートを満たす採用推奨の閾値範囲は、5候補とも **なし** です。
次のユーザー判断へ渡す比較候補は以下です。いずれも品質受入・本番採用ではありません。

| 優先 | 組み合わせ | 次の比較へ残す範囲 | 根拠・残るFAIL |
| --- | --- | --- | --- |
| 第一候補 | bge-m3 Q8_0、CLS、接頭辞なし | 0.52〜0.54（代表0.54） | 0.54は遠い該当なし12/12、同義語・言い換え10/10、日英20/24。0.52なら日英24/24だがfar8/12。nearは全範囲0/10。NoMIRACL AUC 0.800758、MKQA 0.998792、公式llama.cpp組織のGGUF、VRAM増加599 MiB。 |
| 次点 | multilingual-e5-large Q8_0、mean、query: / passage: | 0.60〜0.61（正解保持を優先する比較） | 0.60は同義語・言い換え・日英すべて100%、0.61は日英22/24。far/near・threshold該当なしはどちらも0件。NoMIRACL AUC 0.826914は最良だが、Coreの該当なし性能を保証しない。 |
| 補助候補 | nomic v2 MoE F16、mean、search_query: / search_document: | 0.47〜0.48（正解保持の参考） | 日英23/24、同義語・言い換え10/10。farは0/12〜2/12、nearは0/10。0.49では日英12/24に落ちる。速度とVRAMは有利だが閾値の重なりは未解決。 |
| 推奨しない | nomic v1.5 F16、mean、接頭辞なし/あり | 推奨範囲なし | 0.54で日英0/24、低閾値でも最良21/24。接頭辞追加だけでは分類成績が改善しない。0.55〜0.56はfarを多少抑える参考点だが、日英・nearが未達。 |

第一候補の0.54は主要5分類の最小品質率が50%で、掃引中の他候補の最良点より高いという
均衡を理由に残しています。各分類90%・必須ゲート0という合格条件を50%へ緩めたものではありません。
E5の次点範囲は、翻訳・正解取りこぼしを優先した別の比較目的です。該当なしの受入設定として推奨しません。

近い別属性の質問は、正解のある質問よりrelevanceが高いケースがあります。
今回のモデル・prefix・単一閾値だけでは、その重なりを解消できませんでした。
下位の無関係候補を上位5件へ残すgold禁止ID違反も、正解包含の成功とは別に残ります。
#131ではこの制約と候補の優先をユーザーへ提示し、採用・追加比較・後続方針を決める必要があります。
本作業ではreranker・追加判定・検索件数や期待値の変更を実装していません。

VRAMは今回の短いbatch/ctxで約0.6〜0.9 GiB増加ですが、全長passage・大きな候補数・同時リクエスト時は未測定です。
速度は単一GPU・短い合成ケースの観測で、厳密な性能比較や上限ではありません。
F16とQ8_0の違いもモデル間比較の要因で、量子化だけの効果を分離していません。
元モデルカードのApache-2.0 / MITを確認していますが、E5 community変換の独立tensor検証は残ります。

日英の方向・日常/技術の件数をreportのcross_en_query/cross_ja_query/daily/technicalへ分けています。
bge-m3の0.54は日英の取りこぼしが4件、E5は0件であり、全体成績だけで方向の偏りを隠しません。
今回の合成89件の人物・題材、NoMIRACLの先頭passage、MKQAの先頭99有効ペアへの過適合があり得ます。
固定62件へ一般化するかは今回 **NOT RUN** です。#131でユーザーが採用候補を決めた後の合否判定へ持ち越します。
