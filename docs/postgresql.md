# 明示的に接続するPostgreSQL backend

履歴・記憶の保存先はPostgreSQLへ一本化します（[ADR 0021](adr/0021-postgresql-only-storage.md)）。
SQLiteは撤去済みです。
PostgreSQLへの接続はtrusted起動コードで明示します。
PostgreSQLは空の専用schemaを初期化して使います。データを移す機能はありません。
通常の`create_app()`は保存無効のままで、設定ファイルを置いただけでは接続・保存・記憶抽出を始めません。
接続設計は[ADR 0012](adr/0012-postgresql-storage.md)、現行の正本と検索は
[ADR 0016](adr/0016-memory-kinds-and-records.md)・[ADR 0022](adr/0022-memory-retrieval-from-records.md)を参照してください。

## ローカルDockerでの起動

Docker Engine と Docker Compose v2以降を用意します。
[Compose定義](../core/compose.postgresql.json)は合成試験と同じ公式PostgreSQL 18 imageをdigest固定で使い、
公開先を`127.0.0.1`に限定します。データはnamed volumeの`/var/lib/postgresql`へ永続化します。

リポジトリのルートで、[空の環境変数例](../core/examples/postgresql.env.example)をコピーします。

```sh
cp examples/postgresql.env.example .env.postgresql.local
chmod 600 .env.postgresql.local
```

`.env.postgresql.local`をローカルで編集し、専用のdatabase・user・passwordを設定します。
例ファイルには値を含めません。このローカルファイルは`.gitignore`の`.env.*`で除外されます。
passwordは十分な長さのランダム値を使い、画面・ログ・Gitへ出力しません。
値を環境変数で渡す場合も`DSC_POSTGRES_*`を使い、Coreが拒否する`PG*`は使いません。
5432が使用中なら、同じファイルに`DSC_POSTGRES_PORT`を追加して別の空きportを指定します。

```sh
docker compose --env-file .env.postgresql.local -f compose.postgresql.json config --quiet
docker compose --env-file .env.postgresql.local -f compose.postgresql.json up -d --wait
```

`config --quiet`は構文と必須変数を検査し、秘密を含む展開結果を表示しません。
初回起動時だけdatabase・role・passwordを設定します。既存volumeがある場合、環境変数の変更で
DBのpassword等は変更されません。healthcheckは起動待機用で、Coreの認証・schema検証は以下の明示接続で行います。

trusted起動プロセスへ同じ`DSC_POSTGRES_DATABASE`・`DSC_POSTGRES_USER`・`DSC_POSTGRES_PASSWORD`と
任意の`DSC_POSTGRES_PORT`を環境変数として渡し、次のように接続します。
Composeの`--env-file`はCoreプロセスの環境変数を設定しません。起動側でローカルファイルを安全に読み込み、
または秘密管理から注入してください。秘密をJSON例へ書き込む必要はありません。

```python
import os
from pydantic import SecretStr
from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.storage import StorageConfig, open_storage

postgres = PostgresConfig(
    host="127.0.0.1",
    port=int(os.environ.get("DSC_POSTGRES_PORT", "5432")),
    database=os.environ["DSC_POSTGRES_DATABASE"],
    user=os.environ["DSC_POSTGRES_USER"],
    password=SecretStr(os.environ["DSC_POSTGRES_PASSWORD"]),
    schema_name="digital_souls_core",
)
stores = open_storage(StorageConfig(backend="postgresql", postgres=postgres))
```

停止時はvolumeを残します。再起動は同じ`up -d --wait`です。
データ削除を意図する場合だけ`down --volumes`を使います。このCompose projectの全DBデータを削除します。

```sh
# 停止（データ保持）
docker compose --env-file .env.postgresql.local -f compose.postgresql.json down
# 停止とデータ削除
docker compose --env-file .env.postgresql.local -f compose.postgresql.json down --volumes
```

CIでは既存`postgres-storage`で`config --no-interpolate --quiet`による構文検査、
既存`docs-tooling`のNode試験でimage・公開先・named volume・秘密値なしを検査します。
ローカル用Composeの実起動・TCP接続はCIでは **NOT RUN** です。
合成データによるローカル実起動・schema初期化は[日付付き証跡](evidence/2026-10-07-local-postgresql.md)を参照してください。

## 明示接続

[設定例](../core/examples/storage.postgresql.example.json)は秘密を含まない合成設定です。
database・user・portは例であり、稼働サービスに対応する確認済み値ではありません。
DBと利用roleは運用者が事前に用意します。CoreがDB・role・PostgreSQLサービスを作成することはありません。

