# 意味検索・回答評価の固定ケース

合成のみ。実会話・私的ログ・認証情報は含めません。決定は [ADR 0023](../../docs/adr/0023-semantic-evaluation-contract.md)、本番契約は [ADR 0022](../../docs/adr/0022-memory-retrieval-from-records.md)です。

[調整専用の独立した合成ケースとローカル補助データ](tuning/README.md)は、合否判定用の62ケースと分けて使います。

## 分類と件数

| category | 件数 |
| --- | --- |
| synonym | 10 |
| paraphrase | 10 |
| cross_language | 10 |
| unrelated | 10 |
| negation | 1 |
| update | 1 |
| multisource | 1 |
| multisource_revocation | 1 |
| private | 1 |
| excluded | 1 |
| deleted_source | 1 |
| deleted_memory | 1 |
| binding_character | 1 |
| binding_subject | 1 |
| binding_client | 1 |
| long_text | 1 |
| after_search | 1 |
| after_answer | 1 |
| epoch_change | 1 |
| fact_attachment | 1 |
| fact_version | 1 |
| fact_revocation | 1 |
| semantic_direct | 1 |
| semantic_derived | 1 |
| equivalent_order | 1 |
| threshold | 1 |

合計 **62件**。モデル依存4分類は各10件です。

## 旧ケースとの対応

旧 PR #51 head `5a573e2` のケース名を `legacy_id` に保持します。本文は `normalized_text` に移し、会話履歴は合成し直します。旧sourceのrevision/indexは新しいappend可能な会話へ対応付け直しています。

| legacy_id | 新case id | 移行上の差分 |
| --- | --- | --- |
| synonym-warm-drink-ja | synonym-warm-drink-ja | 本番のEpisode/citationへ変換。 |
| paraphrase-weekend-ja | paraphrase-weekend-ja | 本番のEpisode/citationへ変換。 |
| cross-language-herb | cross-language-herb | 本番のEpisode/citationへ変換。 |
| unrelated-observatory | unrelated-observatory | 本番のEpisode/citationへ変換。 |
| negated-coffee-preference | negated-coffee-preference | 本番のEpisode/citationへ変換。 |
| updated-morning-drink | updated-morning-drink | 旧本文はFact版1。版2と新リンクをregisterし、Episodeへ現行Factを添付。 |
| multisource-picnic | multisource-picnic | 本番のEpisode/citationへ変換。 |
| multisource-partial-revocation | multisource-partial-revocation | 会話単位privateでは残存出典も失効するため、撤回側の往復だけ削除。残存Episodeは事前登録済みで再生成ではない。 |
| private-source | private-source | 本番のEpisode/citationへ変換。 |
| excluded-source | excluded-source | 後付け除外APIはないためappend時の除外。対象記録は登録拒否probe。 |
| deleted-source | deleted-source | 本番のEpisode/citationへ変換。 |
| deleted-memory | deleted-memory | 記憶だけの直接削除APIはないため出典の選択往復削除で本文を消去。 |
| binding-character | binding-character | 本番のEpisode/citationへ変換。 |
| binding-subject | binding-subject | 本番のEpisode/citationへ変換。 |
| binding-client | binding-client | 本番のEpisode/citationへ変換。 |
| long-text-explicit-detail | long-text-explicit-detail | 本番のEpisode/citationへ変換。 |
| post-search-revocation | post-search-revocation | 本番のEpisode/citationへ変換。 |
| post-answer-revocation | post-answer-revocation | dispatch前は有効。応答後の再検証で回答を破棄する期待。 |
| source-epoch-changed | source-epoch-changed | epochの直接書換えはしない。private化→解除で世代を進め、旧記録の自動復活を拒否。 |

`unsupported-public-audience`（分類 `unsupported_audience`）は移しません。現行 `AccessScope.audience` は `local-private` 固定であり、`public` を表現できません。型外のBindingを捏造しません。

## ファイルと利用API

