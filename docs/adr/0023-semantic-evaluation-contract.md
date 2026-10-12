# ADR 0023: 本番の記憶検索経路による意味検索・回答評価

Status: Accepted

日付: 2026-10-08

Accepted日: 2026-10-09

根拠: 2026-10-08 のユーザー決定（[Epic #111 の V1〜V7](https://github.com/FYuki/digital-souls-core/issues/111)）。

> 置換注記（2026-10-09、[Issue #120](https://github.com/FYuki/digital-souls-core/issues/120)）：
> 「実行・合否」の実モデルにおける同等帯の全順序一致と、回答dispatchの順序を含むID完全一致は、
> 2026-10-09のユーザー決定により「上位5件に1位にあるべき記憶が含まれる」判定へ置き換えました。
> 詳細は末尾の「合否判定の置換注記」を参照してください。該当箇所の本文は履歴として保持します。

> 置換注記（2026-10-10、[Issue #131](https://github.com/FYuki/digital-souls-core/issues/131)）：
> 「実行・合否」の本番設定の relevance 閾値0.54は、[ADR 0024](0024-multilingual-memory-embedding.md)（2026-10-10 のユーザー決定）により0.52へ変更しました。
> 評価も本番設定（候補20、最大5、閾値0.52、同等帯0.002）で行います。該当箇所の本文は履歴として保持します。

## 背景

[ADR 0022](0022-memory-retrieval-from-records.md)で、検索候補とcontextはEpisode・Semanticの正本と有効なFactへ切り替わりました。
旧PR #51のpgvector PoC評価ではこの本番経路を評価できません。評価入力と期待値をprompt・設定調整前にcommitで固定し、後続の検索・回答評価が同じデータを使えるようにします。

## 選択肢

旧pgvector評価の継続、現行の最小追従ツールの維持、本番の正本・検索・contextを使う評価への置換を比較しました。V2に従って本番経路へ置換します。形成の実装まで評価を待つ案は採用せず、合成の正本登録で検索・回答を先に測ります。

## 決定

### 評価する経路（V1・V2）

- 旧PR #51・PR #49・Issue #48のpgvector PoCはcloseする方針です。pgvectorは永続的な派生index（SPEC §5 手順9）の時点で再検討します。今回導入しません。
- 合成の会話履歴を用意し、Episode・Fact・EpisodeFactLink・SemanticをPostgreSQL正本へ `MemoryRecordStore.register` で直接登録します。本番の `MemoryRetrieval` と `MemoryContext` のcontext生成・guardを通し、検索結果と、そのcontextによる回答を評価します。
- 会話からの形成・保存・検索・利用までの実接続受入（SPEC §3.2）は形成の実装後に行います。このEpicの合成登録をその受入として扱いません。

### 道具と固定データ（V3・V4・V6）

- 検索評価はPython、プロンプトを含む回答評価はpromptfooを使います。検索ハーネスは[Issue #113](https://github.com/FYuki/digital-souls-core/issues/113)、回答評価は[Issue #114](https://github.com/FYuki/digital-souls-core/issues/114)で実装します。[Issue #112](https://github.com/FYuki/digital-souls-core/issues/112)は形式検証・domain変換・固定データまでで、ハーネスは作りません。
- 入力 [cases.json](../../evals/semantic/cases.json) とgold [expectations.json](../../evals/semantic/expectations.json)を分離します。goldの正解ID・禁止ID・期待順序・必須事実・禁止事実・該当なし・期待挙動を入力へ含めません。分類・対応表は[評価データREADME](../../evals/semantic/README.md)で示します。
- 旧PR #51 head `5a573e2` の合成ケースを移し、言い換え・同義語・日英・無関係を各10件程度へ増やします。有効Fact添付、参照内容版不一致・出典撤回によるFact除外、SemanticのDIRECT_EXTRACTION / EXPERIENCE_DERIVED、同等帯の日時・ID順序、閾値を追加します。
- 旧 `unsupported_audience`（`unsupported-public-audience`）は移しません。現行 `AccessScope.audience` は `local-private` 固定であり、`public` を表現できないためです。残る旧19件は `legacy_id` で対応させます。
- 旧ケースの可変source/memory APIは引き継がず、現行history/storeの公開操作で表します。過去発話の後付け除外はappend時の除外と登録拒否、記憶の直接削除は出典往復の削除、epoch変更はprivate化と解除で表します。Fact更新は現行版を期待版とする内容版追加と必要な新リンク登録です。
- 回答goldの `required_facts` は全グループを満たし、各グループでは許容表記のいずれか1つを含む（AND of ORs）形式です。`forbidden_facts` もグループ形式で、いずれかの候補を含めば違反です。候補は記憶本文・合成会話に根拠のある核心事実と自然な訳・表記揺れに限定し、記憶にない事実を正解へ加えません。日英どちらの言語でも核心事実が一致すればよく、回答言語は採点しません。
- 回答と各候補はUnicode **NFKC → casefold** の順で正規化し、決定論的な部分文字列一致で判定します。空白・句読点を削除せず、単語境界・文意解析・LLM judgeは使いません。空グループ・空文字・空白のみ・正規化後の重複（グループ内/間）・required/forbidden間の同一候補は拒否します。外側の空配列はその側の制約なしです。部分文字列一致には複合語の誤一致や否定関係を完全に判定できない限界があり、[評価データREADME](../../evals/semantic/README.md)に個別判断を記録します。出典の表記は採点対象にしません。回答読み取り時の引用表記の正規化は別途の判断です。
- 第2ラウンドは第1ラウンド `5c52330` の未公開・回答評価未実行goldを、prompt・設定調整と評価結果を見る前に修正するものです。goldのschemaは破壊的な形式変更を明示する2、入力schemaは1を維持します。旧形式を暗黙変換せず拒否します。全62ケースを見直し、22ケースの語句を修正しました。これは評価結果に合わせた期待値の緩和ではありません。

### 実行・合否（V5・V7、SPEC §3.2）

- CIでは有限・非ゼロの固定偽embeddingとfixtureを使い、道具の正しさだけを検証します。fixture成功はモデル品質の証拠ではありません。
- 実モデル評価は手動でcacheなし3回実行し、各回・各分類90%以上とします。モデル・設定・commit・入力版・各回の結果と制約を証跡に残します。平均で不合格の回・分類を相殺しません。
- 本番設定は候補20、最大5、relevance閾値0.54、同等帯0.002です。unit vectorの二乗L2距離から `1/(1+sqrt(distance))` を計算し、同等帯では `last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC` を使います。該当なしは閾値以上の適格候補0件を要求します。
- privacy・Binding・出典・失効の違反0件、閾値未満の混入0件、検証できない記憶への代替0件、同等帯の全順序一致は必須ゲートです。他のケースや品質の平均で相殺しません。contextのdispatch直前guardと、応答後撤回時の破棄も別に確認します。
- 評価結果に合わせて期待値・閾値を緩めません。実モデル品質がFAILでも結果を正確に記録し、合成PASSと区別します。
- 評価用LLMの利用は許容されています。ローカルモデルはGPUの状況を確認して承認なしで利用可、GPT-6 Lunaも承認なしで利用可というV5の範囲を保持します。このデータ固定作業で実モデルは実行しません。

### 厳密性と旧評価との関係

旧PR #51のProposed ADR 0014はmainに存在しないため移しません。必要な契約だけを本ADRへ取り込みます。
入力とgoldを分離し、入力の重複ID・未知ID・不正citation・Binding不整合・非有限/ゼロ/次元不一致vector・同一本文のvector矛盾・ケース集合不一致を拒否します。本文をrepr・エラーへ出しません。
後続ハーネスは0件・欠落ケース・重複結果・unknown ID・error・非有限scoreを評価成功にせず拒否します。
正常な検索0件と、実行・結果ファイル0件を区別します。後者は合格ではありません。

既存 `tests/fixtures/memory-retrieval-evaluation.json`、`tools/evaluate-memory-search.py`、
`src/digital_souls_core/memory_evaluation.py` は **置換** します。
Epic #74の方針どおり旧ツールは作り直しまでの最小追従であり、V2の本番経路での評価と二重管理になるためです。
新しい検索評価ハーネスがCIで動いた時点で、検索評価のIssue #113で撤去します。
このADR・データ固定では旧ツール・fixtureを撤去しません。
利用文書 `docs/memory-evaluation.md` の書き換えは実モデル評価・文書同期の[Issue #115](https://github.com/FYuki/digital-souls-core/issues/115)で行います。

## 影響

ケースとgoldが検索・回答評価の共通契約になります。合成会話の論理IDは後続ハーネスで実際の生成IDへ対応付けます。
形式検証とdomain変換はDBに依存せず、実登録と本番経路でのCI検証はIssue #113へ引き継ぎます。
旧ADR本文は歴史として保持し、SPEC・利用文書の実施状態を書き換える作業は混ぜません。


## 合否判定の置換注記（2026-10-09）

2026-10-09のユーザー決定「上位5件の中に、1位にあるべき記憶が含まれていれば合格。
順序の一致は求めない」（[Issue #120](https://github.com/FYuki/digital-souls-core/issues/120)）により、
上の「実行・合否」にある実モデルの全順序一致と、回答dispatchの順序を含むID完全一致を置き換えます。
本文は決定の履歴として保持します。

1位にあるべき記憶はgoldの `expected_order` があれば先頭、なければ `relevant_ids` の先頭です。
空の `relevant_ids` はtop-1なしとします。gold自体は変更しません。
検索の実モデル必須ゲートは返却上位5件へのtop-1包含です。fixtureではこの包含に加え、
従来の `expected_order` 全順序一致も必須にし、62ケース×3回で本番の同等帯sortを検証します。
回答は `dispatch.valid` の一致を維持し、trueなら送信対象の上位5件へのtop-1包含、
falseなら送信IDが空であることを要求します。top-1なしの場合はID照合を省きます。
禁止IDは引き続き拒否し、no_memoryは従来どおり空contextを要求します。
検索品質の `relevant_ids` 全包含、必須/禁止事実、privacy・Binding・失効、閾値、代替禁止、
no_match、lifecycle、ケース・プロンプト・キャラクター・設定と各回各分類90%は変更しません。
