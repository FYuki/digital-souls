# Ubuntu-dogfood 構築・運用runbook

Ubuntu-dogfoodはUbuntu-devと別のWSL distribution、Linux filesystem、service user、runtime data rootを使用する。Ollamaのprocessはsystemdが所有し、Backend、Frontend、Whisper、VOICEVOX、LiveKitの実行中containerはDocker Composeが所有する。systemdはdogfoodの各Compose stackの起動・停止入口を担う。dev／integration／TAKTはProfileに記載された共有推論endpointを再利用する。

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

Ubuntu-dogfood内でGit、Python、Docker、NVIDIA Container Toolkit、Ollamaを導入する。Node.jsはBackend／Frontend containerの実行要件ではない。Dockerは公式apt repositoryからBuildxとCompose pluginを含めて導入する。

```bash
sudo apt update
sudo apt install -y ca-certificates curl git gnupg python3 python3-venv zstd
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl --proto '=https' --tlsv1.2 --fail --location \
  https://download.docker.com/linux/ubuntu/gpg \
  --output /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf '%s\n' \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker.service
docker compose version
docker buildx version
sudo docker info
```

NVIDIA Container Toolkitは[NVIDIA公式installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)のUbuntu／Debian手順でproduction repositoryを追加し、版を固定して導入する。次は2026-08-31時点の公式安定版を使う例である。版を更新する場合は、先に公式guideの指定値を確認する。

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor \
    -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L \
  https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
DIGITAL_SOULS_NVIDIA_TOOLKIT_VERSION=1.20.0-1
sudo apt-get install -y \
  nvidia-container-toolkit="$DIGITAL_SOULS_NVIDIA_TOOLKIT_VERSION" \
  nvidia-container-toolkit-base="$DIGITAL_SOULS_NVIDIA_TOOLKIT_VERSION" \
  libnvidia-container-tools="$DIGITAL_SOULS_NVIDIA_TOOLKIT_VERSION" \
  libnvidia-container1="$DIGITAL_SOULS_NVIDIA_TOOLKIT_VERSION"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo docker info --format '{{json .Runtimes}}'
sudo docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi
```

最後のcommandは[NVIDIA公式sample workload](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html)と同じ実動確認であり、`docker info`に`nvidia`があるだけでは完了としない。WSL内へLinux display driverは導入せず、Windows側のNVIDIA driverを使用する。bootstrapも配置変更前に`nvidia-ctk`、Dockerの`nvidia` runtime、設定済みWhisper immutable imageからの`nvidia-smi -L`を検証し、失敗時は停止する。

`docker.io`または`docker-compose`を導入済みの場合は、公式repository追加前に競合packageを削除する。既存container imageとvolumeの保全要否を確認してから実行し、導入後は上記の`docker compose version`と`sudo docker info`を両方成功させる。

```bash
sudo apt remove -y docker.io docker-compose docker-compose-v2 docker-doc \
  podman-docker containerd runc