- [cases.json](cases.json)：query、検索Binding、合成会話と記録、操作手順、偽vector。goldを含めません。分類もgold側だけに置きます。
- [expectations.json](expectations.json)：分類、検索候補の `relevant_ids` / `forbidden_ids`、該当なし、期待順、添付Fact ID、dispatch直前のguard、回答の必須語句・禁止語句・挙動と破棄の期待。
- [semantic_evaluation_cases.py](../../src/digital_souls_core/semantic_evaluation_cases.py)：`load_evaluation_cases(cases_path, expectations_path)` / `parse_evaluation_cases(cases_json, expectations_json)` が分離した型と相互検証結果を返します。DB・推論は使いません。各記録の `to_domain()`、citationの `citation()`、`registration_batches(case)`、Fact更新mutationの `batch()` を後続で使えます。

初期記録は版1・ACTIVE。新Fact版は `expected_version` の直後の版で登録します。Factは検索候補ではなく添付で採点し、`required_fact_ids` / `forbidden_fact_ids` を使います。同じFact IDの更新内容は回答の必須・禁止語句で区別します。

### 後続ハーネスの実行順

1. ケース単位で隔離し、各Bindingで `HistoryStore.create` を使います。fixtureの会話IDは論理名なので生成IDへ写像し、履歴・citation・EpisodeEvidenceのsourceも一緒に置換します。
2. 会話のuser/assistant対をturn_revision順に `append` します。message_indexはturn内0始まりです。`stated_at` はtrusted clockから保持します。`exclude_on_append` は **この段階** で `memory_excluded_indices` へ渡します。後付け更新はしません。
3. `registration_batches` が返すBindingごとのbatchを `MemoryRecordStore.register` します。除外出典を引用する記録はbatchから外し、別の登録試行が拒否されることも確認します（その記録のdomain変換自体は可能）。
4. `before_search` の残る操作を配列順に実行します。`set_private` は `controls`、`delete_turn` は `delete_turns(scope="selected")`、`delete_conversation` は `delete`。expected_revisionは現snapshotから取得します。`update_fact` は `batch()` とそのBindingで `register`。新リンクは新IDで登録します。SQLによるsource/epoch変更は禁止です。
5. 本番設定の `MemoryRetrieval` で検索し、本番 `MemoryContext` のcontextを生成します。検索返却時の候補IDと添付Factをgoldと比較します。`after_search` はcontext取得後・dispatch前、`after_answer` は回答生成後・公開前に実行し、その時点のguardを再検証します。

`dispatch.valid` はdispatch **直前** の期待です。after_answerではtrueであり、応答後の失効は `answer.discarded` で表します。guard無効時は送信/公開を拒否します。検索が正常0件なら有効な空contextで `no_memory` とし、架空の事実を補いません。`no_match` は検索時点の期待で、必須ゲートは閾値以上の適格候補0件です。

### 偽embeddingと品質評価

queryと全normalized_text（Fact各版を含む）には共通4次元の有限・非ゼロvectorがあります。同一本文は全ケースで同じvectorです。queryと候補をunit vectorにし、二乗L2距離から `relevance = 1/(1+sqrt(distance))` を計算します。本番の候補20・閾値0.52・同等帯0.002・最大5を使います。Factのvectorは本文対応の完全性用で、Factを独立候補にしません。

[ADR 0024](../../docs/adr/0024-multilingual-memory-embedding.md) のユーザー承認済み Q3 例外として、
`below-threshold` の `below-threshold-record` の偽vectorだけを
`[0.63, 0.7765951326141569, 0.0, 0.0]`（relevance約0.53757）から
`[0.5565557545480253, 0.8308102623821387, 0.0, 0.0]`（relevance 0.515）へ変更しました。
閾値0.52の直下で「該当なし」を検証する意図を保つためです。本文・gold・他の61ケース・
この記録以外のvectorは変更していません。この本文は他ケースと共有されていません。
実モデル評価は偽vectorを使わず、bge-m3 Q8_0（CLS、1024次元、接頭辞なし）を使います。

