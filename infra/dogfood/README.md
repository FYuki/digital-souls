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
sudo apt install -y git python3 docker.io curl ca-certificates zstd
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

bootstrapはdistribution名と`DS_ENVIRONMENT_ID=dogfood`、必須設定、絶対path、pathの非重複、HTTPS repository URL、完全なcommit SHAを配置前に検証する。初回と再実行のどちらもrootで指定revisionを取得し、origin、commit一致、detached HEAD、変更のないworking treeを検証してから生成資材を配置する。検証後のcloneはroot所有に収束し、application service userは変更できない。暗黙のpull、reset、deployは行わない。

bootstrapは検証済み設定からsystemd unitとWindows launcherを生成する。生成されたlauncherは`DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1`に配置されるため、Windows側から`\\wsl$`経由でコピーして使用する。unitのservice user、group、設定file、clone内runner、WSL distributionは同じ設定値から生成される。

標準配置と所有権は次のとおり。

| 対象 | 標準path | 所有者 | 用途 |
|------|----------|--------|------|
| clone | `/opt/digital-souls/current` | `root:digital-souls` | dogfood専用の読み取り専用clone |
| 設定 | `/etc/digital-souls` | `root:digital-souls` | `dogfood.env` |
| data | `/var/lib/digital-souls/data` | `digital-souls:digital-souls` | SQLite、Chroma等の永続data root |
| state | `/var/lib/digital-souls/state` | `digital-souls:digital-souls` | service state |
| log | `/var/log/digital-souls` | `digital-souls:digital-souls` | file log用directory |

directoryは`0750`、設定ファイルは`0640`を基準とする。application service userは`docker`補助groupへ所属させず、VOICEVOX Composeはroot所有のsystemd unitとroot所有cloneのrunnerだけが操作する。data、state、logをclone配下、Ubuntu-devのruntime root、TAKT worktreeへ置かない。

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
