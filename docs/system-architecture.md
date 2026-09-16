# システムアーキテクチャ

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 「AIRIの位置づけ」セクションは本転換に伴い失効しているため削除し、現行の自作構成の記述に置換した。
> 理由・経緯は `docs/decisions/` を参照。

本書は現在のコードの構成・責務境界を説明する。採用設計と実装範囲が異なる箇所は分けて記す。
用語は[用語集](glossary.md)、ADRの優先関係は[ADR案内](decisions/README.md)、起動・配備は
[開発環境](development-environment.md)と[dogfood運用](../infra/dogfood/README.md)を参照する。
`ACTIVE`なADRの存在を、その全体の実装完了・既定有効・dogfood受入済みとは扱わない。

## 基本思想

`digital-souls` では、AI人格の本体を「表示・配信システム」ではなく、「人格・記憶・判断・ツール実行」に置く。

現在はブラウザのテキスト・音声会話と静止画立ち絵を提供する。Live2D／VRM、追加クライアント、
Mac mini等への常時稼働環境移行は拡張方針として分ける。推論先は用途別Targetへ明示的に割り当て、
Windowsやcloudへの暗黙fallbackを前提にしない。

## 全体構成

```text
                  Browser UI（Svelte）
             テキスト・マイク・立ち絵・管理
                    │             │
                   HTTP       LiveKit media/control
                    └──────┬──────┘
                           ▼
                 FastAPI / Conversation Core
                    ├─ Character Card / Lore / PromptBuilder
                    ├─ Conversation History（SQLite）
                    ├─ Persona Memory（SQLite正本 → Chroma index）
                    ├─ Inference Router（用途別Target → Provider Adapter）
                    ├─ Screen Perception（必要なturnだけ）
                    ├─ MCP / Tool Use / Execution Gate / Addon管理
                    └─ Character Life / DBOS（任意有効化・dev/test）
                  音声認識: 共有Whisper HTTP service
                  音声合成: CCV選択のVOICEVOX / Irodori
```

通常の起動はEnvironment ProfileとDockerを使用する。Profileの`managed`は起動管理の所有対象、
`external`は起動済みサービスの再利用、`in_process`はプロセス内依存である。
現行のdev ProfileではBackend／Frontendがmanaged、Ollama／VOICEVOX／Whisper／LiveKitがexternal、
Chromaがin_processである。接続先とreadinessは[dev Profile](../environments/profiles/dev.json)を参照する。

#358作業ブランチのLiveKit入力はCore／private protocol 2.0を使用する。FEは新trackを通知し、
BEの入力開始ACK後にマイクを有効化する。発話境界と割り込み判断はBEが通知し、
FEは再生停止と実再生観測を担当する。FE／BEを一組で更新・切り戻す。
VAD推論失敗は発話を破棄して静音後に回復する。reset失敗・認可trackのreader終了は、
入力世代付きのエラーで当該マイクを停止し、新SIDによる明示再開を必要とする。
[移行契約](voice-backend-migration-contract.md)と[検証記録](validation/voice-backend-vad-358.md)を参照する。
実サービス・前後性能比較・人の実マイク受入は未完了である。

## 自作BE/FE構成

`digital-souls`のCoreは、自作BE（FastAPI）+ 自作FE（Vite + Svelte）で実装している。

### バックエンド（FastAPI, `backend/app/`）

