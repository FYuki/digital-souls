# Ubuntu-dogfood 構築・運用runbook

Ubuntu-dogfoodはUbuntu-devと別のWSL distribution、Linux filesystem、service user、runtime data rootを使用する。Ollamaのprocessはsystemdが所有し、VOICEVOXの実行中containerはDocker Composeが所有する。systemdはVOICEVOX Compose stackの起動・停止入口を担う。dev／integration／TAKTはProfileに記載された起動済みendpointを再利用する。

## Windows上でのdistribution作成

PowerShellを管理者として開き、利用するUbuntu tarまたは既存distributionのexportを用意したうえで、dogfood専用directoryへimportする。

```powershell
wsl.exe --import Ubuntu-dogfood C:\WSL\Ubuntu-dogfood C:\WSL\images\ubuntu.tar --version 2
wsl.exe --distribution Ubuntu-dogfood
```

Ubuntu-dogfood内で`/etc/wsl.conf`に次を設定し、Windows側で`wsl.exe --terminate Ubuntu-dogfood`を実行してから再起動する。

```ini
[boot]
systemd=true
```

Ubuntu-dogfood内でGit、Python、Docker、Ollamaを導入する。Ollamaは版とSHA-256を固定した公式GitHub Release資材を一般ユーザーで取得・検証し、検証成功後だけrootで展開する。SHA-256は[Ollama公式GitHub Release v0.32.5](https://github.com/ollama/ollama/releases/tag/v0.32.5)の対象asset欄を正本とする。次はx86-64用の固定例である。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv docker.io curl ca-certificates zstd
(
  set -euo pipefail
  OLLAMA_VERSION=v0.32.5
  OLLAMA_ARCHIVE=ollama-linux-amd64.tar.zst
  OLLAMA_SHA256=f7d6bdbcf71b83aa8670c4e7dc4b6936c0952fcf8b114eaf6a11cbadb9684214
  curl --proto '=https' --tlsv1.2 --fail --location \
    --output "/tmp/$OLLAMA_ARCHIVE" \
    "https://github.com/ollama/ollama/releases/download/$OLLAMA_VERSION/$OLLAMA_ARCHIVE"
  printf '%s  %s\n' "$OLLAMA_SHA256" "/tmp/$OLLAMA_ARCHIVE" | sha256sum --check --strict -
  sudo tar --zstd -C /usr -xf "/tmp/$OLLAMA_ARCHIVE"
  rm "/tmp/$OLLAMA_ARCHIVE"
)
sudo systemctl enable --now docker.service
```

固定版を更新する場合は、Ollama公式GitHub Releasesの対象tagを開き、対象architectureのasset欄にGitHubが表示するSHA-256を取得する。tag付きURL、asset名、SHA-256を同時に更新し、`sha256sum`が失敗した場合は`sudo tar`を実行しない。`latest` URLや未検証の`install.sh`をrootで実行しない。

## 設定とbootstrap

`env.example`をdogfood専用の一時pathへコピーし、repository URL、配備する完全なcommit SHA、VOICEVOX imageを実環境に合わせる。推論portはここへ追加せず、`environments/profiles/dogfood.json`を唯一の参照元にする。

```bash
cp infra/dogfood/env.example /tmp/dogfood.env
sudo env DOGFOOD_ENV_FILE=/tmp/dogfood.env WSL_DISTRO_NAME=Ubuntu-dogfood \
  scripts/dogfood/bootstrap.sh
```

bootstrapはdistribution名と`DS_ENVIRONMENT_ID=dogfood`、必須設定、絶対path、pathの非重複、HTTPS repository URL、完全なcommit SHAを配置前に検証する。初回と再実行のどちらもrootで指定revisionを取得し、origin、commit一致、detached HEAD、変更のないworking treeを検証してから生成資材を配置する。既存DBがある場合は、指定revisionの一時cloneへBackend実行環境を構築し、その環境でbackupと独立検証を完了してから配備用cloneのfetchへ進む。検証後のcloneはroot所有に収束し、application service userは変更できない。暗黙のpull、reset、deployは行わない。

bootstrapは検証済み設定からsystemd unitとWindows launcherを生成する。生成されたlauncherは`DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1`に配置されるため、Windows側から`\\wsl$`経由でコピーして使用する。unitのservice user、group、設定file、clone内runner、WSL distributionは同じ設定値から生成される。

標準配置と所有権は次のとおり。

| 対象 | 標準path | 所有者 | 用途 |
|------|----------|--------|------|
| clone | `/opt/digital-souls/current` | `root:digital-souls` | dogfood専用の読み取り専用clone |
| 設定 | `/etc/digital-souls` | `root:digital-souls` | `dogfood.env` |
| data | `/var/lib/digital-souls/data` | `digital-souls:digital-souls` | SQLite、Chroma等の永続data root |
| backup | `/var/lib/digital-souls/backups` | `digital-souls:digital-souls` | SQLite backup世代 |
| state | `/var/lib/digital-souls/state` | `digital-souls:digital-souls` | service state |
| log | `/var/log/digital-souls` | `digital-souls:digital-souls` | file log用directory |

directoryは`0750`、設定ファイルは`0640`を基準とする。application service userは`docker`補助groupへ所属させず、VOICEVOX Composeはroot所有のsystemd unitとroot所有cloneのrunnerだけが操作する。data、state、logをclone配下、Ubuntu-devのruntime root、TAKT worktreeへ置かない。

## Conversation historyのbackup／restore

SQLiteの`conversation-history.db`を会話履歴の正本とする。backupはSQLite公式backup APIで作成するため、WALへcommit済みで未checkpointの履歴も整合したsnapshotへ含まれる。Chromaは再構築可能な派生indexであり、backupへ含めない。

保存先はruntime data rootと分離した`DOGFOOD_BACKUP_DIR=/var/lib/digital-souls/backups`である。世代名は`backup-YYYYMMDDTHHMMSSZ-COMMIT先頭12文字-一意ID先頭12文字`、保持数は`DOGFOOD_BACKUP_RETENTION_COUNT=7`とする。完成済みの正規世代だけが古い順に整理され、不明なfile、手動directory、作業中世代は削除されない。

backupの真正性検証にはgeneration外の`DOGFOOD_BACKUP_AUTHENTICATION_KEY`を使う。`openssl rand -hex 32`で生成した64桁の16進数を`dogfood.env`へ設定し、backup作成時からrestore後検証まで同じ値を維持する。この鍵をbackup directory、コマンド引数、Issue、logへ記録しない。鍵を失った場合や変更した場合、既存backupは検証・restoreできない。

以下はすべて`digital-souls` userで実行する。backupと独立検証はdeploy前およびWave 2 #8のschema変更前に必須であり、どちらかが失敗した場合は後続のdeploy／migrationを開始しない。

```bash
sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py backup \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-root /var/lib/digital-souls/backups --retention-count 7

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py backup-verify \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID
```

CLIは成功時に0、identity不一致は10、artifact不正は11、schema不一致は12、非破壊切替失敗は13、その他の入力・OSエラーは1を返す。出力する証跡は環境ID、UTC日時、commit、schema version、conversation件数、検証結果に限定する。metadata、manifest、標準出力、標準エラー、作業logへconversation本文、DB本文、環境変数全体、秘密値を転記しない。

### schema変更失敗時のrollback

Backendを停止し、変更前backupの`backup-verify`を成功させてからrestoreする。restoreはmetadata、manifest、checksum、SQLite整合性、schema、環境identityを切替前に検証し、失敗時は既存DBを維持する。

```bash
sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py restore \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py restore-verify \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID
```

identityエラーでは選択した環境と`.environment-identity.json`を照合する。artifact／schemaエラーでは対象世代を使用せず、直前の検証済み世代へ切り替える。restore safetyエラーではサービスを起動せず、既存DBが維持されていることを確認してから再試行する。

### Issue #56 restore drill

実drillは本タスクの自動テストでは行わずIssue #56で実施する。読み取り専用cloneは`/opt/digital-souls/current`（`root:digital-souls`）、実data rootは`/var/lib/digital-souls/data`（`digital-souls:digital-souls`）である。上記backupを作成・検証後、`digital-souls:digital-souls`所有の空の別data rootを用意し、同じenvironment identity markerを初期化したうえで、`DS_DATA_DIR`だけを別rootへ差し替えてrestoreとrestore-verifyを実行する。

一次証跡にはenvironment ID、UTC日時、commit、schema、conversation件数、検証結果だけを残す。その後Backendを別rootで起動し、readiness、schema、件数、既存conversationを指定した履歴再開が成功することを確認する。会話本文や秘密値は端末logやIssue本文へ記録しない。drill終了後に通常rootへ戻し、再度readinessを確認する。

## 起動・停止・状態確認

```bash
sudo scripts/dogfood/start-services.sh
sudo scripts/dogfood/status.sh
sudo scripts/dogfood/restart-services.sh
sudo scripts/dogfood/stop-services.sh
```

`digital-souls-inference.target`がOllamaとVOICEVOXをまとめる。Ollama unitは失敗時再起動を担い、VOICEVOX unitはrootでCompose stackを起動・停止するoneshotの入口に限定する。実行中のVOICEVOX containerはComposeの`unless-stopped`方針で異常終了後に再起動する。停止timeoutは両unitとも有限であり、OllamaはSIGTERM、VOICEVOXは`docker compose down`で正常停止する。`restart-services.sh`またはVOICEVOX unitの手動restartでは同じrunnerを通じてdown／upする。Composeが所有するのはVOICEVOXだけで、Backend／Frontendはcontainer化しない。

`status.sh`はidentity、runtime root、unit、listen port、CPU、memory、GPU、VOICEVOX containerのmetadataだけを表示する。会話、DB、永続data、journal本文は読まない。

dogfood applicationはservice userで起動する。

```bash
sudo -u digital-souls env DOGFOOD_ENV_FILE=/etc/digital-souls/dogfood.env \
  /opt/digital-souls/current/scripts/start-dogfood.sh
```

## WSL終了・Windows再起動後の復旧

systemd unitのenableだけではWSL instanceの常時維持やWindows起動時のdistribution起動を保証しない。Windows再起動後は次の順序で復旧する。

1. Windowsからbootstrapが`DOGFOOD_CONFIG_DIR`へ生成した`start-dogfood-wsl.ps1`を実行し、設定したdogfood distributionを明示起動する。
2. `systemctl is-system-running`と`systemctl show digital-souls-inference.target --property=ActiveState,SubState`を確認する。
3. `scripts/dogfood/status.sh`でOllama／VOICEVOXのunit、port、container metadataを確認する。
4. Ubuntu-dogfood内で`scripts/start-dogfood.sh`を起動する。
5. dogfood ProfileのFrontend／Backend ready gateを確認する。

## 障害診断と個別復旧

通常状態の確認には本文を表示しない`status.sh`を使う。障害調査で利用者がログ本文を確認する場合だけ、対象unitを限定して次を手動実行する。

```bash
systemctl show digital-souls-ollama.service --property=ActiveState,SubState,Result,ExecMainStatus
systemctl show digital-souls-voicevox.service --property=ActiveState,SubState,Result,ExecMainStatus
journalctl -u digital-souls-ollama.service --since today
journalctl -u digital-souls-voicevox.service --since today
sudo systemctl restart digital-souls-ollama.service
sudo systemctl restart digital-souls-voicevox.service
scripts/dogfood/status.sh
```

VOICEVOX processの異常終了はComposeがcontainerを再起動する。復旧しない場合やDocker daemon自体の障害では`docker ps --filter name=digital-souls-voicevox`とDocker serviceを確認し、VOICEVOX unitだけを再起動してCompose stackをdown／upする。意図的に停止する場合はVOICEVOX unitまたはinference targetをstopし、`docker compose down`でstackを削除する。dev／integration／TAKTから共通推論serviceをstopまたはrestartしない。

## 手動作業と自動検証の境界

distribution作成、Linux user／permission設定、bootstrap成功、systemd／Docker／Ollama／VOICEVOXの実起動、実会話、WSL／Windows再起動後の実復旧は利用者が手動確認する。自動テストは一時directory、fake command、静的資材だけを使い、実dogfood filesystem、process、systemd、Docker daemon、endpointへ接続しない。実会話と再起動後の受入はIssue #56へ引き渡す。
