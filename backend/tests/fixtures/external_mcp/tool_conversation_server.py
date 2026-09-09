"""会話受入専用MCP。Coreをimportせず、追加情報要求と遅延を制御する。"""

import argparse
import asyncio
import anyio
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

parser = argparse.ArgumentParser()
parser.add_argument("--signals", required=True)
options = parser.parse_args()
signals = Path(options.signals)


async def list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="prepare-exhibit",
                description="展示の案内を準備します。色が必要なら追加情報を質問します。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="slow-exhibit",
                description="「時間のかかる確認」という名前の定型処理を実行します。引数や追加情報は不要です。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        ]
    )


async def call_tool(ctx, params):
    with (signals / "calls.txt").open("a") as output:
        output.write(params.name + "\n")
    if params.name == "slow-exhibit":
        (signals / "slow-dispatched").touch()
        # cancellationを受けても外部処理が完了するケースを明示的に再現する。
        with anyio.CancelScope(shield=True):
            await asyncio.sleep(30)
            (signals / "slow-result-attempted").touch()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="遅い照合の結果は金色です。")]
        )
    if params.input_responses is None:
        return types.InputRequiredResult(
            request_state="private-opaque-exhibit-state",
            input_requests={
                "exhibit": types.ElicitRequest(
                    params=types.ElicitRequestFormParams(
                        message="展示の色を教えてください。",
                        requested_schema={
                            "type": "object",
                            "properties": {"color": {"type": "string"}},
                            "required": ["color"],
                            "additionalProperties": False,
                        },
                    )
                )
            },
        )
    assert params.request_state == "private-opaque-exhibit-state"
    color = params.input_responses["exhibit"].content["color"]
    (signals / "resumed").touch()
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text", text=f"展示の色は{color}です。準備内容を確認しました。"
            )
        ]
    )


server = Server(
    "tool-conversation-fixture", on_list_tools=list_tools, on_call_tool=call_tool
)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