* `routers/chat.py` — テキストチャットのHTTPエンドポイント
* `routers/ws.py` — 移行前baselineとして凍結するターン型音声WebSocketエンドポイント。Wave 3機能は追加しない
* `routers/livekit.py` / `livekit_transport/` — LiveKit join認証、Roomとsessionの対応付け、control event配送、character audio runtimeを担うWave 3の正式な音声transport境界
* `voice_input/` — LiveKitの連続PCMをCPUのSilero legacy／libfvadへ入力し、発話区間と正式utterance・入力世代をBEで確定する。モデル資産はhash固定、処理とbufferには上限を設ける
* `livekit_transport/microphone_frames.py` / `microphone_integrity.py` — SDK queue前のsample位置とtrack統計から欠落を確認し、終了済みcaptureを固定してSTTへ渡す
* `livekit_transport/paced_audio.py` — 応答ごとのPCM queueを最大1秒に制限し、入力のある10ms frameだけをbufferなしのnative AudioSourceへ供給する。cancel時はqueueと送信taskを止め、応答末尾は明示的にpaddingしてnative供給完了を待つ
* `livekit_transport/playback_completion.py` — 残りPCMの送出後、応答ID・最終sequenceが一致するブラウザの全出力確認を待つ。Coreの生成pipelineは`ResponseCompletionPort`を介して完了を待ち、その間もcancelできる
* `voice_metrics.py` — transport非依存のmetadata-only trace、集計artifact、保持、LiveKit受入目標判定
* `chat_service.py` / `_chat_runtime.py` — チャットセッションの生成・応答生成のエントリポイント
* `conversation_core/` — Speech/Text入力を共通の応答・中断・履歴処理へ接続する
* `routers/memory_management.py` — 長期記憶・暫定記録の閲覧、訂正、物理削除
* `addon_action/` / `addon_admin/` — 操作承認・確認・結果回復と、接続・credential等の管理
* `character_life/` — 会話外活動とLife State。関連する記憶・内省・人格等の正本との接続範囲は後述
* `characters/loader.py` — `characters/` 配下のCharacter Card V3を検証し、Character Core、Character Book、`extensions.digital_souls`を型付きで読み取る
* `characters/catalog.py` / `routers/character_catalog.py` — Character Cardの表示名と標準立ち絵metadataを再走査し、character境界を検証してPNGを配信する
* `conversation_history/` — `character_id`とUUIDv4の`conversation_id`を境界に、短期会話履歴、スレッド名、アーカイブ状態をSQLiteへ保存する
* `ui_settings/` / `routers/ui_settings.py` — 立ち絵layout、PC／compact別履歴範囲、キャラクター表示状態、キャラクター／スレッドpinをローカルユーザー単位で保存する
* `characters/lore_selector.py` — current userとprivacy処理済み履歴を対象に、Character Book Entryを決定論的に照合・選択する
* `prompting/` — Character Core、Character Lore、RAG、保存済み履歴、現在発言を順序とtoken budgetに従って合成する単一境界
* `inference/` — Coreの用途Targetを環境ローカルな`provider/model`へ解決し、text／画像Capability認可、画像decodeと入力上限、同時実行数、共通error、readiness、metadata観測を一元化する。optionalなVision Targetは構造化観測だけを返し、Ollama、OpenAI API、Codex runtimeの差とBase64化はAdapter内へ閉じ込める
* `screen_perception/` / `routers/screen_perception.py` — 有限leaseの共有session、generation、所有会話、routing同意を検証し、参照が確定したturnだけへ新しい静止画1枚を要求する。参照判断はruleを優先し、競合時だけ`screen-reference` callerでChat Targetを使う。Vision観測は現在request内の非信頼データであり、生画像とともに履歴へ保存しない
* `llm/` — 完成済みpromptをChat Targetへ接続する互換境界。ProviderやModelを直接選択せず、共通Inference Routerだけを呼び出す
* `memory/` — 会話履歴と長期記憶の基盤。SQLiteに同一conversation再開用の履歴と承認済み長期記憶を責務分離して保存し、Chromaは承認済み長期記憶だけの派生検索インデックスとして扱う。`memory_policy.py`は`backend/app/memory/memory_policy.json`の認識設定と、アプリケーションの非緩和policyを組み合わせて保存先別に判定する
* `stt/remote_whisper_client.py` — 共有GPU Whisper HTTP serviceによる音声認識。旧`whisper_client.py`はGoal 2受入までrollback用に保持する
* `model_settings.py` — Whisperモデル、履歴・入力・モデルcontext上限を型付きで解決する。InferenceのProvider、Model、入力／出力上限は`inference/config.py`がTarget単位で解決し、Backendはlifespanの先頭で検証する
* `tts/voicevox_client.py` / `tts/speech_synthesizer.py` — VOICEVOXによる音声合成
* `tts/irodori_client.py` — Irodoriの準備確認と区間単位HTTP合成。CCVをSession開始時に固定し、切断時にはHTTP待機を取り消す
* `audio/transport.py` / `audio_pipeline.py` — 音声フレームの送受信・パイプライン制御

リポジトリルートの `irodori_service/` はBackendから独立し、dogfoodが所有する共有GPUサービス。
固定revision、参照音声登録、実合成warmup、単一workerと待機列を管理する。
Irodori対応はepic実装であり、本採用・性能受入は#329の実測後に判断する。
通常のdev Profileへ必須依存を追加せず、`integration-irodori`で外部サービスの準備を確認する。
合成失敗時の応答終了、逐次区間送信、再接続は既存Conversation Core／LiveKit境界を使う。
詳細は[共有TTS運用](../infra/irodori/README.md)を参照する。

共有VOICEVOX clientは同期HTTP requestを合成全体30秒のdeadline内で実行する。process shutdownでは新規synthesis受付を止め、in-flight requestを既定35秒までdrainしてからclientをcloseする。防御的なdrain timeout時は本文を含まない理由コードを記録してshutdown処理を進めるが、in-flightより先にclientをcloseせず、最後のrequestが終了したthreadで遅延closeする。通常requestは全体30秒deadlineが35秒drainより短いため、このtimeoutはHTTP libraryがdeadlineに従わない異常の識別用である。synthesis lifecycleは`completed`、`request_timeout`、`connection_failed`、`request_failed`を本文なしで記録する。barge-inによるasync response cancelはConversation CoreのTTS stageへ`cancelled`として記録し、process shutdownと区別する。cancel後も同期requestが終了するまではclientを早期closeしない。

