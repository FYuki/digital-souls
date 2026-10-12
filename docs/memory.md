# 記憶の正本の登録と参照検索

Episode・Fact・Semanticの正本と、それを利用する検索・会話contextのPython APIです。
起動時の既定は未接続・拒否です。私的会話を自動取り込みせず、履歴保存だけで記憶形成を始めません。
[ADR 0016](adr/0016-memory-kinds-and-records.md)と
[ADR 0022](adr/0022-memory-retrieval-from-records.md)を参照してください。
旧逐語記憶の抽出・書込・再構成は撤去済みで、schema版6で旧3表を削除します。
構造化記憶の形成（抽出・保存判定・保存文生成）と形成jobは未実装です。
現行の分類・形成・検索・回答の実モデル品質は未受入です。
合成正本の本番検索・contextによる実モデル評価は上位5件包含の基準で検索・回答各3回再実施済み、いずれもFAILです。
[証跡](evidence/2026-10-09-semantic-real-model-evaluation-top5.md)と
[評価手順](memory-evaluation.md)を参照してください。分類器品質と形成〜利用の実環境受入は未実施です。

## 接続と操作

trusted Python起動コードで、履歴と同じPostgreSQLの `MemoryRecordStore`、同じ `PrivacyPolicy`、
検証済みlocal Profileを持つ `LocalClassifier`、任意の明示embeddingを構成します。
[PostgreSQL backend](postgresql.md)と[意味検索との統合例](semantic-postgresql-integration.md)を
参照してください。`open_storage` は同じDBの `history` と `records` を返します。
HTTPによる記憶の登録・検索APIやscope自己申告fieldはありません。
`Binding` のsubject・client・audience・characterを完全一致で照合し、利用者間の共有は提供しません。

```python
from digital_souls_core.memory import MemoryContext
from digital_souls_core.memory_record_store import RecordBatch
from digital_souls_core.memory_retrieval import MemoryRetrieval
from digital_souls_core.storage import StorageConfig, open_storage

# storage_config: StorageConfig（backend="postgresql"、postgresは必須）
# policyはLocalClassifierを持ち、inferenceと同じインスタンスを使う。
# trusted_embedderは起動側が固定したMemoryEmbedding、未接続ならNone。
stores = open_storage(storage_config)
retrieval = MemoryRetrieval(stores.records, policy, embedding=trusted_embedder)
inference.memory_context = MemoryContext(retrieval)
# create_app(inference, history_store=stores.history, history_policy=policy)

# 以下はtrusted callerが保存判定・privacy・保存文生成を済ませた場合の操作。
# episodeは適格な保存履歴へのCitationを持つ検証済みEpisode。
# refs = stores.records.register(binding, RecordBatch(episodes=(episode,)), "formation-v1")
# results = await retrieval.search(binding, "合成の検索語")
```

登録port自体はprivacy分類や保存価値の判断を行いません。呼出し側がこれらを済ませた記録だけを渡し、
portは構造・現在版・Binding・全引用の適格性を検証します。単なる履歴保存の許可で登録を承認しません。
会話経路に明示接続した場合だけguard付きのcontextを取得します。既存stateless APIは検索しません。

## 検索の候補・認可・順位

`MemoryRetrieval.search(binding, query, limit=None, *, authorized=...)` は
`tuple[RetrievalCandidate, ...]` を返します。候補はactiveなEpisodeとSemanticの現在版です。
Episodeには、有効なEpisode–Fact参照と対応するFactの現在版を添付します。
Factは独立のembedding候補にせず、embeddingする候補本文はEpisode・Semanticの `normalized_text` だけです。
Semanticの根拠Episodeは出典検証に使い、追加のembedding候補にはしません。

`MemoryRecordStore.retrievable` は引用（5Wの理由の引用を含む）、出典履歴、依存関係をbatchで照合します。
Binding・記録と参照の版・ACTIVE状態・会話とturnの存在・private・epoch・指定発話除外・
保存拒否の保留と受入・話者・引用範囲を確認します。参照のFact版が現在版と異なる場合は添付しません。
archiveは出典の適格性を変えません。永続index・共有cacheはなく、vectorは呼出し中だけの一時値です。

