# Epic #480 統合受入れ記録（2026-09-24）

## 対象と分離

初期リモートmain `64a9d60c4d03eff73ec1f9708852fbb111bb5628` から開始。PR #479とローカルmainの実装は流用していない。6子IssueのコードはEpic `f9c4cc5` に統合済み。最新mainの通知機能はPR #500（Epic統合 b53cff84）の `37ace8107c7fff53f1c9e4a29b6344b2fcb77956` で同期し、下記の同期後検証を実施した。mainはマージしていない。

## 固定条件の変更前後比較

同じ `speech-v2` fixture（SHA-256 `56e10d861de9fdb8e9057d87c551432e4fea06656f8a60b88b4701929ce6722e`）、Character Card、memory policy、空の履歴・記憶DBを使用した。全6試行でinitial_state_hashは `a93a79d258f36a64ca376e47ee37100756b903b22de44dd083fa0740f4519117` と一致した。各組は初期main→同期後headの順に直列実行し、試行ごとに専用Backend、Frontend、LiveKitとtest dataを作成・終了した。共有推論サービスの設定変更はしていない。

有効設定: Chat/Privacy/Memoryは `ollama/gemma4:e4b`、Embeddingは `ollama/nomic-embed-text:latest`、RAG=false。Chat入力/出力上限7168/1024、その他の推論役割7680/512、Embedding8192。モデル、prompt、Card、音声設定を比較用に変更していない。LiveKit serverは `v1.9.7`、Python SDK `livekit==1.1.16` / `livekit-api==1.2.0`。

既存 `livekit-quality.spec.ts` の独立試行モードと空DB検証をそのまま使用した。正式な100試行計測やnative SDKの追加パッチを使った評価ではなく、独立プロセスの監督用ハーネスによる各3回の診断比較である。

| 観測値（中央値） | 初期main | 同期後head |
|---|---:|---:|
| 本文開始: utterance_finalized→first_text_delta (ms) | 1024.14 | 1016.87 |
| 本文開始: vad_speech_end→first_text_delta (ms) | 1160.53 | 1151.52 |
| 実再生開始: sendAt→startedAt (ms) | 2118.33 | 2099.03 |
| 実再生開始: fixture発話終了→startedAt (ms) | 3025.43 | 3009.33 |
| Backend CPU消費秒（試験期間） | 6.99 | 3.67 |
| Backend CPU使用率（1core=100%） | 42.32 | 28.09 |
| Backend peak RSS (MiB) | 357.02 | 354.11 |

本文開始は同一server_monotonic内の差分、実再生開始は同一browser clock内の差分であり、異なるclockを直接引いていない。実再生はAudioWorklet出力の確認であり、WAV取得やdecode完了で代用していない。fixture transcript、実応答の終端、再生完了を全6回で確認した。

この3組では本文開始・実再生開始の悪化は観測されなかった。試行数が少なく、推論出力の長さや共有GPU負荷も変動するため、統計的な性能保証・CPU改善の認定は `INCONCLUSIVE` とする。GPU負荷は `NOT_RUN`。CPU/RSSはBackendプロセスだけの0.5秒間隔観測であり、共有推論サービスを含む総負荷ではない。初期mainから同期後headまでには後発の通知main変更も含まれ、純粋なリファクタリングだけへの因果帰属はしない。

証拠はリポジトリ内の[集計JSON](epic-480-paired-performance-20260924.json)と[保存方法・再計算手順](evidence/epic-480-20260924/README.md)から取得できる。各行に保存したtrial-manifest、controlled-trace、backend-resourcesを示す。生成IDを除いた公開版の元ファイル／保存版SHA-256も同ディレクトリにある。

| 組 | 初期main | 同期後head |
|---|---|---|
| 1 | [manifest](evidence/epic-480-20260924/baseline/pair-1/trial-manifest.json)・[trace](evidence/epic-480-20260924/baseline/pair-1/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/baseline/pair-1/backend-resources.jsonl) | [manifest](evidence/epic-480-20260924/updated/pair-1/trial-manifest.json)・[trace](evidence/epic-480-20260924/updated/pair-1/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/updated/pair-1/backend-resources.jsonl) |
| 2 | [manifest](evidence/epic-480-20260924/baseline/pair-2/trial-manifest.json)・[trace](evidence/epic-480-20260924/baseline/pair-2/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/baseline/pair-2/backend-resources.jsonl) | [manifest](evidence/epic-480-20260924/updated/pair-2/trial-manifest.json)・[trace](evidence/epic-480-20260924/updated/pair-2/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/updated/pair-2/backend-resources.jsonl) |
| 3 | [manifest](evidence/epic-480-20260924/baseline/pair-3/trial-manifest.json)・[trace](evidence/epic-480-20260924/baseline/pair-3/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/baseline/pair-3/backend-resources.jsonl) | [manifest](evidence/epic-480-20260924/updated/pair-3/trial-manifest.json)・[trace](evidence/epic-480-20260924/updated/pair-3/controlled-trace.jsonl)・[CPU/RSS](evidence/epic-480-20260924/updated/pair-3/backend-resources.jsonl) |

## 実接続検証

同期前（tree一致を確認したEpic f9c4cc5 / 検証版5a324a6）: Backend LiveKit12件、ブラウザ疎通・再接続2件、公開MCP5件、実Ollama ToolService2件が成功。実音声15件のうち14件成功、1件は履歴0件で失敗。

同期後37ace: ブラウザ疎通・再接続2件、実音声14件成功／1件失敗。実サービスで生成中／再生中割込み、音声→text→音声の保存と再生、3往復、privacy、ミュート保持、barge-inを確認した。証拠: `/tmp/ds486-it2-20260924-204618`。

履歴失敗は初期mainでも同じ試験の154行目で再現した。原因確定・修正を別Issue #501へ切り出した。既存不具合と断定して成功へ読み替えることはせず、受入れ未解決項目として残す。

同期後Backend LiveKitは `/tmp/ds486-it2-20260924-204524` で12件のassertion成功後に試験プロセスがnative FFI abort（exit -6）。Backend自体は存続したが、suite全体は成功としない。初期main比較 `/tmp/ds486-it2-20260924-204728` は12件成功・exit 0。診断実行 `/tmp/ds486-it2-20260924-204935` は2件目のbootstrapが503となり、SFUにDTLS timeoutを記録した。終了時異常と503の原因・再現条件は未確定。その後、失敗時だけHTTP応答を記録する診断を追加し、他の実音声試験と重ならない直列実行 `/tmp/ds486-it2-20260924-205309` で同じ12件が成功・exit 0となった。assertionやタイムアウト、SDK、サービスの設定は緩和していない。実接続成功の証拠は得たが、間欠的なnative終了異常と503を修正済みとは扱わない。

## 状態の区別

TAKT成功、PRのCI成功、Epic統合、実接続成功は別に管理する。後続のCI・Container images、main向けCodeRabbit差分レビューと指摘修正は最新headで追跡する。実マイク・聴感はユーザー担当であり、Codexの疎通検証をユーザーへ移管しない。共有サービスとdogfoodデータは保護し、各試験の所有プロセス・コンテナだけを終了した。