### フロントエンド（Vite + Svelte, `frontend/src/`）

* `lib/audio/transport.ts` — 移行前baseline用の `WebSocketAudioTransport`。Wave 3の正式経路には使用しない
* `livekit/` — LiveKit Room接続、microphone publish、応答IDを持つCharacter AudioTrack再生、旧応答trackの再開防止、control event、再接続を担うWave 3音声transport
* `lib/AudioRecorder.svelte` — LiveKitでは端末マイク取得・有効状態を扱い、VAD assetをロードしない。旧WebSocketのPCM録音・FE VADには`lib/audio/pcm-worklet-recorder.ts`／`vad-assets.ts`を保持する
* `lib/AudioRecorder.svelte` / `lib/AudioPlayer.svelte` — マイク入力UI・音声再生UI
* `lib/ChatWindow.svelte` / `lib/InputBar.svelte` — テキストチャットUI
* `lib/MemoryManagement.svelte` / `lib/AddonManagement.svelte` — 記憶管理、Addon接続管理・操作承認のUI
* `lib/ScreenCaptureControls.svelte` / `lib/screen-perception/` — サイドバーメニュー上の共有操作、標準picker、単一monitor／window／browser tabの検証、ローカルpreview、取得時だけの静止画化、共有sessionの失効を担う。サイドバーを閉じても選択中の共有対象を破棄せず、共有ONだけでは画像送信や定期解析を始めない
* `lib/ConversationSidebar.svelte` / `lib/sidebar/controller.ts` — キャラクター別スレッド一覧、設定、操作メニュー、desktop sidebar／compact drawerの状態を管理する
* `lib/CharacterPortrait.svelte` — catalogが返した標準立ち絵URLだけを表示し、未設定・読込失敗時は会話を止めず共通プレースホルダーへ切り替える
* `App.svelte` — テキスト／音声チャット、左サイドバー、立ち絵layoutを統合する。compact時はVisual Viewportへ追従して入力領域をソフトウェアキーボードの上へ保つ

テキストHTTPとLiveKit音声は同じConversation Coreの画面参照判断を使う。結果は
`answer_without_screen`、`inspect_screen`、`clarify_reference`の3分岐で、モデル出力を認可には使わない。
`inspect_screen`でもCoreが共有session、generation、client、character、conversation、routing revision、
画像／派生会話の同意を再検証する。privacy処理後の会話回答はSQLiteへ保存できるが、画面由来lineageを
turnに付けてMemory FormationとChroma投入から除外する。非表示のVision観測は次turnへ継承しない。

会話画面はPCで立ち絵の右配置と履歴背面配置を切り替える。タブレット・モバイルは履歴背面に
固定する。背面時の履歴は入力・音声操作を除いた会話領域の下端を基準に50%、75%、100%を
使用し、立ち絵の上へ透明な履歴領域と高不透明度のメッセージバブルを重ねる。

### 会話スレッドと実行Session

永続スレッドの`conversation_id`、実行中Conversation Sessionの`session_id`、保存Turnの`turn_id`、
音声入力の`utterance_id`、応答の`response_id`は異なる単位である。
同じ実行SessionへSpeech/Textを渡し、入力受理・応答生成・再生完了・履歴保存結果を区別する。
Frontendは選択スレッドに実行Sessionがあればそこへテキストを送り、別スレッドのテキストはHTTP経路へ送る。
`frontend/src/lib/conversation-session.ts`は選択済みスレッドIDの保存helperであり、共通実行clientとは別である。

入力欄focusによる音声入力抑止、利用者によるmute、テキスト送信等による応答中断、Session終了を区別する。
focusだけでは進行中の再生を止めない。詳細は[混在Session契約](decisions/conversation-session-text-input-2026-09.md)と
[共通client契約](decisions/conversation-session-client-2026-09.md)を参照する。
実装があることと遅延・再接続・連続運用の受入完了は別である。既知の制約と未解決事項は
[混在Session受入](conversation-session-acceptance.md)、[dev回復記録](conversation-session-dev-recovery.md)、
[連続操作試験](conversation-session-dev-operations.md)に残す。再送・重複検知履歴は有限であり、無期限Sessionを保証しない。

### Character CardとCharacter Lore

runtime人格定義の正本は`characters/{id}/{id}.card.json`である。Character Card V3の
`data.character_book`がない既存カードは、Character Loreなしとして従来どおり動作する。
Bookがある場合、共通ChatServiceはCharacter CoreとBookを同じload結果から取得する。
光織のCoreは性格中心で、記録・検索等を固定業務として与えない。判断は
[固定役割と人格の分離](decisions/miori-personality-without-fixed-role-2026-09.md)を参照する。