queryは1〜256文字、limitは1〜16です。limitの省略時は最大5件、明示limitは設定の最大件数を減らす指定です。
queryのmemory認可をstorage読み取り前に行い、機微・ABSTAIN・判定不能では検索しません。
embedding未接続時はstorageを読まず空結果を返します。部分文字列検索は撤去済みです。
候補数1000件、候補と添付Factの保存文のUTF-8合計256 KiBを上限とし、超過は413 `memory_limit` です。
切り捨てず、候補本文全体を現在のprivacyで認可してからqueryと候補保存文を1 batchでembeddingします。
候補が0件ならembeddingを呼びません。embeddingのawaitは15秒以内で、検索全体の上限時間ではありません。

`memory_ranking.rank_records` は製品の `RetrievalPolicy` で順位付けします。
単位vector間の二乗L2距離が近い候補20件から、relevance `1 / (1 + sqrt(距離))` が0.52以上を残し、
先頭との差が0.002以内の同等帯だけで以下の順を適用し、最大5件を返します。

```text
last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC
```

これらは正本に永続した日時とIDです。出典turnの保存順による代用はありません。
登録時に渡された最終言及日時を保持し、再言及時のTOUCHは未実装です。
検索・prompt注入・assistantの言及で日時を更新しません。
結果の保存文もprivacy認可し、各await後に出典・記録・Fact・参照・依存関係と設定を再検証します。
policyの同一性・stamp・分類器設定、embedding世代・space、順位設定、storeの同一性を照合します。
不正vector・timeout・拒否・変更は内容なしのエラー、cancelは伝播します。

## モデル向けcontextと送信境界

`MemoryContext(retrieval).context(...)` は `GuardedContext` を返します。JSON配列の各要素には
一時参照名 `memory_ref`、`kind`（episode / semantic）、`text`（保存文）、
Episodeの `experience_time` またはSemanticの `applicability`、添付 `facts` と `sources` を含めます。
Factには保存文と `target_time`、sourcesには `conversation_ref`・turn_revision・message_index・epochを持たせます。
日時は既知の成分・precision・timezoneを保持し、不明はnullまたは省略します。
stated_atやcreated_atから未知の日時を補いません。

`memory-1`・`conversation-1` のような参照名はそのcontext内だけで有効です。
保存ID・UUID・record_idや、引用範囲から切り出した逐語原文はモデルへ渡しません。
実際の記録・出典は内部guardで保持します。記憶は独立したuser roleの `retrieved_memory_data` として渡し、
systemには命令として実行せず人格・system指示を上書きしない固定の扱いだけを置きます。
[ADR 0009](adr/0009-memory-context-references.md)の一時参照名と
[ADR 0022](adr/0022-memory-retrieval-from-records.md)の保存文contextを適用します。
実モデルでのprompt injection完全防御を保証するものではありません。

conversationの最新user文の先頭256文字で検索し、既存context byte budgetを適用します。
過大な送信contextは切り捨てずbudget errorにします。取得後の通常ContextSourceや分類器のawaitを経ても、
推論dispatch直前に `GuardedContext.valid()` が正本・全出典と設定を再検証します。
取得後の失効を空contextへ差し替えて送信することはありません。開始済みの通信は回収できません。

## 会話時の記憶なし継続

memory許可がなければstoreを参照せず通常の推論認可へ進みます。
query拒否・結果拒否・検索中の出典撤回・設定不正・storageのCoreError・embedding失敗/timeout・
制限超過など、検索準備・検索の `CoreError` は記憶を破棄して空contextへ変換します。
障害本文やquery・記憶本文・例外文字列をログや応答へ転記しません。
キャンセルとCoreError以外の予期しない例外は伝播します。
直接の `search` はquery拒否の `MemoryQueryUnavailable`（403 `memory_query_unavailable`）を含む
エラーを返します。会話認可・policyの同一性とstampのguardは障害時にもdispatch直前まで保持します。
正常に検索して0件だったcontextは検索設定のguardも保持します。