```python
from pathlib import Path
from digital_souls_core.memory_retrieval import MemoryRetrieval
from digital_souls_core.storage import StorageConfig, open_storage

config = StorageConfig.model_validate_json(
    Path("examples/storage.postgresql.example.json").read_text(encoding="utf-8")
)
# 明示した接続先へ接続し、空の専用schemaを初期化する。
stores = open_storage(config)

# inferenceと現在のpolicyはtrusted起動側で明示構成済み。
# create_app(inference, history_store=stores.history, history_policy=policy)
# MemoryRetrieval(stores.records, policy, embedding=trusted_embedder)
```

`StorageConfig`は必須の`backend: "postgresql"`と必須の`postgres`だけを受け付けます。
型の暗黙変換、未知field、他のbackend、接続設定の欠落・不正はfail-closedで拒否します。

`StorageStores` は同じDBの `history: HistoryStore` と `records: MemoryRecordStore` を返します。
factoryは `PostgresHistory` と `PostgresMemoryRecords` を構成し、別DBに分ける設定は提供しません。
`open_storage` の明示呼出しには初期化・旧版移行の副作用があります。
履歴経路へのstore/policy注入、正本の明示登録、embeddingの注入は別操作です。
構造化抽出・形成jobは未実装で、履歴保存だけで記憶形成を始めません。stateless APIは自動保存しません。

## PostgreSQL設定

`PostgresConfig`は`digital_souls_core.postgres_db`にあります。

| field | 条件・既定値 |
| --- | --- |
| `host` | `127.0.0.1`、または正規化された絶対POSIX Unix socketディレクトリ。既定は`127.0.0.1` |
| `port` | 1〜65535、既定5432 |
| `database` / `user` | 必須。1〜63文字の単純な名前 |
| `password` | 任意の`SecretStr`。未指定は空値を明示し、環境から補完しない |
| `schema_name` | 小文字の専用識別子、最大63文字。既定`digital_souls_core`。`public`等は拒否 |
| `connect_timeout` | 1〜30秒、既定5秒 |
| `statement_timeout_ms` | 1〜30000 ms、既定5000 ms |
| `lock_timeout_ms` | 1〜30000 ms、既定3000 ms |

hostname・remote host・接続URI・複数hostは受け付けません。libpqへの設定は明示引数に固定し、
非空の`PG*`環境変数がある場合は接続前に拒否します。passfileは`/dev/null/no-passfile`で、ホームの`.pgpass`も
使いません。設定名を`PG*`へ置いたまま暗黙に反映する運用は行いません。
ローカル限定の初期構成ではSSL・GSS暗号化・channel bindingを無効にしています。
認証方式も `none,password,md5,scram-sha-256` に限定し、暗黙の GSS/SSPI/OAuth 認証を使いません。

サンプルにはpasswordを含めず、実際の資格情報はtrusted起動側で管理します。HTTP callerから接続先・
schema・password・scopeを指定する機能はありません。DB接続権限だけでhistory/memory permissionを
許可することもありません。

## 初期化と復旧境界

`PostgresDatabase(config).initialize()`は専用schemaを初期化し、PostgresHistory/PostgresMemoryRecords adapterの構築時にも
同じ処理を使います。schema単位のtransaction advisory lockを取り、存在しなければschemaを作り、
空schemaへtableとversion 7を原子的に登録します。turnの`stated_at`はnullableな`TIMESTAMPTZ`で、
新規turn保存時のUTC日時を保存します。trustedコードで履歴adapterの時計を注入でき、既定は保存時の
現在時刻です。snapshotは保存値をUTCで復元します（[履歴API](history-api.md)）。

既存Core schemaはtable集合・列名/型/null/default/identity・PK/FK/UNIQUE制約・versionを照合します。不一致や途中のschemaは拒否し、
不正schemaを自動修復しません。正常なversion 1〜6は各版の定義で厳密検証した後、同じtransactionで
必要な段階を経てversion 7へ移行し、各段階で新版を検証します。version 1→2で`turns.stated_at`、
2→3で`turns.memory_confirmation`（既定値`{}`）、3→4で`turn_tombstones`・`turn_deletions`、
4→5で下記の正本表を追加します。5→6では `memory_sources` → `memories` → `memory_jobs` の順に
旧逐語3表を削除し、`memory_source_lookup` も所有表と共に削除します。逐語記憶を正本へ移送しません。
6→7ではnullableなTEXT列 `turns.card_sha256`（defaultなし）を追加します。
新規completionは、組み立てに使ったCardの生バイト列のSHA-256を保存します。旧turnはNULLの
まま補完せず、receipt再送で現在のCardを代入しません。列追加とversion更新は原子的で、
型・null・defaultの不正は拒否します。復元metadataとheaderは[履歴API](history-api.md)を参照してください。
version 1の旧turnの`stated_at`はNULLのまま補完せず、確認列追加時は既存行に`{}`を設定します。
履歴・receipt・fingerprint・source epoch・確認保留・往復削除の印と通知・正本・`memory_events` を
保持します。5→6は保持対象表の全行・全列を変更しません。未知versionは拒否します。
移行途中の失敗は追加列・表・DROP・versionをまとめてrollbackし、再実行できます。
この確認を、DBの全設定・権限・任意の改変の監査とは扱いません。
初期化途中の失敗ではtransactionをrollbackし、正常な空状態から再実行できる境界を持ちます。
初期化先に既存データを移送しません。