Lore照合はRAG検索と分離する。current userを先頭に、同じconversationから復元した
privacy処理済みuser／assistant messageを新しい順に`scan_depth`件だけ走査する。literal照合は
message境界を越えず、NFKC正規化後に既定でcase-insensitiveとする。MVPで未対応のregexと
Decoratorを含むEntryは、カード上の値を保持したままruntime注入しない。
`recursive_scanning=true`でもliteral／constantによる初期matchingは行うが、選択済みLore本文を
新しいscan sourceとする再帰scanは行わない。

最終promptの順序は次で固定する。

```text
before_char Character Lore
Character Core
after_char Character Lore
RAG required instruction / RAG Memory
Conversation History
post_history_instructions
Current User
```

Character LoreはEntry単位で扱い、本文を途中で切らない。Book固有の`token_budget`と
PromptBuilderのLore領域上限では、priority、insertion order、カード内indexから作る同じ
決定論的removal keyで低優先Entryから除外する。prompt全体上限では、RAG Memory、古い履歴、
低優先Lore、`post_history_instructions`の順に任意領域を削減する。LoreをRAGへ登録せず、
RAGの`memory_reference`も付けない。詳細な契約は
`docs/decisions/character-book-runtime-2026-08.md`を正本とする。

## 現在の表示と将来の配信拡張

現在のruntime表示はブラウザの静止画立ち絵である。以下のLive2D／VRM連携は拡張方針・検討候補であり、
現行の統合済み機能や採用済みアプリケーション一覧ではない。

### 拡張方針

* Live2Dを標準の姿とする
* パーソナルAI用途では静止画UIも許容する
* VRMは配信時や3D表現が必要な場合のみ利用する

### Live2D

候補:

* VTube Studio
* OBS連携
* 将来的なAPI制御

### VRM

候補:

* 3tene
* Warudo
* VNyan
* VSeeFace
* Unity + UniVRM
* Three.js + three-vrm

VRMは常用ではなく、配信・イベント用の身体として扱う。

## 推論ルーター

Coreは`chat`、`privacy`、`memory-extraction`、`memory-consolidation`、`embedding`、`vision`、`heavy-reasoning`、`tool-routing`、`character-life`の用途Targetを指定する。型の正本は[`inference/contracts.py`](../backend/app/inference/contracts.py)である。環境は各Targetへ`provider/model`を直接割り当て、Provider RegistryがOllama、OpenAI API、Codex runtimeのAdapterを選ぶ。独立したInference Profileや暗黙fallbackは持たない。

`privacy`と`character-life`はローカルProviderに限定する。cloud利用が可能なTargetへcloud Providerを割り当てた場合は起動時にwarningを記録する。起動時には静的設定を検証した後、生成を伴わないProvider別probeを行う。`chat`の利用不能は起動失敗、他の設定済みTargetは`degraded`として起動を継続する。

`/health/ready`は必須Targetのaggregateだけを返し、`/health/inference`はTarget別状態と共通error categoryだけを返す。Provider、Model、endpoint、認証状態は公開しない。Provider／Modelを含む運用metadataはprompt／response本文を除外した構造化logに記録する。設定、認証、実接続受入の手順は[`inference-operations.md`](inference-operations.md)を参照する。

## 音声処理設計

旧WebSocket baselineとLiveKit Conversation Coreは、`WHISPER_BASE_URL`で解決した同じ共有GPU Whisper serviceへ音声を送る。BackendはGPU modelを所有しない。共有serviceが単一workerとglobal single-flightを所有し、競合requestは待機させずcapacity超過として返す。推論timeoutではworker/modelをprocessごと破棄し、次requestで再生成する。失敗理由はpayloadを含まないcodeだけで記録する。

想定同時接続ユーザー数は3程度とし、この前提で単一workerによるスループット低下を許容する。

同時接続ユーザー数が増加した場合は、モデルインスタンスをプール化する設計への切り替えを再検討する。

## 記憶・ツール設計

### 会話履歴とRAG長期記憶

既存の非同期形成経路の許可型は`EPISODIC_EVENT / USER_PREFERENCE / INTERACTION_PREFERENCE`である。
Episodic subjectは`USER / SHARED`で、SELF、`experienced_at`、派生意味記憶、独立した内省記憶、人格適応は
採用済みの拡張設計と現在の型を区別する。[admission型](../backend/app/memory/admission/contracts.py)、
[永続化型](../backend/app/memory/persistence/contracts.py)、[用語集](glossary.md)を参照する。
既存memoryのconsolidationは実装済みだが、意味抽象化・内省・人格更新を行う処理ではない。

UI上のスレッドはBackendの`conversation_id`に対応する。同じ`character_id`と
`conversation_id`の履歴だけを復元し、別conversationの生会話は検索しない。

会話履歴を短期記憶、`approved_memories`を人格の長期記憶として扱う。conversation由来の
長期記憶は保存済み会話履歴からだけ形成する。農業日誌やレシピ等の正確なdomain recordは
人格記憶へ混在させず、暫定providerまたはaddon DBが所有する。

