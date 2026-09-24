# Execution Gate / Tool Use #486 検証記録

## 対象

`ExecutionGate`の認可・承認・journal・dispatch・settlement順序を維持したまま、結果分類と実行準備の内部責務を分離した。`ToolService`の候補投影、router判断、確認・入力再開、結果採用を`decision_loop.py`へ移し、外部dispatchは既存Gate APIだけを通る。新しい権限正本、runner、loop、公開APIは追加していない。

確認した経路は次のとおりである。

| 契約 | 経路 | 所有者 |
|---|---|---|
| `C486-03` | native response / `MCPFailure` → `result_classification.py` → envelope / journal投影 | 分類moduleは副作用なし、Gateが実行状態を所有する |
| `C486-04` | validated input → `execution.py` → live guard → journal claim → source dispatch → settlement | `ExecutionGate`が実行許可と再認可を所有する |
| `C486-05` | Tool request → `decision_loop.py` → router / clarification / confirmation → Gate → source | `ToolService`がrunを所有し、dispatch権限はGateに限定する |

## 実行結果

Python 3.12.3のread-only venvを使用した。unit/moduleでは`FakeSource`、synthetic failure、既存fixtureを使い、実接続ではtest専用一時MCP server rootと一時data rootを使った。dogfood、共有サービス設定、既存containerは変更していない。

| コマンド | 結果 |
|---|---|
| 最終`.../python -m pytest -q backend/tests/unit` | `4623 passed, 1 skipped, 11 warnings` |
| `.../python -m pytest -q backend/tests/unit/test_conversation_core_pipeline.py backend/tests/unit/test_conversation_core_task_tracker.py backend/tests/unit/test_conversation_core_terminal_effects.py backend/tests/unit/test_external_mcp_execution.py backend/tests/unit/test_external_mcp_result_classification.py backend/tests/unit/test_tool_use_decision_loop.py` | `24 passed`。深い入れ子JSONは既存不具合のため対象外 |
| `.../python -m pytest -q backend/tests/unit/test_addon_action_dispatch.py::test_trusted_reads_are_fresh_and_tool_error_does_not_prove_no_side_effect` | `1 passed`。送信済みtool errorを`result_unknown`へ維持した |
| `.../python -m pytest -q backend/tests/module/test_external_mcp_conformance.py backend/tests/module/test_chat.py` | 成功（関連moduleを含む） |
| `.../python -m mypy --config-file backend/mypy.ini backend/app environments` | 成功 |
| `.../python -m pytest -q backend/tests/integration/test_external_mcp_real_servers_integration.py` | 5 passed。公開MCP filesystem/everythingのstdio、streamable-http、bearer認証をtest専用process/containerで実行 |
| `.../python -m pytest -q backend/tests/integration/test_tool_use_real_service_integration.py` | 2 passed。実Ollamaによるfilesystem tool選択・HTTP resource選択を実Gateへ接続 |
| `.../python -m pytest -q backend/tests/integration/test_inference_real_service_acceptance_integration.py::test_configured_provider_real_service_capabilities` | 1 passed。実Ollamaの生成・structured generation・embedding |

標準入口`npm run test:unit -- <対象>`は`backend/.venv/bin/python: not found`で開始できなかったため、許可されたread-only venvから同じpytest対象を実行した。

`backend/tests/module`全体の再実行は`2006 passed, 1 failed, 16 warnings`。失敗は`backend/tests/module/test_environment_up_command.py::test_should_return_after_child_orchestrator_is_ready_and_leave_down_cleanup_to_it`のみで、reportの`failure.category=preparation`、Docker buildx activity pathのread-only filesystemが原因だった。対象test・関連環境コードは基点と差分がなく、#484時点にも同じ制約が記録されている。Gate／Tool Useの変更経路に属さず、共有Docker設定を変更していない。

## 既存不具合の限定確認

深い入れ子JSONは今回の抽出契約へ含めていない。基点`9a08fdebb2a320c5398be70fb5259d20c8759521`の`routing.py`に深さ制限はなく、`projection.py:96-99`の`secret_key`再帰が深い配列で`RecursionError`になることを、次の条件で再現した。

次の一時入力による直接再現は終了status 1で、`backend/app/tool_use/projection.py:99`の`RecursionError`になった。

```text
PYTHONPATH=backend /home/asa/dev/digital-souls-worktrees/issue-358/backend/.venv/bin/python - <<'PY'
import asyncio
from app.tool_use.routing import ToolDecision
from tests.tool_use_test_support import Decisions, runtime

nested = '{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}'

async def main() -> None:
    def invalid_call(context):
        candidate = next(item for item in context["candidates"] if item["name"] == "native-tool")
        return ToolDecision("call", candidate["id"], arguments_json=nested)

    async with runtime(Decisions(invalid_call)) as (service, _source, _gate):
        await service.run("miori", "session-1", "取得")

asyncio.run(main())
PY
```

今回の変更後に`routing.py`へ追加していた深さ制限は削除し、同じ条件を`invalid_decision`へ変換していない。テストからこの条件を除外して成功へ読み替えず、既存不具合の再現結果と影響を本記録に残した。`projection.py`の修正は別責務の残課題である。

## 実接続と未確認範囲

実MCP、実ToolService、実Ollamaは上表のとおり成功した。MCP serverは`/tmp`のtest専用rootにnpm installし、Bearer試験のnginx containerを試験自身が所有して終了させた。実journalは外部共有journalへ接続していない。

固定条件の基点前後性能比較は`INCONCLUSIVE`である。`run_pilot.py`の`measurement_revision()`が未コミットworktreeを拒否し、commitは禁止されている。また比較用の専用LiveKit設定ファイルが存在しない。過去の成功記録やmock結果を今回の性能証拠へ読み替えていない。

## 監督による互換性確認（2026-09-24）

TAKT成果 `e0a8e974` と基点 `9a08fde` のGateへ同じ合成入力を与えたところ、空値の`inputRequests`の正規化と、不正な入力要求を受け取った後の生存確認順序に差があった。基点の空値正規化（`or {}`）を分類器へ移し、`input_required`の生存確認を分類前に戻した。新しい入力拒否ルールは追加していない。

8件のGate回帰テストを追加し、Core・Gate・Toolの関連224件が成功。基点と変更後の8条件比較でもoutcome・error_category・native_errorが一致した。変更した3ファイルのRuffと、app/environments全体378ファイルのmypyが成功した。これは合成sourceによる互換性確認であり、実MCPやLiveKitのIT2合格を意味しない。
