# ローカルdogfood環境分離方針 (2026-08)

## 状態

**ACTIVE**。

本ADRは、TAKTによる開発と並行して安定版を継続利用するローカルdogfood環境の分離境界、
データ保持、デプロイ、Wave 2との依存関係を定める。実装進捗は親Issue #50で管理する。

## 背景

TAKTは開発用worktreeで実装、テスト、レビューを行う。開発中にもテキスト／音声チャットの
使用感を継続して確認したいが、現行の環境オーケストレーターは同じportの既存サービスを
再利用できるため、dogfoodとTAKTが同じFrontend／Backend endpointを使うと、テストがdogfoodの
BackendやSQLiteへ到達する可能性がある。

また、現行BackendはSQLiteとChromaをリポジトリ配下へ保存し、`--reload`で起動する。
main checkoutをそのままdogfoodに使うと、TAKT成果のmerge、依存更新、cleanup、DB再作成が
利用中サービスと実データへ影響する。

dogfoodは単なる検証環境ではなく、実会話履歴を保持する運用相当環境として扱う。

## 決定事項

### 1. 開発とdogfoodを別WSLディストリビューションに分ける

WindowsメインPC上に次の2環境を置く。

```text
Windows 11
├─ Ubuntu-dev
│  ├─ 開発用checkout
│  ├─ TAKT worktree
│  └─ dev／testデータ
└─ Ubuntu-dogfood
   ├─ 安定版の独立clone
   ├─ dogfoodサービス
   └─ dogfood専用データ
```

別WSLによりfilesystem、process ownership、Linux user、mountを分ける。ただしWSL 2の複数distributionは
network namespace、CPU、kernel、memory、swapを共有するため、別WSLだけを完全な隔離境界とは扱わない。

- Microsoft Learn: https://learn.microsoft.com/windows/wsl/about
- Microsoft Learn: https://learn.microsoft.com/windows/wsl/networking

### 2. portとサービス所有権を分ける

初期portは次とする。

| 環境 | Frontend | Backend | ready gate |
|---|---:|---:|---:|
| dev／TAKT | 5173 | 8000 | 4174 |
| dogfood | 15173 | 18000 | 14174 |

FrontendとBackendはloopbackだけへbindする。LAN公開、TLS、認証は別の判断とする。

OllamaとVOICEVOXはdogfoodのenvironment runとは別の共通推論サービスとして運用する。
OllamaはUbuntu-dogfoodのsystemdをprocess ownerとする。VOICEVOXはsystemdをCompose stackの操作入口、Composeを実行中containerの再起動ownerとする。dev／integration／TAKTは`external` dependencyとして同じloopback endpointを再利用する。構築、起動、停止、metadata-only観測、WSL終了後の復旧は`infra/dogfood/README.md`に集約する。
dogfood Profileは両者をexternal dependencyとして扱い、dogfoodの所有reportへ登録せず、
stop／cleanupでも停止しない。GPU、CPU、memoryは共有資源として競合し得るため、並行稼働テストで観測する。

### 3. codeとdeployを分ける

dogfoodはTAKT worktreeやmain checkoutではなく独立cloneを使う。実行commitをdeployment manifestへ
記録し、明示的なdeployを行うまで変更しない。mainへのmergeだけでdogfoodをreload、restart、更新しない。

deploy前にbackupを作成し、依存準備、Frontend build、service restart、readinessを検証する。
失敗時は直前commit、設定、data schemaへrollbackできることを完了条件とする。

### 4. 環境identityとdata rootを必須にする

`dev`、`test`、`dogfood`を区別する`DS_ENVIRONMENT_ID`と、SQLite、Chroma、runtime report、cacheの
保存先を一元解決する`DS_DATA_DIR`を使用する。dogfoodはリポジトリ外の専用data rootを使用する。

data rootには環境identity markerを持たせ、Profile、設定、markerが一致しない場合はSQLite／Chromaを
開く前にfail closedする。dev／testのsetup、fixture、cleanupからdogfood data rootを操作できないようにする。