```text
受信した会話
  └─ Wave 1: 共通の決定論的privacy scanner
       ├─ 現在ターンの応答生成（原文は処理中だけ利用）
       └─ 履歴用policy + assistant応答のsanitizer
            ├─ SKIP_CONTENT / privacy_skipped
            └─ MASK / STORE
                 └─ SQLite: completedなconversation_turns
                      └─ 非同期の長期記憶形成
                           └─ Wave 2: 文脈依存PrivacyAssessment
                                └─ RAG admission policy
                                     └─ ALLOW_STRUCTURED
                                          └─ SQLite: approved_memories + memory_index_outbox
                                               └─ Chroma: 承認済み記憶の派生index
```

共通privacy scannerは保存先を決めず、カテゴリ、原文上の半開区間、reason code、version、
保存拒否scopeを型付きfindingとして処理中だけ返す。公開interfaceは`ScanSuccess`または
metadata-onlyの`ScanFailure`を返す。NFKC等の認識用viewと原文spanの対応はscanner内部だけで
保持し、MVPは日本と米国の固定corpusから開始する。履歴用policyは、APIキー、password、秘密鍵、決済認証、
口座番号、政府ID、私用連絡先、正確な住所等の値をマスクし、明示的な履歴非保存要求または
安全にマスクできない場合は本文を破棄する。health、心理状態、金融状況、第三者情報等の話題は
同一conversationの履歴として保持できるが、MVPではRAG長期記憶へ昇格させない。
userとassistantの双方へ同じscannerとsanitizerを適用し、原文、検出値、マスク前本文を
SQLiteやapplication logへ残さない。

保存拒否findingはMVPでは`RAG`または`BOTH`のscopeを持ち、current userのcurrent turnだけへ
適用する。「履歴に残さないで」は履歴だけでなくRAG記憶形成も拒否する`BOTH`として扱う。
assistant側で`SKIP_CONTENT`になった場合は、保存済みuser本文も同一transactionで消去し、
turn全体を`privacy_skipped`へ遷移する。

文脈依存`PrivacyAssessment`はWave 2でhealth、心理状態、自傷、虐待・性的被害、金融状況、
第三者の非公開情報、暗示的な機微情報を分類する。classifierは保存可否を返さず、
RAG admission evaluatorだけが決定論的findingとassessmentから保存可否を決める。

conversationのアーカイブは履歴をSQLiteへ保持したまま通常一覧、prompt注入、追記対象から
除外する。物理削除はconversationとturnをSQLiteからhard deleteし、RAG長期記憶は暗黙削除しない。

会話履歴DBの版の正本は[`conversation_history/schema.py`](../backend/app/conversation_history/schema.py)の`SCHEMA_VERSION`を参照する。SQLiteを正本、Chromaを再構築可能な派生indexとし、
backup artifactにはSQLiteと検証用JSONだけを含める。WAL稼働中のbackupはSQLite公式backup APIで
整合snapshotを作成する。restoreはchecksum、schema、environment identityを切替前に検証し、
検証済みstaging SQLiteを単一のatomic置換で切り替える。通常の手動restoreでは、切替前の検証・
置換失敗時に既存DBを維持し、自動rollbackは行わない。dogfood起動時のschema migration失敗では
直前に作成・検証したbackupへ自動rollbackし、rollback自体も失敗した場合はmigrationとrollbackの
両方の失敗を保持して起動を中止する。その他の復旧操作は`infra/dogfood/README.md`の手動restore
手順に従う。
dogfoodのdeployとschema migrationは
事前backupの成功を後続処理の開始条件とする。操作手順とIssue #56のrestore drillは
`infra/dogfood/README.md`を正本とする。

RAG長期記憶はpositive allowlist方式とし、allowlistを保存同意として扱う。許可型へ正規化され、
機微情報検査を通過し、current turnに保存拒否がない`ApprovedMemoryCandidate`だけをSQLiteへ
自動保存する。候補ごとの確認と保存通知は行わない。SQLiteを正本、Chromaを派生indexとし、
conversation由来の候補は元turnの履歴本文が保存済みの場合だけ長期記憶へ形成する。
SQLiteへの承認済み記憶保存とoutbox作成を同一transactionで行う。Chroma登録失敗時は本文を
別ファイルへ退避せず、outboxの`memory_id`でSQLiteの承認済み記憶を再読して冪等に再試行する。

長期記憶の訂正はSQLite正本の更新、失効は`expires_at`／状態による取得除外、ユーザー削除は
`approved_memories`行のhard deleteとして区別する。hard deleteでは`character_id`と`memory_id`を
持つmetadata-onlyの`DELETE` outboxを同一transactionで作成し、削除済みSQLite本文を再読せず
Chromaから冪等に削除する。SQLite commit後の同じ削除操作でChroma deleteを同期試行し、
失敗時はoutbox retryで回復する。さらに定期reconciliationでSQLiteに存在しないChroma orphanを
削除し、欠落entryとmetadata不一致をSQLite正本から修復する。

