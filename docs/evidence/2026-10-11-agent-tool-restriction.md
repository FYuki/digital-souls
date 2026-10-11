# 実行基盤の候補の確認：Agent のツールを Core の MCP だけに限定できるか

日付: 2026-10-11

対象: [Issue #155](https://github.com/FYuki/digital-souls-core/issues/155) / [Epic #150](https://github.com/FYuki/digital-souls-core/issues/150)

起点: `origin/epic/poc-adr-realignment` / `731bf9be8ae8b48c38c4c18afdfadf657cad67b7`

## 目的

会話外の活動の実行基盤に汎用 Agent を使う条件（Epic #150 R5）を確かめます。

- Agent 単位で、使えるツールを Core の MCP だけに限定できる。
- 権限確認なしのモード（bypassPermissions）でも、設定で強制できる。
- 開発用の Agent の設定に影響しない。

2026-10-11 のユーザー決定により、候補は T3 の専用プロジェクト（Claude Code の project 設定）だけです。
OpenClaw の Agent ごとの `tools.allow` は確認しません（検討して採用しない）。

## 環境

- Claude Code CLI 2.1.296（`claude -p`、headless）。T3 が使う Claude Code と同じ project 設定（`.claude/settings.json`・`.mcp.json`）を読む。
- 作業場所は `/dev/shm` の使い捨てディレクトリ。リポジトリと利用者の設定ファイルは変更していない。
- Core の MCP はまだないため、標準ライブラリだけで書いたダミーの stdio MCP サーバー
  [dummy_core_mcp.py](2026-10-11-agent-tool-restriction/dummy_core_mcp.py)（ツール `core_echo` が1つ）を使った。
- 利用者単位の設定には、MCP サーバー1件と claude.ai のコネクタ2件がある（名前は設定ファイルの deny を参照）。

## 設定

専用プロジェクトの `.mcp.json` は [mcp.json](2026-10-11-agent-tool-restriction/mcp.json)、
`.claude/settings.json` は [settings.json](2026-10-11-agent-tool-restriction/settings.json) です。

- `enableAllProjectMcpServers: false` と `enabledMcpjsonServers: ["core"]` で、project の MCP をダミーだけにする。
- `permissions.deny` に、このバージョンで提供される組み込みツールと、利用者単位の MCP・コネクタを列挙する。
- `permissions.allow` は `mcp__core` だけ。

## 手順と結果

| # | 内容 | 結果 |
| --- | --- | --- |
| 1 | 専用プロジェクトで、deny を設定する前に `--permission-mode bypassPermissions` で起動し、init の tool 一覧を見る | 組み込みツール（Bash・Read・Write・WebFetch 等）、利用者単位の MCP、コネクタ、ダミーの `mcp__core__core_echo` が並ぶ。設定しなければ限定されないことを確認 |
| 2 | 上の設定で同じモードで起動し、Bash でファイル作成、Read でファイル読み取り、任意のツールで一覧取得、`core_echo` 呼び出しを指示する | **PASS**。init の tool 一覧は `mcp__core__core_echo` だけ。Agent は Bash・Read を呼べず、ファイルは作られていない（`ls` で確認）。`core_echo` は成功（`core_echo:hello`） |
| 3 | 別のディレクトリ（設定なし）で同じモードで起動し、tool 一覧を見る | **PASS**。36個のツールがあり、Bash・Read が使える。`mcp__core__core_echo` はない。専用プロジェクトの設定は他の場所へ影響しない |
| 4 | 利用者の `~/.claude/settings.json` が変わっていないこと | **PASS**。更新日時は確認前（2026-10-06）のまま |
| 5 | T3 アプリから専用プロジェクトのスレッドを起動して同じ確認をする | **NOT RUN**。T3 のプロジェクト・スレッドの作成はユーザーの判断が必要なため、未実施 |

## 分かった制約

- 組み込みツールは**名前を列挙して** deny する必要がある。CLI の更新で新しい組み込みツールが増えると、deny に無いものは使えてしまう。
  起動時の tool 一覧（init）が許可した集合と一致するかを確かめ、一致しなければ活動を始めない、という確認が要る。
- 利用者単位の MCP サーバーは、tool は隠れても process としては接続される（init の `mcp_servers` が connected）。
  Agent からは呼べないが、専用の実行環境では利用者単位の MCP を持たない構成が望ましい。
- 利用者単位の hook（SessionStart 等）は、ツールの deny とは別に実行される。活動用の Agent に不要な hook がある場合は、別の利用者・環境で動かす必要がある。
- この確認は CLI での確認で、T3 アプリ経由の挙動（5）は未確認。
