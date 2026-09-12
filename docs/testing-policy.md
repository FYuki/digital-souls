# テスト方針

本書は `digital-souls` 固有のテスト層、配置、実行方法、実行証跡を定める。

## テスト層

| 層 | 配置・命名 | 外部サービス |
|---|---|---|
| 単体テスト | Backend: `backend/tests/unit/`、Frontend: `*.unit.test.ts` | モック可 |
| 結合テスト | Backend: `backend/tests/module/`、Frontend: `*.module.test.ts` | 境界をモック可 |
| モックE2E | `frontend/e2e/` | Browser 内 Backend をモック |
| インテグレーションテスト | Backend: `backend/tests/integration/test_*_integration.py`、Frontend: `frontend/integration/` | 実接続必須 |

「結合テスト」はアプリ内のモジュール横断を検証する。「インテグレーションテスト」は Ollama、ChromaDB、VOICEVOX、Whisper などの外部サービスへ実接続するテストだけを指す。Backend では ChromaDB と Ollama の実埋め込み API を使う RAG runtime evidence テストをインテグレーションテストとして実行する。

モックを使用する単体・結合・E2Eテストの結果は、外部サービスとの実接続に成功した一次証跡として扱わない。

画面知覚では、unit／moduleで参照3分岐、session・generation・同意、画像上限、Vision観測、
provenance、Memory Formation除外を検証する。`frontend/e2e/screen-perception.spec.ts`は通常UIの
共有ON、文脈参照、明示参照、OFF、再読込、monitor／window／browser surface、静止windowでframe callbackが来ない場合の現在frameフォールバックを合成MediaStreamとbrowser内Backendで検証するが、標準picker、
OSが供給する実フレーム、Chrome／Edge差、実Vision品質の代用にはしない。実機と実モデルの完了条件は
[`screen-perception-browser-acceptance.md`](screen-perception-browser-acceptance.md)に従う。
`frontend/integration/text/screen-perception.spec.ts`は合成browser streamを起点に、実Backendのsession・画像受付、実Ollama Vision、実Chat、会話履歴までを通す。標準pickerとOS frameだけはこの統合試験でもmockのため、Windows実機確認を省略しない。

## dogfood環境との分離

unit、module、E2E、integrationの全テストはdev／test専用Profileとdata rootを使用し、
dogfoodのFrontend、Backend、SQLite、Chromaをテスト対象またはfixtureとして使用しない。

- dogfoodのFrontend `:15173`、Backend `:18000`、ready gate `:14174`へ接続しない
- `DS_ENVIRONMENT_ID=dogfood`またはdogfood identity markerを持つdata rootではテストを開始しない
- テストsetup、DB再作成、cleanup、Playwright teardownからdogfood data rootを操作しない
- Ollama／VOICEVOX／Whisperを共通推論サービスとして実接続する場合も、テストrunはそのprocessを所有・停止しない
- 実接続証跡へdogfoodの会話本文、prompt、SQLite row、Chroma documentを複製しない

pytest fixtureは各テストの一時ディレクトリへ`DS_ENVIRONMENT_ID=test`と`DS_DATA_DIR`を設定し、
identity markerを初期化する。Playwrightもスイートごとに独立したtest data rootを使い、環境reportは
その`runtime/standalone/`配下へ限定する。fixture開始時にdogfood環境IDを検出した場合はfail closedする。

Issue #56で、dogfood稼働中のintegration testとcleanupを含む横断受入を実施する。
環境分離の正本は`docs/decisions/local-dogfood-environment-2026-08.md`とする。

## Playwright スイート

| コマンド | 配置 | Profile | 要求Capability | 結果ディレクトリ |
|---|---|---|---|---|
| `npm run test:e2e:mocked` | `frontend/e2e/` | `test-mocked` | `mocked-e2e` | `frontend/test-results/mocked-e2e/` |
| `npm run test:integration:text` | `frontend/integration/text/` | `integration-text` | `text-chat-real` | `frontend/test-results/integration-text/` |
| `npm run test:integration:voice` | `frontend/integration/voice/` | `integration-voice` | `voice-chat-real` | `frontend/test-results/integration-voice/` |
| `npm run test:integration:livekit` | `frontend/integration/livekit/` | `livekit/chromium` | マイク権限・実LiveKit接続 | `frontend/test-results/` |