偽vectorはCIで道具を検証するためのものです。**モデルの品質証拠ではありません**。実モデル評価は手動・cacheなし3回、各回各分類90%以上、必須ゲート全件合格で判定します。平均で相殺せず、結果に合わせて期待値・閾値を緩めません。

検索の `top_one` ゲートは上位5件に1位にあるべき記憶が含まれることを求めます。
1位はgoldの `expected_order` があれば先頭、なければ `relevant_ids` の先頭、空なら該当なしです。
`expected_order` 全順序一致はfixture modeだけの必須ゲートとし、62×3の
`equivalent-band-order` で本番の同等帯sortを引き続き検証します。
local_model modeでは全順序一致を求めません。`relevant_ids` 全包含の品質条件、
no_match、privacy・Binding・失効、閾値・検証不能代替禁止のゲートとgold値は変更しません。

### 回答事実の判定契約

入力の `schema_version` は1のまま、goldは破壊的な形式変更を明示するため **2** にします。旧goldのschema 1 / 平坦な語句配列は拒否し、暗黙変換しません。未公開の初期形式であり、既存ハーネスの移行はありません。

`answer.required_facts` は `[["scarf", "scarves", "マフラー"], ["火曜", "Tuesday"]]` のようなグループ配列です。**全グループを満たし、各グループ内は許容表記のいずれか1つを含めばよい（AND of ORs）** とします。`forbidden_facts` も同じ形式で、全グループの全候補のいずれか1つでも含めば違反です。外側の空配列はその側の制約なしを表します。空グループ・空文字・空白だけの候補は拒否します。正規化後の同一候補の重複はグループ内・グループ間の両方で拒否し、required/forbidden間の同一候補も拒否します。

回答と各候補の両方に **Unicode NFKC → Unicode casefold** の順で正規化を適用し、その後に決定論的な部分文字列一致で判定します。大文字小文字と全角英数字・半角カナ等のUnicode表記差を吸収します。空白・句読点は削除せず、単語境界・形態素解析・翻訳・文意解析・LLM judgeは使いません。`normalize_fact_text` / `AnswerExpectation.matches_facts` はこの純粋な語句判定だけを提供し、後続promptfooへ契約を引き継ぎます。挙動・guard・破棄の合否判定や評価ハーネスはIssue #114で実装します。出典の表記は採点しません。goldを入力へ注入しません。

候補は記憶本文・合成会話の核心となる事実と、その自然な訳・表記揺れに限定します。日英ケースは記憶側とquery側の言語のどちらで答えても核心事実が一致すればよく、回答言語を採点しません。部分文字列一致の限界と全件見直しの結果は下表に記録します。

## 第2ラウンドの全件gold監査

第1ラウンド `5c52330` のgoldは未公開・回答評価未実行です。以下はprompt・設定調整と評価結果を見る前の判定基準の修正であり、結果に合わせた緩和ではありません。入力 `cases.json` は変更しません。全62件を確認し、非空のrequired/forbiddenを持つ51件をグループ形式へ移しました（残る11件は両方空のまま）。意味・許容表記を変更した22件は次のとおりです。

