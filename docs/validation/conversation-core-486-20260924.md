# Conversation Core #486 検証記録

## 対象

基点`9a08fdebb2a320c5398be70fb5259d20c8759521`から、Conversation Coreの内部責務分割を確認した。`ConversationCoreSession`がresponse、input、lock、generation、状態遷移、first-terminalを所有し、実行・終端副作用・task追跡の内部実装だけを分離した。公開Core API、DB schema、履歴形式、音声設定は変更していない。

確認した経路は次のとおりである。

| 契約 | 経路 | 所有者 |
|---|---|---|
| `C486-01` | Response → `pipeline.py` → LLM / 有界queue / TTS → Session callback | Sessionが状態を所有し、pipelineは実行とcallbackだけを担う |
| `C486-01` | stage/effect coroutine → `task_tracker.py` → 応答単位取消 / shutdown drain | `TaskTracker`がtask集合を所有する |
| `C486-02` | `TerminalOutcome` → `terminal_effects.py` → persistence → delivery → pending start | first-terminalと保存対象の決定はSession、順序付き副作用はterminal effects |

## 実行結果

unit/moduleと型検査はrepository root、Python 3.12.3、read-only venv `/home/asa/dev/digital-souls-worktrees/issue-358/backend/.venv`で実行した。実接続試験は`DS_ENVIRONMENT_ID=test`、`PYTHON_DOTENV_DISABLED=1`、一時data root、test専用の一時MCP server rootを使った。既存のOllama・VOICEVOX・Whisper・LiveKitは設定変更・再起動をせず、dogfoodへ接続していない。

| コマンド | 結果 |
|---|---|
| `PYTHONPATH=backend .../python -m pytest -q backend/tests/unit` | `4623 passed, 1 skipped, 11 warnings` |
| `PYTHONPATH=backend .../python -m pytest -q backend/tests/unit/test_conversation_core_pipeline.py backend/tests/unit/test_conversation_core_task_tracker.py backend/tests/unit/test_conversation_core_terminal_effects.py backend/tests/unit/test_external_mcp_execution.py backend/tests/unit/test_external_mcp_result_classification.py backend/tests/unit/test_tool_use_decision_loop.py` | `24 passed`。深い入れ子JSONは既存不具合のためこの抽出契約には含めていない |
| `PYTHONPATH=backend .../python -m pytest -q backend/tests/module/test_conversation_core_contract.py backend/tests/module/test_conversation_entry_contract.py backend/tests/module/test_external_mcp_conformance.py backend/tests/module/test_chat.py` | `76 passed, 1 warning` |
| `.../python -m mypy --config-file backend/mypy.ini backend/app environments` | `Success: no issues found in 378 source files` |
| `.../python -m ruff check --config backend/ruff.toml`（変更した本番・テスト） | 成功 |
| `python3 -m py_compile`（変更したPython） | 成功 |
| `git diff --check` | 成功 |
| `npm ci`（`frontend/`、既存lockfile、作業用worktreeのみ） | 成功。371 packages追加。共有`node_modules`・借用venvは変更していない |
| `npm --prefix frontend run build` | 成功（Vite 5.4.21、445 modules）。chunk size warningのみ |
| `npm --prefix frontend run check` | 成功（svelte-check 0 errors / 0 warnings、TypeScript成功） |
| `npm --prefix frontend run test:unit` | 79 files / 1013 tests passed |
| `npm --prefix frontend run test:module` | 25 files / 184 tests passed |
| `PYTHONPATH=backend .../python -m pytest -q backend/tests/module/test_voice_input_vad_parity.py` | 43 passed。Frontend依存導入後に再実行 |

標準入口`npm run test:unit -- <対象>`も試行したが、worktreeに`backend/.venv/bin/python`がないため開始できなかった。上表のpytestは、指示されたread-only venvを使い、同じ対象を`PYTHONPATH=backend`で実行した。

module全体の再実行は`2006 passed, 1 failed, 16 warnings`だった。唯一の失敗は`backend/tests/module/test_environment_up_command.py::test_should_return_after_child_orchestrator_is_ready_and_leave_down_cleanup_to_it`である。reportの`failure.category`は`preparation`、messageは`frontend container image preparation failed`で、Docker buildxが`/home/asa/.docker/buildx/activity/.tmp-default...`をread-only filesystemのため更新できなかった。対象test・`start_command.py`・compose adapter・profileは基点と差分がなく、`order.md`にも#484時点の同じDocker buildx read-only制約が記録されている。既存containerの停止・削除、Docker共有設定変更では解消していない。

先行したunit実行で一時的に失敗した2件は、`backend/tests/unit/test_voice_quality_stale_report.py::test_cli_saves_missing_report_without_leaking_arbitrary_process_errors_or_overwriting`と`backend/tests/unit/test_voice_quality_stale_report.py::test_startup_failure_without_trace_remains_missing_and_has_no_fabricated_trace_hash`である。各テストを単独で再実行するとそれぞれ`1 passed`となり、最終のunit全体実行では再現しなかった。対象testと関連reporterは基点とbyte-identicalであり、今回のCore変更経路にも含まれない。したがって、失敗を成功へ読み替えず、今回変更による回帰とも、基点での再現を確認した既存不具合とも断定していない。

