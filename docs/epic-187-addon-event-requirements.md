# Epic #187 Event取り込み・復旧の要件

## 目的・状態

外部Addon/MCPのEventを一度取得し、通知・会話など独立したconsumerへ安全に渡す。
2026-09-16のgrill-meで合意した要求を記録する。#187のEpicでは取得・復旧runtimeと別process適合試験を実装した。
設定・公開結果契約・consumer API・検証入口は[Event runtime](addon-event-runtime.md)を参照する。
進捗と完了条件の管理先は[Epic #187](https://github.com/FYuki/digital-souls/issues/187)、
実装は[#155](https://github.com/FYuki/digital-souls/issues/155)、
公開契約の適合検証は[#190](https://github.com/FYuki/digital-souls/issues/190)とする。

判断の正本は[Event復旧ADR](decisions/addon-event-recovery-2026-09.md)、
通知・会話との境界は[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md)、
用語は[用語集](glossary.md)を参照する。

## 確認済みの前提と範囲

- #104のMCP接続・Capability Snapshot・Execution Gateと抽象 `EventSourcePort` を起点にする。
  EventRuntimeがこの境界を実装し、ToolRuntimeの共有Gateと起動・終了処理へ接続する。
- Event本文・Task状態・結果の正本は提供元。Coreは有限のsanitized Eventバッファを所有する。
- MCP-firstでcursor付きhistory Query/Resourceを利用し、push/Resource更新通知は新着確認の契機とする。
  共通Eventへmappingできない外部MCPでも、通常のTool/Resource利用を妨げない。
- 接続単位の取得を共有し、ingestion cursorとconsumerごとの処理位置を分ける。
  通知保存と通知consumer進捗は#158、会話link・報告保存とその進捗は#365が所有する。
- 取得と配送でも現在の認可・sharing・binding・snapshot・availability・予算を適用する。
  接続済みという理由で許可を省略せず、本文・secret・raw endpointを通常logや通知DBへ残さない。

## 合意した要求

| 項目 | 要求 |
|---|---|
| 初回購読 | 購読開始時点を基準とし、それ以降のEventを取得する。提供元の既存状態は初期状態として扱い、過去Eventを新着として一括配送しない |
| 通常取得 | 初期設定は1分間隔。提供元ごとに変更可能とする。wake-upで早めに取得し、通知欠落は定期確認で補う。障害・利用制限時はbackoffする |
| 再接続・再起動 | 保存済み処理位置から復旧する。バッファとingestion cursorを整合させてディスクへ永続化し、取得済みの未処理Eventも復旧可能にする |
| 保持 | 取得から最大72時間。件数・容量も有限とし、上限に達したら72時間未満でも古い分を除去する。無制限のdomain Event Storeにしない |
| 遅いconsumer | 未処理分があっても共有取得を継続する。除去で再取得が必要となった範囲を対象consumerへ伝え、提供元履歴から復旧する。除去を処理済みに読み替えない |
| 履歴欠落・期限切れ | 欠落を記録し、影響を受ける各consumerへ伝える。提供元の契約に従い最新状態と再開位置を確認できたら自動再開する。確認できない間はそのsourceの取得を停止する |
| snapshotの限界 | 最新状態の取得を過去Eventの復元とみなさない。失われた完了Event等を処理済み・報告済みとして補完しない |
| 不正Event | 検証できないEventを配送せず、理由・欠落のmetadataを記録する。安全な次の取得位置を確認できる場合だけ後続へ進み、確認できなければそのsourceを停止する |
| 購読解除 | 最後のconsumerが明示的に解除されたらsource取得を停止する。他consumerが残る間は継続する。一時遅延・障害、ブラウザ終了、端末オフラインを解除とみなさない |
| オフライン | Backend稼働中は画面がなくても取得を継続する。Backend停止中は新規取得できず、再起動後に提供元履歴・保存済みバッファで復旧する |

重複・stale・out-of-orderはsourceの公開契約に基づき扱う。再配送が通知の重複や集約数の二重加算、
会話の二重報告へ結び付かないよう、後続consumer側の冪等性と独立した処理進捗の責務を維持する。
この文書だけで外部副作用や端末表示のexactly-once保証を追加しない。

## 受け入れ条件

#190ではCoreをimportしないtest-owned MCP serverを別processで動かし、
LLM/ChatService/PromptBuilder/TTSを起動せず、公開契約で次を検証する。
以下を継続的な受入条件とし、対応する実通信・境界試験は[Event runtimeの検証](addon-event-runtime.md#検証)にまとめる。

| 場面 | 観測する結果 |
|---|---|
| 初回接続と開始境界付近のEvent | 既存状態を新着にせず、確定した開始位置以降を取得する |
| pushなし／wake-up欠落 | 既定1分の定期確認で取得し、wake-upだけを唯一の配送路にしない |
| 切断・再接続、永続化途中の障害・再起動 | cursorだけが先行して未処理分を失わず、復旧後もconsumer進捗を混同しない |
| 重複・stale・out-of-order | 公開契約に定めた識別・順序・再配送の扱いが成立する |
| 72時間の期限、件数・容量上限、遅いconsumer | 上限を守り、共有取得とほかのconsumerが進む。除去を正常処理済みにしない |
| バッファ除去後のsource replay | 対象consumerが復旧でき、通常の共有取得をconsumerごとに重複実装しない |
| 提供元履歴の期限切れ・cursor不正 | 欠落を明示し、最新状態と再開位置を確認した場合だけ自動再開する |
| 不正Eventと安全な次位置の有無 | 次位置を確認できれば理由付き除外で継続し、できなければsource停止となる |
| 最後／最後でないconsumerの解除 | 最後の場合のみsource取得を停止する。一時停止や画面切断を解除と混同しない |
| 認可失効・unavailable・degraded・利用制限・shutdown | 既存の安全境界を維持し、理由・復旧可能性をmetadataで観測できる |
| 悪意あるpayload・secret・endpointを含む入力 | バッファは許可済み情報に限定し、不正Eventの記録や通常logにもraw情報を漏らさない |

期限の試験は時刻制御等で決定論的に行い、実時間72時間の待機を必要条件にしない。
本番Addon/ゲーム接続、通知UI・通知保存・会話報告の受入を#190で済んだことにしない。

## #183への引継ぎ

通知一覧・未読管理・絞り込み・詳細への導線はMicrosoft TeamsのActivityフィードを参考にする。
通知metadataと出典参照は未読・既読ともに既定30日保持し、#187の72時間バッファと分ける。
ブラウザが閉じていてもBackendが稼働していれば通知保存を継続し、復帰後に確認できるようにする。
期限切れはTask取消し・監視解除・キャラクター報告完了を意味しない。
バナー・通知音は追加候補であり、今回の必須要件には含めない。
具体的な保存・UI要件と根拠は[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md)を参照する。

## 実装側で具体化する項目

- 件数・容量上限の具体値、制限の適用単位、backoff・wake-up集約・取得予算。
  1分、最大72時間という合意値と上限超過時の扱いを維持し、境界試験で確認する。
- 公開API/schema、Event識別・順序・cursorの表現、source履歴とsnapshotの整合した再開位置の取得方法。
- 永続化方式・transaction境界・冪等な再処理、遅れたconsumerへのreplayの調整方法。
- バッファの許可項目、欠落・停止のreason、欠落metadataの有限な保持・確認方式。
- 通知30日の起算点、集約revisionと期限の関係、通知件数上限、期限切れ後の参照表現は#158で具体化し、#191/#365と整合させる。

これらはユーザーが数値・API名まで確定した項目ではない。保証範囲を変える必要が生じた場合は、
実装上の便宜で変更せず、要求判断として再整理する。

## 制約・非スコープ

- 通知Policy・metadata保存・UI・詳細取得は#183、会話紐付け・依頼報告・自発発話・TTSは#364。
- Event起点の自動write/delete、Task lifecycle、長時間自律session、独自SSE/WSの先行追加は含めない。
- raw Eventを会話履歴・Persona Memory・RAGへ無条件複製しない。
- 本文・成果物を30日または72時間保持できるという保証は追加しない。提供元の保持契約に従う。
- 文書・Issue更新は専用worktreeからEpicへ統合する。mainへのマージはユーザーが行う。