## ローカルembeddingの明示接続

`local_embedding.LocalEmbeddingProfile` と `LocalEmbedding` を使います。
[profile例](../examples/embedding.example.json)は `enabled=false` の bge-m3 Q8_0 設定です。
[ADR 0024](adr/0024-multilingual-memory-embedding.md) に従い alias `bge-m3`、CLS pooling、1024次元、接頭辞なしを採用します。
モデルの SHA-256 は [起動スクリプト](../tools/start-llamacpp.sh) で検証します。
モデルは同梱せず、起動時にprofileを自動で読み込みません。

```python
from pathlib import Path
from digital_souls_core.local_embedding import LocalEmbedding, LocalEmbeddingProfile
from digital_souls_core.memory_retrieval import MemoryRetrieval

# stores・policyはtrusted起動側が構成済み。
profile = LocalEmbeddingProfile.model_validate_json(
    Path("examples/embedding.example.json").read_text(encoding="utf-8")
)
embedding = LocalEmbedding(profile) if profile.enabled else None
retrieval = MemoryRetrieval(stores.records, policy, embedding=embedding)
```

`api_base` は数値port付き `http://127.0.0.1:<port>/v1` のみです。
model alias・model_digest・dimensions（1〜4096）は運用者が固定し、応答modelも一致を要求します。
model_digestは宣言であり実ファイルの検証ではありません。dimensionsはrequestへ送りません。
timeout_secondsは0より大きく15秒以下、enabledは既定falseです。
無効adapterを注入して候補がある場合は送信を拒否します。

固定SDK `openai==2.54.0` の `embeddings.with_raw_response.create` を使い、型変換前のJSONで
件数・model・index・次元・非有限値・ゼロvectorを検証します。proxy・環境の認証情報・organization・
projectを継承せず、redirect・retry・外部fallbackを無効にします。非空の `OPENAI_CUSTOM_HEADERS` は拒否します。
呼出しごとにclientを閉じ、cancel時もcloseを保護します。本文の認可は `MemoryRetrieval` の責務で、
adapter単体の呼出しを認可済み検索とは扱いません。
正規化endpoint・profile ID・timeout・enabled・adapter/SDK識別を `EmbeddingSpace.configuration` に含め、
await後とdispatch前のguardに使います。モデル対応・pooling・資源と実品質は別途確認します。
[意味検索評価](memory-evaluation.md)の合成試験を実モデル品質の合格にしません。

## source失効・削除とschema

[会話全体・往復の削除とprivate化](history-api.md)は、同じtransactionで正本本文を消去し、
撤回イベントと影響記録の本文なしの対応を残します。private化はconversationのmemory epochを進め、
往復削除は対象revisionだけを撤回します。無関係な後続往復は正本登録の出典にできます。
停止したIDの再登録・版追加・冪等再試行や、private解除での旧内容の復活は拒否します。

schema版6は旧版1〜5から段階的に原子的移行し、`memory_sources`・`memories`・`memory_jobs` を削除します。
履歴・receipt・確認状態・往復削除の印と通知・正本・`memory_events` は保持します。
旧turnのstated_atはNULLのままです。詳細は[PostgreSQL](postgresql.md)を参照してください。
撤回イベントのoutboxは保持しますが、旧消費APIは撤去済みです。再生成のconsumerは未実装です。
形成ができるまで再生成しない期間を許容し、旧逐語記憶で補いません。

## 現在の範囲外

構造化抽出・保存判定・保存文生成・形成job・TOUCH・期間検索・補完・consolidation・Reflection・
永続vector index・共有cache・利用者間共有・私的input import・実DB適用は未実装または未実施です。
モデル取得・GPU操作・実環境のIT2/STや実モデル品質受入、tombstone自動掃除・WAL/backup/媒体の
物理消去保証を合成試験の成功と同一視しません。

## 記憶の正本のdomain契約

