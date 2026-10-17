# Issue #201: Codex サブスク回答 Provider（#199実測追記）

正本：[Issue #201](https://github.com/FYuki/digital-souls-core/issues/201)、
[Epic #126](https://github.com/FYuki/digital-souls-core/issues/126)。2026-10-12 に読み取り照合。

## revision・実施境界

- branch `feature/201-codex-luna-provider`、起点 `eb9ab67`（#198 を含む）。対象実装は本証跡を含む commit。
- Python 3.12.3、uv 0.8.22、Node 24.19.0、promptfoo 0.117.2。依存・lock の変更なし。
- 第1ラウンドは実 CLI の `codex --version` / `codex exec --help` で 0.162.1 と必須 flag を確認。
- 第2ラウンドは `dacd64b` から追加変更し、実 CLI の `codex --version` / `codex features list` で
  0.162.1 と追加14機能の存在を確認。認証やユーザー設定ファイルの中身は参照していない。
- 実 `codex exec`・Luna 送信・login status・実モデル IT2/ST：**NOT RUN**。
- 認証ファイルは開いていない・表示していない・複製していない。試験は一時スクリプトと偽 CODEX_HOME だけ。
- chat/embedding サービスは起動・停止・変更なし。push・PR・merge・Issue/PR コメント・remote CI：**NOT RUN**。
- SPEC・ケース・gold・期待値・閾値・Core の messages と判定は変更なし。

## 設計と条件差

評価専用 [CodexSubscriptionAnswerProvider](../../core/src/digital_souls_core/semantic_answer_codex.py) が
Provider port を実装する。本番 LiteLLMProvider・Profile・privacy は変更しない。
[profile 例と操作手順](../../core/evals/semantic/README.md#codex-サブスク方式201)に完全な argv と無効化一覧を記載。
external_model に answer_provider=codex_subscription、codex_executable/codex_home の絶対パスを追加。
chat は sdk / openai/gpt-6-luna、CLI の model は接頭辞を除いた gpt-6-luna。
api_key_env/api_base_env は Codex 方式では不可、API 方式の既定は従来どおり LiteLLM。

本番 Inference.prepare → PrivacyPolicy.authorize external → inference.check → Provider を共用する。
Provider の中で版と help を検査し、空の一時 cwd と -C、ephemeral/read-only/JSON で非対話実行する。
CLI 版の下限は PoC と同じ 0.152.0。必要な exec flag が欠ければ本文なしの provider_error。
stream/tools/未知の payload キーを拒否し、messages を同じ JSON 配列のまま stdin に包む。
timeout は chat.timeout_seconds、非0終了・不正 JSONL・応答欠落・tool event・turn.failed も provider_error。
stdout/stderr を capture し、例外のメッセージは固定値だけにする。

`--disable` の無効化対象は次の30機能です。
apps、browser_use、browser_use_external、browser_use_full_cdp_access、code_mode_host、
computer_use、fast_mode、goals、hooks、image_generation、in_app_browser、in_app_local_automation、
multi_agent、plugin_sharing、plugins、remote_plugin、shell_snapshot、shell_tool、
skill_mcp_dependency_install、skill_search、sleep_tool、tool_call_mcp_elicitation、tool_suggest、
ultrafast_mode、unified_exec、unified_exec_tty、view_image、workspace_dependencies、worktrees、
write_stdin_approval。

第2ラウンドの追加は goals、sleep_tool、remote_plugin、plugin_sharing、skill_mcp_dependency_install、
tool_call_mcp_elicitation、workspace_dependencies、in_app_local_automation、worktrees、shell_snapshot、
unified_exec_tty、write_stdin_approval、fast_mode、ultrafast_mode。
CLI 0.162.1 の `codex features list` では追加14機能すべてが stable / true で存在し、除外は0件。
fast_mode / ultrafast_mode は比較にサービス tier の差を持ち込まないため無効化する。
無効化の実効確認は後続#199で間接確認した（末尾の実測追記）。#201実装時は実送信NOT RUN。

認証は CLI の既存 ChatGPT ログインに委譲する。Core は認証ファイルを開かない。
CLI 子の環境は PATH/LANG/LC_ALL/TZ と profile の HOME/CODEX_HOME パスだけ。
API キー、proxy、ambient Codex/MCP/Node/Python 設定は渡さない。
Node network_guard と Python の既存方針を維持し、embedding/分類器を外部へ移さない。

Codex 専用の report は provider=codex_subscription、model=gpt-6-luna、codex_cli_version、
condition_note=codex-cli-adds-own-instructions-not-pure-api を追加する。
CLI 独自の指示と messages の包装があるため、純粋な API 条件とは違う。
external_send=true、cache=false（評価キャッシュなし）、runs/run_count、commit、classifier_reason を維持。
manifest/dry-run と送信拒否ケースは版 null、CLI 起動なし。成功した dispatch の版を照合し、
ケース間・run 間の版の変化は FAIL。認証・パス・本文を公開 report に含めない。

## 受入条件と試験

[Python](../../core/tests/test_semantic_answer_codex.py)、[偽 CLI](../../core/tests/codex_answer_support.py)、
[JS](../../core/tools/semantic-answer.test.mjs)で外部通信なしに検証する。

- a/b：test_codex_dispatch_checks_production_policy_before_process の
  permission/grant/scanner/classifier/revoke は CLI 起動0回、None は external authorize → check → CLI。
- c：test_codex_argv_environment_payload_and_final_text で argv、無効化30機能、空 cwd、stdin、環境を照合。
  JS codex runner forwards no credentials, proxies, ambient Codex home or MCP configuration。
- d：test_codex_public_provider_wiring_uses_fake_cli_and_redacts_output、
  test_codex_identity_and_dry_run_never_start_or_read_credentials、JS codex report records dispatched CLI
  version and condition difference without private output。偽 stdout/stderr/auth ファイルの sentinel を検査。
- e：test_codex_failures_are_content_free、test_codex_profile_fail_closed、
  test_codex_unsupported_payload_never_starts、test_codex_stream_never_starts。
  JS codex report rejects invalid or drifting runtime version と version changes between runs。
- dry-run：test_codex_dry_run_counts_all_cases_without_cli_or_credentials。
- 回帰：既存 UT/IT1/PG と JS の fixture/local_model/API 方式の試験を変更せず実行。
  fixture manifest の厳密なフィールド集合、既存 API report の本文非公開を維持。

## 品質ゲート

第2ラウンドは argv の既存 IT1 を先に30機能へ更新し、旧実装で1件 FAIL を確認。
無効化一覧を追加した後に偽 CLI の Python 30件で回帰確認し、commit 後に下記ゲートをすべて実行する。

TDD の最初の実行は新規モジュール未追加による collection error（FAIL）。実装後の対象 Python 30件、
回答 JS 48件は PASS。最終 commit 後に次をすべて実行し、SHA・件数・結果は完了報告に記録する。

```sh
T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
```

uv sync --locked、uv lock --check、api.yml と同じ対象で ruff check / format --check / mypy、
pytest -m ut、pytest -m it1、uv build --no-build-isolation、bash tools/test-postgres.sh、
evals/semantic の npm ci --no-audit --no-fund、Node required reporter の tools/*.test.mjs、
node tools/check-docs.mjs、git diff --check。
新規 Python は src/tests 配下で既存の検査対象に含まれ、workflow の対象追加は不要。
合成試験の PASS は Luna の品質合格・実モデル IT2/ST の PASS を意味しない。
実送信とSPEC同期は後続#199で実施済み（末尾）。

CLI の flags はローカル 0.162.1 の help と
[公式非対話実行文書](https://developers.openai.com/codex/noninteractive)を照合した。
実 exec でのアカウント制限・追加指示・managed config の実効状態はこの Issue では未測定。

## #199の実測追記（2026-10-12）

監督によるcommit `2fe921cc7255e277c19300c0bea6dbd2ffbb076d`、dirty=falseでの実送信を
[Luna回答評価証跡](2026-10-12-semantic-luna-answer-evaluation.md)と
[本文なしreport](2026-10-12-semantic-luna-answer-codex.json)に記録した。#201当時のNOT RUNは履歴として上記に残す。

- classifier=synthetic（baseline-parity-no-classifier-export）、同じbge-m3/0.52、cacheなし3回。
  external_send_allowed → privacy authorize(external) → inference.checkを通った各回61件が正常応答。
  after_searchは送信直前guardにより不送信でgoldどおり。CLI版は全3回0.162.1で一致。
- JSONLのtool eventをProviderがFAILにする設計で全送信が正常応答だったため、無効化の実効を間接確認。
  生JSONLは保持しておらず、tool event不在の直接再監査はNOT RUN。30機能を個別に実機検査した証拠ではない。
- 各回品質61/62・全条件53/62、long_text 0/1、dispatch違反1件・no_memory違反8件、
  forbidden/lifecycle違反0件。gemmaと全ケースの観測値・判定が一致し、FAIL・品質未受入。
- 事前見込みは183呼び出し・入力202,530 token（byte推定、CLI独自指示は除外）。
  既存ChatGPTサブスク内で従量課金なし・新規有料契約なし。実token使用量は未測定。
- CLI独自の指示・messages包装と既存token予算移行の条件差をLuna証跡へ記録。
  回答文比較・実環境IT2/STはNOT RUN、本番採用判断は範囲外。

文書担当は監督の既存reportを再集計しただけで、評価・dry-run・CLI実送信を再実行していない。
認証情報・私有profile・私的ログは読まず、証跡へ転記していない。
