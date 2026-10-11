"""IT2 for ADR 0026: real llama.cpp template acceptance and token counts.

Usage: it2_prompt.py <characters.json> <output.json> <runs>
Prints token counts, roles and status only; response bodies are never recorded.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from digital_souls_core.application import Inference
from digital_souls_core.character import load_characters
from digital_souls_core.contracts import CompletionInput, Message
from digital_souls_core.prompt_builder import ByteTokenCounter
from digital_souls_core.provider import LiteLLMProvider


def message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def user(text: str) -> dict[str, str]:
    return message("user", text)


def assistant(text: str) -> dict[str, str]:
    return message("assistant", text)


def system(text: str) -> dict[str, str]:
    return message("system", text)


CASES: dict[str, tuple[str, list[dict[str, str]]]] = {
    "user_only": ("miori", [user("こんばんは。自己紹介をお願いします。")]),
    "caller_system_mid": (
        "miori",
        [
            system("合成試験：あなたは外部クライアントから呼ばれています。"),
            user("今日は晴れでした。"),
            assistant("晴れの日は気持ちがいいですね。"),
            user("夕方は何をしていましたか。"),
        ],
    ),
    "post_history": ("miori-phi", [user("好きな景色を教えてください。")]),
    "leading_assistant": ("miori", [assistant("こんばんは、光織です。"), user("こんばんは。")]),
    "history_and_system_and_phi": (
        "miori-phi",
        [
            system("合成試験の呼び出し側の指示です。"),
            user("一つ目の質問です。"),
            assistant("一つ目の答えです。"),
            user("二つ目の質問です。"),
            assistant("二つ目の答えです。"),
            user("三つ目の質問です。"),
        ],
    ),
    "leading_assistant_long": (
        "miori",
        [
            assistant("こんばんは、光織です。"),
            user("一つ目です。"),
            assistant("一つ目の答えです。"),
            user("二つ目です。"),
        ],
    ),
    "tool_free_history_only_assistant_first": (
        "miori-phi",
        [system("合成試験の指示です。"), assistant("最初の挨拶です。"), user("こんにちは。")],
    ),
    "estimate_counter": ("miori-est", [user("こんばんは。")]),
}


async def run_case(
    inference: Inference, provider: LiteLLMProvider, character_id: str, messages: list[Any]
) -> dict[str, Any]:
    request = CompletionInput.model_validate(
        {"messages": messages, "max_completion_tokens": 24, "temperature": 0.0}
    )
    prepared = await inference.prepare(character_id, request, alias=False)
    sent = tuple(Message.model_validate(m) for m in prepared.payload["messages"])
    result = await provider.complete(prepared.character.config.profile, prepared.payload)
    return {
        "counted": prepared.usage.total_tokens,
        "estimate": ByteTokenCounter()(sent),
        "roles": [m["role"] for m in prepared.payload["messages"]],
        "prompt_tokens": (result.get("usage") or {}).get("prompt_tokens"),
        "finish": result["choices"][0].get("finish_reason"),
    }


async def main(config: str, runs: int) -> list[dict[str, Any]]:
    provider = LiteLLMProvider()
    inference = Inference(load_characters(Path(config)), provider)
    records = []
    for name, (character_id, messages) in CASES.items():
        for run in range(runs):
            record: dict[str, Any] = {"case": name, "run": run + 1}
            try:
                record |= await run_case(inference, provider, character_id, messages)
                record["ok"] = True
            except Exception as error:  # record only the type and public code
                record["ok"] = False
                record["error"] = f"{type(error).__name__}:{getattr(error, 'code', '')}"
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    return records


if __name__ == "__main__":
    results = asyncio.run(main(sys.argv[1], int(sys.argv[3])))
    Path(sys.argv[2]).write_text(json.dumps(results, ensure_ascii=False, indent=1))
