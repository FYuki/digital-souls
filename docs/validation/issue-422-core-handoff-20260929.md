# #422への共通Core・Runner引渡し（2026-09-29）

対象はEpic #3の`5d10ab490be8c7a2ec5b13bcaf9fc9a3386be429`。本書は引渡し資料であり、#422実装の開始・完了を表さない。[今回の受入記録](issue-3-acceptance-20260929.md)に未実施項目を残す。

## 差替え点と維持する契約

| 対象 | 引渡し先が維持するもの |
|---|---|
| #537 Runner port | `backend/app/inference/conversation_runner.py`の`ConversationInferenceRunner`と`create_conversation_inference_runner`。`generate_text`、`stream_text`（`latency_sensitive`を含む）、`estimate_input_tokens`の契約を保ちfactory内の実装を交換する。呼出側にSDK型を漏らさない |
| Target | `CHAT` callerと既存Inference RouterによるTarget／Provider／Capabilityの解決、入力上限、cancel・error・出力の意味を保つ。privacy・character-lifeのローカルProvider制約を緩和しない。Card・prompt・モデル設定は変更しない |
| Tool | 既存ToolServiceの候補提示・確認再開、Capability Snapshot、Execution Gate、送信検査、取消後dispatch拒否、結果不明の扱いを維持する。入口ごとの別Tool loopや権限判定を増設しない |
| 履歴 | HTTPは`invoke_text`、Speech/Textは`prepare_core_reply`に接続する。SessionがID・generation・割込・取消を所有し、履歴はSQLite正本、Chromaは派生index、所有は`character_id`で分離する。通常完了・保存済みturnのみ`submit_completed_turn`を1回呼び、失敗・取消・画面由来を形成予約しない |
| 再生範囲 | BE送出位置−dの区間境界への切下げと送出完了＋dを維持する。FE観測は差分記録のみ。生成本文全体を再生済みprefixとして保存しない |

設計の正本は[共通Core呼出契約](../decisions/common-core-invocation-2026-09.md)、現在の構成は[アーキテクチャ](../system-architecture.md)を参照する。

## 引き継ぐ固定fixtureと検査

- #538: `backend/tests/core_entry_conformance_fixture.py`。HTTP `/chat`・Session Text・Speechの3入口の同条件比較を維持する。Runner交換後も入口別の人格・履歴・Memory・Tool規則を分岐させない。
- #539: `backend/tests/voice_adapter_test_support.py`。LiveKit不要のテスト用Voice adapterで確定入力・応答ID・割込・取消・履歴prefixを検証する。実STT/TTS・ブラウザ再生の受入とは区別する。
- #537: `backend/tests/unit/test_conversation_runner.py`。Runner委譲と既存例外・streamingの回帰を確認する。
- #540: `backend/tests/unit/test_voice_downlink_delay.py`。推定とoptionalなFE観測の分離を維持する。
- 計測: `scripts/voice_quality/compare_core_acceptance.py`と[再実行手順](issue-3-acceptance-20260929.md#前後比較の再実行)。交換前後の本文開始・端末再生開始・負荷を同条件で採る。

## 既知制約

`owner/private`以外の主体・公開出力は予約された契約であり、現行入口では拒否する。CLIや活動型、公開記憶の規則を#422で先取りしない。STT/TTS接続方式、DBOS・DB・記憶モデルも交換対象外。

下り遅延300msは実測で確定していない。今回のdevでは共有推論サービスへ到達できず、性能回帰と過大推定0件の受入は未完了。実マイク・聴感はユーザー確認待ち。#507のLiveKit取消・終了は独立追跡し、新規回帰を機械的に#507へ帰属させない。
