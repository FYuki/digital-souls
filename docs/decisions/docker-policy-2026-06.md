# Docker移行方針 (2026-06、2026-08-30方針転換)

## 状態

**ACTIVE**。

2026-06のDocker限定利用方針を再検討し、Backend、Frontendと共有Whisper推論サービスを
段階的にDockerへ移行する。本ADRは移行後の目標構成と、移行中も維持する契約を定める。

Issue #135 Goal 1では、コード、設定、CI、dogfood配備資材を実機配備直前まで実装する。
実Ubuntu-dogfoodへの適用、実GPUでのVRAM／latency計測、連続会話と再起動受入はGoal 2で行う。
Issue #112は本決定と実装の対象外である。

## 背景

当初は個人開発・単一マシンであり、FastAPI、Vite、Ollama、WhisperをWSL2上で直接実行する方が
単純であると判断した。その後、次の要件が具体化した。

- `Ubuntu-dev`と`Ubuntu-dogfood`で同じサービス構成を再現しつつ、dogfoodのコードとデータを守る
- Backend／Frontendの依存と実行環境をcommit単位で固定し、image単位でdeploy／rollbackする
- NVIDIA GeForce RTX 4070 Ti SUPER 16GB上でfaster-whisperをGPU実行する
- VRAM上へWhisperモデルを二重ロードせず、dev／integration／dogfoodから1 instanceを共有する
- 将来の常時稼働先とGPU workerの分離に備え、推論境界をBackend processから分離する

VOICEVOXとLiveKitですでにComposeとsystemdの責務分離を運用しており、同じ操作モデルを
Backend、Frontend、Whisperへ拡張する方が、直接実行とDockerを混在させ続けるより運用契約を
明確にできると判断した。

## 決定事項

### 1. Docker化の対象

段階的なDocker移行の対象を次のとおり定める。

| 対象 | 移行後 | 所有者 |
|---|---|---|
| Backend | dev／dogfood別container | 各environment run |
| Frontend | dev／dogfood別container | 各environment run |
| Whisper | dev／integration／dogfood共通GPU container | Ubuntu-dogfoodのsystemd＋Compose |
| VOICEVOX | 既存containerを継続 | Ubuntu-dogfoodのsystemd＋Compose |
| LiveKit | 既存containerを継続 | Ubuntu-dogfoodのsystemd＋Compose |
| Ollama | 当面はWSL2上の直接実行を継続 | Ubuntu-dogfoodのsystemd |
| SQLite／Chroma | Backendから利用する永続data | environmentごとの`DS_DATA_DIR` |

全サービスを単一Compose projectへ統合しない。アプリケーション、共通推論サービス、LiveKitの
lifecycleと停止責任を分け、devのcleanupがdogfoodまたは共通推論サービスを停止できない境界を維持する。

OllamaのDocker化は本移行の完了条件に含めない。VRAM競合、model cache、起動時間、GPU割当を
Whisperとの並行稼働で計測した後、独立した判断として再検討する。

### 2. 既存の操作入口とProfile契約を維持する

利用者とテストの操作入口として、次を維持する。

- `scripts/start-all.sh`、`scripts/start-voice-chat-e2e.sh`
- `scripts/start-dogfood.sh`、`scripts/status-dogfood.sh`、`scripts/stop-dogfood.sh`
- `environments/up.sh`、`down.sh`、`status.sh`、`verify.sh`
- `dev`、`test-mocked`、`integration-text`、`integration-voice`、`dogfood` Profile
- Frontend、Backend、ready gate、LiveKitの既存公開port
- readiness、所有権、run report、停止順序、失敗分類の契約

Environment CLIをホスト側control planeとして残し、managed adapterの内部実装をprocess起動から
Compose container操作へ差し替える。Backend／Frontendのrun reportはcontainer identityを記録し、
記録したenvironment runが所有するcontainerだけを停止する。

WSL2上の既存loopback URLとportを維持するため、アプリケーションとWhisperのComposeはhost networkを
使用する。containerはProfileで解決したloopback endpointをそのまま利用し、LANへ公開しない。

### 3. devとdogfoodの分離をDocker volumeだけへ委ねない

別WSL distribution、別port、独立clone、環境identity、専用data rootを引き続き分離境界とする。
Docker化はこの境界を置き換えない。

- dev／testのdataは破棄可能とする
- dogfoodのSQLiteはbackup、migration、検証、rollback対象とする
- dogfoodの`DS_DATA_DIR`はリポジトリ外の絶対pathをbind mountする
- identity markerとruntime projectionをSQLite／Chroma初期化前に検証する
- container image、named volume、build cacheを会話データの正本にしない
- SQLiteとChromaはLinux filesystem上のbind mountを使用し、Windows filesystemへ置かない

container userとbind mountのUID／GIDを明示し、root所有fileの混入や権限緩和で解決しない。

### 4. Whisperを共通GPU推論サービスへ変更する

WhisperはBackendの`in_process`依存から、共通の`external` HTTP依存へ変更する。
Ubuntu-dogfoodの`digital-souls-inference.target`がWhisper Compose stackを所有し、
dev／integration／dogfoodは同じloopback endpointをreadiness確認して利用する。

初期設定は次とする。