```

Ollamaは版とSHA-256を固定した公式GitHub Release資材を一般ユーザーで取得・検証し、検証成功後だけrootで展開する。SHA-256は[Ollama公式GitHub Release v0.32.5](https://github.com/ollama/ollama/releases/tag/v0.32.5)の対象asset欄を正本とする。次はx86-64用の固定例である。

```bash
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
```

固定版を更新する場合は、Ollama公式GitHub Releasesの対象tagを開き、対象architectureのasset欄にGitHubが表示するSHA-256を取得する。tag付きURL、asset名、SHA-256を同時に更新し、`sha256sum`が失敗した場合は`sudo tar`を実行しない。`latest` URLや未検証の`install.sh`をrootで実行しない。

## 更新経路の選択

変更内容を次の表に照らし、経路①〜③のいずれかを選ぶ。distributionの作り直しは既存環境で3経路を完了できない場合の最終手段であり、3経路と同列の更新手段には含めない。変更が複数行に該当する場合や判定に迷う場合は、安全側の経路②を選ぶ。

| 変更種別・環境の状態 | 使用する更新経路 |
|---|---|
| Backend／Frontend／Whisperアプリコード | 経路① |
| `scripts/setup-backend.sh` | 経路① |
| `environments/profiles/dogfood.json` | 経路① |
| `scripts/dogfood/bootstrap.sh`／`load-environment.sh`／`render-assets.sh` | 経路② |
| `deployment-lib.sh`のうちbootstrapが使用する関数 | 経路② |
| `infra/dogfood/templates/*`／`infra/dogfood/systemd/*` | 経路② |
| `infra/dogfood/env.example`のenvキー契約の増減 | 経路② |
| service user／group／home／標準pathの定義 | 経路② |
| Docker／Compose／Buildx／NVIDIA Container Toolkitなどの依存ツール要件 | 経路② |
| partial構築または破損した環境 | 経路③ |

Backend／Frontend／Whisper imageはCIがcommit SHA tagでGHCRへ公開し、deployが3 digestを解決するため経路①で反映できる。`scripts/setup-backend.sh`はhost側Environment CLIとbackup／restoreのため`dogfood_prepare_backend`から再実行する。`environments/profiles/dogfood.json`も実行時に読み込まれるため経路①とする。

## 経路①: 通常のアプリケーション更新

bootstrap管理資材を変更せず、正常稼働している環境へアプリケーション変更を反映する場合は、`deploy.sh --commit <SHA>`だけで完結させる。具体的なコマンド、rollback、manifestの契約は「deployとrollback」に従う。

通常deployはdataディレクトリもbackupディレクトリも削除・再作成しない。既存世代を保持したまま検証済みの論理backupを追加する。ただし、サービス再起動後のmigrationや通常処理はdataを更新し得るため、実機検証前には「実機検証時のデータ保全」も実施する。

## 経路②: bootstrap管理資材のin-place更新

正常稼働環境へbootstrap管理資材を反映するときは、次の固定順序を変更しない。

1. 論理backupを作成し、`backup-verify`を成功させる。
2. `stop-services.sh`でサービスを停止する。
3. 新revisionの`bootstrap.sh`を実行する。
4. `deploy.sh --commit <同一SHA>`を実行する。
5. `status.sh`とreadinessで確認する。

事前backupが必要なのは、deployによるbackupがbootstrap後にしか実行されず、bootstrapの失敗や中断を保護できないためである。事前停止は、bootstrapがsystemd unitの差し替え、active image設定、clone全体のchown/chmodを行い、稼働中containerと競合することを防ぐ。

bootstrapはcheckoutを変更するが、backup、manifest更新、restartを行わない。bootstrap後に同一SHAのdeployを必ず実行し、deployがbackup、manifest、rollback履歴、restart、readinessを担うことで、deployment stateとサービスの状態を整合させる。readiness失敗時は、deployが既定で直前commitへ自動rollbackする。

ただし、deployment manifestまたはenvキーのcontractが変わるrevisionへ初めて移行する場合は、通常の経路②へ進む前に新revisionの`migrate-deployment-contract.sh`を1回実行する。旧revisionへ新envを読み込ませず、作業コピーにある新scriptへbootstrap用一時envを渡す。`dogfood.revision`を作業者が先にtargetへ書き換えてはならない。

```bash
sudo env DOGFOOD_ENV_FILE="$dogfood_env" WSL_DISTRO_NAME=Ubuntu-dogfood \
  scripts/dogfood/migrate-deployment-contract.sh \
  --commit '<検証済みmain commit SHA>'
```

migrationは現在のcleanなdetached HEADを移行元として固定し、旧schemaの全manifestを検証して`DOGFOOD_STATE_DIR/deployments/legacy-v0-<from>-to-<target>/`へ保全し、migration markerと`dogfood.revision`を原子的にtargetへ収束させる。manifestの削除、手編集、作業者による手動退避は行わない。その後、同じ一時envとSHAでbootstrap、deployを続ける。

旧contractにはDocker image digestが存在せず、新envを旧revisionが拒否するため、この境界を越える自動rollbackは安全に構成できない。最初の新contract deployは`previousCommit: null`の新baselineとし、失敗時は旧commitへ自動rollbackせず停止する。検証済み論理backup、filesystem保全、legacy archiveを維持したまま原因を修正して同じtargetへ再deployする。成功時はmarkerをlegacy archive内の`migration.json`へ移し、以後の同一contract内deployは通常どおり直前commitへ自動rollbackする。

対象SHAは`origin/main`の祖先commitだけに限定する。実機検証で問題が判明した場合は、未mergeのrevisionへ切り替えず、修正commitをmainへ積んで同じ経路を再実行する前進復旧を行う。

## 実機検証時のデータ保全

bootstrap／deploy変更の実機検証を始める前に論理backupを作成し、`backup-verify`を成功させる。会話本文、backup認証鍵、秘密値はコマンド出力、Issue、検証logへ記録しない。

`stop-services.sh`でサービスを停止した後に、filesystem単位の退避を実施する。稼働中SQLiteの単純コピーは禁止する。少なくとも次を保全対象とする。

- `dogfood.env`
- backup認証鍵
- `dogfood.revision`
- data root
- backup generations
- deployment state
- Ollama model
- Whisper model cache
- `dogfood-images.env`

`/var/lib/digital-souls`の全削除を通常の検証・復旧手段としない。やむを得ず再構成する場合の退避先は、`/var/lib/digital-souls`外かつdata root外の独立path（例: `/var/tmp/digital-souls-preserve-<UTC timestamp>`）とし、root所有、mode `0700`にする。

復元時は所有者と権限を標準配置へ再適用する。元データは、`backup-verify`、environment identityの確認、`status.sh`、readinessがすべて成功するまで削除しない。退避先も復元成功を確認するまで維持する。

## 設定とbootstrap

GHCR packageがprivateの場合は、[GitHub公式Container registry手順](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)に従い、`read:packages`だけを持つpersonal access token (classic)をrootのDocker credentialへbootstrap前に登録する。tokenは対話入力し、`dogfood.env`、shell history、Issueへ保存しない。

```bash
(
  set -euo pipefail
  trap 'unset DIGITAL_SOULS_GHCR_TOKEN' EXIT
  read -r -s -p 'GHCR read:packages token: ' DIGITAL_SOULS_GHCR_TOKEN
  printf '\n'
  printf '%s' "$DIGITAL_SOULS_GHCR_TOKEN" \
    | sudo docker login ghcr.io --username '<GitHub user>' --password-stdin
)
```

CIが公開した対象commit SHA tagからBackend、Frontend、Whisperのmanifest digestをroot credentialで解決する。取得したdigest単体ではなく、次の出力どおり完全な`repository@sha256:...`を一時envへ設定する。Ollamaはhost process、VOICEVOXとLiveKitは別の固定tag設定であり、この3 digestの対象ではない。

```bash
(
  set -euo pipefail
  DIGITAL_SOULS_TARGET_COMMIT='<検証済みmain commit SHA>'
  for DIGITAL_SOULS_COMPONENT in backend frontend whisper; do
    DIGITAL_SOULS_REPOSITORY="ghcr.io/<owner>/digital-souls-$DIGITAL_SOULS_COMPONENT"
    DIGITAL_SOULS_DIGEST=$(sudo docker buildx imagetools inspect \
      "$DIGITAL_SOULS_REPOSITORY:$DIGITAL_SOULS_TARGET_COMMIT" \
      --format '{{.Manifest.Digest}}')
    if ! [[ "$DIGITAL_SOULS_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "ERROR: $DIGITAL_SOULS_COMPONENT imageのdigestが不正です" >&2
      exit 1
    fi
    printf 'DOGFOOD_%s_IMAGE=%s@%s\n' \
      "$(printf '%s' "$DIGITAL_SOULS_COMPONENT" | tr '[:lower:]' '[:upper:]')" \
      "$DIGITAL_SOULS_REPOSITORY" "$DIGITAL_SOULS_DIGEST"
  done
)
```

`env.example`をdogfood専用の一時pathへmode `0600`で作成し、repository URL、上で取得した3つの完全なimmutable image、VOICEVOX／LiveKit image、LiveKit API key／secretを実環境に合わせる。`LIVEKIT_URL`はdogfood Profileと同じ`ws://127.0.0.1:17880`から変更しない。key／secretは`livekit-server generate-keys`または安全な乱数生成器で新規作成し、端末出力、shell history、Issue、Gitへ残さない。通常bootstrapではrevisionを秘密設定と分離した`/etc/digital-souls/dogfood.revision`へ完全なcommit SHA 1行だけで書く。contract migration時は`migrate-deployment-contract.sh`がrevisionを更新するため、作業者は書き換えない。単純な`cp infra/dogfood/env.example /tmp/dogfood.env`のまま使用してはならない。service portはここへ追加せず、`environments/profiles/dogfood.json`を唯一の参照元にする。

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

bootstrapはdistribution名と`DS_ENVIRONMENT_ID=dogfood`、必須設定、絶対path、pathの非重複、HTTPS repository URL、revisionファイルの完全なcommit SHA、3つのGHCR immutable digest、Docker group、`docker compose version`、`docker buildx version`を配置前に検証する。さらに、root credentialで3 imageをdigestまで解決し、Dockerの`nvidia` runtimeとimmutable Whisper imageによるGPU実動確認を成功させる。これによりGHCR未認証、存在しないdummy digest、NVIDIA runtime未設定をservice起動前に拒否する。初回は指定revisionを取得し、origin、commit一致、detached HEAD、変更のないworking treeを検証してcloneを作成する。再実行時もoriginを検証してrevisionをfetch・解決し、既存cloneがcleanかつdetached HEADの場合だけ指定revisionへcheckoutする。差分またはbranchを検出した場合は内容を報告し、resetやcleanを行わず停止する。

service userのhomeは`DOGFOOD_SERVICE_HOME_DIR`、Ollama modelは`DOGFOOD_OLLAMA_MODELS_DIR`へ分離する。既存userではhome、primary group、shellだけを収束し、旧homeのfileは移動・削除しない。backupとbackup-verifyは`GIT_CONFIG_GLOBAL`を明示し、service userのglobal Git設定としてこのhome直下の`.gitconfig`だけを使用する。bootstrapを唯一の収束点とし、`safe.directory`は`realpath`で正規化した`DOGFOOD_CLONE_DIR` 1件へ毎回上書きするため、手動追加した別pathや`*`は次回bootstrapで除去される。`.gitconfig`内の他キーは維持し、所有者をservice user、modeを`0640`へ収束する。

ただし、service userの`.gitconfig`がsymlinkの場合は`ERROR: service userの.gitconfigにsymlinkは使用できません`、存在するが通常ファイルでない場合は`ERROR: service userの.gitconfigが通常ファイルではありません`として、bootstrapは自動修復せず停止する。また、`.gitconfig`の`[include]`または`[includeIf]`が参照する別ファイルに`safe.directory`がある場合も、`ERROR: include経由のsafe.directoryは使用できません`として停止する。通常ファイルの`.gitconfig`へ直接記述された`safe.directory`だけが上書き対象であり、これらの異常配置は除去対象に含まれない。

失敗時は表示された`ERROR`を確認し、対象を保全してから復旧する。symlinkまたは非通常ファイルはリンク先や内容を確認・退避したうえでその特殊な配置を取り除き、service user所有の通常ファイルとして`.gitconfig`を用意する。include経由の場合は、次の読取専用コマンドで各値の定義元を確認し、include元ファイルから`safe.directory`だけを取り除く。他のGit設定とinclude自体は維持する。修正後は上記と同じ手順で一時envを作り直し、bootstrapを再実行する。

```bash
sudo -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  GIT_CONFIG_GLOBAL=/var/lib/digital-souls/home/.gitconfig \
  git -C /opt/digital-souls/current config \
  --global --includes --show-origin --get-all safe.directory
```

bootstrapはhost側Environment CLI用のBackend venvを準備し、初期3 digestをroot専用の`dogfood-images.env`へ原子的に配置する。アプリケーションimageをhostでbuildせず、サービスも起動しない。初回はbootstrap後に`digital-souls-dogfood.target`を起動し、application containerのBackend起動によって`conversation-history.db`を作成する。

`dogfood-images.env`は`0600 root:root`を維持し、active digestを必要とするroot control planeとWhisper runnerだけが明示的に読み込む。非rootで常駐するOllamaと、固定tag設定を使うVOICEVOX／LiveKit runnerはこのファイルを読み込まない。汎用のroot運用入口は従来どおりactive digestを読み、deployは切替前のdigestを保持するため明示的に読み込む。

更新時は、運用者の作業コピーにある新revisionの`bootstrap.sh`を実行する。bootstrapがdogfood cloneを指定revisionへ収束させた後に、そのrevisionのloaderで正規envを配置する。この順序により、旧revisionの`load-environment.sh`へ新しいenvキーを先に渡す過渡状態を避ける。

bootstrapは検証済み設定からsystemd unit、LiveKit Server設定、Backend専用のLiveKit環境ファイル、Windows launcherを生成する。`livekit.yaml`と`livekit-backend.env`は`DOGFOOD_CONFIG_DIR`へ`0640 root:digital-souls`で配置する。Backend専用ファイルに含めるのは`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`だけであり、backup認証鍵を含む`dogfood.env`全体はapplication processへ渡さない。生成されたlauncherは`DOGFOOD_CONFIG_DIR/start-dogfood-wsl.ps1`に配置されるため、Windows側から`\\wsl$`経由でコピーして使用する。unitのservice user、group、設定file、clone内runner、WSL distributionは同じ設定値から生成される。

標準配置と所有権は次のとおり。

| 対象 | 標準path | 所有者 | 用途 |
|------|----------|--------|------|
| clone | `/opt/digital-souls/current` | `root:digital-souls` | dogfood専用の読み取り専用clone |
| 設定 | `/etc/digital-souls` | `root:digital-souls` | 通常設定は`0640`、active digestと生成container envは`0600 root:root` |
| data | `/var/lib/digital-souls/data` | `digital-souls:digital-souls` | SQLite、Chroma等の永続data root |
| service home | `/var/lib/digital-souls/home` | `digital-souls:digital-souls` | `.gitconfig`、Ollama設定・鍵などのhome生成物 |
| Ollama model | `/var/lib/digital-souls/models/ollama` | `digital-souls:digital-souls` | DL済みmodel |
| Whisper cache | `/var/lib/digital-souls/models/whisper` | `10001:10001` | 共有Whisper専用cache。会話dataを置かない |
| backup | `/var/lib/digital-souls/backups` | `digital-souls:digital-souls` | SQLite backup世代 |
| state | `/var/lib/digital-souls/state` | `root:digital-souls` | deployment state |
| log | `/var/log/digital-souls` | `digital-souls:digital-souls` | file log用directory |

directoryは`0750`、設定ファイルは`0640`を基準とする。stateとその親pathはrootが管理し、symlinkやservice userが書き換え可能なpath要素を使用しない。application containerはhostのservice UID／GIDで非root実行し、Docker socketをmountしない。Backend／Frontend／Whisper／VOICEVOX／LiveKit Composeはroot所有のsystemd unitまたはrootのdogfood deployだけが操作する。data、state、logをclone配下、Ubuntu-devのruntime root、TAKT worktreeへ置かない。

## deployとrollback

bootstrap後の昇格は、検証済みmain commitを明示して実行する。mainへのmergeだけではclone、revision、processは変化しない。

```bash
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/deploy.sh --commit <完全なcommit SHA>
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/deploy.sh --commit <完全なcommit SHA> --no-auto-rollback
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/rollback.sh
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/rollback.sh --to <保存済みcommit SHA>
```

`sudo`は既定で`WSL_DISTRO_NAME`を引き継がないため、rootで直接実行する手順では明示的に渡す。`wslinfo --name`は利用可能な環境でだけfallbackとして使用し、distributionを解決できない場合は推測せず拒否する。

deployはdirty checkout、origin/main上で解決できないcommit、設定不足を拒否する。最初に3つのGHCR commit SHA tagをBuildxでimmutable digestへ解決する。`conversation-history.db`がなければBackend依存の準備より前に初回起動を案内して、backup、manifest、revision、checkoutを変更せず停止する。DBが存在する場合だけ、backup前に現在HEADのBackend依存を準備し、backupとbackup-verifyを完了してからmanifestとrevisionを更新する。その後、detached checkout、Backend依存準備、3 digestの原子的配置、権限再適用、service restart、Profile準拠readinessの順で実行する。`Type=simple`のrestart完了はapplication readinessを意味しないため、deployとrollbackはFrontend／Backendを1秒間隔、最大180回、request timeout 2秒で有限待機する。単発probeの`not_ready`ではrollbackせず、待機timeout後だけ既定で直前commitと3 digestへ自動rollbackする。`--no-auto-rollback`指定時だけ現在状態を維持して停止する。backupを省略するオプションはない。

rollbackは引数なしで現在manifestの直前commitへ、`--to`で保存済みmanifestが存在する任意commitへ戻す。rollback先manifestのSQLite data schemaと現在DBのschemaが一致しない場合は、保存済みbackupを検証・restoreするまでcommitの切替を拒否する。保存済み3 digestが欠落・不正な旧manifestも拒否する。どちらもimage pull、restart、readiness確認を行うため数分かかる場合がある。

自己参照manifest（`previousCommit == targetCommit`）を発見した場合は、引数なしrollbackを実行せず、manifestも手編集しない。保存済み世代から復旧対象を確認し、そのmanifestのschemaとcommit SHAを検証したうえで、`rollback.sh --to <SHA>`により明示的にrollbackする。

初回deployでは、直前のcommitが存在しないことを`previousCommit: null`で表す。この状態では引数なしrollbackもreadiness失敗時の自動rollbackも実行できない。原因調査後、検証済みの保存世代があれば`rollback.sh --to <SHA>`で明示的に戻し、保存世代がなければ問題を修正して再deployする。

deployment manifestは`DOGFOOD_STATE_DIR/deployments/`へ`root:digital-souls`、`0640`で保存する。1操作1 JSON、`current.json`が最新状態を表し、履歴は新しい20世代だけを保持する。commit、Profile schema、SQLite data schema、backup ID、Backend／Frontend／Whisper digest、UTC deploy時刻だけを記録し、会話本文、prompt、秘密値は保存しない。`dogfood.env`はdeploy、rollback、manifest、logへ複製しない。

## SQLite artifactのbackup／restore

SQLiteの`conversation-history.db`を会話履歴、`persona-memory.db`を人格記憶の正本とし、同じgenerationで一組としてbackup／restoreする。backupはSQLite公式backup APIで作成するため、WALへcommit済みで未checkpointのデータも整合したsnapshotへ含まれる。Chromaは再構築可能な派生indexであり、backupへ含めない。

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
  HOME=/var/lib/digital-souls/home \
  GIT_CONFIG_GLOBAL=/var/lib/digital-souls/home/.gitconfig \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py backup \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-root /var/lib/digital-souls/backups --retention-count 7

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  GIT_CONFIG_GLOBAL=/var/lib/digital-souls/home/.gitconfig \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py backup-verify \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID

unset DOGFOOD_BACKUP_AUTHENTICATION_KEY
set -o history
```

CLIは成功時に0、identity不一致は10、artifact不正は11、schema不一致は12、非破壊切替失敗は13、backup公開結果不確定は14、restore durability不確定は15、restore中断からの復旧要求は16、その他の入力・OSエラーは1を返す。`backup-verify`、`restore`、`restore-verify`の成功時JSONは、従来のトップレベル`schemaVersion`と`conversationCount`を返さず、`artifacts`配列の各要素に`filename`、`schemaVersion`、`recordCount`を返す。既存の運用scriptや手順で旧fieldを参照している場合は、対象artifactの`filename`で配列要素を選択して新fieldを読むよう更新する。出力する証跡は環境ID、UTC日時、commit、artifactごとのschema version、record件数、検証結果に限定する。metadata、manifest、標準出力、標準エラー、作業logへconversation本文、DB本文、環境変数全体、秘密値を転記しない。

### schema変更失敗時のrollback

Backendを停止し、変更前backupの`backup-verify`を成功させてからrestoreする。restoreはmetadata、manifest、checksum、SQLite整合性、schema、環境identityを切替前に検証し、失敗時は既存DBを維持する。
前段のcleanup後にrestoreを行う場合は、同じ履歴停止・認証鍵読込手順をもう一度実行する。

```bash
sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/data \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py restore \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
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

runtime data root直下の`.sqlite-restore-intent.json`は、restoreのDB切替が完了したと確認できない状態を示す。markerが存在する間はBackendを起動せず、通常のbackup、schema操作、会話履歴DBと人格記憶DBの参照も行わない。markerは旧WALが復元済みDBへ適用されることを防ぐ安全機構であるため、手動で削除、編集、移動しない。

復旧には、中断したrestoreで指定したものと同一のbackup generationを使い、Backendを停止したまま上記の`restore`コマンドを再実行する。認証済みmetadata、artifact checksum、environment identityがmarkerと一致した場合だけ、同じartifactによる切替とsidecar除去が再実行される。成功後に`restore-verify`を実行してからBackendを起動する。

終了コード16、異なるgeneration、または不正なmarkerが報告された場合は復旧操作を停止する。別generationへの切替やmarkerの手動修復は行わず、runtime data rootと使用したgenerationを変更しない状態で保全して調査へ引き渡す。証跡にはconversation本文、backup認証鍵、marker本文を貼り付けず、終了コード、環境ID、generation名、UTC日時だけを記録する。

### Issue #56 restore drill

実drillは本タスクの自動テストでは行わずIssue #56で実施する。読み取り専用cloneは`/opt/digital-souls/current`（`root:digital-souls`）、実data rootは`/var/lib/digital-souls/data`（`digital-souls:digital-souls`）である。上記backupを作成・検証後、`digital-souls:digital-souls`所有の空の別data rootを用意し、同じenvironment identity markerを初期化したうえで、`DS_DATA_DIR`だけを別rootへ差し替えてrestoreとrestore-verifyを実行する。

```bash
sudo install -d -m 0750 -o digital-souls -g digital-souls \
  /var/lib/digital-souls/restore-drill
```

```bash
sudo -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/restore-drill \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py init-data-root \
  --environment dogfood --repository-root /opt/digital-souls/current
```

認証鍵の読込、restore、restore-verifyは以下をまとめて同じshellへ貼り付けて実行する。サブシェル内でのみ認証鍵をexportし、正常終了時と割り込み時のどちらでも認証鍵を破棄してhistory設定を開始時の状態へ戻す。

```bash
(
set +x
DOGFOOD_RESTORE_DRILL_HISTORY_STATE=$(
  set -o | awk '$1 == "history" {print $2}'
)

dogfood_restore_drill_cleanup() {
  unset DOGFOOD_BACKUP_AUTHENTICATION_KEY
  if [ "$DOGFOOD_RESTORE_DRILL_HISTORY_STATE" = on ]; then
    set -o history
  else
    set +o history
  fi
}

dogfood_restore_drill_abort() {
  status=$1
  dogfood_restore_drill_cleanup
  trap - EXIT INT TERM HUP
  exit "$status"
}

trap dogfood_restore_drill_cleanup EXIT
trap 'dogfood_restore_drill_abort 130' INT
trap 'dogfood_restore_drill_abort 143' TERM
trap 'dogfood_restore_drill_abort 129' HUP

set +o history
DOGFOOD_BACKUP_AUTHENTICATION_KEY=$(sudo awk -F= \
  '/^DOGFOOD_BACKUP_AUTHENTICATION_KEY=/{print substr($0, index($0, "=") + 1)}' \
  /etc/digital-souls/dogfood.env)
export DOGFOOD_BACKUP_AUTHENTICATION_KEY

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/restore-drill \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py restore \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID \
  || dogfood_restore_drill_abort $?

sudo --preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  DS_ENVIRONMENT_ID=dogfood DS_DATA_DIR=/var/lib/digital-souls/restore-drill \
  /opt/digital-souls/current/backend/.venv/bin/python \
  /opt/digital-souls/current/environments/environment_cli.py restore-verify \
  --environment dogfood --repository-root /opt/digital-souls/current \
  --backup-directory /var/lib/digital-souls/backups/backup-YYYYMMDDTHHMMSSZ-COMMIT-UNIQUEID
)
```

一次証跡にはenvironment ID、UTC日時、commit、schema、conversation件数、検証結果だけを残す。その後Backendを別rootで起動し、readiness、schema、件数、既存conversationを指定した履歴再開が成功することを確認する。会話本文や秘密値は端末logやIssue本文へ記録しない。drill終了後に通常rootへ戻し、再度readinessを確認する。

## 起動・停止・状態確認

```bash
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/start-services.sh
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/status.sh
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/restart-services.sh
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/stop-services.sh
```

`digital-souls-inference.target`がOllama、VOICEVOX、Whisperをまとめる。Ollama unitは失敗時再起動を担い、VOICEVOX／Whisper unitはrootで各Compose stackを起動・停止するoneshotの入口に限定する。LiveKitは推論層へ含めず、独立した`digital-souls-livekit.service`がhost networkのCompose stackを操作する。実行中containerはComposeのrestart方針で異常終了後に再起動する。停止timeoutは各unitとも有限であり、OllamaはSIGTERM、containerは`docker compose down`で正常停止する。Backend／Frontendはenvironment専用のapplication Compose projectとして起動する。

`digital-souls-dogfood.target`は推論target、LiveKit、`digital-souls-application.service`を`Requires`／`After`で束ねる。Whisper unitはComposeの`--wait --wait-timeout 600`でhealthcheck完了まで最大10分待ち、systemdの`TimeoutStartSec=900s`でその前段のimage pullにも最大5分の猶予を持たせる。この範囲で初回model download、CUDA初期化、検証推論を完了する。推論targetがWhisper readyになる前にactiveへ進まないため、application側の30秒待機へWhisper cold startを押し付けない。

application unitは推論targetとLiveKitの起動後、`wait-inference.sh`でOllama、VOICEVOX、Whisper、LiveKitのHTTP readinessを有限時間再確認してから、rootのhost control planeとして`scripts/start-dogfood.sh`を実行する`Type=simple` unitである。`start-dogfood.sh`は`environment_cli.py up`を`exec`し、ready後もsupervisionとcleanupを担う同一processをforegroundに維持する。readyを確認すると子orchestratorを残して正常終了する汎用`environments/up.sh`はsystemdの`ExecStart`に使用しない。これによりsystemdはapplication processの生存を追跡し、ready直後の正常終了と誤認して`ExecStop`を呼ぶことがない。明示停止時は`down.sh`がrun reportのprocess identityへSIGTERMを送り、foreground orchestratorが所有するBackend／Frontendをcleanupする。

Backend／Frontend container自体はservice UID／GIDで非root実行する。target restart直後のOllama GPU検出中に`/api/tags`が一時的な500を返しても、applicationの一発検証を先に実行しない。待機がtimeoutした場合はapplicationを起動せず、systemdとjournalへ失敗を残す。Backendへ渡す秘密は専用ファイル内のLiveKit 3設定に限定し、Frontendへ`DOGFOOD_BACKUP_AUTHENTICATION_KEY`を渡さない。通常起動は上記`start-services.sh`またはWindows launcherを使う。どちらも`systemctl start digital-souls-dogfood.target`へ委譲するため、PC／WSL再起動後も事前停止なしで同じ入口を実行でき、起動済みならno-opとなる。Windows launcherは`wsl.exe`の非ゼロ終了を失敗として通知する。

`status.sh`はidentity、runtime root、unit、container identity、listen port、CPU、memory、GPU、各containerのmetadataだけを表示する。会話、DB、永続data、journal本文、LiveKit資格情報は読まない。application unitがactiveなのにrun reportのcontainer ID／起動時刻と実containerが一致しない場合は異常終了し、`restart-services.sh`を案内する。

## 共通推論サービスとmodel移行

VRAM制約下でdevとdogfoodを並行稼働するため、Ollama、VOICEVOX、WhisperはUbuntu-dogfood側の1 instanceへ集約する。これは#50の環境別サービス分離に対する明示的な例外である。`dev.json`、`integration-voice.json`、`dogfood.json`は3サービスを`source: external`として同じloopback endpoint（Ollama `11434`、VOICEVOX `50021`、Whisper `50022`）で参照する。Ubuntu-dev側は共通サービスを起動・停止・cleanupせず、Ubuntu-devに別途導入済みのOllama systemd unitは停止・無効化する。

```bash
sudo systemctl disable --now ollama
```

会話履歴、SQLite、Chroma、data rootの分離は、従来どおり環境ごとの`DS_DATA_DIR`とidentity markerで維持する。VOICEVOXは`voicevox/voicevox_engine:cpu-*`を既定とする。GPU版への移行は専用GPU確保後に別Issueで扱い、本タスクではGPU化もIssue起票も行わない。

WhisperはRTX 4070 Ti SUPER 16GB向けに`medium`、`device=cuda`、`compute_type=int8_float16`、device index 0、model instance 1、global inflight 1で固定する。CPU fallbackは行わず、CUDAまたは最小推論に失敗した場合は`/health/ready`を成功させない。model revision、faster-whisper、CTranslate2、CUDA、cuDNN、base imageを固定し、`/version`で本文を含まないruntime情報を確認できる。音声と文字起こし本文は保存・access log出力しない。

同時requestは待ち行列へ入れずcapacity超過でfail fastする。推論timeoutではworkerを破棄し、次requestでmodelを再生成する。model cacheは会話data rootと分離した`DOGFOOD_WHISPER_MODEL_CACHE`へ置く。通常のapplication deployはBackend／Frontend／Whisperの同一commit digestを一組で切り替える。

旧`$DS_DATA_DIR/ollama/models`のmodelはbootstrapが移動しない。既存modelを使う場合はサービス停止後に`blobs`と`manifests`を新保存先へ手動で移動し、所有権を収束させる。

```bash
sudo systemctl stop digital-souls-ollama.service
sudo mv /var/lib/digital-souls/data/ollama/models/blobs \
  /var/lib/digital-souls/models/ollama/
sudo mv /var/lib/digital-souls/data/ollama/models/manifests \
  /var/lib/digital-souls/models/ollama/
sudo chown -R digital-souls:digital-souls \
  /var/lib/digital-souls/models/ollama
```

再取得する場合は旧modelを動かさず、新HOMEとmodel保存先を明示してBackend既定のchat modelをpullする。
`source: external`のserviceはorchestratorの自動prepare対象ではないため、modelのpullは初回起動前に運用者が行う。

```bash
sudo -u digital-souls env \
  HOME=/var/lib/digital-souls/home \
  OLLAMA_MODELS=/var/lib/digital-souls/models/ollama \
  ollama pull gemma4:e4b
```

required model名の正本は`backend/app/model_settings.py`の`OLLAMA_MODEL_NAME`であり、未指定時の
`OLLAMA_CHAT_MODEL`へ使われる。既定値を変更する場合は、このpullコマンドを同時に更新する。
環境変数で`OLLAMA_CHAT_MODEL`を上書きする場合は、上記コマンド末尾をeffectiveなmodel名へ置き換える。
readiness検証も解決済みの`OLLAMA_CHAT_MODEL`を使用するため、既定値と上書きのどちらでも不足model名を表示する。

旧homeだったdata root直下の`.ollama`、`.cache`等は自動削除しない。`sudo ls -la /var/lib/digital-souls/data`で内容と必要性を利用者が確認し、保全後に個別判断する。

## 経路③: partial構築／破損状態からの障害復旧

distributionの作り直しではなく、次の順序で既存環境を収束させる。root操作と実Ubuntu-dogfoodへの適用は利用者が実施し、AI／TAKTは実行しない。

1. 収束操作より前に論理backupを作成し、`backup-verify`を成功させる。検証に失敗した場合は復旧を開始しない。
2. `stop-services.sh`でapplicationと推論層を含むdogfood target全体を停止する。

   ```bash
   sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/stop-services.sh
   ```

3. 「実機検証時のデータ保全」に従い、停止後にfilesystem単位で同節の9項目を独立した保全先へ退避する。特に`DOGFOOD_BACKUP_AUTHENTICATION_KEY`を失うと既存backupを永久に検証・restoreできないため、秘密を表示せずmode `0600`で保全する。稼働中SQLiteの単純コピーは行わない。
4. 上記の競合package削除手順を経てDocker公式repositoryとCompose pluginへ移行する。
5. 修正版`bootstrap.sh`をrootで再実行し、service user、home、model保存先、所有権、権限を冪等に収束させる。

   ```bash
   sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/bootstrap.sh
   ```

6. `conversation-history.db`が存在しない場合だけ、dogfood targetをrootで起動してBackendの初回DB作成を完了する。DBが存在する場合はこの手順を省略する。deployはDBが存在しなければbackup、manifest、revision、checkoutを変更せず停止するため、次の手順へ進む前にDBの存在を確認する。

   ```bash
   sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/start-services.sh
   ```

7. bootstrapがcheckoutした同一SHAを指定して`deploy.sh --commit <同一SHA>`を実行する。これによりbackup、manifest、rollback履歴、restart、readinessと、readiness失敗時の既定の自動rollbackを正規deploy経路へ戻す。

8. root権限で`status.sh`とreadinessを確認し、Docker daemonとCompose pluginの状態も確認する。

   ```bash
   sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/status.sh
   sudo docker compose version
   sudo docker info
   ```

9. data root直下の`.ollama`、`.cache`等の旧home残骸を手動確認し、必要なものを保全してから個別に整理する。

in-place復旧が失敗した場合だけ、保全物を維持したまま別名のdogfood distributionを新規作成し、設定とdataを検証しながら復元する。元distributionは復元完了まで削除しない。

非rootかつ非対話実行などroot操作を継続できない場合、スクリプトは終了コード`3`と貼り付け可能な`sudo env ...`コマンドを表示する。設定・identity等の検証失敗を表す終了コード`2`とは区別する。表示されたrootコマンドは利用者が内容を確認して実行する。

## WSL終了・Windows再起動後の復旧

systemd unitのenableだけではWSL instanceの常時維持やWindows起動時のdistribution起動を保証しない。Windows再起動後は次の順序で復旧する。

1. Windowsからbootstrapが`DOGFOOD_CONFIG_DIR`へ生成した`start-dogfood-wsl.ps1`を実行し、設定したdogfood distributionを明示起動する。
2. launcherは冪等な`systemctl start digital-souls-dogfood.target`だけを実行するため、事前停止せず同じlauncherを再実行する。
3. `systemctl is-system-running`と`systemctl show digital-souls-dogfood.target --property=ActiveState,SubState`を確認する。
4. `scripts/dogfood/status.sh`でapplication／推論unit、orchestrator、port、container metadataを確認する。
5. dogfood ProfileのFrontend／Backend ready gateを確認する。

## 障害診断と個別復旧

通常状態の確認には本文を表示しない`status.sh`を使う。障害調査で利用者がログ本文を確認する場合だけ、対象unitを限定して次を手動実行する。

```bash
systemctl show digital-souls-ollama.service --property=ActiveState,SubState,Result,ExecMainStatus
systemctl show digital-souls-voicevox.service --property=ActiveState,SubState,Result,ExecMainStatus
systemctl show digital-souls-whisper.service --property=ActiveState,SubState,Result,ExecMainStatus
journalctl -u digital-souls-ollama.service --since today
journalctl -u digital-souls-voicevox.service --since today
journalctl -u digital-souls-whisper.service --since today
sudo systemctl restart digital-souls-ollama.service
sudo systemctl restart digital-souls-voicevox.service
sudo systemctl restart digital-souls-whisper.service
scripts/dogfood/status.sh
```

`status.sh`が「application unitはactiveだがorchestrator processが存在しない」と報告した場合は、次を実行してtarget全体を再起動し、もう一度状態を確認する。

```bash
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/restart-services.sh
sudo env WSL_DISTRO_NAME=Ubuntu-dogfood scripts/dogfood/status.sh
```

VOICEVOX／Whisper processの異常終了はComposeがcontainerを再起動する。復旧しない場合やDocker daemon自体の障害では対象containerとDocker serviceを確認し、対象unitだけを再起動してCompose stackをdown／upする。意図的に停止する場合は対象unitまたはinference targetをstopし、`docker compose down`でstackを削除する。dev／integration／TAKTから共通推論serviceをstopまたはrestartしない。

## 手動作業と自動検証の境界

distribution作成、Linux user／permission設定、bootstrap成功、systemd／Docker／Ollama／VOICEVOX／Whisperの実起動、実会話、WSL／Windows再起動後の実復旧は利用者が手動確認する。Issue #135 Goal 1の自動テストは一時directory、fake command、静的資材、開発環境のDocker Composeを使用するが、実Ubuntu-dogfoodのfilesystem、process、systemd、endpointは操作しない。RTX 4070 Ti SUPER上のCUDA／VRAM、dev・dogfood同時会話、連続会話品質、再起動後の受入はGoal 2へ引き渡す。
