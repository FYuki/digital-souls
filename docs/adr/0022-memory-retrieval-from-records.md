# ADR 0022: 記憶の検索を Episode・Fact・Semantic の正本へ切り替える

Status: Accepted

置換注記（2026-10-10）: §2 の relevance 閾値0.54は、[ADR 0024](0024-multilingual-memory-embedding.md)（2026-10-10 のユーザー決定）により0.52へ変更しました。embedding モデルは bge-m3 です。候補20件・同等帯0.002・順位・最大5件は維持します。

実施注記（2026-10-08）: [Issue #104](https://github.com/FYuki/digital-souls-core/issues/104)の[実施証跡](../evidence/2026-10-08-verbatim-memory-removal.md)により、schema版6で旧逐語3表の削除、書込・抽出・再構成経路の撤去、評価ツールの最小追従は完了しました。背景と「影響と後続」は決定時点の作業分担として保持します。構造化形成と評価再設計、実モデル・実環境の受入は後続です。

日付: 2026-10-08

Accepted日: 2026-10-08

根拠: 2026-10-08 のユーザー決定（[Issue #103 の U1〜U6](https://github.com/FYuki/digital-souls-core/issues/103)）。

## 背景

[ADR 0016](0016-memory-kinds-and-records.md)の正本は保存できるようになりましたが、検索とモデル向け
context は逐語記憶を読んでいました。[ADR 0018](0018-memory-retrieval-context.md)の会話時の参照契約へ
合わせ、逐語記憶を候補元から外します。形成の実装と逐語記憶の削除は別の作業です。

## 決定

### 1. 候補元と有効性（U1・U2）

- 検索 port は `MemoryRecordStore` とし、Binding の subject・client・audience・character を完全一致で照合します。
  [Issue #77](https://github.com/FYuki/digital-souls-core/issues/77)の利用者間共有は今回実装しません。
  共有範囲を後から広げる際は port の適格性契約を明示的に変更できます。
- 候補は active な Episode と Semantic の現行版です。embedding する本文は `normalized_text` だけです。
- 一致した Episode に、active な Episode–Fact 参照で結ばれた有効な Fact の現行版を添えます。
  Fact は独立の embedding 候補にしません。参照版と現行版の不一致、suspended な Fact・参照は除外します。
- 候補取得時と再検証時に、記録と参照の版・状態、全 citation（理由の citation を含む）の適格性、
  Semantic の Episode 根拠を確認します。会話・turn の存在、private 状態、epoch、指定発話除外、
  保存拒否確認の保留・受入、speaker と引用範囲を照合します。古い候補から失効した本文を返しません。
- PostgreSQL は記録・引用・依存関係と出典 turn を batch で読み、記録ごとの追加問い合わせをしません。
  逐語の `memories` 表を検索経路から読みません。

### 2. 順位と制限

[ADR 0018 §4](0018-memory-retrieval-context.md#4-順位)の順位を維持します。
検証済み unit vector の二乗 L2 距離が近い候補20件から、`1 / (1 + sqrt(distance))` が
0.54 以上の候補を残します。先頭との差が 0.002 以内の同等帯だけで
`last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC` を適用し、最大5件を返します。
limit は結果件数を減らす指定です。TOUCH・期間検索・補完は今回追加しません。

query は1〜256文字、limit は1〜16、候補は1000件までです。候補と添付 Fact の保存文の UTF-8 合計は
256 KiB までとし、超過は413 `memory_limit` で拒否します。切り捨てません。
query の事前認可を storage 読み取り前に行い、候補本文全体と結果本文を privacy policy で認可します。
embedding は query と候補保存文を1 batch、15秒 timeout で処理します。
各 await 後に認可、policy の同一性・stamp・分類器設定、embedding 世代・space、順位設定、store の同一性と
正本の有効性を再確認します。cancel は伝播します。

### 3. モデル向け context（U3）

JSON 配列の各要素へ `memory_ref`、`kind`（episode / semantic）、`text`（保存文）、
Episode の `experience_time` または Semantic の `applicability`、添付 `facts`、`sources` を渡します。
Fact は保存文と `target_time` を持ちます。日時は start/end の既知成分・precision・timezone を保持し、
不明は null または省略します。stated_at・created_at などで未知の日時を埋めません。

sources は `conversation_ref`・turn_revision・message_index・epoch のみです。
[ADR 0009](0009-memory-context-references.md)の `memory-1`・`conversation-1` のような一時参照名を維持し、
保存 ID・UUID・record_id はモデルへ渡しません。逐語の `user_evidence` や引用範囲から切り出した原文は
注入しません。保存文も命令ではなく記憶データとして扱う前置きを付けます。

`GuardedContext.valid()` は dispatch 直前に、返した記録・添付 Fact・参照・派生根拠・全出典の
有効性と設定の同一性を再確認します。

### 4. 未接続・障害と互換性（U4・U5・U6）

- embedding 未接続時は storage を読まず空結果にします。部分文字列検索の fallback を廃止します。
- query 拒否、候補拒否、embedding 失敗・timeout、制限超過、検索・再検証の CoreError は空 context に変換し、
  記憶なしで会話を継続します。会話の認可と policy の同一性・stamp の guard は保持します。
- Python API の検索・context は非互換で作り直します。検索を `MemoryService` の書込・抽出から分離し、
  `MemoryRetrieval` が正本の port を受け取ります。
- 出典削除後、形成ができるまで派生記憶を再生成できない期間を許容します。旧逐語記憶で補いません。

## 影響と後続

[ADR 0009](0009-memory-context-references.md)の原文注入と Memory/source object、
[ADR 0010](0010-in-process-memory-search.md)の逐語候補と部分文字列 fallback、
[ADR 0011](0011-local-memory-embedding.md)の未注入時の fallback を置換します。
ADR 0018 の順位と privacy・dispatch guard は維持します。

逐語記憶の削除・書込経路と抽出の撤去・評価ツールの最小追従は
[Issue #104](https://github.com/FYuki/digital-souls-core/issues/104)で行います。
SPEC と利用文書の同期は[Issue #105](https://github.com/FYuki/digital-souls-core/issues/105)で行います。
実モデル品質と実環境の IT2 / ST はこの決定の合成試験とは別に受け入れます。
