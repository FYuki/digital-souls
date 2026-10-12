# PostgreSQL と意味検索の統合・配備境界

[意味検索](memory.md)と[PostgreSQL backend](postgresql.md)を同時に選択できます。
store の変更で検索方式を切り替えず、trusted 起動コードが保存先と embedding を別々に明示します。
履歴保存・正本登録・検索は既存の permission、source、policy、設定世代の境界を維持します。

## 起動側で必要な接続

```python
from digital_souls_core.local_embedding import LocalEmbedding, LocalEmbeddingProfile
from digital_souls_core.memory import MemoryContext
from digital_souls_core.memory_retrieval import MemoryRetrieval
from digital_souls_core.storage import StorageConfig, open_storage

# trusted起動側が検証した設定・LocalClassifierを持つpolicy・inferenceを用意する。
# storage_config: StorageConfig (backend="postgresql"、専用schemaを明示)
# embedding_profile: LocalEmbeddingProfile (有効化は確認済みendpointだけ)
# Coreの通常起動はこの処理を自動実行しない。
stores = open_storage(storage_config)
retrieval = MemoryRetrieval(
    stores.records, policy,
    embedding=LocalEmbedding(embedding_profile),
)
inference.memory_context = MemoryContext(retrieval)
# create_app(inference, history_store=stores.history, history_policy=policy)
# 自動形成・構造化抽出は未実装。登録はtrusted callerが保存判定を済ませたRecordBatchだけを渡す。
```

設定例は[PostgreSQL](../examples/storage.postgresql.example.json)と
[embedding](../examples/embedding.example.json)に分離しています。embedding例は [ADR 0024](adr/0024-multilingual-memory-embedding.md) の bge-m3 Q8_0
（alias `bge-m3`、実ファイルのSHA-256、1024次元、loopback 18082）です。
CLS pooling・接頭辞なし・relevance閾値0.52で使い、候補20・最大5・同等帯0.002は維持します。
embedding profile は既定無効であり、実モデルと endpoint の確認なしに有効化しません。
`StorageConfig` の読込みや保存先の変更だけで、正本履歴を自動抽出しません。

PostgreSQL の候補検索はBinding完全一致で有効なEpisode・Semanticの現行版だけを返し、
Episodeへ有効なFactを添付します。embedding未接続時はstorageを読まず空結果です。
embedding へ送る前・完了後・context送信直前に source と設定を再検証します。
vector は呼出し内だけの一時値で、pgvector、永続 index、共有 cache は不要です。
正本の版・引用のref/epochは内部guardへ保持します。モデル向けには保存文・部分日時・Factと
一時参照名を渡し、逐語引用・保存IDは注入しません。旧抽出・再構成APIは撤去済みです。

## 配備前に決める事項

1. 対象プロセスと同じ環境から接続できる PostgreSQL 18 の loopback / Unix socket、専用 DB・role・schema を確定する。
2. 既存 role で対象 DB の CONNECT と専用 schema の作成・利用ができるかを確認する。新しい role・資格情報・権限が必要なら、その変更を別途承認してから用意する。
3. trusted 起動側で `StorageConfig` を明示注入し、PG* の暗黙設定やホームの `.pgpass` に依存しない接続を構成する。
4. 履歴保存とmemory/local permission、分類器を同じ現行policyに結び付ける。正本登録はtrusted callerの保存判定が前提で、履歴保存だけで形成を始めない。
5. bge-m3 Q8_0 のdigest・alias・1024次元・CLS pooling・専用loopback endpointと資源割当を確認し、明示 profile を構成する。
   [llama.cpp運用](llamacpp-operations.md) の手順で手動作成の旧nomicコンテナを停止して切り替える。
6. 実モデルによる[検索・回答品質評価](memory-evaluation.md)は合成正本で各3回実施済み、いずれもFAIL。
   [証跡](evidence/2026-10-09-semantic-real-model-evaluation-top5.md)を参照し、品質受入と配備先の動作確認を別途行う。

この実装は DB server / role / credential の作成や、稼働 service / security 設定の変更を行いません。
対象実環境の接続可否はコードの合成試験から推定しません。DB・role をすでに利用できるかの調査結果と、
実際に変更する設定は配備判断時に照合します。データ移送は行いません。

## 合成試験と未実施範囲

`bash tools/test-postgres.sh` は固定 PostgreSQL 18 の使い捨て DB と偽 embedding で、
同時有効化の検索・由来・範囲分離・除外・private・削除・await 中の変更を検証します。
公式 SDK を使う経路も fake HTTP 応答で確認し、実モデルは呼びません。
UT / IT1 / PostgreSQL の独立 CI を実行しますが、検索品質・実モデル安全性の合格を示しません。

main マージ・dogfood 適用・実データ import・モデル取得・GPU操作は今回の統合検証に含めません。