| case id | required（グループ間はAND、` / `はOR） | forbidden（いずれかを含めば違反） | 理由 |
| --- | --- | --- | --- |
| cross-language-herb | ミント / mint | なし | 日英の自然な訳語を許容。 |
| negated-coffee-preference | コーヒー / 珈琲 | なし | 飲み物の名前だけを要求。紅茶の否定関係は部分文字列で確実に判定できないため禁止を追加しない。 |
| updated-morning-drink | ほうじ茶 / 焙じ茶 | 緑茶 | 現行茶の表記揺れを許容し、旧内容は語順に依存しない名詞で禁止。 |
| multisource-partial-revocation | サンドイッチ | りんごジュース / リンゴジュース / 林檎ジュース | 撤回済み飲み物のかな/漢字表記も禁止。 |
| long-text-explicit-detail | 橙 / だいだい / オレンジ | なし | 色を問うため自然な色名表記を許容。 |
| synonym-06 | 傘 | なし | 雨具を問うため核心の傘だけを要求し、色と助詞の連結を採点しない。 |
| paraphrase-02 | 庭、散歩 / 歩く / 歩き | なし | 場所と動作を分離し、庭を/庭での助詞差を許容。 |
| paraphrase-03 | 目覚まし / めざまし / アラーム、二つ / 2つ / 二個 / 2個 / 二台 / 2台 | なし | 時計と数を分離し、語順・数字/漢数字・助数詞を許容。二つという固有事実を維持。 |
| paraphrase-04 | しおり / 栞 | なし | 中断位置の記録方法を問うため核心のしおりだけを要求。 |
| paraphrase-06 | カレンダー | なし | 予定の記入先を核心の名詞にし、壁との助詞連結を要求しない。 |
| paraphrase-08 | 直後 / すぐ / 終えたら / 終わったら | なし | 時期を問うため料理終了直後の自然な表記を許容。鍋だけでは時期を採点できない。 |
| cross-language-02 | フルート / flute | なし | 日英の自然な訳語を許容。 |
| cross-language-03 | 海岸 / coast / beach / shore / seaside | なし | 海岸を表す自然な英訳を許容。 |
| cross-language-04 | かぼちゃ / カボチャ / 南瓜 / pumpkin | なし | 日英訳・かな/漢字表記を許容。 |
| cross-language-05 | ユリ / ゆり / 百合 / lily / lilies | なし | 日英訳・単複・かな/漢字表記を許容。 |
| cross-language-06 | 赤 / あか / レッド / red | なし | 色の自然な訳語・表記を許容。赤は赤い/赤色を包含。 |
| cross-language-07 | scarf / scarves / マフラー | なし | 日英訳と不規則な単複を許容。 |
| cross-language-08 | Tuesday / 火曜 | なし | 日英訳を許容。火曜は火曜日も包含。 |
| cross-language-09 | autumn / fall / 秋 | なし | 日英訳と米英表記を許容。 |
| cross-language-10 | mountain / 山 | なし | 日英訳を許容。mountainはmountainsも包含。 |
| direct-semantic | 静か / 静けさ / 静寂 | なし | 好みの核心を静けさとし、静かな/静かで等の語尾差を許容。 |
| derived-semantic | 静か / 静けさ / 静寂 | なし | 好みの核心を静けさとし、静かな/静かで等の語尾差を許容。 |

残る40件は語句の意味を維持（非空の配列だけ単要素グループ化）: `synonym-warm-drink-ja`、`paraphrase-weekend-ja`、`unrelated-observatory`、`multisource-picnic`、`private-source`、`excluded-source`、`deleted-source`、`deleted-memory`、`binding-character`、`binding-subject`、`binding-client`、`post-search-revocation`、`post-answer-revocation`、`source-epoch-changed`、`synonym-02`、`synonym-03`、`synonym-04`、`synonym-05`、`synonym-07`、`synonym-08`、`synonym-09`、`synonym-10`、`paraphrase-05`、`paraphrase-07`、`paraphrase-09`、`paraphrase-10`、`unrelated-02`、`unrelated-03`、`unrelated-04`、`unrelated-05`、`unrelated-06`、`unrelated-07`、`unrelated-08`、`unrelated-09`、`unrelated-10`、`valid-fact-attachment`、`stale-fact-link`、`revoked-fact-source`、`equivalent-band-order`、`below-threshold`。

短い語も全件確認しました。`private-source` / `excluded-source` / `deleted-source` / `deleted-memory` の必須 `青` は「青い」「青色」を包含します。禁止 `紫` は単独の色名による失効情報の漏洩も拒否するため維持します。「紫外線」「紫陽花」でも誤検出する限界がありますが、これらの事実は合成会話・記憶に存在せず正解候補へ加えません。語境界・文意の判定を行わない方式では、単独の「紫」を漏洩として検出しつつ全ての複合語を除くことはできません。`青` / `赤` / `橙` / `秋` / `山` も他語を包含し得ます。色・季節・題材を問うケースの最小事実として採用し、完全な意味判定と同一視しません。

