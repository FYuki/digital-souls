# ADR 0010: 明示注入したプロセス内embeddingによる記憶検索

Status: Proposed

置換注記（2026-10-10）: `RetrievalPolicy` の既定 relevance 閾値0.54は、[ADR 0024](0024-multilingual-memory-embedding.md)（2026-10-10 のユーザー決定）により0.52へ変更しました。embedding モデルは bge-m3 です。最大5件・候補20件・同等margin 0.002 は維持します。

置換注記（2026-10-08）: 部分文字列検索 fallback、逐語 Memory からの候補取得と順位の対象は、[ADR 0022](0022-memory-retrieval-from-records.md) の Episode・Semantic（有効な Fact を添付）で置換します。

一部を[ADR 0015 §3「Coreの既存決定との優先関係」](0015-memory-model-reorganization.md#3-coreの既存決定との優先関係)の記憶モデル再編で置き換えます（検索の順位と障害時の扱い。ADR 0018を参照）。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（SQLiteから候補を取得する記述とSQLite rowid。検索の認可・出典・撤回の境界は維持）。

日付: 2026-10-05

## 背景

[ADR 0007](0007-memory-provenance-and-revocation.md)の初期検索は、SQLite内の部分文字列一致と
出典・公開範囲・privacyの再検証を備えます。質問と記憶が異なる語句を使う場合にも検索できるよう、
既存の保存・撤回境界を維持したまま、embeddingによる順位付けの接続口を追加します。
実履歴の自動取り込み、稼働サービスへの接続、実モデル評価はこの決定に含みません。

## 選択肢と決定

永続vector indexや別DBを導入すると、private化・履歴削除時に派生データを消すtransactionと
復旧手順が増えます。まず既存SQLiteから適格な候補を取得し、検索呼出し中だけvectorを生成する
構成とします。DB schema、記憶の抽出・採用、削除outbox、再構築jobは変更しません。

`memory_ranking.py`にstorage非依存の`MemoryEmbedding` Protocolと、変更不可の
`EmbeddingSpace(model, revision, dimensions)`を置きます。dimensionsは1〜4096です。
trusted起動コードが`MemoryService(..., embedding=trusted_embedder)`で明示注入した場合だけ
意味検索を使います。未注入時はcasefold後の部分文字列一致と最新作成順を維持します。

portはプロセス内で処理する実装をtrusted起動側が接続する契約です。新規の外部通信adapter、
モデル取得、API key、GPU利用を追加しません。Python Protocolは通信を制限するsandboxではなく、
注入された任意の実装が契約を守ることをCoreだけで保証しません。model/revisionもtrusted実装が
宣言する処理設定の識別であり、実モデルの真正性や品質を証明しません。

## 検索の順序と上限

queryは1〜256文字、limitは1〜16件とし、1 Bindingのactive memoryは既存どおり1000件までです。
意味検索では適格候補の本文をUTF-8にした合計も256 KiBまでとし、超過は切り捨てず拒否します。
件数と本文量を制限して、候補全体の再認可と一時的なvector計算の範囲を有限にします。

1. 現在のpolicyでqueryの記憶利用を認可し、呼出しscopeと設定の変更を照合します。
2. 同じBindingのactive memoryから、全sourceが現在も適格でepochが一致する候補を取得します。
3. 候補本文全体を現在のpolicyで認可し、全候補のsourceと設定を再照合します。
4. 候補がある場合、queryと全候補本文を1 batchでembeddingへ渡します。awaitは最大15秒です。
5. 全候補のsourceと設定を再照合し、下記のPoC互換の順位付けを行います。
6. 最大取得件数（limit指定時はその小さいほう）以内の結果を現在のpolicyで認可し、返却前にもsourceと設定を照合します。

### PoC互換の順位付け（2026-10-06改訂）

当初は正のcosine降順・最新作成順としていましたが、PoCの検索順位から移植漏れがあったため改訂します。
参照元は公開`FYuki/digital-souls`の固定commitの
[ranking.py](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/memory/ranking.py)と
[memory_policy.json](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/memory/memory_policy.json#L42-L48)です。

- `RetrievalPolicy`の既定値はPoCと同じ最大取得5件、候補20件、relevance閾値0.54、同等margin 0.002です。
- 単位vector間の二乗L2距離（PoCのChroma既定空間）が近い順に候補20件へ絞ります。
- relevance `1 / (1 + sqrt(距離))` が閾値未満の候補は返しません。
- 先頭とのrelevance差がmargin以内を同等帯とし、帯の中はユーザーの最終言及が新しい順、作成が新しい順、IDの順です。
- 最終言及はPoCの`last_user_mentioned_at`（出典user発話の最新時刻）に相当します。Coreの履歴は時刻を保存しないため、
  出典turnの保存順（SQLite rowid、PostgreSQL seq）の最大値で同じ順序を表し、schemaは変更しません。
- PoCの再言及時のTOUCH更新、期間検索、語彙補完、自己申告の現在値補完はCoreに未実装で、この改訂に含みません。

後続の実装ではturnに`stated_at`を保存するようになりました。上記の「時刻を保存しない」「schemaは変更しません」は
この改訂時点の記述です。移行前のturnは`stated_at`がNULLのままで日時による順序を付けられないため、
現在も同等帯の並びには出典turnの保存順を使います。最終言及日時`last_user_mentioned_at`の正式な保存と
TOUCHは[ADR 0018 §5](0018-memory-retrieval-context.md#5-最終言及日時とtouch)に従い後続で実装します。

候補がない場合はembeddingを呼びません。vectorの件数・次元・有限性・非ゼロ長を検証し、不正値を
許容した順位付けや別方式へのfallbackはしません。embedding失敗・timeoutは内容や例外文字列を
含まないエラーとし、cancelは伝播します。実モデル品質は偽embeddingを使う試験では証明しません。

## プライバシー・由来・互換性

候補取得には既存のprivate thread/turn、指定発話除外、user発話限定、source epochの照合を使います。
subject/client/audience/characterが異なるBindingの記憶は候補に入りません。archiveは検索対象を
変えません。private化・履歴削除は従来どおり同一transactionで記憶本文を消し、private解除で
旧記憶を復活させません。検索中にsourceが撤回された場合も古い候補を返しません。

vectorは呼出し中の一時値で、DB・index・共有cacheへ保存しません。返却するMemoryのID・本文・
型・source refs/epochsと保存済み抽出provenanceは変更しません。モデル向けcontextの一時参照名は
[ADR 0009](0009-memory-context-references.md)を維持します。

embeddingの差し替え世代とspaceは検索専用guardで保持し、保存済み抽出jobの`_versions()`へ
追加しません。これにより検索設定の変更で旧抽出承認を失効させず、進行中の検索・contextは
設定変更後に使えなくします。このguardを空contextにも保持し、後続の分類器やContextSourceの
awaitを経た推論dispatch直前にも検証します。既に開始した処理や通信の回収は保証しません。

queryの事前認可拒否だけを扱う`MemoryQueryUnavailable`の契約は維持します。候補認可の拒否、
source撤回、embedding設定変更・不正出力・失敗を、会話の記憶なし継続へ変換しません。

この会話継続の契約は[ADR 0018 §2](0018-memory-retrieval-context.md#2-検索前のquery判定と障害時の扱い)で
置き換えられました。現在は`MemoryContext.context`が検索時の`CoreError`（`MemoryQueryUnavailable`を含む）を
空contextへ変換し、記憶なしで会話を継続します。会話の認可とpolicyの同一性・stampは引き続き検証し、
cancelは伝播します。`MemoryService.search`を直接呼ぶ場合は空結果へ変換せず、従来どおりエラーを返します。

## 検証と残る範囲

合成履歴と偽embeddingで、語句が一致しない候補の順位付け、同点、境界値、private・除外・削除、
Binding分離、由来の保持、各await中の撤回・設定変更、dispatch guardを検証します。
実embedding adapter、モデル/GPUの操作、検索精度・速度の実測、永続vector index、自動履歴importは
後続です。通常起動とdogfoodの履歴保存設定を変更せず、私的実会話を意味記憶へ取り込みません。

関連Issue: [#39](https://github.com/FYuki/digital-souls-core/issues/39)。
利用方法は[記憶API](../memory.md)を参照してください。