[記憶の種別・正本・日時](adr/0016-memory-kinds-and-records.md)に対応する保存非依存の値型を
`digital_souls_core.memory_records` に定義します。旧逐語記憶の型・抽出・書込経路は撤去済みです。
`Episode` は経験の5W・日時・仮定/創作の文脈を持ち、`Fact` は呼出し側が採番した安定IDの内容一版です。
`Episode` / `Fact` の各版は保存文と引用を独立して保持します。
`EpisodeFactLink` は両者のID・版付き参照を持ち、保存文や最終言及日時を持ちません。
`Semantic` は構造化命題と適用時期を持ち、直接抽出はuser引用を必須とします。
経験からの形成は同一Episode IDの出典をまとめ、互いに素な `SourceReference` 集合を持つ
Episodeが2件以上なければ拒否します。元発言のepochや引用範囲の違いは独立性を増やしません。

`Citation` はBinding・既存の `SourceVersion`・話者・message内の半開文字範囲 `[start, end)` を
結び付け、元の本文をコピーしません。5Wは述語を必須とし、不明な項目を補いません。
`ExplicitReason` は明示された理由と引用を持ちます。`PartialDateTime` / `TemporalValue` は
部分日時の精度、単一の点、同じ精度の端点の範囲、不明、解釈時のタイムゾーンを保持します。
月精度で日を埋めず、未知の日時は `TemporalValue()` として明示します。

全記録にID・1以上の整数版・Binding・tz-awareな登録日時・ACTIVE/SUSPENDEDの状態を持たせます。
`Episode` / `Fact` / `Semantic` はさらに非空の保存文とtz-awareな最終言及日時（不明可）を持ちます。
別Bindingの引用・参照や不正な値は生成時に拒否し、本文・構造化内容をreprに出しません。
`RecordRef` と純粋関数
`dependency_invalidated` は、現在の種別・ID・Binding・版・有効状態が一致する場合だけ依存先を
有効と判定します。参照先が存在しない場合や版が一致しない場合も失効です。本文消去後も `RecordHead`（参照と状態だけの値）で判定できます。

Episode・Semanticの新規IDは版1で登録し、同一IDへの内容版追加はFactだけが提供します。
これらは構造の契約です。保存portでの現在版・適格性・引用・Episode出典集合の照合、
旧版保持と撤回時の本文消去は次節のadapterで扱います。抽出・保存文の生成は後続の実装で扱います。


## 記憶の正本の保存port

