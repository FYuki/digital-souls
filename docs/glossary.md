# 用語集

`digital-souls`で使う概念と、現在のコード・契約上の名前を結び付ける参照用の用語集です。一般的なAI用語の定義ではなく、このリポジトリでの意味を記します。

## 読み方と正本

「実装」はmainにある処理・型、「一部実装」は基盤や境界のみが接続済み、「設計」は採用済みでも当該処理がmainへ未実装の概念を表します。実装済みでも既定有効・dogfood受入済みとは限りません。今回の照合基準は2026-09-13のmain（`2c03565`）です。

用語の正式な判断は[記憶・人格の用語契約](decisions/memory-personality-terminology-2026-09.md)等のADR、現在の挙動はコード・[アーキテクチャ](system-architecture.md)、進捗はIssuesが正本です。本書で仕様を新設・上書きしません。ADRの`ACTIVE`は「有効な設計判断」であり「実装完了」ではありません。優先関係は[ADR案内](decisions/README.md)を参照してください。

通知・非同期結果の節は#183のEpic開始時に追加した設計用語です。既存コードの照合基準や、他の項目の実装状態を変更するものではありません。

## キャラクター・人格定義

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Character / キャラクター、`character_id` | 会話・記憶・活動の所有境界。表示名と識別子は別で、別キャラクターの記憶を混ぜない | 実装：[loader](../backend/app/characters/loader.py)、[履歴モデル](../backend/app/conversation_history/models.py) |
| Character Card V3 | `characters/{id}/{id}.card.json`に置くruntime定義の正本。`personality.md`・`world.md`は編集用補助資料 | 実装：[Card契約](decisions/character-card-v3-prompt-builder-2026-07.md) |
| Character Core | Cardから組み立てるキャラクターの基本的な性格・話し方等のprompt領域。変動人格の更新処理そのものではない | 実装：[loader](../backend/app/characters/loader.py) |
| Character Book / Character Lore | BookはCard内の設定Entry群、Loreは会話文脈に照合して選ぶ補足設定。経験から形成するRAG記憶ではない | 実装：[Lore契約](decisions/character-book-runtime-2026-08.md)、[selector](../backend/app/characters/lore_selector.py) |
| Temperament / 気質、Immutable Core | 経験で変更しない不変パーソナリティ。変動人格より上位の制約 | 概念契約：[用語ADR](decisions/memory-personality-terminology-2026-09.md)。Cardの全項目と同義ではない |
| Personality / 人格、Mutable Personality | 経験・内省から緩やかに変わる持続的傾向。キャラクター全体や記憶DBの別名ではない | 人格適応は設計：[Character Life共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |
| Big Five Aspects | 変動人格の正本に採用した10 Aspect。Egogram（CP/NP/A/FC/AC）は評価用で、人格パラメータや注入コンテキストの正本ではない | 設計：[Character Life共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |

光織に記録・検索等を固定業務として与える古い説明は、[固定役割と人格の分離](decisions/miori-personality-without-fixed-role-2026-09.md)で置き換えられています。このprompt調整と、自動的な人格・興味形成機能の完成は別です。

## 会話・入力・音声

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Conversation / 会話スレッド、`conversation_id` | SQLiteへ保存する会話のまとまり。作成・一覧・再開・アーカイブ・削除の対象。UUIDv4を使用 | 実装：[履歴モデル](../backend/app/conversation_history/models.py) |
| Conversation Session、`session_id` | 特定のキャラクター・スレッドに結び付く実行中の会話Session。Speech/Textの入力、応答、再生、接続状態を扱う。永続スレッドそのものではない | 実装：[混在入力契約](decisions/conversation-session-text-input-2026-09.md)、[LiveKit client](../frontend/src/livekit/voice-session.ts) |
| ConversationSessionManager | 現在選択したスレッドIDをキャラクター別に保持・復元する既存UI helper。同名に近いが、Speech/Text共通実行clientとは別 | 実装：[conversation-session.ts](../frontend/src/lib/conversation-session.ts) |
| Conversation Turn、`turn_id` | ユーザー入力と応答の保存単位。`processing / completed / interrupted / failed / privacy_skipped`を区別する | 実装：[履歴モデル](../backend/app/conversation_history/models.py) |
| Speech / Text input | 同じ会話処理へ渡す音声由来／テキスト由来の入力。音声Sessionがない通常のテキスト会話にはHTTP経路もある | 実装：[入力契約](decisions/conversation-session-text-input-2026-09.md)、[Core](../backend/app/conversation_core/) |
| Utterance / 発話、`utterance_id` | 音声入力の発話区間を識別する単位。永続Turnや応答IDと同一視しない | 実装：[音声契約](decisions/voice-session-contract-2026-08.md) |
| Response / 応答、`response_id` | 生成・音声出力・中断の対象となる応答の単位。遅れて届いた旧応答を新応答へ混ぜない | 実装：[音声契約](decisions/voice-session-contract-2026-08.md) |
| 入力受理 / 応答完了 / 再生完了 | 入力が受け付けられたこと、応答処理が終わったこと、ブラウザ出力が終わったことは別。受理済みでも完了・成功とは限らない | 実装：[共通client契約](decisions/conversation-session-client-2026-09.md)、[再生完了境界](../backend/app/livekit_transport/playback_completion.py) |
| Generation / 世代 | Session・応答・画面共有等で古い処理を識別して採用しないための世代。どの領域の世代かを明記する | 実装：[音声契約](decisions/voice-session-contract-2026-08.md)、[画面契約](decisions/browser-screen-perception-2026-09.md) |
| VAD | 音声の発話開始・終了を検出する処理。STT（音声の文字起こし）とは別 | 実装：[音声UI](../frontend/src/lib/AudioRecorder.svelte)、[音声契約](decisions/voice-session-contract-2026-08.md) |
| Barge-in / 割り込み | キャラクターの応答中に新しい入力を優先し、旧応答を止めること。古い生成・音声結果の不採用も必要 | 実装：[Core](../backend/app/conversation_core/)、[音声契約](decisions/voice-session-contract-2026-08.md) |
| Input suppression / 入力抑止 | 入力欄focus等により音声入力の採用を一時的に抑えること。利用者のmute、応答中断、Session終了とは区別する | 実装：[混在入力契約](decisions/conversation-session-text-input-2026-09.md) |
| AudioTransport / LiveKit | 音声・control eventの送受信境界と現行の通信実装。Room／Participant／Trackはtransport側の概念で、会話スレッドの正本ではない | 実装：[transport契約](decisions/livekit-transport-2026-08.md)、[実装](../backend/app/livekit_transport/) |
| STT / TTS | STTは共有Whisper HTTP serviceによる認識、TTSはVOICEVOXによる合成。現行BackendはWhisperのGPUモデルを所有しない | 実装：[remote client](../backend/app/stt/remote_whisper_client.py)、[TTS](../backend/app/tts/) |

Sessionの再送・重複検知履歴は有限です。スレッドを永続化できることは、接続を無期限維持できる保証ではありません。[連続操作試験](conversation-session-dev-operations.md)を参照してください。

## 記憶の種類と処理

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Working Memory / 作業記憶 | 現在の入力・目的・処理コンテキスト等。会話履歴や長期記憶の別名ではない | 概念契約：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Conversation History / 会話履歴 | 同一スレッドの連続性を保つ保存済みuser/assistant履歴。長期のpersona memoryとは別 | 実装：[conversation_history](../backend/app/conversation_history/) |
| Long-term Memory / 長期記憶 | 会話Sessionを超えて保持する記憶の総称。概念上はepisodic・semantic・reflective・proceduralを含むが、同じDB・tableを意味しない | 一部実装：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Persona Memory / 人格記憶 | キャラクターが保持する承認済み長期記憶。現行の`approved_memories`を中心とする。変動人格の数値や正確な業務記録ではない | 実装：[永続化モデル](../backend/app/memory/persistence/contracts.py)、[providers](../backend/app/memory/providers.py) |
| Episodic Memory / エピソード記憶、`EPISODIC_EVENT` | 具体的な出来事の記憶。現行のsubjectは`USER / SHARED`、event typeは`SHARED_MILESTONE / ACHIEVEMENT / DECISION / OUTCOME / CHANGE` | 実装：[admission型](../backend/app/memory/admission/contracts.py) |
| SELF Episode / 本人経験 | キャラクター自身の会話外活動・観測の記憶。活動handoffや実行ログを保存しただけではSELF Episode形成済みとはしない | 設計・未接続：[共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md)、[Life運用](character-life-operations.md) |
| Semantic Memory / 意味記憶 | 特定の一出来事ではなく、事実・安定した好み等。現行の直接形成型は`USER_PREFERENCE / INTERACTION_PREFERENCE` | 一部実装：[admission型](../backend/app/memory/admission/contracts.py)、[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Derived Semantic Memory / 派生意味記憶 | 複数Episodeから根拠付きで一般化した記憶。元Episodeの削除・置換ではない | 設計：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Reflection / 内省、Reflective Memory / 内省記憶 | 本人が経験をどう認知・意味付けしたかを形成する処理と、その永続的な結果。客観的な一般化を目指すSemantic Memoryとは別 | 設計：[用語ADR](decisions/memory-personality-terminology-2026-09.md)。通常会話のRAGへ原則直接注入しない |
| Memory Formation / 記憶形成 | 保存済み会話履歴から候補を抽出し、検証・保存する非同期処理。抽出成功だけでは保存承認ではない | 実装：[formation](../backend/app/memory/formation/)、[Wave 2契約](decisions/wave2-memory-formation-retrieval-2026-08.md) |
| Memory Consolidation / 記憶統合 | 既存記憶の整理・統合・置換・完全重複削除。同種記憶の整理であり、意味抽象化・内省・人格適応ではない | 実装：[consolidation](../backend/app/memory/consolidation/)、[永続化型](../backend/app/memory/persistence/contracts.py) |
| Semantic Abstraction / 意味抽象化 | 複数Episodeから派生意味記憶を形成する処理 | 設計：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Personality Adaptation / 人格適応 | Reflection群と根拠Episodeから長期傾向を評価し、変動人格を限定的に更新する処理。単一Episodeの直接加算ではない | 設計：[共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |
| Procedural Memory / 手続き記憶、Skill | 実行方法を保持する記憶。実行ログ・成功失敗・修正からのProcedural Learningで更新する。通常会話の一般RAGとは別 | 設計：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |
| Forgetting / 忘却 | 通常の忘却は検索・想起しにくさ（retrievability低下）として設計する概念。ユーザーによる物理削除や有効期限切れとは別 | 保留中の設計：[用語ADR](decisions/memory-personality-terminology-2026-09.md) |

## 保存・検索・privacy

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Candidate / 候補、Approved Memory / 承認済み記憶 | 候補はschema・evidence・privacy・policy等の検証前。承認済み記憶だけを正本へ保存・検索対象化する | 実装：[admission](../backend/app/memory/admission/)、[永続化型](../backend/app/memory/persistence/contracts.py) |
| RAG Admission / 保存受入、positive allowlist | 型付きの許可対象とprivacy評価を組み合わせる保存境界。「機微でない」だけで無制限に保存しない | 実装：[admission service](../backend/app/memory/admission_service.py)、[privacy契約](decisions/rag-memory-privacy-policy-2026-07.md) |
| Privacy scanner / assessment / sanitizer | scannerは検出、assessmentは文脈依存の分類、sanitizerは履歴本文の処理。最終的な保存判断とは責務を分ける | 実装：[privacy](../backend/app/privacy/)、[アーキテクチャ](system-architecture.md) |
| Source of Truth / 正本、Derived Index / 派生index | 承認済み長期記憶の正本はSQLite。Chromaは再構築可能な検索indexで、検索結果をSQLiteで再検証する | 実装：[RAG service](../backend/app/memory/rag_service.py)、[index sync](../backend/app/memory/index_sync.py) |
| RAG / Retrieval | 現在の入力に関連する許可済み記憶を検索し、回答のcontextへ渡す処理。別スレッドの生会話全文検索やLore照合とは別 | 実装：[RAG service](../backend/app/memory/rag_service.py) |
| Transactional Outbox / 記憶index outbox | SQLiteの記憶変更とindex同期要求を同じtransactionで記録し、Chromaへの反映を再試行する仕組み。音声transportの送信待ちoutboxとは別物 | 実装：[index outbox](../backend/app/memory/persistence/index_outbox_repository.py)、[index sync](../backend/app/memory/index_sync.py) |
| Provenance / 出典、Lineage / 派生関係 | 出典は元turn・記録等の由来。lineageは記憶間の統合・置換等の関係。画面由来turnのlineageは別領域の追跡情報 | 実装：[永続化型](../backend/app/memory/persistence/contracts.py)、[画面provenance](../backend/app/screen_perception/provenance.py) |
| `occurred_at / stated_at / created_at` | 出来事の発生時刻／根拠発言の時刻／登録時刻。過去の出来事を今聞いた場合も同じ時刻へまとめない | 実装：[永続化型](../backend/app/memory/persistence/contracts.py) |
| `experienced_at` | キャラクター本人が経験を得た時刻。発生時刻とは別の設計概念で、現行のApprovedMemory型には未追加 | 設計：[共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |
| Archive / Hard Delete / 失効 | Archiveはスレッドを通常利用から外す操作、Hard Deleteは対象の物理削除、失効は状態・期限による取得除外。スレッド削除で長期記憶は暗黙削除しない | 実装：[履歴](../backend/app/conversation_history/)、[記憶管理](../backend/app/routers/memory_management.py) |
| Addon Record / 業務記録 | 農業日誌等の正確なdomain record。Persona Memoryと分離し、暫定providerまたはaddon側が所有する | 基盤実装：[providers](../backend/app/memory/providers.py)、[Wave 2契約](decisions/wave2-memory-formation-retrieval-2026-08.md) |

## 推論・画面知覚・外部ツール

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Inference Target | Coreが指定する推論用途。現在は`chat / privacy / memory-extraction / memory-consolidation / embedding / vision / heavy-reasoning / tool-routing / character-life` | 実装：[Target型](../backend/app/inference/contracts.py) |
| Provider / Model / Adapter | 推論の接続先・使用モデル・接続先固有の実装境界。用途Targetへ`provider/model`を割り当てる。環境Profileや人格とは別 | 実装：[Inference運用](inference-operations.md)、[inference](../backend/app/inference/) |
| Inference Caller | 同じTargetを呼ぶ処理の識別。`screen-reference`はChat Targetのcallerであり、独立したTargetではない | 実装：[画面契約](decisions/browser-screen-perception-2026-09.md)、[アーキテクチャ](system-architecture.md) |
| Capability | Inferenceでは画像・構造化出力等の推論能力、MCPでは公開されるTool・Resource等。権限と能力は同義ではない | 実装：[Inference型](../backend/app/inference/contracts.py)、[MCP基盤](external-mcp-foundation.md) |
| Screen Session / lease / generation | 利用者が選択した共有対象と会話・同意・有効期限を結び付ける状態。永続スレッドや音声Sessionとは別 | 実装：[screen_perception](../backend/app/screen_perception/)、[capture](../frontend/src/lib/screen-perception/capture.ts) |
| `answer_without_screen / inspect_screen / clarify_reference` | 画面を見ず回答／必要な静止画を参照／参照対象を確認、の3分岐。LLM出力だけで画像送信や認可を決めない | 実装：[画面契約](decisions/browser-screen-perception-2026-09.md) |
| Vision observation / 画面由来lineage | Visionの構造化観測はrequest内の非信頼データ。生画像と観測は保存せず、privacy処理済み回答に由来を付けてMemory Formationから除外する | 実装：[画面provenance](../backend/app/screen_perception/provenance.py)、[アーキテクチャ](system-architecture.md) |
| MCP Connection / 接続 | 登録済み外部MCPの設定・認証・許可の単位。native Tool名だけでは接続先や実行権限を決めない | 実装：[MCP基盤](external-mcp-foundation.md)、[Addon管理](addon-admin.md) |
| Capability Snapshot / Tool Catalog | 接続から得たcapabilityの検証済みsnapshotと、選択に使う候補集合。取得したannotationをそのまま安全性の根拠にしない | 実装：[external_mcp](../backend/app/external_mcp/)、[会話利用](tool-use.md) |
| Execution Gate / 実行ゲート | 実行直前にsnapshot・接続世代・許可・schema・停止・予算等を検証する共通境界。会話や自律runtimeから直接MCP呼出しで迂回しない | 実装：[MCP基盤](external-mcp-foundation.md) |
| Binding / 対象拘束 | 呼出し先の対象を、登録済み設定や今回の会話で選んだ対象へ結び付ける制約 | 実装：[会話利用](tool-use.md)、[MCP基盤](external-mcp-foundation.md) |
| Egress Privacy Check / 外部送信検査 | 外部へ渡す最終引数等の検査。長期記憶への保存可否とは別の判断 | 実装：[addon_action](../backend/app/addon_action/)、[共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |
| Approval / 確認 / Result Recovery | 操作を許可する条件、今回の実行確認、不明な実行結果の回復を区別する。結果不明は成功や安全な再実行と同義ではない | 実装：[操作承認・回復契約](decisions/addon-action-approval-recovery-2026-09.md)、[受入](addon-action-185-acceptance.md)。Life接続範囲は別途確認 |

## 通知・非同期結果（設計）

以下の新しい名称は概念上の名前であり、実装クラス・API・DB列の存在を示しません。具体的な型と状態は#158／#363／#365等の実装時に対応づけます。

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Event / イベント、Event ingestion | 提供元の出来事とCoreへの取得・復旧。提供元が本文の正本を持ち、Coreの有限bufferや通知を正本の代わりにしない | 後続設計・#187：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| Notification / 通知 | ユーザーが出来事を確認するためのmetadataと出典参照。会話メッセージ・発話命令・Task状態そのものではない | 設計・#183／#158：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 通知Policy、`ignore / state-only / notify` | 無視／管理metadataだけ更新／通知を作成・更新、の分類。notifyだけでは会話取込・発話を許可しない | 設計・#158：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 登録時担当 / 担当キャラクター | 非同期処理を追加した時点の担当・呼出主体。依頼ユーザー、閲覧ユーザー、外部接続認証主体とは別で、画面切替に追従しない | 設計・#158／#363：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 登録時対象 / 対象設定revision | 接続・認証主体参照・binding・Task／ruleの対象と版。会話の対象変更とは独立し、開始済みTaskへ新設定を遡及しない。現在権限は取得時に再検証する | 設計・#158／#363：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| Origin / Target / Delivery policy | Originは依頼・設定元、Targetは結果の届け先、Policyは通知のみ等の配送方針。元の会話がない通知もあり、現在表示中の会話とは同義でない | 設計・#365：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 通知用read-only caller | 登録時担当で通知内容を安全に取得する処理。LLMのToolDecisionを必要としないが、共有Gate・認可・binding・snapshot・予算を通す。Inference Callerとは別 | 設計・#363：[Tool利用ADR](decisions/tool-use-foundation-2026-09.md)、[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 取得実行 / 継続的な予算集計単位 | 個々の短い取得と、Task／rule等に紐付く再試行を含む制限の範囲。元の会話loopを保持せず、新実行IDで制限をリセットしない | 設計・#363：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 内容取得済み / 既読 / 会話取込済み / 報告済み | 取得成功、ユーザー確認、会話文脈への採用、担当の報告turn保存を区別する。どれもTask取消しや音声再生完了と同義ではない | 設計・#191／#365：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| 報告起点 / 任意の自発発話候補 | 前者は元の依頼・結果を参照するCore認可済みの応答起点。後者は許可・idle・cooldown・TTL等で判断する任意候補。依頼報告を任意候補TTLで捨てない | 設計・#365／#189：[Session接続点](decisions/conversation-session-text-input-2026-09.md)、[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |
| コピー・会話引用の非伝播 | 明示共有された範囲だけを別キャラクターとの通常会話で扱い、元の非同期処理の担当・設定・状態へ会話や返答を伝えない。通知閲覧だけでは共有しない | 設計・#366：[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md) |

## Character Life・環境・運用

| 用語・実装名 | このリポジトリでの意味・区別 | 状態・参照 |
|---|---|---|
| Character Life Runtime / DBOS | 会話外の活動・queue・schedule・再開を扱う基盤。DBOSのcheckpointは記憶・人格等のdomain正本ではなく、外部副作用のexactly-once保証でもない | 実装・既定無効：[runtime](../backend/app/character_life/runtime.py)、[Life運用](character-life-operations.md) |
| Life State | 会話終了後も持続する中期の自己状態。`INTEREST / ONGOING_ACTIVITY / GOAL_INTENTION / IMPLEMENTATION_INTENTION / NEXT_ACTION_CANDIDATE / SHARE_CANDIDATE`を扱う | 基盤実装：[Life運用](character-life-operations.md)、[共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md) |
| Current Interests / 現在の興味関心 | 今何に注意・好奇心が向いているか。気質・変動人格・Reflectionそのものとは別。Reflection等からの自動形成は関連正本の接続を必要とする | 状態基盤は実装、全体接続は未完了：[用語ADR](decisions/memory-personality-terminology-2026-09.md)、[Life運用](character-life-operations.md) |
| Goal / Implementation Intention | 今後どうしたいかという目標意図／条件付きの実行意図。Interestがあるだけでは外部実行を許可しない | 基盤実装：[Life運用](character-life-operations.md) |
| Autonomy Grant | connection単位で与える自律活動の許可。会話でその接続を利用できることとは別 | 実装：[Life運用](character-life-operations.md) |
| Handoff / Share Candidate / `DEFERRED` | handoffは後続domainへの引渡し用作業記録、Share Candidateは共有候補、DEFERREDは保留。Life Stateの保存成功をEpisode・人格更新全体の成功とは扱わない | 実装：[Life運用](character-life-operations.md) |
| Environment Profile、`DS_PROFILE` | `dev / test-mocked / integration-text / integration-voice / dogfood`等のサービス構成を選ぶ入口。推論Targetとは別 | 実装：[開発環境](development-environment.md)、[dev Profile](../environments/profiles/dev.json) |
| Environment ID、`DS_ENVIRONMENT_ID` | `dev / test / dogfood`というデータの実行環境識別。Profile名と同じ集合ではない | 実装：[runtime paths](../backend/app/runtime_paths.py) |
| Runtime Data Root、`DS_DATA_DIR` | SQLite・Chroma・runtime report等の格納起点。環境identityを照合し、dogfoodはリポジトリ外の絶対パスを使う | 実装：[runtime paths](../backend/app/runtime_paths.py) |
| managed / external / in_process | 起動管理が所有する依存／起動済み外部依存／プロセス内依存。externalはアプリのstop対象ではない | 実装：[dev Profile](../environments/profiles/dev.json)、[開発環境](development-environment.md) |
| dogfood | 継続利用する運用相当環境。dev/testのデータ破棄・fixture・cleanupを適用しない | 運用契約：[dogfood手順](../infra/dogfood/README.md) |
| ADR / `ACTIVE` / `ARCHIVED` | 判断履歴／有効な設計判断／全面失効・統合済みの履歴。ACTIVEを機能の完成・有効化状態として使わない | 文書契約：[ADR案内](decisions/README.md) |

## 更新時の扱い

新しい概念、公開API・schema上の用語、既存語の意味・責務境界を変更するPRでは、本書の対応行と参照先を更新します。設計時に追加した「設計」項目も、mainへの実装・接続時に状態を見直します。新しい仕様判断はADRへ、未実装の作業はIssueへ残し、用語集だけで決定しません。
