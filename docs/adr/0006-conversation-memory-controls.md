# ADR 0006: 履歴と長期記憶の明示的な操作境界

Status: Proposed

一部を[ADR 0037](0037-message-level-conversation-history.md)で置き換えます（2026-10-18 ユーザー承認。指定発話の記憶除外、プライベートモード、アーカイブ。会話の削除はユーザーの操作で行う）。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（SQLite transactionとSQLite schema v1→v2の記述。削除と通知を原子的に確定する契約と操作仕様は維持）。

日付: 2026-10-03

## ユーザーが指定した操作仕様

| 操作 | 会話履歴 | 長期記憶 |
|---|---|---|
| 通常 | 保持 | 対象 |
| 記録しないで（指定発話） | 保持 | 対象外 |
| プライベートモード（スレッド単位） | 保持 | 対象外 |
| 会話履歴削除 | 削除 | そこから作られた記憶も削除 |
| アーカイブ | 一覧で非表示 | 維持 |

この表は[ADR 0005](0005-privacy-boundaries.md)の自然言語保存拒否に関する仮案に優先します。
secret/機微検査・外部送信許可は別です。操作によって秘密値の保存・送信を許可しません。
controls単体の「対象」は必要なsource条件を表します。Stage3の抽出・検索は[記憶API](../memory.md)を参照してください。

## 決定案

自然文から期間・スレッド設定を推測しません。UI/Agentが明示APIで指定します。
completionの`memory_excluded_indices`で今回の入力配列内の発話を選びます。
指定された発話は通常どおり履歴へ保存し、元データを変更せず記憶sourceの対象外とします。
返答は除外発話に依存し得るため、そのassistantも対象外です。以後のassistant/toolも同じ履歴を
参照する限り対象外を維持します。新しい独立user発話は明示除外されなければ対象候補です。

threadの`private_mode`と`archived`はPATCHで変更し、期待revisionによる排他を用います。
private中に保存したturnは解除後も自動で対象へ戻しません。現在privateのthreadは全sourceを利用不可とします。
controls単体ではsource gateまででした。Stage3では同一DBの派生本文削除と永続epochを追加します。
archiveは通常一覧から隠すだけで、直接復元・source eligibility・削除は引き続き可能です。

sourceはtrusted Bindingと、conversation ID・turn revision・message indexで識別します。
復元APIはmessage順にsource参照と現在のeligibilityを返します。source条件の通過だけで
privacyや根拠・候補型の審査を省略できません。Stage3は採用時・取得時に再検証します。

会話削除と内容を含まない`SourceDeletion`通知を同じSQLite transactionで確定します。
削除後はsourceの照合が即時falseになるため、通知の処理待ちを理由に古いsourceを有効としません。
通知はscope・characterで分離し、trusted consumerが派生memory/indexの削除後にacknowledgeします。
HTTPに通知ack操作を公開しません。通知は本文を含まず、stage3 consumer未接続中は保持します。
同じ会話の再削除は通知を増やしません。未知IDや別scopeの削除にも通知を作りません。

SQLite schema v1→v2はALTERと通知table作成を単一transactionで実行します。
既存の履歴・receipt・fingerprintを保持し、新しい除外fieldが空の場合は旧request fingerprintを維持します。
control変更もconversation revisionを進め、進行中の旧revision推論はcommitできません。

## 代替案と影響

- 自然語から保存範囲を推定する方式は「指定発話」を越えて適用し得るため採用しません。
- archiveをdeleteで表現する方式は記憶sourceを失うため採用しません。
- メモリ削除を同期callbackだけで伝える方式は停止中の通知を失うため、永続通知にします。
- 生成応答の厳密な依存解析は未実装なので、過去の除外内容を参照し得るassistant/toolを保守的に除外します。
  除外を含む会話で生成された安全な独立応答も対象外になり得ます。
- controls単体には後付け訂正・除外変更、consumer、memory実削除を含めませんでした。
  Stage3の追加範囲と未実装範囲は[記憶API](../memory.md)で区別します。

## Stage3の確定した製品動作（2026-10-03）

以下の2点はユーザーが提案を承認しました。実データを今削除する許可ではなく、実装仕様です。

1. threadをprivateへ切り替えたら、そのthread由来の既存memoryを削除します。
   private解除で削除済memoryを自動復活させません。将来の新規抽出はsource適格性を再判定します。
2. 複数sourceのmemoryで一つのsourceが削除/対象外になったら、旧memoryを直ちに利用停止し、
   残る適格sourceだけから再構成します。成功して再審査を通るまで検索/コンテキストへ出しません。
   根拠が残らなければ削除状態を維持します。private化もsource対象外化として同じ規則を適用します。

controls実装時点では上記は未実装でした。その時点のprivate切替はoutboxを生成せず、source gateだけでは
旧memoryの復活を防げません。現行`SourceDeletion`は履歴が消えたことを表すため、履歴を残すprivate化の
通知に流用しません。Stage3で撤回通知・永続的な撤回世代・memory状態を追加する必要があります。
既存source参照は維持し、削除通知の既存契約も壊さずに拡張します。
fixture consumerの成功は実memory削除の証明ではありません。
Stage3で上記の撤回通知・epochを追加します。詳細な設計・ゲートは[ADR 0007](0007-memory-provenance-and-revocation.md)に分離します。

## 参照

- [履歴API](../history-api.md)
- [privacy境界](../privacy.md)
- [第1段階ADR](0004-conversation-history.md)

- [Issue #24: 操作仕様とStage3後続受入条件](https://github.com/FYuki/digital-souls-core/issues/24)
- [PR #25: controls実装](https://github.com/FYuki/digital-souls-core/pull/25)
- [PR #27: 承認済み仕様の整理](https://github.com/FYuki/digital-souls-core/pull/27)
- [PR #28: main統合レビュー](https://github.com/FYuki/digital-souls-core/pull/28)
- [契約整理の検証証跡](../evidence/2026-10-03-stage3-contract-review.md)
