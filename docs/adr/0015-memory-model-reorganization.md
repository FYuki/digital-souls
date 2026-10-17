# ADR 0015: PoCの記憶モデル決定の移設と再編

Status: Accepted

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（§2の読み替え表でSQLiteまたはPostgreSQLを保存backendの選択肢とする記述）。

一部を[ADR 0016](0016-memory-kinds-and-records.md)で置き換えます（既存の保存済み記憶の移行計画。既存の逐語記憶は移行せず削除）。

日付: 2026-10-06

Accepted日: 2026-10-07

根拠: 2026-10-06のユーザー決定（[SPEC §4.1の決定表](../../SPEC.md#41-決定済み2026-10-06のユーザー決定)）と
[PR #67](https://github.com/FYuki/digital-souls-core/pull/67)のレビュー。

## 背景

Coreの記憶実装（[ADR 0007](0007-memory-provenance-and-revocation.md)、
[ADR 0010](0010-in-process-memory-search.md)ほか）は、PoCで採用済みの記憶モデルを前提にせず
新たに設計されていました。その結果、検索順位、記憶の構造化、自動形成、時間・矛盾の扱い、
内省・人格との接続などがPoCから欠落しています。ユーザーは2026-10-06に、これらの差の大半を
移植対象とし、まずADRを再編してから分解・実装する順序を指示しました。

移設元は公開`FYuki/digital-souls-poc`の固定commit `fce7382884d981c42be7fbd3ddaffe7469e27588`の
次の決定です。いずれもPoCで`ACTIVE`（採用済み設計）であり、PoC側でも実装完了を意味しません。

| PoCの決定 | 主な内容 | Coreでの移設先 |
| --- | --- | --- |
| [RAG privacy方針](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/rag-memory-privacy-policy-2026-07.md) | 絶対保存禁止、正本と派生index、取得時の再検証、metadata-only log | [ADR 0017](0017-memory-formation-admission.md)、[ADR 0018](0018-memory-retrieval-context.md) |
| [Wave 2記憶形成・検索](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/wave2-memory-formation-retrieval-2026-08.md) | 履歴・記憶・記録の分離、保存判定、非同期形成、検索順位 | ADR 0016〜0018 |
| [Episode・Fact・Semantic境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/episode-fact-semantic-boundaries-2026-09.md) | 正本と形成責務、5W、日時、Fact照合・更新、参照失効 | [ADR 0016](0016-memory-kinds-and-records.md)、ADR 0017、[ADR 0019](0019-memory-correction-invalidation.md) |
| [意味記憶の訂正・時間変化・矛盾](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/semantic-memory-lifecycle-2026-09.md) | 訂正と時間変化、矛盾保留、自己申告優先、削除と再処理防止 | ADR 0019 |
| [記憶・人格の用語契約](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/memory-personality-terminology-2026-09.md) | 用語と概念境界、会話時の参照契約 | [CONTEXT](../../CONTEXT.md)、ADR 0016、ADR 0018 |
| [Character Life共通契約](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-memory-personality-autonomy-2026-09.md) | Reflection、Life State、人格、関係、自律活動との接続 | [ADR 0020](0020-reflection-personality-relationship.md) |

PoCの要件書（[#340 Episodic Memory](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/epic-340-episodic-memory-requirements.md)、
[#341 意味記憶](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/epic-341-semantic-memory-requirements.md)）の
受入条件は[SPEC](../../SPEC.md)へ整理します。

## 決定

### 1. 移設の原則

- PoCの上記決定の判断内容を、Coreの記憶モデルの前提として採用します。PoC内の優先関係
  （2026-09のEpisode・Fact・Semantic境界とCharacter Life共通契約が、2026-08/07の決定に優先する）を維持します。
- PoCのIssue番号（#340等）は責務の出典として残しますが、Coreの作業単位はCoreのIssueで新たに管理します。
- 音声、UI、Addon、Chroma固有の運用、dogfood固有の手順など、PoCの実行環境に固有の規則は移植しません。
  ただし、その規則が守っていた不変条件（例：正本と派生indexの分離）は移植します。
- 移設は文書上の採用であり、実装・実モデル受入の完了を意味しません。実装状況は[SPEC](../../SPEC.md)で区別します。

### 2. Coreへの読み替え

| PoCの概念 | Coreでの読み替え |
| --- | --- |
| `character_id`の境界 | Binding（subject・client・audience・character）。PoCより細かい境界として維持 |
| `conversation_id` / thread | Coreの会話（conversation）。スレッドと同義 |
| 会話turn・元発言ID | Coreのsource参照（conversation・turn revision・message index）とsource epoch |
| SQLite正本・`persona-memory.db` | 選択した保存backend（SQLiteまたは[PostgreSQL](0012-postgresql-storage.md)）の正本 |
| Chroma派生index | 派生index一般。現行は呼出し中だけの一時vector。永続indexを導入する場合も正本から再構築可能にする |
| transactional outbox | 正本更新と派生indexへの反映予定を同一transactionで確定する原則として維持 |
| Inference Target / Ollama | CoreのProvider portとloopbackのローカルprofile（[ADR 0003](0003-local-llamacpp-provider.md)） |

### 3. Coreの既存決定との優先関係

ユーザーがCoreで直接指定・承認した操作仕様は、PoCの同種の決定より優先します。

- [ADR 0006](0006-conversation-memory-controls.md)の操作表（指定発話の記憶除外、スレッド単位の
  プライベートモード、会話履歴削除で派生記憶も削除、アーカイブは記憶を維持）と、
  private化・複数sourceの一部撤回時の扱いを維持します。PoCの自然文による保存拒否の語彙は、
  この明示APIを置き換えません。自然文は検出してプライベートモードへの切替を確認する契機に使います
  （[ADR 0019](0019-memory-correction-invalidation.md)）。
- 秘密値・直接識別値の扱い（[ADR 0005](0005-privacy-boundaries.md)の全文拒否）は維持します。

AIが作成したCoreの次の判断は、本ADR群で置き換えます。

| Coreの記述 | 置き換え |
| --- | --- |
| ADR 0005「PoC全体の移植はmemory schema・worker等への依存を増やすため行いません」 | 本ADRの移設原則 |
| ADR 0007「抽出器は型とsource indexのみを返し、本文は選ばれたuser発話全体」「episodeは分類ラベルで正規化時間schemaは未実装」 | ADR 0016・0017の構造化記録（5W、日時、Fact、引用範囲） |
| ADR 0007「自動履歴取り込み、常駐worker、schedulerは追加しない」 | ADR 0017の非同期形成（実行はPrivateAgent、Coreは予約とdrain API） |
| ADR 0007/0010「SQLite内の語句検索」「正のcosine・最新作成順」 | ADR 0018の検索・順位 |
| [CONTEXT](../../CONTEXT.md)旧版「worker、schedulerは現在のCoreの対象外」 | ADR 0017の形成責務と、ADR 0020の実行基盤の境界 |

上記以外のADR 0004〜0012の決定（履歴の保存境界、privacy permission、出典epoch、撤回の原子性、
構造化出力、一時参照名、embedding adapter、PostgreSQL adapter）は維持し、新しいモデルの下層として使います。

## 影響

- 記憶の保存形式が「選択されたuser発話の逐語JSON」から構造化記録へ変わります。既存の保存済み記憶と
  dogfoodのPostgreSQLに対しては、backup・移行・検証・rollbackを伴う移行計画が必要です（ADR 0016）。
- 発話時刻を持たない現行の履歴schemaに、元発言日時（stated_at）の保存を追加する必要があります。
- 実装は機能ごとのEpic・Issueに分解し、TDDで進めます。分解は[SPEC](../../SPEC.md)の機能一覧を起点にします。

## 参照

- PoC参照revision: `fce7382884d981c42be7fbd3ddaffe7469e27588`
- 検索順位の先行修正: [Issue #52](https://github.com/FYuki/digital-souls-core/issues/52)、
  [PR #53](https://github.com/FYuki/digital-souls-core/pull/53)
