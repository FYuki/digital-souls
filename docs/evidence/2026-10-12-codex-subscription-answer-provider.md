# Issue #201: Codex サブスク回答 Provider（実送信 NOT RUN）

正本：[Issue #201](https://github.com/FYuki/digital-souls-core/issues/201)、
[Epic #126](https://github.com/FYuki/digital-souls-core/issues/126)。2026-10-12 に読み取り照合。

## revision・実施境界

- branch `feature/201-codex-luna-provider`、起点 `eb9ab67`（#198 を含む）。対象実装は本証跡を含む commit。
- Python 3.12.3、uv 0.8.22、Node 24.19.0、promptfoo 0.117.2。依存・lock の変更なし。
- 実 CLI では `codex --version` / `codex exec --help` だけを実行し、0.162.1 と必須 flag の存在を確認。
- 実 `codex exec`・Luna 送信・login status・実モデル IT2/ST：**NOT RUN**。
- 認証ファイルは開いていない・表示していない・複製していない。試験は一時スクリプトと偽 CODEX_HOME だけ。
- chat/embedding サービスは起動・停止・変更なし。push・PR・merge・Issue/PR コメント・remote CI：**NOT RUN**。
- SPEC・ケース・gold・期待値・閾値・Core の messages と判定は変更なし。

## 設計と条件差

評価専用 [CodexSubscriptionAnswerProvider](../../src/digital_souls_core/semantic_answer_codex.py) が
Provider port を実装する。本番 LiteLLMProvider・Profile・privacy は変更しない。
[profile 例と操作手順](../../evals/semantic/README.md#codex-サブスク方式201)に完全な argv と無効化一覧を記載。
external_model に answer_provider=codex_subscription、codex_executable/codex_home の絶対パスを追加。
chat は sdk / openai/gpt-6-luna、CLI の model は接頭辞を除いた gpt-6-luna。
api_key_env/api_base_env は Codex 方式では不可、API 方式の既定は従来どおり LiteLLM。

本番 Inference.prepare → PrivacyPolicy.authorize external → inference.check → Provider を共用する。
Provider の中で版と help を検査し、空の一時 cwd と -C、ephemeral/read-only/JSON で非対話実行する。
CLI 版の下限は PoC と同じ 0.152.0。必要な exec flag が欠ければ本文なしの provider_error。
stream/tools/未知の payload キーを拒否し、messages を同じ JSON 配列のまま stdin に包む。
timeout は chat.timeout_seconds、非0終了・不正 JSONL・応答欠落・tool event・turn.failed も provider_error。
stdout/stderr を capture し、例外のメッセージは固定値だけにする。

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

[Python](../../tests/test_semantic_answer_codex.py)、[偽 CLI](../../tests/codex_answer_support.py)、
[JS](../../tools/semantic-answer.test.mjs)で外部通信なしに検証する。

- a/b：test_codex_dispatch_checks_production_policy_before_process の
  permission/grant/scanner/classifier/revoke は CLI 起動0回、None は external authorize → check → CLI。
- c：test_codex_argv_environment_payload_and_final_text で argv、無効化16機能、空 cwd、stdin、環境を照合。
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
実送信と SPEC 同期は #199。

CLI の flags はローカル 0.162.1 の help と
[公式非対話実行文書](https://developers.openai.com/codex/noninteractive)を照合した。
実 exec でのアカウント制限・追加指示・managed config の実効状態はこの Issue では未測定。