## 実接続と未確認範囲

| 対象 | コマンド・条件 | 結果 |
|---|---|---|
| 実Ollama推論 | `test_inference_real_service_acceptance_integration.py::test_configured_provider_real_service_capabilities`、Ollama `gemma4:e4b` / `nomic-embed-text:latest`、test data root | 1 passed。推論・structured generation・embeddingを実APIで確認 |
| 実Ollama／実Whisper準備 | `test_voice_model_preparation_integration.py`、Ollama `127.0.0.1:11434`、Whisper `127.0.0.1:50022` | 2 passed。準備後の生成とSTTを確認 |
| 実LiveKit transport | 一時Backendをport 18000/18001、既存dev LiveKit `127.0.0.1:7880`、一時data rootで実行 | 完走せず。最初の単独caseでBackendがtoken endpoint処理中に`Nvidia Decoder is supported.`後`terminate called without an active exception`でabortし、client側は`httpx.RemoteProtocolError: Server disconnected without sending a response`。一括12件も同じnative abortで中断。Core回帰とは断定していない |
| 実マイク・聴感 | 人の操作・聴感 | `NOT_RUN`。このstepで自動代替しない |
| 固定条件の基点前後性能比較 | `scripts/voice_quality/run_pilot.py` | `INCONCLUSIVE`。runnerの`measurement_revision()`が未コミットworktreeを`ValueError: measurement requires a committed worktree`として拒否する。commitはこのstepで禁止され、`infra/livekit/.env`も存在せず、比較用の専用LiveKit設定を追加していない。過去artifactを今回の変更後結果へ読み替えていない |

### LiveKit native abort の引継ぎ

- 実行したpytest対象は`backend/tests/integration/test_livekit_transport_integration.py`全体と、単独の`test_real_livekit_bootstrap_joins_user_and_character_to_one_room`である。秘匿値を含む環境変数の値は記録していない。再現可能なコマンド形は次のとおりである。

  ```text
  PYTHONPATH=backend DS_ENVIRONMENT_ID=test PYTHON_DOTENV_DISABLED=1 DS_DATA_DIR=/tmp/takt-c486-livekit.AK1zgG LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_TEST_BACKEND_URL=http://127.0.0.1:18000 LIVEKIT_API_KEY='[test-owned-redacted]' LIVEKIT_API_SECRET='[test-owned-redacted]' /home/asa/dev/digital-souls-worktrees/issue-358/backend/.venv/bin/python -m pytest -q backend/tests/integration/test_livekit_transport_integration.py
  PYTHONPATH=backend DS_ENVIRONMENT_ID=test PYTHON_DOTENV_DISABLED=1 DS_DATA_DIR=/tmp/takt-c486-livekit-single.Ves2iX LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_TEST_BACKEND_URL=http://127.0.0.1:18001 LIVEKIT_API_KEY='[test-owned-redacted]' LIVEKIT_API_SECRET='[test-owned-redacted]' /home/asa/dev/digital-souls-worktrees/issue-358/backend/.venv/bin/python -m pytest -q backend/tests/integration/test_livekit_transport_integration.py -k test_real_livekit_bootstrap_joins_user_and_character_to_one_room
  ```

- test専用Backendは今回のworktreeから一時port `18000` / `18001`で起動し、`DS_ENVIRONMENT_ID=test`、`PYTHON_DOTENV_DISABLED=1`、上記一時data rootを使用した。既存dev LiveKit `127.0.0.1:7880`、既存APIを使用したが、設定変更・再起動・既存container停止は行っていない。保存済みのBackend起動コマンド全文はなく、起動ログからUvicornが該当portでlistenしたことを確認している。
- 実行環境はPython `3.12.3`、`livekit` `1.1.16`、`livekit-api` `1.2.0`、`livekit-protocol` `1.1.26`、`httpx` `0.28.1`、`httpx2` `2.13.0`、`uvicorn` `0.53.0`。既存LiveKit server imageは`livekit/livekit-server:v1.9.7`だった。
- 秘匿値を除いたログは、`/tmp/takt-c486-livekit-backend.gxDOeW.log`（port 18000）と`/tmp/takt-c486-livekit-single-backend.QWYakm.log`（port 18001）に保存されている。両ログで`GET /health/ready 200`、conversation作成`201`、`Nvidia Decoder is supported.`、`terminate called without an active exception`を確認した。単独caseのclient側例外は`httpx.RemoteProtocolError: Server disconnected without sending a response`である。Backendプロセスとpytestの終了コードは保存ログに記録されておらず、未確認とする。
- 変更前比較は未実施である。`backend/app/livekit_transport/`と対象integration testは基点`9a08fdebb2a320c5398be70fb5259d20c8759521`との差分がなく、現在の既存LiveKitを使った同一原因の再実行を避けた。専用test LiveKit設定・独立した基点実行資源がないため、変更起因か環境・native SDK起因かは未判定のまま引き継ぐ。