各storage操作は接続を作り、transactionでcommit/rollback後にcloseします。read/writeとも
Binding単位で直列化し、分類器やembeddingのawait中はtransactionを持ち越しません。
競合・再試行・source撤回は既存のrevision/epoch/receipt契約を使います。接続・timeout・SQL障害を
内容なしのstorageエラーとして返し、別backendへ自動切替しません。

## private・除外・削除・公開範囲

[会話操作](history-api.md)と[記憶操作](memory.md)の意味は保存先に依存させません。
指定発話は履歴へ残しながら記憶対象から除き、private thread由来のsourceを使いません。
private化と履歴削除は、派生memory本文のNULL化、epoch/通知の更新を同じtransactionで確定します。
再生成が未実装の期間も旧本文を検索できず、private解除や遅い登録で旧記憶を復活させません。
archiveは一覧から隠すだけで、記憶sourceの条件を変えません。

Bindingのsubject/client/audience/characterを全操作で照合し、正本のID・版・引用のrevision/epochを
保持します。出典撤回は正本と依存結果を即時に停止し、本文を消去します。
撤回イベントと影響記録は保持しますが、消費・再生成のAPIは提供しません。
形成が実装されるまで残る適格sourceから再生成しない期間を許容します。
この分離はapplicationのport契約であり、DB管理者に対する秘匿やRLS・公開multi-tenant認証の実装ではありません。

削除は取得停止と派生本文の論理的消去です。PostgreSQLの旧tuple、WAL、backup、snapshot、物理媒体の
消去保証は追加しません。今回は実データがなく、backup/restore手順や移行dry-runも作成しません。

## 合成データによる独立検証

依存は`psycopg[binary]==3.3.6`をlockし、PostgreSQL 18公式imageの使い捨てserverを検証対象にします。
通常の`ut`・`it1`は外部通信を禁止したまま、実DB試験は独立した`postgres` markerで実行します。
LLM、API key、GPU、pgvectorは不要です。

再現するには、Docker と固定 uv を用意して `bash tools/test-postgres.sh` を実行します。
ネットワークなし・公開ポートなしの一時 DB と専用 socket を作り、終了時に削除します。

Unix socketを持つ使い捨てserver・専用DBを個別に用意したテスト環境では、次の4変数を明示します。
以下の値は合成例であり、既存の運用DBへ向けて実行しません。

```sh
DSC_TEST_POSTGRES_SOCKET=/tmp/dsc-postgres-test \
DSC_TEST_POSTGRES_PORT=5432 \
DSC_TEST_POSTGRES_DATABASE=core_synthetic \
DSC_TEST_POSTGRES_USER=core_synthetic \
uv run --no-sync pytest -m postgres -q
```

設定が欠落した場合はSKIPせず失敗します。各ケースは一意な`dsc_test_<ID>` schemaを作り、終了時に
そのケースで生成したschemaだけを削除します。CIではネットワーク・公開portなしの使い捨てserverへ
Unix socketで接続し、通常の品質ゲートと区別して実DB結果を確認します。

PostgreSQLの管理運用、dogfoodへの切替、既存データの移送、私的実会話import、実モデル評価は対象外です。
backend試験の成功をこれらの完了として扱わず、正確なrevision・環境・結果は日付付き証跡に残します。


## 正本記憶のschema（version 7）

