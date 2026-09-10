"""障害受入専用MCP。外部正本はSQLiteで保持し、Coreをimportしない。

request-key-v1のstatus/replayは保存済み依頼だけを参照する。
control.jsonはテスト運営側の障害注入用で、MCPの公開操作ではない。
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def definitions():
    key = {"request_id": {"type": "string"}}
    return [
        types.Tool(
            name="change",
            description="破棄可能な検証状態を変更する。",
            input_schema={
                "type": "object",
                "properties": {
                    **key,
                    "value": {"type": "integer"},
                    "mode": {
                        "type": "string",
                        "enum": [
                            "apply",
                            "conflict",
                            "failed",
                            "no_change",
                            "task",
                            "disconnect",
                        ],
                    },
                },
                "required": ["value", "mode"],
                "additionalProperties": False,
            },
        ),
        *[
            types.Tool(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": key,
                    "required": ["request_id"],
                    "additionalProperties": False,
                },
            )
            for name in ("status", "replay", "cancel")
        ],
    ]


async def main(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.pid").write_text(str(os.getpid()))
    database = root / "external.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, result TEXT NOT NULL, cancel_requested INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS calls (operation TEXT NOT NULL, key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS domain (value INTEGER NOT NULL, effects INTEGER NOT NULL);
            INSERT INTO domain SELECT 0, 0 WHERE NOT EXISTS (SELECT 1 FROM domain);
        """)

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=definitions())

    async def call_tool(ctx, params):
        args = params.arguments or {}
        key = args.get("request_id", str(uuid4()))
        control_path = root / "control.json"
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO calls VALUES (?, ?)", (params.name, key))
            row = db.execute(
                "SELECT result, cancel_requested FROM requests WHERE key=?", (key,)
            ).fetchone()
            result = json.loads(row[0]) if row else {"status": "result_unknown"}
            if params.name == "change" and row is None:
                mode = args["mode"]
                status = {
                    "apply": "applied",
                    "disconnect": "applied",
                    "task": "running",
                }.get(mode, mode)
                if status == "applied":
                    db.execute(
                        "UPDATE domain SET value=?, effects=effects+1", (args["value"],)
                    )
                value, effects = db.execute(
                    "SELECT value, effects FROM domain"
                ).fetchone()
                result = {
                    "status": status,
                    "result": {"value": value, "effects": effects},
                }
                if status == "conflict":
                    result["latest_state"] = {"value": value, "version": effects}
                if status == "running":
                    result["task_id"] = "task-" + key
                db.execute(
                    "INSERT INTO requests(key, result) VALUES (?, ?)",
                    (key, json.dumps(result)),
                )
            elif params.name == "cancel" and row:
                db.execute("UPDATE requests SET cancel_requested=1 WHERE key=?", (key,))
                if result["status"] == "running":
                    result = {**result, "status": "cancel_requested"}
            elif params.name == "status" and row:
                if (
                    row[1]
                    and control.get("cancel_completed")
                    and result["status"] == "running"
                ):
                    result = {**result, "status": "cancelled"}
                    db.execute(
                        "UPDATE requests SET result=? WHERE key=?",
                        (json.dumps(result), key),
                    )
                if control.get("lookup_unknown"):
                    result = {"status": "result_unknown"}
        # 外部commit後にtransportを失う。実際にprocessを終了し、例外mockで代替しない。
        if params.name == "change" and args["mode"] == "disconnect":
            os._exit(31)
        return types.CallToolResult(content=[], structured_content={"action": result})

    server = Server(
        "action-recovery-fixture", on_list_tools=list_tools, on_call_tool=call_tool
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    asyncio.run(main(parser.parse_args().root))