dogfood cloneを更新・再作成するときは、リポジトリ外の`DS_DATA_DIR`とmarkerを保持し、新cloneにも
同じ`DS_ENVIRONMENT_ID=dogfood`と絶対パスを設定する。起動前検証を通過してからサービスを切り替え、
data root自体をcloneの削除対象へ含めない。

### 5. dev／testとdogfoodの保持契約を分ける

| 対象 | dev／test | dogfood |
|---|---|---|
| conversation history SQLite | 再作成可能 | 実データとして保持 |
| persona memory SQLite | 再作成可能 | Wave 2開始後は正本として保持 |
| Chroma | 再作成可能 | SQLiteから再構築可能な派生index |
| schema変更 | migration非保証 | backup、migration、検証、rollback必須 |

dogfoodのbackupはSQLite WALを考慮した整合性を持ち、環境ID、commit、schema version、作成日時、
検証結果をmetadataとして保持する。restoreは空の別data rootで定期的に実証する。

複数SQLite artifact対応への移行後、`backup-verify`、`restore`、`restore-verify`の成功時JSONは、
従来のトップレベル`schemaVersion`と`conversationCount`を返さない。代わりに`artifacts`配列の
各要素が`filename`、`schemaVersion`、`recordCount`を返す。旧fieldを参照する運用scriptは、
対象の`filename`で配列要素を選択して新fieldを読むよう移行する。

### 6. Wave 2受入まではdogfoodのRAGを無効にする

dogfood環境の利用はWave 2より先に開始してよいが、Wave 2親Issue #28の受入までは
`RAG_ENABLED=false`とする。この期間に実データとして保持するのはconversation historyだけであり、
旧Chromaへ会話本文を保存しない。

Wave 2ではpersona memory SQLiteを新しい正本として空状態から開始し、Chromaはその正本だけから構築する。
dogfoodのconversation historyは削除せず、対応schemaのbackup、migration、検証、rollbackを適用する。

実施順序は次とする。

```text
#50（#51〜#56、2026-08-17完了）
  -> #22
  -> #33
  -> #8
  -> (#29 || #30)
  -> Wave 2後続
```

### 7. Dockerは環境分離の必須条件にしない

VOICEVOXは既存どおりDocker containerを利用できる。Backend、Frontend、Ollamaを含む全面的な
Docker Compose化は、今回のdogfood分離とミニPC移行の完了条件に含めない。

### 8. 推論サービスだけをUbuntu-dogfoodへ集約する

#50のサービス分離方針に対する明示的な例外として、OllamaとVOICEVOXはUbuntu-dogfood側の1 instanceだけを運用し、Ubuntu-devからもexternal dependencyとして再利用する。devとdogfoodを別instanceにすると、VRAM制約下での並行稼働要件を満たせず、共有network namespace上の同一port（Ollama `11434`、VOICEVOX `50021`）とも競合するためである。

`dev.json`と`dogfood.json`は両サービスを`source: external`かつ同一portで定義済みのため、Profileは変更しない。Ubuntu-devは推論サービスのprocess lifecycleを所有せず、起動、停止、restart、cleanupを行わない。Ubuntu-dev側のOllama systemd自動起動も無効化する。一方、会話履歴、SQLite、Chroma、data rootは共有せず、環境ごとの`DS_DATA_DIR`とidentity markerによる分離を維持する。

## 子Issue

- [x] #52 runtime data rootと環境identity
- [x] #51 managedサービスのport分離とdogfood Profile
- [x] #53 Ubuntu-dogfoodと共通推論サービス
- [x] #54 deploy、rollback、常駐運用
- [x] #55 backup、restore
- [x] #56 TAKTとの並行稼働・データ分離受入

## 結果

- TAKTの変更、テスト、cleanupからdogfoodのcodeとデータを守れる。
- ミニPC調達前から運用相当の使用感とデータ保持を検証できる。
- 別WSLでも共有network／物理資源の競合は残るため、port分離とresource観測が必要になる。
- dogfood開始後はSQLite schema変更を破壊的な開発作業として扱えず、backupとmigrationが必須になる。
- 将来のミニPC移行では、独立clone、data root、deployment manifest、backup契約をそのまま移植できる。