検索時はChromaの結果をそのままpromptへ渡さず、`memory_id`をSQLiteで引き直し、
`character_id`、状態、TTL、policy versionを確認する。さらにSQLiteの`normalized_text`へ
共通の決定論的絶対禁止scannerを再適用し、検出した記憶をpromptへ渡さない。

current user queryに絶対禁止finding、意味分類の`SENSITIVE`／`ABSTAIN`、または判定障害がある場合は
RAG検索自体をskipし、RAGなしで会話を続ける。検索順位は意味的関連度を主とし、関連度が同等の
候補間だけ`last_user_mentioned_at`をtie-breakに使う。検索やassistantの言及では同日時を更新しない。

詳細な不変条件とMVP境界は
`docs/decisions/rag-memory-privacy-policy-2026-07.md`および
`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`を参照する。

promptへcontextを供給する境界は次に分ける。

```text
ContextProvider
├─ ConversationHistoryProvider
├─ PersonaMemoryProvider
└─ AddonRecordProvider
```

初期domain provider候補:

- `core`: 人格記憶
- `temporary:agriculture`: addon完成前の農業記録
- `temporary:recipe`: addon完成前のレシピ記録

記憶は人格ごとに分離できるようにする。

```text
characters/
└─ miori/
   ├─ miori.card.json  # runtime人格定義のSource of Truth
   ├─ personality.md   # 人格設計の編集資料（runtimeでは未使用）
   ├─ world.md
   └─ memory-policy.md  # 方針本文と実装設定への案内
```

現行の記憶・記録モデルは`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`、
RAG privacyの不変条件は`docs/decisions/rag-memory-privacy-policy-2026-07.md`で管理する。
`docs/decisions/archive/miori-memory-policy-2026-06.md`は初期検討の履歴ADRとして保持する。
`backend/app/memory/memory_policy.json`は認識語彙・pattern・閾値・追加禁止設定の実行時Source of
Truthとするが、ADRとtyped policy schemaが定める絶対禁止を削除・許可へ反転できない。

### Episode / Factの保存・登録・管理基盤

`memory/episodic/`はEpisode（所有キャラクターの経験）とFact（話題の情報）を、
`persona-memory.db`の`episodic_records`へ独立したIDで保存する。
`episodic_versions`に内容版と根拠、`episodic_links`にEpisode–Fact参照、
`episodic_merges`にFact間の版付き統合関係を保持する。既存の`approved_memories`とは別の正本である。
内容版の識別子・出典・形成設定はDB制約で不変とし、本文の消去とその再試行だけを許可する。
Persona Memory schema v5への移行では既存の版を保全し、v3・v4のバックアップも検証・復元対象として保持する。

登録サービスは元発言ID・source revision・引用範囲とprivacyを検証し、同じcharacter・threadでの
Fact照合、明確な補足訂正、取得経緯の追加、冪等登録を扱う。曖昧な対象は推測更新しない。
Episodeの経験と話題のFactの5Wを分け、後日の語り直しをFactの内容更新と混同しない。
別threadのFact統合、派生Semantic / Reflectionへの昇格は行わない。

検索用投影はChromaへ同期し、検索時にはEpisode / Factの版・有効な出典・参照・統合先を
SQLiteで再確認する。回答が参照したID・版の記録を使い、管理訂正・削除で旧版からの派生を無効化する。
`routers/episodic_memories.py`と`EpisodicMemoryManagement.svelte`は監査とFact単位の訂正・削除を提供する。

会話スレッド全体の新モデルへの抽出は、永続予約を処理する非同期workerへ接続する。
反復18を採用したv13-compact18がFact操作・Episode境界・内容確認を段階的に判断し、
ID・引用・参照リンクはコードで組み立てて通常の登録検証へ渡す。入力予算超過時は分割・全catalog照合を行う。
HTTP/旧WebSocketとSpeech/LiveKitはいずれも、最終promptが参照した記憶の版を履歴確定前に記録する。
中断応答の記録を次の応答へ流用せず、参照記録が失敗した応答は完了履歴にしない。
この接続実装・固定品質評価と、実LLMを含む全経路受入は区別する。
契約は[Episode / Fact境界ADR](decisions/episode-fact-semantic-boundaries-2026-09.md)、
条件は[要件・受入](epic-340-episodic-memory-requirements.md)と#291 / #344を参照する。

## LiveKitトランスポート

`backend/app/livekit_transport/`がRoomとsessionの対応付け、メモリ上のoutbox、ACK/retry、generation、transport私有mappingを所有する。Conversation CoreはLiveKitのRoom、Participant、Trackを知らず、検証済みCore eventとtransport available/unavailableのみを観測する。詳細は`docs/decisions/livekit-transport-2026-08.md`を参照する。