同じ理由で「コーヒーと紅茶が苦手」は必須のコーヒーを含むためこの語句判定だけでは拒否できません。紅茶全体を禁止すると「紅茶は好きで、コーヒーが苦手」という正しい回答を拒否し、特定の否定句だけを禁止しても語順差を網羅できません。監督の指示に従い、このケースはコーヒーだけを必須とし、禁止関係の完全検出を主張しません。否定や時間的関係を含む語句一致は、自然言語の真偽・矛盾を完全には判定できません。

## 回答評価（Issue #114、promptfoo 0.117.2）

Python依存は増やさず、このディレクトリの `package.json` / `package-lock.json` だけで
promptfoo **0.117.2** を固定します。Nodeは `.node-version`、uvは0.8.22です。
内部DB依存は `better-sqlite3` **13.0.3** にoverrideで固定します。11.10.0では固定Node 24.19.0の
Statement GC時に `RemoveEnvironmentCleanupHook` assertionでSIGABRTが再現しました。
13系のN-API bindingはLinux x64用のバイナリを同梱し、この破綻するObjectWrap経路を使いません。
`.npmrc` の `ignore-scripts=true` によりinstall scriptは全て無効です。評価経路に必要なscriptはありません。
対応する同梱bindingがない環境はFAILとし、勝手にbuild/download scriptを許可しません。

```sh
uv sync --locked
(cd evals/semantic && npm ci --no-audit --no-fund)
node --test --test-reporter=./tools/required-tests-reporter.mjs tools/*.test.mjs
bash tools/evaluate-semantic-answer.sh --runs 3 --output /dev/shm/semantic-answer-report.json
```

fixture が既定です。`with-test-postgres.sh` のnetworkなし使い捨てPostgreSQLへ、#113と同じ
`isolated_case` で合成履歴・正本を登録します。本番 `MemoryRetrieval`（本番設定）、
`MemoryContext`、`Inference.prepare` のchat組み立てと `Inference.check` を通します。
分類器の **provider応答だけ** は合成NOT_SENSITIVEで、reportに `classifier=synthetic` を残します。
回答は `answer-fixtures.json` の独立した入力をProvider portで返すfakeです。fakeがcontextから
回答を生成するわけではなく、この成功はモデル品質の証拠になりません（`quality_evidence=false`）。

[characters.json](characters.json) と [evaluation.card.json](evaluation.card.json) は最小system promptの
固定した合成キャラクターです。ケースの検索Bindingへcharacter IDだけを合わせます。
日本語goldとの不要な言語不一致を避けるため、実モデル評価前に「ユーザーの質問と同じ言語で答える」を固定します。
既存Mioriカードは読み込みません。モデルにはqueryと本番が生成したcontextだけを渡し、
providerは `cases.json` だけを入力として検証します。goldはassertion / report bridgeのみが読みます。

assertionとgateはPython `AnswerExpectation.matches_facts` を共有し、NFKC→casefoldの
AND of ORs / 禁止語句を決定論的に判定します。出典表記は採点しません。
`no_memory` は空contextと禁止事実不在を確認します。検索後mutationはdispatch直前のguardで
送信を拒否し、回答後mutationは公開直前のguardで生成済み回答を破棄します。
`dispatch.valid` と `answer.discarded` をgoldと照合します。valid=trueの送信対象IDは、
上位5件に1位にあるべき記憶が含まれることを求め、完全一致や順序一致は求めません。
1位はgoldの `expected_order` の先頭、なければ `relevant_ids` の先頭、空なら該当なしです。
top-1なしならID照合を省き、valid=falseならID空を要求します。禁止IDは拒否します。
送信対象IDは公開reportの観測値であり、モデルへの入力には含めません。

各回の全ケース・全分類を再計算し、**各回・各分類90%以上**を要求します。禁止事実・Binding・失効・
dispatch・破棄の必須ゲートは平均で相殺しません。0件、欠落、重複、unknown ID、実行error、
非有限score、skip、cache利用、欠落assertion、採点metadataと再採点の不一致を拒否します。
promptfooの平均score・終了コードだけでは合格にしません。

