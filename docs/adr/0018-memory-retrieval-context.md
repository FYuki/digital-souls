# ADR 0018: 記憶の検索と会話での利用

Status: Accepted

置換注記（2026-10-10）: §4 の既定 relevance 閾値0.54は、[ADR 0024](0024-multilingual-memory-embedding.md)（2026-10-10 のユーザー決定）により0.52へ変更しました。0.52 は bge-m3 の relevance 空間での値です。候補20件・同等margin 0.002・最大5件・順位・relevance の式は維持します。

置換注記（2026-10-08）: 逐語記憶の暫定候補元・出典 turn の保存順による最終言及の代用は、[ADR 0022](0022-memory-retrieval-from-records.md) の正本と日時列で置換します。§4 の順位と guard は維持します。

日付: 2026-10-06

Accepted日: 2026-10-07

根拠: 2026-10-06のユーザー決定（[SPEC §4.1の決定表](../../SPEC.md#41-決定済み2026-10-06のユーザー決定)）と
[PR #67](https://github.com/FYuki/digital-souls-core/pull/67)のレビュー。

## 背景

[ADR 0010](0010-in-process-memory-search.md)の意味検索は、PoCの検索順位から移植漏れがありました。
順位付けは[Issue #52](https://github.com/FYuki/digital-souls-core/issues/52)で先行して修正中ですが、
期間検索、語彙による補完、自己申告の現在値補完、矛盾の注意、最終言及日時の更新、障害時の会話継続は
未移植です。[ADR 0015](0015-memory-model-reorganization.md)に従い、PoCの
[Wave 2](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/wave2-memory-formation-retrieval-2026-08.md)第9〜11節、
[RAG privacy方針](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/rag-memory-privacy-policy-2026-07.md)第11節、
[用語契約](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/memory-personality-terminology-2026-09.md)第6節、
PoCの[検索実装](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/memory/rag_service.py)を採用します。

## 決定

### 1. 会話時の参照契約

| 参照の仕方 | 対象 |
| --- | --- |
| 常時またはcharacter contextとして参照 | Temperament、Personality、Current Interests、Working Memory、Conversation History |
| 必要時に検索 | Episodic Memory（有効なFact参照を含む）、Semantic Memory |
| 通常会話へ直接注入しない | Reflective Memory |
| 実行時に参照 | Procedural Memory / Skill |

Reflectionは、Current InterestsやPersonalityへ変換された状態を通じて会話へ反映します。

### 2. 検索前のquery判定と障害時の扱い

- 現在のuser発言が機微な場合、記憶の検索自体を行いません。scannerの絶対禁止・判定失敗、
  意味分類のSENSITIVE・ABSTAINで検索をskipします。機微なqueryをembeddingしてindexへ問い合わせる
  時点で関連付けが生じるため、検索後の再検証だけでは代替しません。
- 検索前の分類は同期経路のため短いtimeout（PoC初期値3秒）で1回だけ呼び、retryしません。
- query判定・embedding・検索・再検証のいずれかが失敗した場合は、**記憶なしで会話を継続**します。
  記憶の利用は止め（fail-closed）、会話そのものは止めません。cancelは伝播します。

現行Coreは、query拒否以外の検索失敗（embedding失敗等）を会話のエラーとして返しています。これを変更します。

### 3. 候補の取得と再検証

1. 派生index（現行は一時vector）で候補を取得する。
2. 正本で、Binding、状態、有効期限、policy versionの互換、本文の決定論的再検査を確認する。
3. Fact・source・統合関係・派生根拠の有効性と版を確認し、古いindexから無効な情報を返さない。
4. 下記の順位で並べ、最大件数とprompt予算の範囲で注入する。

### 4. 順位

```text
意味的関連度 DESC
-> last_user_mentioned_at DESC NULLS LAST（同等関連度の帯の中だけ）
-> created_at DESC
-> id ASC
```

- 既定値はPoCと同じ、候補20件、relevance閾値0.54、同等margin 0.002、最大5件。設定値として持つ。
- relevanceはPoCのChroma既定空間（二乗L2距離）に合わせ`1 / (1 + sqrt(距離))`とする。
- 新しさを理由に、明確に関連度の低い記憶を繰り上げない。
- 検索回数による強化、自動減衰、固定の複合重み（直近性・重要度・関連度の加重和）は採用しない。

### 5. 最終言及日時とTOUCH

`last_user_mentioned_at`はユーザーの明示的な新規言及・再言及・訂正でのみ更新します。
検索、prompt注入、assistantの言及、consolidationでは更新しません。新しいuser由来の記憶は言及日時を
設定し、consolidationではsource群の最大日時を引き継ぎます。

既存の記憶と同じ内容をユーザーが再言及した場合は、新しい記録を作らずに既存記憶の
`last_user_mentioned_at`だけを更新します（TOUCH）。TOUCHは本文・構造化値・内容版・更新日時を変えず、
派生indexの更新も作りません。保存拒否・privacy拒否のturnではTOUCHしません。

[PR #53](https://github.com/FYuki/digital-souls-core/pull/53)は、発話日時の列がない現行schemaの暫定措置として
出典turnの保存順を最終言及の代わりに使います。stated_atとlast_user_mentioned_atの保存を追加した時点で
置き換えます。

### 6. 期間検索

決定論的なparserで時間条件を抽出できたqueryでは、正本の期間検索と意味検索を併用します。
一致種別は「両方一致 > 意味一致 > 期間一致」の順とし、同じ種別の中では意味的関連度を主に、
意味距離のない期間一致同士では最終言及日時で並べます。時間条件がない・解析できない場合は意味検索だけに
縮退します。query解析の同期経路へLLM呼出しを追加しません。

経験日時とFactの対象日時を区別し、部分日時を確定日時として照合・表示しません。季節は対象日時の月から
導出し（春3〜5月、夏6〜8月、秋9〜11月、冬12〜2月、冬は年を跨ぐ）、年精度・不明は季節照合から除外します。
promptには検証済みの日時と精度を併記します。期間検索と意味検索が正常に終わって0件なら、
該当なしであることと推測禁止を明示します。

### 7. 補完と注意

- **語彙による補完**：Semanticの属性名・本人の根拠発言がqueryと一致する記憶を、意味検索の結果に補う。
- **自己申告の現在値**：過去の自己申告だけが検索に当たった場合、同じ主体・属性の現在の自己申告を補う。
  正本から補う場合はベクトル距離を捏造しない。
- **矛盾の注意**：未解決の矛盾がある属性がqueryに関係する場合、主体と属性だけを注意として渡し、断定を避ける。

### 8. ログ

検索に使った記憶ID・日時・精度・一致種別はmetadataとして追跡できますが、query・記憶本文・prompt・
出力全文をログへ残しません。

## 影響

- MemoryContextは、検索失敗時に空のcontextで会話を続ける形に変わります。
- 期間検索には[ADR 0016](0016-memory-kinds-and-records.md)の日時列が、補完と注意にはSemanticの構造化値と
  矛盾関係（[ADR 0019](0019-memory-correction-invalidation.md)）が前提になります。
- 検索の評価はPoCの固定corpusの考え方（関連度と順位を分けて測る、合成データのみ）を引き継ぎます（[SPEC](../../SPEC.md)）。

## 参照

- [ADR 0009](0009-memory-context-references.md)：モデル向けcontextの一時参照名（維持）
- [ADR 0011](0011-local-memory-embedding.md)：ローカルembedding adapter（維持）
