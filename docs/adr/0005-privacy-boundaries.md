# ADR 0005: 保存・推論送信・記憶形成の機微情報境界

Status: Proposed

一部を[ADR 0037](0037-message-level-conversation-history.md)で置き換えます（2026-10-18 ユーザー承認。会話履歴の保存の前の判定。送信と記憶形成の境界は維持）。

一部を[ADR 0038](0038-mvp-prompt-window-and-input-scan.md)で置き換えます（2026-10-18 ユーザー承認。推論への送信の前の意味分類器による判定。応答の経路では決定論的な scanner だけにする）。

一部を[ADR 0015](0015-memory-model-reorganization.md)の記憶モデル再編で置き換えます（「PoC全体の移植は行わない」の判断）。

一部を[ADR 0021](0021-postgresql-only-storage.md)のPostgreSQL一本化で置き換えます（SQLite transactionの記述。保存transaction中にclassifierを待たない原則は維持）。

日付: 2026-10-03

## 背景

第1段階の[履歴保存](0004-conversation-history.md)は明示policy注入で既定拒否です。
第2段階ではこの境界に決定論的検査を接続し、機微な本文の意味分類をローカル推論へ限定します。
私的実会話の取り込み、記憶抽出・検索、tenant認証、GPUサービス変更は行いません。

## 決定

保存、ローカル送信、外部送信、将来の記憶形成をhistory/local/external/memoryの別permissionとします。
trusted Binding（scopeとcharacter）単位でoperatorが許可を明示し、HTTP本文やLLMは変更できません。
external/memoryの分類器呼出しにもlocal許可が必要です。設定変更中の非同期判定は世代番号で失効させます。
各呼出しで再判定し、結果の永続化や共有cacheは作りません。

履歴と通常ローカル会話には決定論的scannerを使います。秘密値・直接識別値を検出した場合は
本文全体を拒否し、部分マスクしません。tool引数・resultを改変せず、既存receiptの意味を保つためです。
保存・記憶操作は[ADR 0006](0006-conversation-memory-controls.md)の後続ユーザー仕様を優先します。
自然語で履歴保存を拒否する旧仮案は採用せず、指定発話の記憶除外・thread modeを明示APIで扱います。
同一conversationのhealth等の話題自体は一律拒否しません。外部送信・記憶形成では意味分類が必要です。
記憶permissionの検査は将来の採用判定の一条件であり、source検証や候補採用を実装したものではありません。

InferenceとConversationsへ同一PrivacyPolicyを注入します。通常のstateless経路の既存互換性を維持し、
privacyを選択した起動構成だけが新しい検査を有効にします。履歴を通常起動で自動有効化しません。
入力だけでなく合成後のsystem/Lore/context/tool定義を含む実payloadを送信前に検査します。
履歴の最終保存・read・retry receiptは既存の同期HistoryPolicyで現在の許可とsecretを再検査します。
SQLite transaction中にclassifierを待ちません。履歴とexternalの異なる許可を混同しません。

分類器は既存Provider portと検証済みllamacpp_chat loopback Profileを使います。
SDK上のモデル名だけをローカル保証とはしません。外部fallbackを持たず、分類前に同じscannerを必須にします。
未知enum・余分/欠落/重複キー・矛盾した分類・policy version不一致・timeout・不正応答は拒否します。
自由文理由、本文、matched value、正規化本文、例外文字列をログ・DB・判定結果へ残しません。
model digestはoperatorが固定する実行構成の識別情報であり、実モデル取得・品質検証をした保証ではありません。

## 代替案と影響

- PoC全体の移植はmemory schema・worker等への依存を増やすため行いません。
- spanマスクは原文対応・tool構造・receipt同一性の追加契約が必要なため、現段階は保守的な全文拒否です。
- 全履歴への意味分類は通常ローカル会話を分類器障害に連動させるため採用しません。
- 同期HistoryPolicy内のLLM呼出しはDB近傍の待機を生むため非同期送信判定から分離します。
- NFKC/casefold/format除去・JSON内文字列の復号・既知credential/連絡先/checksum検査を使います。
  全秘密・全PII・任意の暗号化/難読化の検出を保証しません。誤検出・見逃しを合成corpusで明示します。
  モデルの意味理解品質はmock試験で証明せず、実モデル評価は別途必要です。

## 参照

PoC参照revision: `fce7382884d981c42be7fbd3ddaffe7469e27588`。

- [privacy scanner](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/privacy/scanner.py)
- [normalization](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/privacy/normalization.py)
- [history sanitizer](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/privacy/history_sanitizer.py)
- [semantic classifier](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/privacy/semantic/classifier.py)
- [後続ADR](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/wave2-memory-formation-retrieval-2026-08.md)
  を優先し、廃止済みの候補ごとの確認方式を再導入しません。

利用手順は[privacyガイド](../privacy.md)を参照してください。

作業範囲・受入条件は[Issue #22](https://github.com/FYuki/digital-souls-core/issues/22)で追跡します。