各設定は Profile、収集ディレクトリ、成果物の出力先を固定する。spec 内で環境変数や依存 mode によってモックと実接続を切り替えない。各 spec が受け入れる要求Capabilityは1つだけとする。実接続 spec では mock WebSocket、`page.route`、HARによる外部通信の置換を禁止する。

Epic #151のresponsive会話画面は`frontend/e2e/portrait-layout.spec.ts`でPC、tablet、mobile、
Visual Viewport縮小、立ち絵未設定・取得失敗を検証する。PC右配置、PC背面配置、tablet、mobileの
画面はPlaywright attachmentとして各テスト結果へ保存する。手動受け入れ手順と証跡名は
[`epic-151-acceptance.md`](epic-151-acceptance.md)を正本とする。

`voice-chat-real`はOllama、Whisper、VOICEVOXに加えてLiveKitのreadinessを要求する。`npm run test:integration:voice`の実行時は、Profileへ秘密値を保存せず、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`を実行環境からBackendへ渡す。

## 実行入口

リポジトリルートから次を実行する。

```text
npm run test:unit
npm run test:module
npm run test:integration:backend
npm run test:integration:inference
npm run test:integration:screen-vision
npm run test:e2e:mocked
npm run test:integration:text
npm run test:integration:voice
npm run test:integration:livekit
npm run baseline:websocket
npm run eval:screen-reference:conformance
npm run lint:python
npm run check
npm run build
```

`npm run test:integration:backend` は ChromaDB パッケージ、Ollama、`nomic-embed-text:latest` モデルを必要とする。
`npm run test:integration:inference`は`RUN_INFERENCE_REAL_SERVICE_TESTS=true`を明示した場合だけ実行し、`INFERENCE_ACCEPTANCE_PROVIDER`で1つのProviderを選ぶ。通常CIではskipし、#181完了時はOllama、OpenAI API、Codex runtimeを個別に実行する。証跡の正本は#181のIssueコメントとし、実行方法と記載項目は[`inference-operations.md`](inference-operations.md)に従う。
音声品質の指標、clock、artifact schema、WebSocket baseline条件は
[`voice-quality-measurement.md`](voice-quality-measurement.md) を参照する。

CI は単体テスト、結合テスト、モックE2E、型チェック、Frontend build、Compose契約検証、Backend／Frontend image buildを実行する。別workflowはmainと`epic/**`へのpushでBackend／Frontend／Whisper imageをGHCRへcommit SHA tagで公開しdigestを記録する。実GPU・実接続スイートは外部サービスを必要とするため自動実行せず、Pull Request の検証欄へローカル実行結果または未実行状態を記録する。

Backendの音声品質テストは、保存済みartifactの測定版を`git show`で照合し、Python CLIからTypeScriptの音声観測replayを実行する。
そのためBackend CIもGit履歴全体を取得し、Node.js 22と`npm ci --prefix frontend`で固定されたreplay依存を準備する。履歴・依存の不足をテストのskipや監査省略で回避しない。

Issue #135 Goal 1ではremote client、single-flight、capacity超過、timeout、worker再生成、Profile、Compose、deploy／rollbackをfakeまたはCPU不要の自動テストで検証する。RTX 4070 Ti SUPER上のCUDA／VRAM証跡、dev・dogfood同時会話、連続会話品質、WSL再起動復旧はGoal 2の手動受入とし、Goal 1の成功を実GPU受入済みとは扱わない。

Issue `#146`の回帰テストは新規backup pathの検証、検証失敗時の更新停止、既知の汚染manifestの読込、
保持期限でbackupが削除された世代へのrollback、旧履歴の不変性を一時環境で確認する。
2026-09-06決定により、#135は残実装のmain取り込み時にcloseし、dogfoodのbackup／restore・
失敗時rollbackの実機受入は別タスクとして扱う。手順は`infra/dogfood/README.md`に従う。

## Pull Requestレビュー

Pull RequestではGitHub ActionsのCIに加え、GitHub Appとして導入したCodeRabbitによる自動レビューを実行する。レビュー設定の正本はリポジトリルートの`.coderabbit.yaml`とし、`AGENTS.md`および同設定の`knowledge_base.code_guidelines.filePatterns`に登録した規約・設計文書をレビュー基準として使用する。

有効化時はリポジトリ管理者がCodeRabbitのGitHub Appに当該リポジトリへのアクセスを許可する。CodeRabbitはPull RequestイベントをGitHub Appとして処理するため、GitHub ActionsのworkflowへCodeRabbit用jobやsecretは追加しない。

CodeRabbitの指摘はコードレビューの補助であり、GitHub Actionsやローカルで実行したテスト結果の代替にはしない。特に、CodeRabbitのレビュー完了を外部サービスとの実接続に成功した一次証跡として扱わない。

main向けPRはCI成功に加え、最新差分へのCodeRabbitレビューと指摘の確認・必要な対応を受入条件とする。
自動レビューがskipされた場合は`@coderabbitai full review`で依頼する。skip時の成功statusを
実レビュー済みと扱わない。子PRのEpic統合はCI成功を条件とし、mainのマージはユーザーが行う。

## Episode / Factの実接続受入（#340 / #344）

#344はdev環境の専用テストdata rootと合成シナリオを使い、実LLMによる会話由来の抽出から、
実SQLiteへの保存、実Chroma/Embeddingでの検索、通常会話の応答への利用まで検証する。
#343の管理UIによるFact単位の訂正・削除と、その後の検索・会話への反映も確認する。
直近会話履歴だけで答えられる条件や、DBへの候補直接投入、mock、readinessをこの受入の代用にしない。
実行commit・model/設定・実接続先の環境区分・scenario・期待値・結果・未検証事項を記録する。
履歴由来の応答と記憶利用を区別する根拠は、既存metadata-only policy内の参照ID・版等で追跡する。

詳細なシナリオは[実行指示書](epic-340-episodic-memory-requirements.md)を参照する。
本節は受入要件であり、検証済みの宣言ではない。dogfoodとの分離・共有推論サービスの所有権は本書の「dogfood環境との分離」に従う。

## LLM classifier conformance

意味分類器とpersona memory抽出器の品質評価は、通常のpytest unit testと分離したpromptfoo suiteで行う。抽出器はenum一致を決定論的に評価し、topic妥当性とhallucination非発生を独立したローカルOllama judgeで評価する。

- pytest unitはprompt組立、schema、parser、fail-closed、決定論的evaluatorをfakeで検証する
- conformanceは固定した合成caseで実modelとproduction providerを評価し、prompt-labは同じ固定modelで候補promptと独立rubricを評価する
- 実ユーザー本文をcase、結果、logへコピーしない
- 機微caseが`NOT_SENSITIVE`になることを許容せず、判定不能は`ABSTAIN`として保存を拒否する
- model、prompt、policyのversionを固定し、重要な変更は対象suiteを3回反復してから全suiteを実行する
- 外部serviceを必要とするため通常CIへ混在させず、release時の実接続証跡として扱う

配置、prompt tuningとproduction conformanceの分離、合格基準は
`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`を正本とする。

## LiveKit実サーバーsuite

LiveKitの状態遷移、outbox、mapping、再生済みprefixはfake clock/portを使うunit/module testでCI内検証する。実Room、WebRTC media、browser microphoneの結合は`LIVEKIT_TEST_BACKEND_URL`、`LIVEKIT_TEST_FRONTEND_URL`、`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`を設定し、リポジトリルートの`npm run test:integration:livekit`でBackend pytestとPlaywright Chromiumをまとめて実行する。このスイートはself-host LiveKitとUDP到達性が必要なためCIでは自動実行しない。

`test:integration:livekit`はtransport単体のRoom、権限、outbox、microphone、AudioTrack購読graph、再接続を検証し、テスト専用character音声はproduction runtimeへ注入しない。Whisper、Ollama、VOICEVOXを含む通常conversation UIの逐次text/audio応答は`test:integration:voice`で検証する。

Wave 3のEpic受入シナリオ、自動testと利用者dogfoodの責務分離、再実行手順は
[`wave3-acceptance-2026-08.md`](wave3-acceptance-2026-08.md)を正本とする。

## Addon / MCP conformance

#104 MCP-first接続基盤は、contract検証、test-owned MCP、実Addon/外部MCPを同じ証跡として扱わない。
正本contractは`contracts/addon/`、設計判断は`docs/decisions/addon-connection-foundation-2026-09.md`、SDK version適合は`docs/addon-mcp-sdk-compatibility-2026-09.md`とする。

### contract / unit

`backend/tests/unit/`でJSON Schema Draft 2020-12とsemantic validatorを検証する。

- `manifest.schema.json` / `addon-meta.schema.json` / `capability-snapshot.schema.json` / `execution-envelope.schema.json`
- `contracts/addon/fixtures/valid/`は受理する
- `contracts/addon/fixtures/invalid/`は拒否する
- raw secretをManifestで受理しない
- Core restrictionは安全側だけ
- annotation未信頼時に`readOnlyHint=true`をeffective read-onlyへ昇格しない
- `effect_source=unknown`はunknown / serial / retryなし

これは外部MCPへ接続した証跡ではない。

### test-owned MCP / module

#159では`backend/tests/module/`からtest-owned MCP serverを別processで起動し、通常CIで実行可能なconformanceとする。

- SDKは#152で確認した`mcp==2.0.0`をpinする
- external Streamable HTTP（none / preconfigured Bearer）
- external stdio server
- self-ownedのOrigin拒否・localhost bind・mandatory Bearerは#221へ分離
- MCP 2026-07-28 Tools / Resources / Prompts discovery
- MRTR `input_required` →回答→元request再実行
- mapping不能Tool
- trusted/untrusted annotations
- Tool追加/削除/schema変更、snapshot activation境界
- effective read-onlyのみ並列/1 retry
- write/destructive/unknownは直列/automatic retryなし
- budget 6 / same-tool 3 / identical 2 / normal 3 cycle
- Tasks必須operationは`unsupported`へ落とし、同serverの通常Toolを壊さない
- native result保持、secret/raw payload非logging

実行入口は`backend/tests/module/test_external_mcp_conformance.py`。unitは`test_external_mcp_registry.py`、`test_external_mcp_client.py`、`test_external_mcp_gate.py`、contract検証は`test_addon_contracts.py`に配置する。

Core package/DBをtest MCP serverからimportしない。test-owned MCPの成功をDevelopment Observerや第三者MCPへの実接続成功とは扱わない。

### real Addon / integration

#58 Development Observer等の実Addon processとの接続は`backend/tests/integration/test_*_integration.py`に置く。
実AddonのStreamable HTTP endpoint、service auth、process/config/store分離を実接続で検証する。

第三者/コラボMCPを本番credentialで通常CIへ接続しない。必要な実接続受入は対象Issueで明示し、credentialをfixture/evidenceへ保存しない。

### 外部MCPの独立実装との実接続

`npm run test:integration:mcp`は公開されたFilesystem / Everything Serverに実接続する。
`RUN_MCP_REAL_SERVICE_TESTS=true`と`MCP_REAL_SERVER_ROOT`を設定して明示実行する。
通常CIでは実行しない。開始後の依存不足・認証・接続失敗はskipに変換しない。
再実行手順、固定version、検証範囲と証跡は
[`external-mcp-integration-2026-09.md`](external-mcp-integration-2026-09.md)を参照する。

### 会話からのTool利用

`test_tool_use.py`と`test_tool_use_boundaries.py`は候補・元schema・binding・MRTR・停止・予算を合成portで検証する。
`test_tool_use_real_service_integration.py`は実LLMと公開Filesystem/Everythingへの接続を明示実行する。
ブラウザのテキスト・LiveKit音声、追加質問と停止の実行方法・公開MCPと制御fixtureの区別は
[会話からの外部MCP利用](tool-use.md)を参照する。実接続スイートをCIのmock結果で代替しない。
この受入runnerはテスト所有LiveKitと動的portを使う独立入口であり、既存Profileの環境オーケストレーターを起動しない。
共通reporterの`environment-run.json`／`evidence.json`の代わりに、テストdata rootの`runtime/tool-use/`
へ解決済みProfileと`runtime-manifest.json`（run ID、実行時刻、実依存、所有process、結果）を記録する。
共有用の専用成果物ディレクトリにはpath・endpoint・process/container識別子を除いた写しだけを置く。
使用modelは実接続受入の再現条件として共有用にも記録する。
起動途中の例外・中断はrunnerの非zero終了として扱い、readinessだけを成功証跡にしない。

### SDK/version更新

`mcp` package version更新は通常の依存更新として無条件mergeしない。protocol negotiation、Streamable HTTP、stdio、Tools/Resources、MRTR、trust/snapshot境界を#159 conformanceで再確認する。
MCP Tasks extension等、SDKで未対応の任意能力は`unsupported`として明示し、通常Tool/Resource利用を失敗させない。

## Capability不足と失敗

スイートの要求Capabilityが resolved Profile にない場合、テストは不足Capabilityと解決済み依存を理由に `skip` する。スイートを明示的に開始した後の次の失敗は skip に変換しない。

- Profile 解決失敗: `profile`
- 環境準備・起動失敗: `preparation` または `startup`
- readiness 失敗: `readiness`
- Playwright テスト失敗: `test`

環境ライフサイクルの詳細と失敗カテゴリは `environment-run.json` に保持する。

## スイート別証跡

各結果ディレクトリには次を保存し、別スイートの成果物を上書きしない。

- `playwright-results.json`
- `resolved-profile.json`
- `environment-run.json`
- `evidence.json`

正常に関連付けられた `evidence.json` は `suite`、`testLayer`、`profile`、`testStatus`、`runId`、`environmentReport` を記録する。`runId` は同じディレクトリの `environment-run.json` と一致しなければならず、Profile名も実行スイートと一致しなければならない。Profile解決に成功した場合は `profileReport` も記録する。環境ライフサイクルまたはPlaywrightテストが失敗した場合は、`failureCategory` に `profile`、`preparation`、`startup`、`readiness`、`supervision`、`test`、`teardown` のいずれかを記録する。

証跡生成処理自体が失敗した場合も `evidence.json` を保存する。この失敗証跡は `suite`、`testLayer`、`profile`、`testStatus` に加えて、`evidenceStatus: "failed"`、`failureCategory: "evidence"`、`failureStage`、`failureMessage` を記録する。`testStatus` はPlaywrightテストの成否だけを表し、`passed` であっても `evidenceStatus: "failed"` の証跡を実接続成功として扱わない。

`failureStage` は失敗した処理に応じて次のいずれかとする。

- `test-result`: `environment_cli.py test-result` の実行
- `environment-report-read`: `environment-run.json` の読込
- `environment-report-json`: JSON解析
- `environment-report-validation`: environment reportの契約検証
- `environment-report-association`: 実行スイートとProfileの関連付け

証跡生成失敗時は関連付けが完了していないため、`runId`、`profileReport`、`environmentReport` を記録しない。`failureMessage` には失敗原因のエラーメッセージを記録し、reporterは失敗証跡の保存後も呼び出し元へ失敗を伝える。


## 音声CHATの合成context回答値

`test_voice_context_quality_integration.py`は、通常のprompt構築とInference Routerから実Ollamaへ要求する。
光織の名前、合成履歴の好み、合成記憶の場所、情報がない場合のnullをJSONの値として照合する。
JSON形式は質問で依頼し、providerのstructured出力APIは使わないため、通常のthinking設定を強制無効にはしない。
固定語句が否定文に出ただけでは正答にしない。6条件×2設定×3要求の全36件を保存し、失敗を除外しない。
検索・privacy・永続化・自然な話し方の受入は別途必要である。

```bash
RUN_VOICE_CONTEXT_QUALITY=true \
VOICE_CONTEXT_INFERENCE_ENV=/path/to/inference.env \
VOICE_CONTEXT_EVIDENCE_PATH=/tmp/voice-context-answer-01.json \
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest \
  backend/tests/integration/test_voice_context_quality_integration.py -q
```

他の実推論測定を終了し、commit済みのworktreeで実行する。出力先は新規ファイルに限定する。
生成本文、prompt、provider例外本文を証跡へ保存しない。schema・匿名性・試行の独立した組合せと全分母を
検証した後に証跡を保存し、不正解があればテストを失敗させる。明示実行しない場合のskipは実接続合格に数えない。

合成context照合のschema 1.1では、JSONの固定キー・型・完全一致値を検査する前に、回答全体が
単一のJSONコードブロックである場合だけその囲みを展開する。説明文や複数候補は除去しない。
`strict_format_valid`で元のJSONのみという指定への適合を別記し、`passed`は取得できた回答値を
評価する。これは音声回答にJSON形式を要求する製品仕様ではない。旧schema 1.0の失敗証跡は
その定義のまま保存し、回答本文を保存していない過去試行を再採点しない。
