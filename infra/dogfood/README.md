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

`env.example`をdogfood専用の一時pathへmode `0600`で作成し、repository URLとVOICEVOX imageを実環境に合わせる。revisionは秘密設定と分離した`/etc/digital-souls/dogfood.revision`へ完全なcommit SHA 1行だけを書き込む。単純な`cp infra/dogfood/env.example /tmp/dogfood.env`のまま使用してはならない。推論portはここへ追加せず、`environments/profiles/dogfood.json`を唯一の参照元にする。

```bash
dogfood_env=$(mktemp)
install -m 0600 infra/dogfood/env.example "$dogfood_env"
sudo groupadd --force --system digital-souls
sudo install -d -m 0750 -o root -g digital-souls /etc/digital-souls
printf '%s\n' '<検証済みmain commit SHA>' | sudo install -m 0640 \
  -o root -g digital-souls /dev/stdin /etc/digital-souls/dogfood.revision
sudo env DOGFOOD_ENV_FILE="$dogfood_env" WSL_DISTRO_NAME=Ubuntu-dogfood \
  scripts/dogfood/bootstrap.sh
rm -f -- "$dogfood_env"
```

bootstrap用一時envの`0600`は、内容を読み込む前の秘密保護契約である。bootstrapが正規配置する`/etc/digital-souls/dogfood.env`の`0640 root:digital-souls`とは別の契約であり、一時envへ`0640`を使用しない。bootstrapの成否を確認した後、一時envは削除する。

bootstrapはdistribution名と`DS_ENVIRONMENT_ID=dogfood`、必須設定、絶対path、pathの非重複、HTTPS repository URL、revisionファイルの完全なcommit SHAを配置前に検証する。初回だけ指定revisionを取得し、origin、commit一致、detached HEAD、変更のないworking treeを検証してcloneを作成する。再実行時は既存cloneのoriginだけを検証し、checkoutやservice restartを行わない。検証後のcloneはroot所有に収束し、application service userは変更できない。

bootstrapは検証済み設定からsystemd unitとWindows launcherを生成する。生成されたlauncherは`DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1`に配置されるため、Windows側から`\\wsl$`経由でコピーして使用する。unitのservice user、group、設定file、clone内runner、WSL distributionは同じ設定値から生成される。

標準配置と所有権は次のとおり。

| 対象 | 標準path | 所有者 | 用途 |
|------|----------|--------|------|
| clone | `/opt/digital-souls/current` | `root:digital-souls` | dogfood専用の読み取り専用clone |
| 設定 | `/etc/digital-souls` | `root:digital-souls` | `dogfood.env`、`dogfood.revision`（ともに`0640`） |
| data | `/var/lib/digital-souls/data` | `digital-souls:digital-souls` | SQLite、Chroma等の永続data root |
| backup | `/var/lib/digital-souls/backups` | `digital-souls:digital-souls` | SQLite backup世代 |
| state | `/var/lib/digital-souls/state` | `root:digital-souls` | deployment state |
| log | `/var/log/digital-souls` | `digital-souls:digital-souls` | file log用directory |

directoryは`0750`、設定ファイルは`0640`を基準とする。stateとその親pathはrootが管理し、symlinkやservice userが書き換え可能なpath要素を使用しない。application service userは`docker`補助groupへ所属させず、VOICEVOX Composeはroot所有のsystemd unitとroot所有cloneのrunnerだけが操作する。data、state、logをclone配下、Ubuntu-devのruntime root、TAKT worktreeへ置かない。

## deployとrollback

bootstrap後の昇格は、検証済みmain commitを明示して実行する。mainへのmergeだけではclone、revision、processは変化しない。

```bash
sudo scripts/dogfood/deploy.sh --commit <完全なcommit SHA>
sudo scripts/dogfood/deploy.sh --commit <完全なcommit SHA> --no-auto-rollback
sudo scripts/dogfood/rollback.sh
sudo scripts/dogfood/rollback.sh --to <保存済みcommit SHA>
```

deployはdirty checkout、origin/main上で解決できないcommit、設定不足を拒否する。毎回backupとbackup-verifyを完了してからmanifestとrevisionを更新し、detached checkout、Backend依存準備、Frontend build、権限再適用、service restart、Profile準拠readinessの順で実行する。readiness失敗時は既定で直前commitへ自動rollbackし、`--no-auto-rollback`指定時だけ現在状態を維持して停止する。backupを省略するオプションはない。

rollbackは引数なしで現在manifestの直前commitへ、`--to`で保存済みmanifestが存在する任意commitへ戻す。どちらも再build、restart、readiness確認を行うため数分かかる場合がある。

