# 記憶の正本の登録と参照検索

Episode・Fact・Semanticの正本と、それを利用する検索・会話contextのPython APIです。
起動時の既定は未接続・拒否です。私的会話を自動取り込みせず、履歴保存だけで記憶形成を始めません。
[ADR 0016](adr/0016-memory-kinds-and-records.md)と
[ADR 0022](adr/0022-memory-retrieval-from-records.md)を参照してください。
旧逐語記憶の抽出・書込・再構成は撤去済みで、schema版6で旧3表を削除します。
型付き保存判定の評価器と分類の段は実装済みです。構造化抽出・保存文生成と、
形成から保存判定への呼出しは未実装です。
形成jobの台帳・予約・有限drainの枠は実装済みで、processorは未接続です。
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

## 型付きの保存判定

[Issue #207](https://github.com/FYuki/digital-souls-core/issues/207)の
`memory_admission.evaluate` は同期の純粋関数です。`Candidate` の `RecordKind` と
抽出側が明示する `memory_worthy`、`Scan`、`AssessmentResult` または未分類の `None`、
trusted 起動側の `AdmissionPolicy`、呼出し側が確認した `SourceState`、分類器の由来を受け取ります。
候補本文・Binding・保存先は値型に持たず、LLM・DB・embeddingを呼びません。
値型は frozen で、型変換をせずに検証し、エラーに入力値を含めません。

| 状態（上ほど優先） | 条件 |
| --- | --- |
| DENY_SENSITIVE | scannerのsecret、またはcategoryを問わずSENSITIVE |
| DENY_USER_REQUEST | 指定発話の除外、private、拒否確認の保留、scannerのno_memory/no_history |
| ABSTAIN_UNKNOWN | scanner失敗、未分類、ABSTAIN、分類器failure、source不明・履歴未保存、価値不明、未知policy版・由来欠落 |
| NOT_MEMORY_WORTHY | policyの許可型外、または明示された長期的価値なし |
| ALLOW_STRUCTURED | 上の拒否条件がなく、許可型・privacy・source・価値が確認済み |

結果は状態、全該当理由のコード（厳しい状態順）、分類器failure種別、由来を返します。
`can_store` は ALLOW_STRUCTURED だけで true です。保存処理は行いません。
許可型は `frozenset[RecordKind]` として起動側が必ず渡し、既定の許可型を持ちません。
空の許可集合はすべての型を対象外にします。会話・人格prompt・候補の保存許可flagは拒否を緩和しません。
価値は評価器が推測しません。sourceの各flagと価値は `None` で不明を表せます。

`memory_admission.admit` は、すでにlocal送信が認可された管理下の分類器を受け取る非同期段です。
本文を既存scannerで検査し、secretまたはfailedなら分類器もprovenanceも参照せずに評価します。
分類器なし・未対応policy版も分類せずに判定不能とします。それ以外はkeywordによる意味的な
事前除外をせず分類し、同期評価器へ渡します。除外・private・型の不許可・価値なしでも、
SENSITIVEの優先順位を維持するため分類結果を取得します。分類器呼出しは15秒以内とし、
timeout・例外・不正な結果はABSTAIN_UNKNOWN、キャンセルは伝播します。
DB・embedding・LLM transportの直接呼出しや保存は行いません。

由来は `scanner_version`、`policy_version`、`classifier_version`、`model_id`、`model_digest`、
`prompt_version` の独立fieldです。分類器の4 fieldは `LocalClassifier.provenance` の固定設定から
取得し、未呼出しなら `None` です。本文・モデル応答・自由文理由・例外文字列は結果に保持しません。
分類器の由来が不正・欠落なら呼び出さずに判定不能とします。endpoint等の既存provenanceを変更しません。

### 絶対禁止と検出の限界

[ADR 0017 §3](adr/0017-memory-formation-admission.md)の絶対禁止は以下の結果で
DENY_SENSITIVEへ写像します。意味分類のUTは固定した合成Assessmentであり、実モデルの検出保証ではありません。

| 絶対保存禁止 | 現行scannerまたは意味分類の拒否経路 | 検出の穴・未確認 |
| --- | --- | --- |
| 認証情報 | 既知prefix・ラベル付きpassword/token等のsecret | 無印opaque値、未知形式、難読化 |
| 秘密鍵 | PEM private key・ラベル付き秘密鍵のsecret | 無印・未知形式 |
| 決済・口座情報 | Luhn整合カード・ラベル付き口座のsecret、SENSITIVE/FINANCIAL | 非Luhn番号・無印口座の網羅なし |
| 政府ID | ssn・マイナンバー等のラベル付き値のsecret | 他国形式・無印IDの網羅なし |
| health・服薬・生体情報 | SENSITIVE/HEALTH、SENSITIVE/OTHER_SENSITIVE等 | scanner専用検出なし。promptはpersonal healthを扱うが服薬・生体の明示列挙はなく、実検出未検証 |
| 住所・正確な位置 | 住所・address・現在地等のラベル付き値のsecret、SENSITIVE/OTHER_SENSITIVE等 | 無印住所・自然文の座標はscannerで未検出。分類器promptで明示列挙なし |
| 私用連絡先 | email・限定した電話形式のsecret、SENSITIVE/OTHER_SENSITIVE等 | 全世界の電話・非email連絡先の網羅なし。分類器promptで明示列挙なし |
| 第三者の非公開情報 | SENSITIVE/THIRD_PARTY_PRIVATE等 | scanner専用検出なし。意味分類の見逃しは評価器で補えない |

### 仮置きと後続接続

sourceの履歴未保存・不明をABSTAIN_UNKNOWN、scannerの拒否語をDENY_USER_REQUESTへ写像する点、
明示価値flagの形は今回の実装指針による仮置きです。保存policy版は現行 `POLICY_VERSION` だけを
対応済みとし、由来欠落でもfail-closedにしています。独自の保存allowlist版を導入するかは要確認です。

[Issue #209](https://github.com/FYuki/digital-souls-core/issues/209)の形成側で、分類前のlocal/memory
permission、schema・根拠・Binding、sourceの現在版とawait後の失効を確認する必要があります。
`SourceState` は評価時のsnapshotで、permission付与・DB照合の代替ではありません。
[ADR 0019 §7](adr/0019-memory-correction-invalidation.md)は現在userの拒否語とassistantの引用を
区別します。この段は話者を解釈せずscannerのflagを受けるため、形成側で拒否語検査に渡す本文の
範囲とsource保留を照合する必要があります。scanner・分類器・PrivacyPolicyは変更していません。
形成経路への接続・保存判定経路の実モデル品質・IT2/STは未検証です。
合成検証の条件と件数は[Issue #207の証跡](evidence/2026-10-13-memory-admission.md)を参照してください。

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

## 形成jobの台帳と有限drain（Issue #208）

[ADR 0017 §4](adr/0017-memory-formation-admission.md#4-会話からの非同期形成)と
[ADR 0027 §3・§5](adr/0027-tool-use-and-activity-runtime.md)に沿い、timerが有限drainを呼びます。
2026-10-13のユーザー選択（Q1=A・Q2=A）はPython APIとlocalhost HTTPの両入口、
trusted起動コードからの明示注入、memory許可付きの同transaction予約です。ADRの追加承認ではありません。
Coreは常駐worker・scheduler・checkpointを持ちません。
`MemoryService.extract` は2026-10-08にADR 0022で撤去済みです。
管理・試験用extract経路は復活させず、形成の実行入口はdrainだけにします。

```python
from digital_souls_core.formation import FormationConfig
from digital_souls_core.postgres_formation import PostgresFormation
from digital_souls_core.storage import open_storage

# policyはinferenceと同じPrivacyPolicy。Bindingごとのmemory許可はtrusted起動側で設定する。
# storage_config、inference、trusted_processorは起動側が構成する。
stores = open_storage(storage_config, formation_policy=policy)
formation = PostgresFormation(stores.history, policy, trusted_processor,
                              config=FormationConfig())
# Pythonによる有限実行（timerは結果だけを受け取る）:
# result = await formation.drain(max_jobs=5)
# HTTPを提供する場合:
# app = create_app(inference, history_store=stores.history,
#                  history_policy=policy, formation=formation)
```

`formation_policy` の省略時は予約しません。`PrivacyPolicy.permits(binding, "memory")` がtrueの
履歴append・controls・確認回答・往復削除だけが、履歴保存・revision更新と同じtransactionで予約します。
空会話の作成や既存履歴の移行では予約せず、既存receiptの再送でも再予約しません。
同じBinding＋conversationの台帳は1行です。pendingを最新revisionへ集約し、leased中の更新は
target revisionだけを進めます。旧処理の成功・失敗後も新版があればpendingへ戻し、試行回数を戻します。
会話経路は予約のSQLだけを待ち、drain・processor・LLM形成を呼びません。
予約障害で履歴を含むtransactionをrollbackする方式は**仮置き・要確認**です。
予約を捨てて履歴だけ保存する案に比べ、形成対象の欠落を黙って残さないため推奨します。
生成済みの応答も保存成功のreceiptとして返らず、形成機能を接続した保存経路の障害として返ります。

`PostgresFormation(history, policy, processor=None, *, config=..., clock=...)` は、
同じpolicyで形成を有効にしたPostgresHistoryだけを受け付けます。未接続processorは
503 `formation_processor_unavailable` で拒否し、pendingを消費しません。#208に製品用no-opはありません。
`FormationProcessor.process(FormationInput)` は後続#209以降の接続portです。
`FormationInput.origin_*` は予約の起点（Binding・会話・revision・epoch・適格なsource）を表します。
照合/読取範囲は同一Binding・同一会話に固定せず、processorが注入された認可portで決めます
（後続#77・#214のキャラクター単位のFact照合・利用者単位の訂正への考慮。#208では認可を拡張しません）。
private・削除・指定除外・保存拒否の保留/受入・それらに依存するassistant/toolは除きます。
周囲の除外本文をcontextとして渡しません。archiveは適格性を変えません。
このsource確認とmemory permissionは保存判定・privacyの内容認可を代行しません。
processorは現在のprivacyを認可し、各await後・送信前と正本登録直前にsource/epochを再検証し、
元発言・版・引用範囲と形成versionに基づく冪等書き込みを行う責務を持ちます。本文hashをjobキーにしません。
processorはtimeout/cancelを尊重し、checkpointを読まず毎回最初から実行します。

`drain(max_jobs=None)` は設定上限以内の件数だけを処理します。同一呼出し中に同じ台帳行を再取得せず、
処理中の更新やbackoffなしの失敗も次のdrainへ残します。`DrainResult` は
`succeeded`・`failed`・`excluded` と `status`（success / failure）だけです。
試行中の失敗が1件でもあればfailure、除外と処理対象0件はsuccessです。未処理全件の完了を意味しません。
台帳更新のDB障害・cancelは伝播し、source取得の障害は失敗として再試行します。
本文・prompt・LLM出力・例外文字列を台帳や結果へ保存しません。

以下は運用値の**仮置き・要確認**で、品質閾値ではありません。高負荷でも残る予約を保持します。

| `FormationConfig` | 既定値 | 許容範囲・意味 |
| --- | --- | --- |
| `max_jobs` | 10 | 1〜100。1回の処理上限。引数/HTTPはこれを減らすだけ |
| `lease_seconds` | 300秒 | 1〜3600秒。中断したleaseを回収できる期限 |
| `processor_timeout_seconds` | 60秒 | 1〜300秒。1 jobのprocessor await上限。DB操作は既存の接続/SQL timeoutを使う |
| `max_attempts` | 3 | 1〜100。同じ対象revisionの実行回数。中断も数える |
| `retry_backoff_seconds` | 60秒 | 0〜3600秒。失敗後の固定待機。0でも同じdrain内では再試行しない |

失敗はpending＋backoffに戻し、上限に達した対象revisionはfailedとして台帳を保持します。
新版の履歴更新で再予約し、旧版の失敗/中断回数を新版に持ち越しません。
failedを単なるtimer再呼出しで無制限に再試行する方式や手動reset APIは実装しません。
この停止方針も**仮置き・要確認**です（上限後もbackoff付きで継続する案との比較が必要）。
lease期限切れは次回drainで最初からやり直します。処理中の専用DB接続が持つsession advisory lockにより、
まだ生きている処理は期限切れだけで並行実行されません。処理のawait中はBinding lockや
DB transactionを保持せず、会話更新を進められます。接続が閉じればsession lockは解除されます。
正本commitと台帳完了の間の中断は再実行となるため、正本の冪等性は別途必要です。
合成fakeが既存の正本登録portへ書き込む試験でこの境界を検証し、実抽出の冪等性は#209で扱います。

会話削除はFK CASCADEで同transaction内に台帳行も消去します。private化と往復削除では予約を
最新revisionへ更新し、drain直前に再検証して不適格sourceだけを除きます。sourceが0件ならdone /
`source_excluded` にします。memory許可の撤回もprocessorを呼ばず除外します。
削除/private化がprocessor await中に起きる場合は、後続processorの登録直前の出典検証が拒否する境界です。
台帳を保持してdrain時に除外する方式は**仮置き・要確認**で、private化時の即時取消案との選択です。

[HTTP境界](history-api.md#形成drainのlocalhost-http入口)と
[schema版8](postgresql.md#形成jobのschemaversion-8)も参照してください。

## 現在の範囲外

構造化抽出・保存判定・保存文生成・TOUCH・期間検索・補完・consolidation・Reflection・
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
[PostgreSQL schema version 8](postgresql.md#正本記憶のschemaversion-8)を参照してください。
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