LiveKitは`frontend/src/App.svelte`の通常会話UIへ統合済みで、現在の正式な音声経路である。
#113の専用画面は基盤検証の経緯であり、通常利用で別の検証画面へ移動する前提にはしない。
既存WebSocket音声pipelineを新機能へ拡張してから切り替える二段階実装は行わない。

## 外部MCP接続・実行基盤

`backend/app/external_mcp/`はnative capabilityと検証済みstaged/active snapshot、登録済みconnectionの
trust・sharing・binding検証、Execution Gateを所有する。公式SDKのstdio/Streamable HTTP Clientは
通信とnative結果を扱い、Toolの安全性や会話routingを判断しない。

Gateはloop開始時のsnapshotと接続世代を固定し、実行直前にgrant、入力schema、停止、予算を検証する。
未信頼annotationはunknownとして直列・retryなしにし、実効read-onlyだけ並列・最大1 retryを許可する。
Resourcesをnativeに読み取り、Promptsはdiscoveryまでに限定する。入力待ちは上位判断へ返し、
回答後も同じloopと許可で再実行する。self-owned runtimeは#221が担当する。
設定・公開Python API・制限は[外部MCP利用基盤](external-mcp-foundation.md)を参照する。

管理UIはExternal MCPのHTTP/stdio設定と接続専有credentialを管理する。接続・credentialは
専用SQLiteで永続化し、同一transactionで削除する。希望ON/OFF、runtime availability、
現在設定の成功履歴を独立させ、設定revisionと実行世代で古い確認結果を破棄する。
接続確認は管理sessionのdiscoveryを再利用し、Tool実行を伴わない。
操作・移行手順は[Addon/連携の管理](addon-admin.md)、実接続証跡は[#242受入](mcp-admin-242-acceptance.md)を参照する。

`backend/app/tool_use/`はこのGateをテキスト会話とLiveKit音声で共有する。管理設定から接続し、
loopに固定した候補を資格・関連性・schema容量で絞り、専用`tool-routing` Targetで選択する。
元schemaで引数を検証し、会話内の対象選択を呼出しごとのbindingとしてGateへ渡す。
結果は秘密情報・生エラーを除いた非信頼データとして現在turnの最終回答へ統合する。
履歴・Memory Formationには既存privacy方針を通った通常の会話だけが渡り、native payloadは渡らない。

MRTRの追加情報は既存contextで補える場合に再開し、不足時は通常の入力欄・音声で質問する。
入力待ちは同じsnapshot・grant・budget・bindingを最大10分保持する。停止・会話切替・音声切断で破棄し、
回答中のbarge-inでは古い音声を止めつつ入力待ちを保つ。Target未設定なら通常会話を維持する。
設定、停止の意味、検証入口は[会話からの外部MCP利用](tool-use.md)を参照する。

`backend/app/addon_action/`は操作承認・実行確認・結果回復を扱う。管理UIでは接続や操作群、実行場面に応じた
許可と今回の確認を区別する。現在の契約と検証は[承認・回復ADR](decisions/addon-action-approval-recovery-2026-09.md)、
[#185受入](addon-action-185-acceptance.md)、[承認管理UI受入](addon-approval-admin-305-acceptance.md)を参照する。
この基盤が存在することと、Character Lifeへ高影響操作・副作用回復が接続済みであることは別である。

## 通知とキャラクター会話の分離（後続設計）

以下は#183で整理し#187のEpicへ引き継いだ後続設計であり、上記の現行構成に通知タブ・通知用caller・
新しい応答起点が実装済みであることを意味しない。詳細は
[通知／会話分離ADR](decisions/notification-conversation-separation-2026-09.md)を正本とする。

```text
提供元のEvent・Task結果
  → #187 取り込み・復旧・有限buffer
  → #183 通知Policy・metadata・未読管理・安全な詳細参照
       ├─ 会話・LLMなしで使える通知タブ
       └─ 通知ID・登録時担当・出典・発生元参照
            → #364 会話への関連付け・認可済み報告起点
                 → 既存Conversation Core・履歴・必要時のTTS
```

通知の正規化・保存・確認と、キャラクターの文脈・報告生成を分ける。同一Backendの責務分離であり、
別サービスや並列人格を追加する要求ではない。#187のsource取得は共有し、会話archive／削除で
他consumer向けの取り込みを止めない。通知消費・既読・会話取込・報告済み・再生完了は独立した状態とする。

| 保存・処理対象 | 責務 |
|---|---|
| Event本文・Task状態・結果／成果物 | 提供元のdomain正本。Coreへ無条件複製しない |
| 通知metadata・出典・未読状態 | #183。集約せず個別保存し、固定文言と許可metadataを用いる。LLMを直接呼ばない |
| 会話への関連通知・依頼相関・報告状態 | #364。元の依頼と同じ担当の届け先を使い、表示中の別会話へ混ぜない。取得に必要な依頼参照を通知削除へ連動させない |
| キャラクターの報告文 | 既存の会話privacy・記憶policyを通した会話メッセージ |

非同期処理を追加した時点の担当キャラクター・接続・対象を保持し、取得時には現在権限・binding・
snapshotを再検証する。#363はLLMを介さなくても共有Gateを通り、元の会話loopを保持せず有限の取得を行う。
実行IDを新しくしてもTask／rule等の継続的な予算をリセットしない。監視の最新状態と特定Task結果を分け、
提供元で削除・期限切れになった結果を推測や別実行で補完しない。

#365はSpeech／Textとは別のCore認可済み報告起点を既存会話契約へ追加する設計とする。
架空のユーザー発言・utteranceを作らず、具体的なschema・protocol・API・生成型は実装と同期して変更する。
自動報告・自発発話は対象会話のactive／foreground／lease／idleを検証し、進行中の発話を中断しない。
依頼への報告を任意の自発発話候補のTTLで破棄しない。

通知の閲覧だけでは別キャラクターへ内容を渡さない。ユーザーがコピー・会話引用で明示共有した範囲は
通常会話の話題として扱い、その会話・回答・記憶を元の非同期処理の担当・対象・監視・実行・報告状態へ
伝播させない。通知からの会話導線は登録時担当へ戻す。用語の対応は[用語集](glossary.md)を参照する。

Event取得・復旧の詳細は[Event復旧ADR](decisions/addon-event-recovery-2026-09.md)と
[要件指示書](epic-187-addon-event-requirements.md)を参照する。取得位置と有限sanitized bufferの永続化、
consumerごとの欠落復旧、画面オフラインと購読解除の区別は、Epic #187のEventRuntime / EventStoreに実装した。
Epic #183は共有Eventを独立購読するNotificationRuntime / NotificationStoreと、
共有Gateで有限取得するNotificationReader、会話と独立したNotificationCenterへ接続する。
個別保存・ユーザーごとの通知設定・初回保存から30日と件数上限・独立参照は[通知runtime](notification-runtime.md)を参照する。
既存ToolRuntimeの共有Gateとライフサイクルへ接続し、MCP標準Tool/Resourceを使う。
設定・consumer API・公開結果契約・検証入口は[Event runtime](addon-event-runtime.md)を参照する。
通知の個別保存・保持・設定・削除後の再取得は通知／会話分離ADRの2026-09-16追記と[通知要件](epic-183-notification-requirements.md)で定め、後続consumerで実装する。

## Character Life Runtime

`backend/app/character_life/`が会話外の実行を所有する。FastAPI lifespanでDBOSを一度だけ起動し、
会話と同じToolRuntimeのRegistry・Execution Gate・BindingResolverを使用する。
`DS_CHARACTER_LIFE_ENABLED`は既定falseで、今回の受入対象はdev/testである。

DBOSはqueue・UTC schedule・再開・復旧を担当し、引数は参照IDと実行世代に限定する。
`character-life.db`は6種類のLife State、revision履歴、Autonomy Grant、活動・内省形成の結果、監査を保持する。
承認済みの観測handoffを関連domainへの呼出し前に固定し、再起動しても同じ時刻・本文・source・冪等keyで再送する。
この作業記録は#100のSELF Episode正本ではない。共有候補と活動完了は同じtransactionで確定する。

Interestだけでは開始せず、Goal Intentionまたは利用者が要求したImplementation Intentionとconnection単位のGrantを必要とする。
元Reflectionのrevisionを採用直前に照合し、訂正・非公開化した派生状態を休眠化する。
正本の照合不能時は派生状態を会話・活動に使用しない。状態履歴とprovenanceは保持する。
Resource読取とTool呼出しはいずれもGateへ渡し、Binding制約を適用した最終引数へEgress判定を行う。
高影響確認・副作用回復の接続前は、未分類の操作を`action_recovery_unavailable`で保留する。

認知の前後とdispatch直前にforegroundを確認し、DBOS queueでは利用者要求を自律活動より優先する。
同じGoalへの未完了の自律要求と同じcharacterへの未完了の内省形成を重ねない。活動queueはcharacterごとに100件まで受け付ける。
停止時は進行中の結果を破棄し保留を確定してからDBOS・MCPを終了する。
`_chat_runtime`は応答開始時の有効なLife Stateを非信頼データとして固定し、入力budgetに収まる分だけ参照する。

Character Life基盤は、関連Epic #100/#101/#102のdomain正本実装や、#185の高影響確認・副作用回復との接続までを含まない。
#185自体の操作承認・回復基盤は前節に記載した範囲で存在する。Life側の未接続結果はDEFERREDとして表示する。
Reflectionの取得元が未接続の場合も、内省形成・人格更新が成功したと扱わない。
設定・API・保存・復旧・実接続検証は[Character Life運用手順](character-life-operations.md)を参照する。