`MemoryRecordStore` の PostgreSQL 実装は `PostgresMemoryRecords(database)` で明示構築します。
履歴と同じ `PostgresDatabase` を渡します。`open_storage` はこのadapterを `stores.records` として
返し、`MemoryRetrieval` が検索に使います。正本表は版5で導入済みで、版6・7でも維持します。domain契約と操作は[記憶の正本](memory.md#記憶の正本の保存port)を参照してください。

各新表の `seq` は BIGINT identity / UNIQUE、`binding` は NOT NULL TEXT です。
記録表の共通列は `id`（TEXT）、`version`（INTEGER、1以上）、`state`（active / suspended）、
`created_at`（TIMESTAMPTZ、NOT NULL）。本文表の共通列は `normalized_text`（nullable TEXT）、
`last_user_mentioned_at`（nullable TIMESTAMPTZ）です。本文消去後もID・版・状態・登録日時を保持します。
日時の範囲列は `time_start` / `time_end`（nullable TIMESTAMPTZ）と
`time_precision`（nullable TEXT、year / month / day / hour / minute / second）です。

| 表 | 共通列以外の列と型 | 主キー・参照 |
| --- | --- | --- |
| `memory_episodes` | 本文共通列、`five_w` / `experience_time`（nullable JSONB）、`experienced_at`（nullable TIMESTAMPTZ）、`context`（actual / hypothetical / fiction）、日時範囲列 | binding + id。binding + id + versionもUNIQUE |
| `memory_facts` | Fact head。記録共通列だけを持つ | binding + id。versionは現在の内容版 |
| `memory_fact_versions` | 本文共通列、`five_w` / `target_time`（nullable JSONB）、日時範囲列 | binding + id + version。Fact headへのbinding付きFK |
| `memory_episode_fact_links` | `episode` / `fact`（TEXT）、`episode_version` / `fact_version`（INTEGER） | binding + id。両正本のID・版へbinding付きFK |
| `memory_semantics` | 本文共通列、`formation_type`（direct_extraction / experience_derived）、`proposition` / `applicability`（nullable JSONB）、日時範囲列 | binding + id。binding + id + versionもUNIQUE |
| `memory_semantic_episodes` | `semantic` / `episode`（TEXT）、`semantic_version` / `episode_version`（INTEGER） | binding + 両ID・版。両正本へbinding付きFK |
| `memory_record_citations` | `record_kind` / `record_id`、`version`、nullableな`episode` / `fact` / `semantic`、`conversation`、`revision`、`position`、`epoch`、`speaker`、`citation_role`、`start_offset` / `end_offset` | binding + 所属記録の種別・ID・版 + 出典参照・epoch・話者・用途・文字範囲。所属正本へbinding付きFK |
| `memory_event_records` | `event`、`record_kind` / `record_id`、`version`、nullableな`episode` / `fact` / `semantic` / `link` | binding + event + 種別・ID・版。撤回イベントと正本へbinding付きFK |
| `memory_record_registrations` | `id`（引用集合と形成versionから作る冪等キー）、`request_digest`（正規化した登録内容の比較用nullable TEXT）、`results`（本文を含まない参照配列、NOT NULL JSONB） | binding + id |

`memory_record_registrations.request_digest` は本文由来の比較値なので、撤回時に、直接・依存の
影響記録のいずれかを `results` に含む登録行でNULLにします。Factは停止した全版を照合します。
消去は履歴・正本本文・撤回イベントと同じtransactionで、途中の失敗時はすべてrollbackします。
登録行・冪等キー・本文なしの結果参照は保持し、NULLのダイジェストを持つキーの再試行は
内容にかかわらず拒否します。新規扱いや復活はせず、無関係な登録のダイジェストは保持します。

記録の版、引用のrevision・epoch・文字範囲、enum値、日時範囲、引用と影響記録の所属種別にCHECKを置きます。
引用表の `citation_role` は record / reason（5Wの明示理由）で、両者とも出典検証・撤回の対象です。
所属正本のFKは、対応する nullable ID 列のうち一つだけが `record_id` と一致するCHECKと組み合わせます。
履歴を削除しても引用の出典アドレスを保持するため、引用から履歴へのCASCADE FKは置きません。
`memory_events` に binding + id のUNIQUEを追加し、影響記録の別Bindingへのevent参照も拒否します。
型付き日時範囲とJSONBの対応はadapterで算出し、契約試験で照合します。期間検索の索引は追加しません。

歴史的な4→5は正本表を追加して既存行を保持する段階です。版6以降では、続く5→6で旧逐語3表を
削除し、履歴・確認状態・往復削除の印・通知と正本表を保持します。版6・7に旧3表が残る場合は拒否します。
新版の新規作成と、各旧版の厳密検証・段階移行・新版の厳密検証は一つのtransactionです。
新表も `TABLE_COLUMNS`・型/null/default/identity・PK/UNIQUE/FK/CHECK・relation集合で検証し、
不正schemaを自動修復しません。途中失敗はDDLとversionをrollbackし、再試行できます。
