# アーキテクチャ決定記録（ADR）

ファイル名は`NNNN-short-title.md`とし、Status（Proposed / Accepted / Superseded）、
日付、背景、選択肢、決定、影響、関連Issue・PR・証跡のリンクを記載します。
Statusは次の方針で扱います。

- Proposed：合意・レビュー前の提案。
- Accepted：ユーザーとの合意またはレビューでの決定を参照し、合意の日付とAcceptedにした日付を記載します。
- Superseded：新しいADRで置き換えた決定。置換先を参照します。

履歴を黙って書き換えず、新しい記録で以前の決定を置き換えます。
ADR 0001〜0012はレビューの機会がなかったため、Proposedのままです。
ADR 0015〜0020は、2026-10-06のユーザー決定とPR #67のレビューを根拠に、2026-10-07にAcceptedとしました。
ADR 0026〜0030は、2026-10-11のユーザーの承認を根拠に、同日にAcceptedとしました。ADR 0031とADR 0032は、2026-10-12のユーザーの承認を根拠に、同日にAcceptedとしました。

- [ADR 0001: キャラクター文脈付き推論API](0001-character-inference-api.md)
- [ADR 0002: Ollama native Chat経路の明示](0002-ollama-native-chat.md)
- [ADR 0003: Coreのローカルllama.cpp Chat接続](0003-local-llamacpp-provider.md)
- [ADR 0004: 明示的な会話履歴の永続化](0004-conversation-history.md)
- [ADR 0005: 保存・推論送信・記憶形成の機微情報境界](0005-privacy-boundaries.md)
- [ADR 0006: 履歴と長期記憶の明示的な操作境界](0006-conversation-memory-controls.md)
- [ADR 0007: 出典を持つ最小記憶と撤回・再構成](0007-memory-provenance-and-revocation.md)
- [ADR 0008: 管理された分類器・抽出器の構造化出力](0008-managed-structured-output.md)
- [ADR 0009: モデル向け記憶contextの一時参照名](0009-memory-context-references.md)
- [ADR 0010: 明示注入したプロセス内embeddingによる記憶検索](0010-in-process-memory-search.md)
- [ADR 0011: 固定SDKによる明示的なローカルembedding接続](0011-local-memory-embedding.md)
- [ADR 0012: 明示選択するPostgreSQL履歴・記憶backend](0012-postgresql-storage.md)

記憶モデルはPoCの採用済み決定を移設して再編しました（ADR 0015〜0020）。
PoCは2026-10-17に`FYuki/digital-souls`から`FYuki/digital-souls-poc`へ改名し、非公開にしました。
ADR・証跡にある「公開」のPoCという記述は記録した時点の状態です。外部の読者はPoCへのリンク先を参照できません。
機能の実装状況と受入条件は[SPEC](../../SPEC.md)、用語は[CONTEXT](../../CONTEXT.md)を参照してください。

- [ADR 0015: PoCの記憶モデル決定の移設と再編](0015-memory-model-reorganization.md)
- [ADR 0016: 記憶の種別・正本・日時](0016-memory-kinds-and-records.md)
- [ADR 0017: 記憶の形成と保存判定](0017-memory-formation-admission.md)
- [ADR 0018: 記憶の検索と会話での利用](0018-memory-retrieval-context.md)
- [ADR 0019: 記憶の訂正・時間変化・矛盾・削除と失効](0019-memory-correction-invalidation.md)
- [ADR 0020: 内省・人格・関係・生活状態と記憶の接続](0020-reflection-personality-relationship.md)
- [ADR 0021: SQLiteを廃止しPostgreSQLへ一本化する](0021-postgresql-only-storage.md)
- [ADR 0022: 記憶の検索を Episode・Fact・Semantic の正本へ切り替える](0022-memory-retrieval-from-records.md)
- [ADR 0023: 本番の記憶検索経路による意味検索・回答評価](0023-semantic-evaluation-contract.md)
- [ADR 0024: 記憶検索の embedding を多言語モデル bge-m3 へ切り替え、閾値を0.52にする](0024-multilingual-memory-embedding.md)
- [ADR 0025: 常駐Coreの共通runtimeと外部インターフェース](0025-shared-runtime-interfaces.md)
- [ADR 0026: プロンプトの組み立てと領域ごとのtoken予算](0026-prompt-assembly.md)
- [ADR 0027: 会話中のツール利用と会話外の活動の実行基盤](0027-tool-use-and-activity-runtime.md)
- [ADR 0028: Character Book の解釈と Lore の選択](0028-character-book.md)
- [ADR 0029: 入口をまたぐ人格の一貫性の評価](0029-persona-consistency-evaluation.md)
- [ADR 0030: 人格の上書き（インジェクション）への耐性の評価](0030-injection-resistance-evaluation.md)
- [ADR 0031: 呼び出し側の system を受け取らない](0031-reject-caller-system.md)
- [ADR 0032: privacy 分類器の評価と PoC ケースの移行](0032-privacy-classifier-evaluation.md)（Accepted、ABSTAIN の扱いは決定 C〈別集計〉、2026-10-12 ユーザー決定）
- [ADR 0039: 記憶の出典情報・開示区分・参照の文脈を分け、場に応じて参照可否を判定する](0039-memory-provenance-disclosure-separation.md)（Proposed）

ADR 0021のSQLite撤去は Epic #79（[Issue #88](https://github.com/FYuki/digital-souls-core/issues/88)）で完了しました。本文は決定時点の履歴を保持します。

記憶検索はADR 0022の正本へ切替済みで、逐語記憶の書込・抽出・再構成経路は撤去済みです。
schema版6の削除・保持範囲は[撤去の証跡](../evidence/2026-10-08-verbatim-memory-removal.md)、
現行APIは[記憶API](../memory.md)を参照してください。ADR本文と既存証跡は当時の背景・判断を保持します。
