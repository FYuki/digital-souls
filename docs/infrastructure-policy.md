# インフラ方針

## 基本方針

`digital-souls`のインフラは、継続利用する安定環境と、TAKTが変更・検証する開発環境を分ける。
常時稼働する軽量サーバーと必要時だけ使う高性能計算資源を分ける長期方針は維持する。

現在のBackendはFastAPI、FrontendはVite + Svelte、記憶はSQLite正本とChroma派生indexを使用する。
AIRI、PostgreSQL、Qdrant、Redisは現行の通常起動構成に含めない。

環境分離の詳細な判断は`docs/decisions/local-dogfood-environment-2026-08.md`を正本とする。

## ミニPC調達前の構成

WindowsメインPCのWSL2へ、開発用とdogfood用の別distributionを置く。

```text
Windows 11 / RTX
├─ Ubuntu-dev
│  ├─ 開発用checkout
│  ├─ TAKT worktree
│  ├─ Frontend :5173
│  ├─ Backend  :8000
│  └─ dev／test専用SQLite・Chroma
└─ Ubuntu-dogfood
   ├─ 安定版の独立clone
   ├─ Frontend :15173
   ├─ Backend  :18000
   ├─ dogfood専用SQLite・Chroma
   ├─ Ollama :11434
   └─ VOICEVOX :50021
```

dogfoodは使用感を継続確認する運用相当環境であり、実conversation historyを保持する。
別WSLでもnetwork namespaceと物理資源は共有されるため、portを分け、Ollama／VOICEVOXの
ownershipとcleanup境界を明示する。

dogfood構成はIssue #50で実装する。完了までは現行`dev` Profileをdogfood用途へ流用しない。

## 環境ごとの責務

| 環境 | 責務 | データ保持 |
|---|---|---|
| `Ubuntu-dev` | 実装、TAKT、unit／module／integration／E2E | 破棄・再作成可能 |
| `Ubuntu-dogfood` | 安定版の継続利用、使用感確認、運用手順検証 | backup・migration対象 |
| WindowsメインPC | RTXを使う推論、画像生成、配信等の高負荷処理 | 用途別に管理 |
| Cloud GPU / VM | ローカル資源不足時の一時的な代替 | 必要時だけ |

dogfoodは独立cloneを明示deployで更新し、mainへのmergeだけでは実行commitを変更しない。
SQLiteとChromaはリポジトリ外のdogfood専用data rootへ置き、環境identity不一致を起動前に拒否する。

全環境の永続・実行データは`DS_DATA_DIR`を唯一のdata rootとし、SQLiteを
`conversation-history.db`、Chromaを`chroma/`、runtime reportを`runtime/`、cacheを`cache/`へ置く。
`.environment-identity.json`と`DS_ENVIRONMENT_ID`（`dev` / `test` / `dogfood`）をストア初期化前に
照合する。dogfoodではリポジトリ内data root、dev／testではdogfood markerを拒否する。

## Wave 2との関係

Issue #50のdogfood分離と手動受入は2026-08-17に完了した。
#22をcleanなmainから再開し、その完了後に#33以降へ進む。
Wave 2親Issue #28の受入まではdogfoodのRAGを無効にし、旧Chromaデータを作らない。

dogfoodのconversation historyは実データとして保持する。Wave 2開始後のpersona memoryはSQLiteを
正本として空状態から開始し、ChromaはSQLiteから再構築可能な派生indexとする。

## Docker方針

Dockerは環境分離の必須条件にしない。VOICEVOX containerには引き続き使用できる。
Backend、Frontend、Ollamaを含む全面的なDocker Compose化は、必要性が明確になった時点で判断する。

Ollamaのprocess lifecycleはUbuntu-dogfoodのsystemdが所有する。VOICEVOXではsystemdがCompose stackの起動・停止入口を担い、Composeが実行中containerの再起動を所有する。dev、integration、TAKTはProfileのexternal endpointをreadiness確認して再利用し、起動、停止、restart、container操作を行わない。Ubuntu-dogfoodの標準配置、metadata-only観測、Windows再起動後の復旧手順は`infra/dogfood/README.md`を正とする。

## ミニPC調達後の構成

ミニPCは人格、記憶、軽量推論、Web UI等の常時稼働先とする。dogfoodで確立した独立clone、
data root、deployment manifest、backup、restore、rollback契約を移植する。

WindowsメインPCは大型LLM、Whisperの高負荷処理、画像生成、ComfyUI、配信処理等の
必要時ワーカーとして残す。Cloud GPU / VMはWindows未起動時または能力不足時の代替先とする。

## ローカルモデル設定

Ollamaのchat modelは`OLLAMA_CHAT_MODEL`、runtime contextは`OLLAMA_CONTEXT_TOKENS`で指定する。
Profile resolver、Ollama readiness／prepare、Backend payloadは同じ解決値を使用する。
モデル最大contextは`LLM_CONTEXT_TOKEN_LIMIT`として分離し、prompt予算はruntime contextから
`OLLAMA_RESPONSE_RESERVE_TOKENS`を差し引く。Whisperは`WHISPER_MODEL`をBackend実行と
cache準備の共通契約とする。

## インフラ判断

- 開発・テストのcleanupからdogfoodのprocessとデータを操作しない
- dogfoodのSQLite schema変更はbackup、migration、検証、rollbackを伴う
- Chromaを記憶の正本にしない
- dogfoodのFrontend／Backendを認証なしでLAN公開しない
- 常時稼働先は省電力・静音性を重視する
- GPU常時稼働は避け、重い処理はWindowsまたはCloudへ委譲する
