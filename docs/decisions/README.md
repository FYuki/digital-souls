# Architecture Decision Records

`docs/decisions/`直下には、現行設計の根拠として参照できるADRだけを置く。
完全に置換・失効したADRは`archive/`へ移動し、判断当時の経緯を残す。

## 状態と参照方法

| 状態 | 意味 | 配置 |
|---|---|---|
| `ACTIVE` | 判断の全体または一部が有効（Nygard形式のacceptedに相当） | `docs/decisions/` |
| `ARCHIVED` | 全面失効、または有効な判断を現行文書へ統合済み（supersededに相当） | `docs/decisions/archive/` |

状態は判断の有効性だけを表し、実装・既定有効・受入の進捗を表さない。
現在の構成は[アーキテクチャ](../system-architecture.md)とコード、用語と実装名の対応は[用語集](../../CONTEXT.md)、進捗・完了条件・受入結果はIssuesを参照する。

### 書き方と変更

[文書の正本と更新責務](documentation-governance-2026-09.md)に従う。

- ADRは決定時点の記録とする。背景には日付を付けた決定時点の前提（改修前の状態）を書き、実装・受入の進捗やブランチ名を書かない。
- 判断を変える場合は新しいADRを作る。一部だけを置き換える場合は旧ADRを書き換えず、本書の優先関係に置換範囲を記載する。
- 誤記・リンク切れ・誤った進捗記述の削除は旧ADRを直接修正してよい。

archive内の文書を現行仕様や実装のSource of Truthとして使用しない。

## 現行の優先関係

- [`documentation-governance-2026-09.md`](documentation-governance-2026-09.md)は、文書ごとの正本、ADRを決定時点の記録とする運用、README・AGENTSの範囲を定める。[リポジトリ運用方針](../repository-policy.md)と本書の旧記述より優先する。
- [`companion-target-architecture-2026-09.md`](companion-target-architecture-2026-09.md)は、目標とするユーザーシナリオ（複数の利用場所での一貫性、キャラクター設定の一元管理、共有体験）、ローカル／クラウドLLMの併用、モジュール境界と切り出しの原則、外部から呼ばれる入口の扱いを定める。[複数入口ADR](multi-entry-character-core-2026-09.md)の要件へ目標シナリオを追加し、その不変条件を維持する。
- [`conversation-activity-agents-2026-09.md`](conversation-activity-agents-2026-09.md)は、会話エージェントと活動エージェントの分離、共有する正本、活動からの報告経路、推論の優先度、Cardの参照規則、ペルソナ一貫性評価を定める。[複数入口ADR](multi-entry-character-core-2026-09.md) §8の未決事項「複数入口からの同時会話」のうち、会話と会話外活動の競合をこのADRで決める。
- [`voice-playback-estimation-speech-services-2026-09.md`](voice-playback-estimation-speech-services-2026-09.md)は、再生済み範囲と出力完了をBEの送出位置から推定する契約、STT／TTSの接続方式、割り込み判断を自前で維持することを定める。[Backend集約ADR](voice-backend-authority-2026-09.md)の決定2のうち実再生観測の所有、[LiveKit transport ADR](livekit-transport-2026-08.md)の全出力確認待ち、[共通client ADR](conversation-session-client-2026-09.md)の再生済みprefix送信を置き換える。
- [Web・音声共通Core呼出](common-core-invocation-2026-09.md)は、#3の既存入口の呼出主体・識別子・応答準備・保存／形成の所有と後続機能の拒否を定める。複数入口の目標は次のADRを優先する。
- [`multi-entry-character-core-2026-09.md`](multi-entry-character-core-2026-09.md)は、#3で合意した複数入口（Web・Discord・スマホ・CLI・SNS）から同じ人格コアを使う要件、最終構成、入口ごとの会話と認証、呼出主体、公開範囲、自律SNS参加の制御、実施順を定める。自律活動・外部送信・高影響操作はCharacter Life共通契約を再利用し、緩和しない。進捗は#3／#516／#517／#518で管理する。

- [音声Session開始準備](voice-session-preparation-2026-09.md)は、#350/#423/#424の開始準備、全体の固定時間上限なし、処理側の失敗検出、手動再試行を定める。既存の個別要求・worker・入力ACK等の期限や通信再接続は維持する。要求は[共通指示書](../voice-quality-350-423-424-requirements.md)を参照する。

- [`addon-event-recovery-2026-09.md`](addon-event-recovery-2026-09.md)は、#187の共有取得、有限バッファと処理位置の永続化、上限超過・欠落時の復旧を具体化する。この範囲はAddon接続ADR・通知／会話分離ADRの抽象記述に優先する。通知の保持とUIは通知／会話分離ADRの2026-09-16追記、実装要求は[要件指示書](../epic-187-addon-event-requirements.md)を参照する。
- [`voice-backend-authority-2026-09.md`](voice-backend-authority-2026-09.md)は、#358のBEによる音声活動・正式発話境界の所有、FE／BE一括更新・切り戻し、復元不能な音声欠落の扱いを定める。この移設範囲では既存音声・Speech/Text・共通client ADRより優先する。実再生観測の所有は再生済み範囲推定ADRが置き換える。詳細は[要件指示書](../epic-358-voice-backend-requirements.md)を参照する。

