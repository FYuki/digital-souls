from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


class SliceState(TypedDict, total=False):
    character_id: str
    episode_ids: list[str]
    reflection: dict[str, object]
    life_state: dict[str, object]
    result: str


def form_reflection(state: SliceState) -> SliceState:
    return {
        "reflection": {
            "character_id": state["character_id"],
            "source_episode_ids": state["episode_ids"],
            "content": "複数の経験を振り返り、共同作業を続けたいと感じた",
        }
    }


def form_goal_intention(state: SliceState) -> SliceState:
    return {
        "life_state": {
            "kind": "GOAL_INTENTION",
            "status": "ACTIVE",
            "content": "関連する共同作業をもう少し続けたい",
            "reflection": state["reflection"],
        },
        "result": "APPLIED",
    }


def main() -> None:
    database_path = Path("character-life-langgraph-spike.sqlite").resolve()
    connection = sqlite3.connect(database_path, check_same_thread=False)
    checkpointer = SqliteSaver(connection)

    builder = StateGraph(SliceState)
    builder.add_node("reflection", form_reflection)
    builder.add_node("goal_intention", form_goal_intention)
    builder.add_edge(START, "reflection")
    builder.add_edge("reflection", "goal_intention")
    builder.add_edge("goal_intention", END)
    graph = builder.compile(checkpointer=checkpointer)

    thread_id = "character-life-spike-1"
    result = graph.invoke(
        {
            "character_id": "miori",
            "episode_ids": [str(uuid4()), str(uuid4())],
        },
        {"configurable": {"thread_id": thread_id}},
    )
    print(result)
    connection.close()


if __name__ == "__main__":
    main()