`invalid-answer-fixtures.json` は禁止語句混入、必須事実欠落（synonym 8/10）、provider errorの
独立した不正入力です。Python/PostgreSQLとJS試験でgateがFAILになることを検証します。
全実行経路を手動確認する場合は `--fixture-variant forbidden|missing|error` を付けられます。
いずれも期待する終了結果はFAILであり、必須CIの正常fixtureと区別してください。

通信・保存境界：runnerは資格情報・proxy・libpq環境・dotenv・Node設定を子プロセスへ継承しません。
必要な専用PostgreSQL socket変数とPATHだけを渡し、HOME / dotenv / promptfoo保存先を専用0700一時領域へ
置きます。NodeのHTTP/TCP/TLS/UDP/DNS/fetchを拒否し、telemetry・更新確認・共有・remote生成・cacheを
無効にします。fixture providerもIP socket接続を拒否し、DBは明示Unix socketだけを使います。
固定版は `--no-write` だとJSON exportの結果が空になるため、このflagは使いません。
内部DB・raw exportは一時領域に生成し、終了時に削除します。本文・query・回答全文を公開reportや
証跡へ出しません。reportにはcommit/dirty、mode、classifier、profileの機密情報を除いたモデル識別、
embedding space、本番検索設定、入力/gold/カード/fixtureのSHA-256、promptfoo版、各回の結果と分類集計を残します。
モデル識別は設定値で、実backendの同一性を独立検証した証拠ではありません。
reportの `executions` は各runの試行数（常に1、再試行なし）、終了コード・signal・raw有無・
stdout/stderrの既知パターン分類だけを記録し、`toolchain` にNodeと内部DB依存の版を記録します。
異常終了やgate拒否でも部分runとFAIL理由をreportへ書き、公開stderrには構造診断だけを出します。
生stdout/stderrは同じ0700一時領域の0600ファイルへ保存し、通常は終了時に削除します。
明示的な診断用 `--keep-private-artifacts` の場合だけraw・生ログを保持します。表示されるローカルパスを
調査後に削除し、その内容を公開証跡へ転記しないでください。

### 明示的な実モデル実行

[answer-profile.example.json](answer-profile.example.json) は無効な例です。
git管理外の絶対パスのprofileで、トップレベル `enabled`、embeddingの `enabled`、
chatの `external_send_allowed` を明示的に有効にします。embeddingのmodel/digest/dimensionsと
既存管理loopback endpointを実設定へ合わせてください。Coreの既存 `LocalEmbedding` と
`LiteLLMProvider` / llama.cppのgemma4-12b経路を使い、資格情報を要する経路へfallbackしません。

```sh
bash tools/evaluate-semantic-answer.sh --mode local_model --execute-local-model \
  --profile /absolute/path/answer.local.json --runs 3 --output /dev/shm/semantic-answer-real.json
```

実行flagまたはprofileが欠ければNOT RUN（exit 3）、無効profileや通信失敗はFAILです。
このツールはサーバー・GPU・モデルを起動しません。
採用したbge-m3 Q8_0（CLS、1024次元、接頭辞なし）・閾値0.52 / gemma4-12bで、
上位5件包含の基準による検索・回答各3回の実モデル再評価を実施済みです。
各回とも検索品質/全条件53/62、回答品質61/62・全条件53/62でFAIL、品質は未受入です。
日英は検索・回答とも10/10ですが、検索は無関係3/10・長文0/1・閾値0/1、回答は長文0/1が品質未達です。
必須ゲートは検索top_one 1件/no_match 8件、回答dispatch 1件/no_memory 8件の違反が残ります。
分類品質と必須ゲートを分け、前回nomic/0.54との比較も記録した
[実モデル証跡](../../docs/evidence/2026-10-10-semantic-real-model-evaluation-bge-m3.md)と
[利用手順](../../docs/memory-evaluation.md)を参照してください。