- [`tts-engine-reference-voice-2026-09.md`](tts-engine-reference-voice-2026-09.md)は、#329のSession開始時のTTS選択と、Irodoriのvoice ID・参照音声の固定対応を追加する。既存の音声／Speech・Text共通Session契約と環境分離境界を維持する。要件は[Irodori差し替え要件](../irodori-tts-requirements.md)、進捗は#329を参照する。
- [`episode-fact-semantic-boundaries-2026-09.md`](episode-fact-semantic-boundaries-2026-09.md)は、#352で確定したEpisode / Fact / Semantic / Reflectionの正本責務、述語のみ必須の5W、config timezone、FactのID参照・版・失効、同thread登録時照合と後続#354の別thread非同期整理を定める。#340で合意した日常経験・仮定/創作、経験の単位、Fact更新と統合の区別、管理操作・dev受入も含む。この記憶再編の範囲ではCharacter Life・用語・Wave 2・RAG privacy ADRの旧記述より優先する。

- [`conversation-session-text-input-2026-09.md`](conversation-session-text-input-2026-09.md)は、#319のSpeech/Text共通入力、受理結果照合、focus抑止、スレッド別送信を定める。この範囲は既存の音声／LiveKit契約より優先する。進捗は#321〜#327、受入の証跡は[混在Session受入](../conversation-session-acceptance.md)等を参照する。
- [`conversation-session-client-2026-09.md`](conversation-session-client-2026-09.md)は、#323の共通client API、送信結果管理、履歴表示、入力抑止・中断の利用境界を定める。WebとDesktop #317から共用する。
- [`notification-conversation-separation-2026-09.md`](notification-conversation-separation-2026-09.md)は、#187の取り込み、#183の会話非依存通知、#364のキャラクター報告を分離する設計契約である。旧Addon接続ADRの#183責務を置き換え、登録時の担当・対象、通知用read-only caller、予算、既読／報告の独立、コピー・引用の非伝播を定める。2026-09-16の第11節は旧レコード集約案を置き換え、個別保存・初回保存から30日（件数上限の例外あり）・削除後の再取得・通知ON／OFFを定める。実装要求は[通知要件](../epic-183-notification-requirements.md)を参照する。通知起点の応答は#365で明示的に拡張する接続点であり、このADRだけで現行Speech／Text schemaやAPIを変更・有効化しない。Gate・認可・privacy・通常入力の既存契約は継続する。
- [`addon-action-approval-recovery-2026-09.md`](addon-action-approval-recovery-2026-09.md)は、#185の接続・操作群・実行場面別承認、確認キュー、60秒待機、結果回復を定める。承認の効力・UI範囲はCharacter Life共通ADRより本ADRを優先する。
- [`character-life-runtime-2026-09.md`](character-life-runtime-2026-09.md)は、#249のtop-level runtimeへDBOSを採用し、domain正本・関連Epicとの責務境界を定める。現在の任意有効化・接続範囲は[Life運用](../character-life-operations.md)を参照する。
- [`character-life-memory-personality-autonomy-2026-09.md`](character-life-memory-personality-autonomy-2026-09.md)は、本人視点のEpisode、Semantic/Reflection分離、Life State、Big Five Aspects人格、Relationship、Autonomy Target、外部送信privacy、高影響操作の共通契約を定める。
- 上記範囲で`wave2-memory-formation-retrieval-2026-08.md`、`rag-memory-privacy-policy-2026-07.md`、`tool-use-foundation-2026-09.md`と競合する場合は、2026-09のCharacter Life ADRを優先する。ただし、採用した拡張設計と現在の許可型・永続modelは区別する。
- [`memory-personality-terminology-2026-09.md`](memory-personality-terminology-2026-09.md)は、気質／変動人格、各記憶種別、記憶統合／意味抽象化／内省、現在の興味関心、会話での参照を定義する共通語彙の正本である。実装方式そのものは確定せず、実装契約と競合する場合はその契約側を優先する。[用語集](../../CONTEXT.md)は既存実装との対応を補助する。
- [`miori-personality-without-fixed-role-2026-09.md`](miori-personality-without-fixed-role-2026-09.md)は、光織のCharacter Coreを性格中心とし、記録・検索等の固定業務を外す。2026-06のキャラクターADRの該当部分を置き換える。興味・役割の自動形成機能の追加とは別の変更である。

新しい記憶表現を理由にprivacyや外部実行権限を緩和しない。長期記憶保存時の絶対禁止、SQLite正本／Chroma派生index、transactional outbox、#104/#182のCapability Snapshot／Execution Gate等、後続ADRで明示的に変更していない既存契約は継続する。

## Archive

archiveの一覧と置換先は[`archive/README.md`](archive/README.md)を参照する。

## 意味記憶の追加契約

[意味記憶の訂正・時間変化・矛盾](semantic-memory-lifecycle-2026-09.md)は#341の判断である。
意味記憶の時間変化・矛盾・UI操作の範囲で共通境界ADRに優先し、privacy・根拠失効は維持する。
