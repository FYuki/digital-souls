from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from dbos import DBOS, DBOSConfig, SetWorkflowID


QUEUE_NAME = "character-life-spike"


@DBOS.step()
def form_reflection(character_id: str, episode_ids: list[str]) -> dict[str, Any]:
    return {
        "character_id": character_id,
        "source_episode_ids": episode_ids,
        "content": "複数の経験を振り返り、共同作業を続けたいと感じた",
    }


@DBOS.step()
def form_goal_intention(reflection: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "GOAL_INTENTION",
        "status": "ACTIVE",
        "content": "関連する共同作業をもう少し続けたい",
        "reflection": reflection,
    }


@DBOS.workflow()
def character_life_slice(character_id: str, episode_ids: list[str]) -> dict[str, Any]:
    reflection = form_reflection(character_id, episode_ids)
    life_state = form_goal_intention(reflection)
    return {
        "result": "APPLIED",
        "reflection": reflection,
        "life_state": life_state,
    }


def main() -> None:
    database_url = os.environ.get(
        "DBOS_SYSTEM_DATABASE_URL",
        f"sqlite:///{Path('character-life-dbos-spike.sqlite').resolve()}",
    )
    config: DBOSConfig = {
        "name": "digital-souls-character-life-spike",
        "application_version": "1",
        "system_database_url": database_url,
    }
    DBOS(config=config)
    DBOS.launch()
    DBOS.register_queue(QUEUE_NAME, global_concurrency=1)

    workflow_id = os.environ.get("CHARACTER_LIFE_WORKFLOW_ID", "character-life-spike-1")
    episode_ids = [str(uuid4()), str(uuid4())]
    with SetWorkflowID(workflow_id):
        handle = DBOS.enqueue_workflow(
            QUEUE_NAME,
            character_life_slice,
            "miori",
            episode_ids,
        )
    print(handle.get_result())


if __name__ == "__main__":
    main()
