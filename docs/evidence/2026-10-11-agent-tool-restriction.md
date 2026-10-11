# 実行基盤の候補の確認：Agent のツールを Core の MCP だけに限定できるか

日付: 2026-10-11

対象: [Issue #155](https://github.com/FYuki/digital-souls-core/issues/155) / [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)

起点: `origin/epic/poc-adr-realignment` / `731bf9be8ae8b48c38c4c18afdfadf657cad67b7`

## 目的

会話外の活動の実行基盤に汎用 Agent を使う条件（Epic #150 R5）を確かめます。

- Agent 単位で、使えるツールを Core の MCP だけに限定できる。
- 権限確認なしのモード（bypassPermissions）でも、設定で強制できる。
- 開発用の Agent の設定に影響しない。

同日、ユーザーから「禁止方式（deny の列挙）ではなく許可方式（allowlist）にできないか」という質問があり、
CLI の許可方式のフラグも確認しました（後半の「許可方式」）。

2026-10-11 のユーザー決定により、候補は T3 の専用プロジェクト（Claude Code の project 設定）だけです。
OpenClaw の Agent ごとの `tools.allow` は確認しません（検討して採用しない）。

## 環境

- Claude Code CLI 2.1.296（`claude -p`、headless）。T3 が使う Claude Code と同じ project 設定（`.claude/settings.json`・`.mcp.json`）を読む。
- 作業場所は `/dev/shm` の使い捨てディレクトリ。リポジトリと利用者の設定ファイルは変更していない。
- Core の MCP はまだないため、標準ライブラリだけで書いたダミーの stdio MCP サーバー
  [dummy_core_mcp.py](2026-10-11-agent-tool-restriction/dummy_core_mcp.py)（ツール `core_echo` が1つ）を使った。
- 利用者単位の設定には、MCP サーバー1件と claude.ai のコネクタ2件がある（名前は設定ファイルの deny を参照）。

## 禁止方式の設定

専用プロジェクトの `.mcp.json` は [mcp.json](2026-10-11-agent-tool-restriction/mcp.json)、
`.claude/settings.json` は [settings.json](2026-10-11-agent-tool-restriction/settings.json) です。

- `enableAllProjectMcpServers: false` と `enabledMcpjsonServers: ["core"]` で、project の MCP をダミーだけにする。
- `permissions.deny` に、このバージョンで提供される組み込みツールと、利用者単位の MCP・コネクタを列挙する。
- `permissions.allow` は `mcp__core` だけ。

## 禁止方式（project 設定の deny）

### 手順と結果

| # | 内容 | 結果 |
| --- | --- | --- |
| 1 | 専用プロジェクトで、deny を設定する前に `--permission-mode bypassPermissions` で起動し、init の tool 一覧を見る | 組み込みツール（Bash・Read・Write・WebFetch 等）、利用者単位の MCP、コネクタ、ダミーの `mcp__core__core_echo` が並ぶ。設定しなければ限定されないことを確認 |
| 2 | 上の設定で同じモードで起動し、Bash でファイル作成、Read でファイル読み取り、任意のツールで一覧取得、`core_echo` 呼び出しを指示する | **PASS**。init の tool 一覧は `mcp__core__core_echo` だけ。Agent は Bash・Read を呼べず、ファイルは作られていない（`ls` で確認）。`core_echo` は成功（`core_echo:hello`） |
| 3 | 別のディレクトリ（設定なし）で同じモードで起動し、tool 一覧を見る | **PASS**。36個のツールがあり、Bash・Read が使える。`mcp__core__core_echo` はない。専用プロジェクトの設定は他の場所へ影響しない |
| 4 | 利用者の `~/.claude/settings.json` が変わっていないこと | **PASS**。更新日時は確認前（2026-10-06）のまま |
| 5 | T3 アプリから専用プロジェクトのスレッドを起動して同じ確認をする | **NOT RUN**。T3 のプロジェクト・スレッドの作成はユーザーの判断が必要なため、未実施 |

### 禁止方式で分かった制約

- 組み込みツールは**名前を列挙して** deny する必要がある。CLI の更新で新しい組み込みツールが増えると、deny に無いものは使えてしまう。
  起動時の tool 一覧（init）が許可した集合と一致するかを確かめ、一致しなければ活動を始めない、という確認が要る。
- 利用者単位の MCP サーバーは、tool は隠れても process としては接続される（init の `mcp_servers` が connected）。
  Agent からは呼べないが、専用の実行環境では利用者単位の MCP を持たない構成が望ましい。
- 利用者単位の hook（SessionStart 等）は、ツールの deny とは別に実行される。活動用の Agent に不要な hook がある場合は、別の利用者・環境で動かす必要がある。
- この確認は CLI での確認で、T3 アプリ経由の挙動（5）は未確認。

## 許可方式（CLI のフラグ）

### 方法

`/dev/shm` の設定のない空のディレクトリで、[run_allow.sh](2026-10-11-agent-tool-restriction/run_allow.sh) から
`claude -p` を起動しました。Core の MCP の代わりにダミーを [core-mcp.json](2026-10-11-agent-tool-restriction/core-mcp.json) で渡します。
実行中は 0.5 秒ごとに、起動した claude の**子孫プロセス**だけを記録しました（この確認を行っているセッション自身の MCP プロセスを数えないため）。
hook の実行は stream-json の `hook_started`・`hook_response` で、認証の種類は init の `apiKeySource` で見ました。
集計は [summarize.py](2026-10-11-agent-tool-restriction/summarize.py) です。指示は禁止方式と同じ4手順です。

### 手順と結果

| # | フラグ | 結果 |
| --- | --- | --- |
| 6 | `--permission-mode bypassPermissions --tools "" --mcp-config core-mcp.json --strict-mcp-config --setting-sources ""` | **PASS**。tool 一覧は `mcp__core__core_echo` だけ。接続した MCP は core だけで、利用者単位の MCP と claude.ai のコネクタは一覧にない。子孫プロセスに利用者単位の MCP（`takt-mcp`）はなく、ダミーだけ。hook のイベントは0件。`apiKeySource` は `none`（API キーではなくサブスクリプションの認証で動作）。Bash・Read は呼べず、ファイルは作られていない。`core_echo` は成功 |
| 7 | 対照：6 から `--strict-mcp-config` と `--setting-sources ""` を外す | 利用者単位の MCP とコネクタが tool 一覧に並び、子孫プロセスに `takt-mcp` が現れ、SessionStart hook が実行された。6 の結果が測り方の誤りではないことを確認 |
| 8 | `--restricted --permission-mode bypassPermissions ...` | 起動しない（`bypassPermissions not supported in restricted mode`）。`--restricted` は権限確認なしのモードと併用できない |
| 9 | `--restricted --tools "" --mcp-config core-mcp.json --strict-mcp-config --allowedTools mcp__core`（既定の権限モード） | **PASS**。tool 一覧はダミーだけ、hook なし、子孫プロセスはダミーだけ、`apiKeySource` は `none`。`core_echo` は `--allowedTools` で許可したため確認なしで成功 |
| 10 | 利用者の `~/.claude/settings.json` と `~/.claude.json` が変わっていないこと | **PASS** |

`--tools ""` は組み込みツールをすべて無効にする指定で、名前を列挙しないため、CLI の更新でツールが増えても漏れません。
`--bare` は hook を飛ばしますが、認証が API キーに限られるため確認していません（NOT RUN）。

### T3 アプリから渡せるか

確認時点の T3（server 0.0.46-nightly.20261007.2774）について、分かる範囲で調べました。

- このセッション自身の claude プロセスの引数を見ると、T3 は `--tools default`、`--permission-mode bypassPermissions`、
  T3 自身の MCP を含む `--mcp-config` と `--allowedTools mcp__t3-code__*`、`--settings` を渡している。
  `--strict-mcp-config` と `--setting-sources` は渡していない。
- T3 の設定ファイル（`settings.json`）、thread の設定（provider・model・mode）、project の設定（model・環境・scripts 等）には、
  claude へ追加のフラグを渡す項目が見当たらない。

このため、T3 アプリから起動する Agent では許可方式のフラグを渡せない見込みです（**制約**）。T3 経由では、
project の設定（禁止方式）で限定することになり、T3 自身の MCP（`mcp__t3-code__*`）も deny で外す必要があります。
T3 アプリ経由の実際の確認（5）は引き続き NOT RUN です。