| 設定 | 初期値 |
|---|---|
| GPU | NVIDIA GeForce RTX 4070 Ti SUPER 16GB |
| model | `medium` |
| device | `cuda` |
| compute type | `int8_float16` |
| device index | `0` |
| model instance | 1 |
| global inflight | 1 |

サービスは起動時にCUDA、compute type、model artifactを検証し、最小推論を通過してからreadyとする。
CPUへの暗黙fallbackを許可しない。faster-whisper、CTranslate2、CUDA、cuDNNとbase imageを固定し、
実行versionとGPU metadataを本文を含まない証跡として記録する。

旧WebSocket baselineとLiveKit Conversation Coreの両方をremote Whisper clientへ切り替え、
どちらの経路からもBackend内へGPU modelをロードしない。音声、文字起こし本文、秘密値をWhisper serviceの
永続data、access log、error logへ保存しない。model cacheはdogfoodの会話data rootから分離した
共通推論service専用pathへ置く。

共通serviceがdevとdogfoodをまたぐ単一のcapacity、推論timeout、worker破棄、再生成を所有する。
初期実装は待ち行列を持たないsingle-flightとし、競合したrequestは既存の
`stt_capacity_exceeded`へ変換できる明示的な応答でfail fastする。実行中推論のpreemptionは行わない。

### 5. dogfood deployをimage単位へ変更する

GitHub Actionsは対象commitからBackend／Frontend／Whisper imageをbuildしてGHCRへcommit SHA tagで公開する。
dogfood deployは3 imageのtagをdigestへ解決し、immutableなimage digestを
deployment manifestへ記録してから切り替える。既存のcommit、Profile schema、SQLite data schema、
backup ID、deploy日時にimage digestを追加する。

deploy前backup、backup検証、readiness、失敗時rollbackを維持する。rollbackは保存済みmanifestの
commit、schema、backupとimage digestの組を検証して切り替える。mainへのmerge、image build、
registry更新だけではdogfoodの実行imageを変更しない。

3 imageは`dogfood-images.env`へ原子的に反映し、systemd targetの再起動で同じcommitの組へ切り替える。
失敗時は直前manifestのcommitと3 digestを一組で復元する。Whisper image、CUDA runtime、modelまたは
protocolを変更した場合は、Goal 2でdev／dogfood双方の互換性とGPU実機受入を行う。

### 6. 同じEpicで扱い、段階的に切り替える

Docker移行とWhisper外部化は、Compose、GPU runtime、container network、Profile、readiness、
ownershipを共有するため、同じEpicで設計・受入を管理する。ただし単一変更で全面切替しない。

```text
契約・schema拡張
  -> 共通Whisper containerとremote client
  -> dev Backend／Frontend container
  -> dogfood image deploy／rollback
  -> dev／dogfood並行稼働・GPU受入
```

各段階で従来経路または直前のimageへ戻せる状態を保ち、共通Whisperの実接続受入が完了する前に
Backend内Whisper実装と`backend/requirements-whisper-legacy.txt`を削除しない。legacy依存は通常の
Backendへインストールせず、明示rollback時だけ使用する。

## 受入条件

Goal 1の自動・ローカル受入は次を対象とする。

- 既存の起動、status、停止スクリプトとProfile選択が同じ操作で利用できる
- devとdogfoodのBackend／Frontendが既存portを維持できる
- dev cleanupがdogfood container、共通Whisper、Ollama、VOICEVOX、LiveKitを停止しない
- devとdogfoodのSQLite／Chroma／runtime reportが混在しない
- dogfoodのbackup、restore、deploy失敗時rollbackがimage移行後も成功する
- 同時STT要求、capacity超過、推論timeout、worker再生成の結果が契約どおりである
- 音声と文字起こし本文をcontainer log、metrics、永続volumeへ残さない

Goal 2の実Ubuntu-dogfood／GPU受入は次を対象とする。

- devとdogfoodの実音声利用中もGPU上のWhisper model instanceが1つである
- Whisperが`cuda`／`int8_float16`で動作し、CPUへfallbackしていない
- OllamaとWhisperの同時常駐・連続会話でOOMせず、VRAM使用量と応答latencyを記録できる
- WSL／Docker再起動後にsystemdの所有順序どおり復旧する

## 旧判断と再検討結果

2026-06時点では、複数人開発、本番container基盤、同一OS内での環境分離がなく、バージョン固定だけを
目的とするDocker化は過剰と判断した。この判断は当時の要件に対して妥当だった。

2026-08にdogfoodの独立deploy／rollbackと共有推論serviceが実装され、さらにGPU Whisperの単一instance化が
必要になった。Dockerを環境分離そのものには使わない一方、再現可能なGPU runtime、image単位deploy、
service ownershipを実現する手段として採用条件を満たしたため、限定利用から段階的移行へ方針を変更する。

## 関連

- `docs/development-environment.md` — 現行の起動・開発手順
- `docs/infrastructure-policy.md` — インフラ全体方針
- `docs/decisions/local-dogfood-environment-2026-08.md` — 開発／dogfoodの分離境界
- `docs/decisions/livekit-transport-2026-08.md` — Conversation Coreとtransportの境界
- `docs/decisions/voice-session-contract-2026-08.md` — STT capacity、timeout、障害回復契約
- `docs/decisions/archive/docker-airi-policy-2026-06.md` — 失効したAIRI個別方針の検討履歴
