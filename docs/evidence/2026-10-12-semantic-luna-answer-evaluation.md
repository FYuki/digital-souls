# GPT-6 Luna（Codex サブスク）による62ケースの回答評価

日付: 2026-10-12。正本: [Issue #199](https://github.com/FYuki/digital-souls-core/issues/199)、
[Epic #126](https://github.com/FYuki/digital-souls-core/issues/126)、
[ADR 0023](../adr/0023-semantic-evaluation-contract.md)、[ADR 0024](../adr/0024-multilingual-memory-embedding.md)。
実行commit: `2fe921cc7255e277c19300c0bea6dbd2ffbb076d`、dirty=false。
公開report: [2026-10-12-semantic-luna-answer-codex.json](2026-10-12-semantic-luna-answer-codex.json)。
比較基準: [gemma4-12b / bge-m3 の証跡](2026-10-10-semantic-real-model-evaluation-bge-m3.md)と
[回答report](2026-10-10-semantic-real-model-answer-bge-m3.json)。既存結果は変更していません。

**3回とも FAIL・品質未受入**です。監督が実行済みの本文なしreportを文書担当が独立に再集計しました。
起動・停止・時刻・GPU・課金条件は監督の観測提供、ケース単位の数値・判定・版はreportで照合しています。
文書担当による評価・dry-run・CLI実送信の再実行はありません。

## 固定条件と事前見込み

合成62ケース・26分類、input schema=1、gold schema=2。各回各分類の品質条件90%以上と
全必須ゲート合格で判定します。期待値・閾値・プロンプト・gold・ケース・判定・コードは本作業で変更していません。
goldをモデルに送らず、本番の検索・contextで生成します。

| 項目 | 実行条件 |
| --- | --- |
| mode / answer_provider | external_model / codex_subscription |
| 回答モデル / 接続 | gpt-6-luna / Codex CLI 0.162.1、既存ChatGPTサブスク（2026-10-12ユーザー決定） |
| chat profile / timeout | sdk、openai/gpt-6-luna / 300秒 |
| 分類器 | synthetic、classifier_reason=baseline-parity-no-classifier-export |
| cache | なし（promptfoo --no-cache、report cache=false） |
| embedding | ローカルbge-m3 Q8_0、CLS、1024次元、接頭辞なし |
| GGUF | SHA-256 aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173、634,553,760 bytes（監督がADR 0024と照合） |
| 検索 | 閾値0.52、候補20、最大5件、同等帯0.002、本番設定 |
| 固定依存 | Node 24.19.0、promptfoo 0.117.2、better-sqlite3 13.0.3、embedding SDK 2.54.0 |
| condition_note | codex-cli-adds-own-instructions-not-pure-api |

classifier=syntheticはgemma比較基準にそろえるためです。送信時は本番のローカルclassifier/scanner/policy境界を
通しますが、実分類器の出力品質をこの評価で測定したことにはなりません。embedding・分類器を外部へ移していません。

2026-10-12T13:26Z頃、監督が外部送信なしのdry-runを16.5秒で実行し、実送信前に次を見積もりました。
本文なしdry-run reportのestimateと独立照合済みです。

| 事前見込み | 値 |
| --- | --- |
| ケース / 回数 | 62件 / 3回 |
| 送信可能件数 | 61件/回、合計183呼び出し |
| 入力token見込み | 202,530（utf8-byte-estimate、3回合計） |
| 出力token / 従量費用の計算 | 出力仮定・API単価は未指定。サブスク内のため従量課金なし、新規有料契約なし |

入力tokenは実tokenizerの測定値ではなく、Codex CLI独自の指示文を含みません。
サブスクの利用上限・実token数・出力token数・実課金明細は測定していません。
dry-runのexternal_send=false / quality_evidence=falseは品質のPASSではありません。

## 実行と送信境界

30分単位に収めるため、監督が明示実行flag付きexternal_modelを `--runs 1` で3回実行しました。
各コマンドtimeoutは1800秒。各回61件の正常応答、after_search 1件は送信直前guardで不送信（goldどおり）です。
送信はexternal_send_allowed → privacy authorize(external) → inference.checkを通過した合成ケースだけです。

| 回 | 開始UTC | 終了UTC | promptfoo exit | attempts | signal | raw_present |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-10-12T13:26:48Z | 2026-10-12T13:28:46Z | 100（assertion FAIL） | 1 | なし | true |
| 2 | 2026-10-12T13:37:07Z | 2026-10-12T13:39:05Z | 100（assertion FAIL） | 1 | なし | true |
| 3 | 2026-10-12T13:39:05Z | 2026-10-12T13:41:03Z | 100（assertion FAIL） | 1 | なし | true |

実行error・再試行・欠落ケースはありません。成功dispatchのCLI版は全3回0.162.1で一致しました。
ProviderはJSONLのtool eventをFAILにする設計です。全61件/回が正常応答だったことから、
ツール無効化の実効を間接確認しました。生JSONLは保持しておらず、tool event不在の直接再監査は **NOT RUN** です。
[#201設計証跡](2026-10-12-codex-subscription-answer-provider.md)の無効化30機能の設計試験とは区別します。

監督は既存exitedのembeddingコンテナ `digital-souls-core-llamacpp-bge-m3` を13:25:58Zにstart、
評価後13:41:16Zにstopしexitedを確認しました。chatコンテナは操作せずUpのままで、Luna回答には使いません。
GPUはRTX 4070 Ti SUPER、16376 MiB。使用量snapshotは起動前13706 MiB、起動後14374 MiB、停止後13750 MiB。
他作業を含むhost全体の値で、Lunaの消費量や専有peakではありません。文書担当のコンテナ操作はありません。

## 各回と分類別の結果

quality_passedは品質条件、case.passedは全条件。全体率は参考で、分類別90%と全必須ゲートで合否を判定します。
quality_evidence=trueは実モデル測定の印であり、品質合格を意味しません。

| 回 | 完走 | 品質条件 | 全条件 | dispatched | 合否 |
| --- | --- | --- | --- | --- | --- |
| 1 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 61 | FAIL |
| 2 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 61 | FAIL |
| 3 | 62/62 | 61/62（98.39%） | 53/62（85.48%） | 61 | FAIL |

分類別のquality_passed/totalとrate。gemma列は比較基準の各回共通値です。

| 分類 | Luna 1 | Luna 2 | Luna 3 | Luna各回rate | gemma各回 |
| --- | --- | --- | --- | --- | --- |
| synonym | 10/10 | 10/10 | 10/10 | 100% | 10/10 |
| paraphrase | 10/10 | 10/10 | 10/10 | 100% | 10/10 |
| cross_language | 10/10 | 10/10 | 10/10 | 100% | 10/10 |
| unrelated | 10/10 | 10/10 | 10/10 | 100% | 10/10 |
| negation | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| update | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| multisource | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| multisource_revocation | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| private | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| excluded | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| deleted_source | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| deleted_memory | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| binding_character | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| binding_subject | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| binding_client | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| long_text | 0/1 | 0/1 | 0/1 | 0% | 0/1 |
| after_search | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| after_answer | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| epoch_change | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| fact_attachment | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| fact_version | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| fact_revocation | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| semantic_direct | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| semantic_derived | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| equivalent_order | 1/1 | 1/1 | 1/1 | 100% | 1/1 |
| threshold | 1/1 | 1/1 | 1/1 | 100% | 1/1 |

long_text 0/1だけが90%未満です。unrelatedとthresholdはgroundedではないため品質条件が真でも、
no_memory違反のケースは全条件FAILです。

## 必須ゲートとケース別失敗

違反件数はケース単位、複数ゲート違反は重複計上します。

| ゲート | Luna 1 | Luna 2 | Luna 3 | gemma各回 |
| --- | --- | --- | --- | --- |
| forbidden | 0 | 0 | 0 | 0 |
| dispatch | 1 | 1 | 1 | 1 |
| lifecycle | 0 | 0 | 0 | 0 |
| no_memory | 8 | 8 | 8 | 8 |

次の9ケースはLuna・gemmaとも全3回で同じ判定です。本文・query・回答全文は含めません。

| case ID | 分類 | 品質条件 | 違反ゲート |
| --- | --- | --- | --- |
| long-text-explicit-detail | long_text | FAIL | dispatch |
| unrelated-observatory | unrelated | PASS | no_memory |
| unrelated-02 | unrelated | PASS | no_memory |
| unrelated-03 | unrelated | PASS | no_memory |
| unrelated-06 | unrelated | PASS | no_memory |
| unrelated-07 | unrelated | PASS | no_memory |
| unrelated-09 | unrelated | PASS | no_memory |
| unrelated-10 | unrelated | PASS | no_memory |
| below-threshold | threshold | PASS | no_memory |

long_textは正常な空検索結果で必要な記憶を取得できず、必須事実欠落とdispatchのtop-1欠落が発生しました。
no_memory 8件は該当なしの期待に対して検索が記憶を返し、空contextを満たせないことに起因します。
検索側のno_match / top_oneに対応する違反で、回答モデルの変更でも残りました。
入力切詰め等の内部原因は断定しません。dispatch_validは全ケースgoldと一致し、
このdispatch違反1件を外部送信認可やprivacy違反とは扱いません。forbidden / lifecycle違反は0です。

## gemma4-12bとの並列比較と条件差

| 比較対象 | ローカルgemma4-12b（2026-10-10） | GPT-6 Luna / Codex（2026-10-12） |
| --- | --- | --- |
| 回答生成 | llama.cpp、chat ctx=4096、timeout=60秒 | Codex CLI 0.162.1、timeout=300秒 |
| 検索 | ローカルbge-m3 Q8_0 / 0.52 / 最大5件 | 同じモデルdigest・次元・検索設定 |
| 分類器 / cache / 回数 | synthetic / なし / 3回 | synthetic / なし / 3回 |
| 各回品質 / 全条件 | 61/62 / 53/62 | 61/62 / 53/62 |
| 分類90%未達 | long_text 0/1 | long_text 0/1 |
| 各回ゲート違反 | dispatch 1、no_memory 8、forbidden 0、lifecycle 0 | 同左 |
| 失敗ケースID・件数 / 送信記憶ID等の観測値 | 9件（上表） | 全ケースをID照合して完全一致 |
| 合否 | FAIL・品質未受入 | FAIL・品質未受入 |
| 回答文そのものの比較 | NOT RUN | NOT RUN |

検索はローカルの同じ設定で、回答生成先を替えた比較です。判定差はありません。
回答文の優劣・同一性やモデルだけの因果効果は、この本文なし・語句一致評価からは判断できません。
Codex CLI独自の指示とmessagesのJSON包装が加わり、純粋なAPIと条件が違います。
gemmaはctx4096のllama.cppで、今回chatコンテナの常用設定はLuna比較には使っていません。

加えて、比較基準から本実行までに既存のPromptBuilder/token予算移行が入っています。
`characters.json` は旧context_budget_bytes=65536からprompt_budget（total=28672、各領域=65536）、
memory_floor=3 / history_floor=3へ移行済みです。そのためcharacters SHAは異なります。
ケース・gold・カードのSHAは同一、embedding configurationはprofile_idだけ異なり、endpoint・SDK・timeoutは同一です。
本作業でこれらを変更していません。新旧の実送信prompt全文の一致検査は **NOT RUN** です。

| 固定ファイル / report | Luna SHA-256 | gemmaとの照合 |
| --- | --- | --- |
| cases.json | 44b90af81bdbc9a7c117fd5158d23bde4878571de2584b99bb1973f85a2ecdcf | 同一 |
| expectations.json | 0e979d561be09a45faf019baba285c14605f46ebacc6194f86b5298aa981caa4 | 同一 |
| evaluation.card.json | 17093470ae0139424d127bfc3308057b9bcface15dd70d0cf8bc45728c4bf3fa | 同一 |
| characters.json | 74eb61fcfd18a222718c4db0dc4b9599652d6deca838a8904e08b27c4bd1c7d9 | 旧0cfdd7aec0e6a69ecf601ab0ab16ad3696e53b987cf022a43c73ca09d533726fから上記移行 |
| 公開Luna回答report | c10f275c6744b25464d3b315db4836698221f72f85a2f0f5055422487811e99f | 3個別reportを統合 |

## report検査・文書同期・制約

各個別reportの62ケースのquality_passed・gates・passedを再集計し、分類別total/passed/rate、
gates_passed・run.passed・report.passed、dispatched件数、CLI版を独立照合しました。すべて監督の集計と一致。
3個別reportのmanifest一致を確認し、runsを結合、executions.runを1/2/3、run_countを3へ対応付けました。
ケース観測値・判定は変更せず、gemmaとも行順に依存せずIDで照合しました。
生回答を再採点したのではなく、既存の本文なしreportを再集計した検証です。

公開JSONのdecoded全キー/文字列とbyteを検査し、非ASCII・改行本文・秘密項目・私有パスの混入なしを確認。
path状文字列はモデル識別子と既存のローカルembedding endpoint設定だけです。
ケースは固定ID・送信記憶ID・boolean、診断は固定分類に限定し、query・入力本文・回答全文・認証情報を含めません。
私有profile・CLI認証情報・私的ログの内容は読まず、転記していません。
SPEC §2.4 / §3.2、README、評価README、評価手順、#201証跡を実測と同期しました。

- 語句判定はNFKC→casefoldの部分文字列AND of ORs。否定・時間関係・意味全体の判定ではありません。
- LLM judge・出典表記・回答言語・人格品質・injection耐性・性能受入は **NOT RUN**。
- 本測定の分類器はsynthetic。実分類器を組み込んだ統合評価は **NOT RUN**。
- 形成・保存判定・再生成・TOUCHを含む実環境IT2/STは **NOT RUN**。
- サブスク経路の実送信は実施済みですが、GPT-6 Lunaの本番採用判断はIssueの範囲外です。
- backend真正性の独立検証は **NOT RUN**。CLI版の一致は接続モデルの独立真正性検証ではありません。
- GitHub CI・push・PR・merge・Issue操作は **NOT RUN**。監督のレビューと必須CI確認が残ります。

## 文書作業の検証

固定toolchain（uv 0.8.22 / Node 24.19.0）で実行しました。評価・dry-runの再実行はありません。

| 検証 | 結果 |
| --- | --- |
| uv sync --locked | PASS、lock変更なし |
| uv run --no-sync pytest -m ut -q | PASS、1244件、1448 deselected、8.43秒、skip/xfail/xpassなし |
| Node required reporter（check-docs / required-tests-reporter） | PASS、27件、skip/TODO/cancelledなし |
| node tools/check-docs.mjs | PASS、新規証跡/reportもstage後に検査 |
| git diff --check / git diff --cached --check | PASS |
| report独立再集計・manifest/CLI版・gemma ID照合 | PASS |
| 公開JSONの非ASCII・秘密項目・私有パス・本文なし形式検査 | PASS |
| IT1・PostgreSQL・実環境IT2/ST・GitHub CI | NOT RUN（文書担当の本作業） |

UTのdeselectedはmarker対象外でskipではありません。既存StarletteのDeprecationWarning 1件があります。
ローカル文書・UTのPASSを実モデル品質や未実施の必須CIのPASSとして扱いません。