storage非依存の `memory_record_store.MemoryRecordStore` は、前節の値型だけで入出力します。
PostgreSQL実装は `postgres_memory_records.PostgresMemoryRecords`、物理設計は
[PostgreSQL schema version 6](postgresql.md#正本記憶のschemaversion-6)を参照してください。
このportはtrusted callerが形成・保存判定を済ませた記録を保存します。推論・privacy分類・保存文生成や
HTTP API、Fact照合・統合、Reflection、残る根拠からの再構成は実装しません。
検索の候補取得・再検証はこのportを使い、順位付けと送信認可は `MemoryRetrieval` が行います。

| API | 契約 |
| --- | --- |
| `register(binding, batch, formation_version)` | `RecordBatch` のEpisode・`FactWrite`・参照・Semanticを同じtransactionで登録し、`tuple[RecordRef, ...]` を返す |
| `get(binding, kind, record_id, *, version=None)` | 有効記録を前節の型へ復元。Factは現在版、指定時は有効な旧内容版も取得可能。欠落・別Binding・停止済みはNone |
| `list(binding, kind)` | Binding内の有効な現在版のtuple（登録順） |
| `retrievable(binding)` | 有効なEpisode・Semanticの現在版と有効なFact・参照・根拠Episodeを含む `tuple[RetrievalCandidate, ...]` |
| `current(binding, values)` | 取得済み候補の記録・Fact・参照・根拠・全引用が現在も有効か再検証 |
| `head(binding, kind, record_id)` | 停止済みを含む本文なしの `RecordHead`。欠落・別BindingはNone |
| `affected(binding, event_id)` | 撤回イベントの影響記録（各版）の本文なしの `tuple[RecordRef, ...]`。再評価対象の対応を保持し、消費APIは提供しない |

新しいIDは版1・ACTIVEだけを受け付けます。`FactWrite(fact, expected_version=None)` は新規登録、
既存Factには期待する現在の内容版を必須とし、その次の整数版だけを追加できます。
更新は安定したfact_idを保持し、旧版の保存文・5W・対象日時・引用を独立して保持します。
同じBindingのtransaction lockにより履歴操作と登録、並行再試行とFact更新を直列化します。
別Bindingの入力・参照・根拠Episodeや、欠落・不正な版・停止済みの参照先は拒否します。
EpisodeEvidenceの出典集合は参照先Episodeの引用のSourceReference集合と一致する必要があります。

登録直前に、全引用（5Wの明示理由と根拠Episodeの引用も含む）の出典を履歴へ照合します。
turn revision・epoch、履歴本文の存在、private・除外・確認保留状態、話者と半開文字範囲を検証します。
保存拒否の確認を断った発話は既存の確認契約に従い対象へ戻せます。
assistant等の引用も履歴側で適格な場合だけ保存できます。直接抽出Semanticのuser引用必須条件は維持します。
引用は元本文を複写せず、話者・文字範囲を参照します。登録に失敗すると途中の記録・引用・冪等対応も残りません。

冪等キーはBinding、登録の全引用（出典参照・epoch・文字範囲・話者）の正規化集合、呼出し側の
形成version文字列、リンクがある場合は各リンクの両端の組（episodeとfactのRecordRef、本文なしのアドレス）
の正規化集合から作ります。組の集合は並び順や重複に左右されず、組の中のepisodeとfactの区別を保ちます。
根拠として参照する既存Episodeの引用も含み、生本文をキー材料にしません。
同じキーかつ正規化した登録内容が同一なら元の参照を返し、異なれば409の衝突です。
引用・根拠・登録の集合の並び順や引用の重複は一致判定を変えません。
本文を保存する再試行台帳を作らず、比較用ダイジェストと結果の参照だけを保持します。
撤回で結果に含む記録が一つでも停止した登録は、本文由来の比較ダイジェストもNULLにします。
登録行・冪等キー・本文なしの結果参照は残し、同じキーの再試行は内容にかかわらず409で拒否します。
NULLのダイジェストを新規登録として扱ったり、旧記録を復活させたりしません。
再試行でも出典と既存結果の有効性を確認し、停止した結果を返したり再有効化したりしません。

Episodeの経験日時、Factの各版の対象日時、Semanticの適用時期はJSONBと型付き範囲を持ちます。
範囲は `[開始, 終了)`。開始は始点の精度の期間の最初、終了は終点（なければ始点）の精度の期間の次です。
精度列は始点の精度で、JSONBの欠損成分は補完しません。始点なしは3列ともNULL、timezoneなしは
開始・終了だけNULLです。timezone名はzoneinfoで解決し、不正な名前や表現不能な範囲は登録を拒否します。

既存の会話削除・private化・往復削除は、同じtransactionで新正本も停止します。
撤回した出典を引用するEpisode・Semantic、Fact全体の全版を停止し、保存文・5W・日時・命題のJSONB・
型付き日時範囲・experienced_atをNULLにします。ID・引用アドレス・参照・登録日時等の本文なしの情報を残します。
依存するEpisodeFactLinkと、撤回されたEpisodeを根拠にするSemanticも停止し、Semantic本文を消去します。
停止記録の各版を既存の撤回イベントへ紐付け、即時に取得を止めます。
直接・依存の影響記録を結果に含む登録の比較ダイジェスト消去も同じtransactionで行い、
撤回が中断した場合は履歴・本文・イベント・ダイジェストをすべてrollbackします。無関係な登録は保持します。
再有効化するAPIはなく、停止IDでの再登録・Fact版追加・冪等再試行も拒否します。
撤回イベントのoutboxは保持しますが、消費・再生成のAPIはありません。
形成が実装されるまで再生成しない期間を許容します。期間検索の索引は未実装です。