deployment manifestは`DOGFOOD_STATE_DIR/deployments/`へ`root:digital-souls`、`0640`で保存する。1操作1 JSON、`current.json`が最新状態を表し、履歴は新しい20世代だけを保持する。commit、Profile schema、SQLite data schema、backup ID、UTC deploy時刻だけを記録し、会話本文、prompt、秘密値は保存しない。`dogfood.env`はdeploy、rollback、manifest、logへ複製しない。

## Conversation historyのbackup／restore

SQLiteの`conversation-history.db`を会話履歴の正本とする。backupはSQLite公式backup APIで作成するため、WALへcommit済みで未checkpointの履歴も整合したsnapshotへ含まれる。Chromaは再構築可能な派生indexであり、backupへ含めない。

保存先はruntime data rootと分離した`DOGFOOD_BACKUP_DIR=/var/lib/digital-souls/backups`である。世代名は`backup-YYYYMMDDTHHMMSSZ-COMMIT先頭12文字-一意ID先頭12文字`、保持数は`DOGFOOD_BACKUP_RETENTION_COUNT=7`とする。完成済みの正規世代だけが古い順に整理され、不明なfile、手動directory、作業中世代は削除されない。

backupの真正性検証にはgeneration外の`DOGFOOD_BACKUP_AUTHENTICATION_KEY`を使う。`openssl rand -hex 32`で生成した64桁の16進数を`dogfood.env`へ設定し、backup作成時からrestore後検証まで同じ値を維持する。この鍵をbackup directory、コマンド引数、Issue、logへ記録しない。鍵を失った場合や変更した場合、既存backupは検証・restoreできない。

以下はすべて`digital-souls` userで実行する。backupと独立検証はdeploy前およびWave 2 #8のschema変更前に必須であり、どちらかが失敗した場合は後続のdeploy／migrationを開始しない。

実行前に履歴記録を一時停止し、rootだけが読める正規設定から認証鍵を呼び出し元shellへ読み込む。値を端末へ表示しない。backup／restore作業が終わったら必ず変数を破棄し、履歴記録を戻す。

```bash
set +o history
DOGFOOD_BACKUP_AUTHENTICATION_KEY=$(sudo awk -F= \
  '/^DOGFOOD_BACKUP_AUTHENTICATION_KEY=/{print substr($0, index($0, "=") + 1)}' \
  /etc/digital-souls/dogfood.env)
export DOGFOOD_BACKUP_AUTHENTICATION_KEY
```

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

unset DOGFOOD_BACKUP_AUTHENTICATION_KEY
set -o history
```

CLIは成功時に0、identity不一致は10、artifact不正は11、schema不一致は12、非破壊切替失敗は13、backup公開結果不確定は14、restore durability不確定は15、restore中断からの復旧要求は16、その他の入力・OSエラーは1を返す。出力する証跡は環境ID、UTC日時、commit、schema version、conversation件数、検証結果に限定する。metadata、manifest、標準出力、標準エラー、作業logへconversation本文、DB本文、環境変数全体、秘密値を転記しない。

### schema変更失敗時のrollback

Backendを停止し、変更前backupの`backup-verify`を成功させてからrestoreする。restoreはmetadata、manifest、checksum、SQLite整合性、schema、環境identityを切替前に検証し、失敗時は既存DBを維持する。
前段のcleanup後にrestoreを行う場合は、同じ履歴停止・認証鍵読込手順をもう一度実行する。

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

unset DOGFOOD_BACKUP_AUTHENTICATION_KEY
set -o history
```

identityエラーでは選択した環境と`.environment-identity.json`を照合する。artifact／schemaエラーでは対象世代を使用せず、直前の検証済み世代へ切り替える。restore safetyエラーではサービスを起動せず、既存DBが維持されていることを確認してから再試行する。

### restore中断markerからの復旧

runtime data root直下の`.conversation-history.restore-intent.json`は、restoreのDB切替が完了したと確認できない状態を示す。markerが存在する間はBackendを起動せず、通常のbackup、schema操作、会話履歴DBの参照も行わない。markerは旧WALが復元済みDBへ適用されることを防ぐ安全機構であるため、手動で削除、編集、移動しない。

復旧には、中断したrestoreで指定したものと同一のbackup generationを使い、Backendを停止したまま上記の`restore`コマンドを再実行する。認証済みmetadata、artifact checksum、environment identityがmarkerと一致した場合だけ、同じartifactによる切替とsidecar除去が再実行される。成功後に`restore-verify`を実行してからBackendを起動する。

終了コード16、異なるgeneration、または不正なmarkerが報告された場合は復旧操作を停止する。別generationへの切替やmarkerの手動修復は行わず、runtime data rootと使用したgenerationを変更しない状態で保全して調査へ引き渡す。証跡にはconversation本文、backup認証鍵、marker本文を貼り付けず、終了コード、環境ID、generation名、UTC日時だけを記録する。

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
