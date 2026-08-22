from __future__ import annotations

import atexit
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.formation.config import resolve_memory_formation_settings
from app.memory.formation.extractor import MemoryCandidateExtractor
from app.memory.formation.ollama_client import OllamaMemoryExtractorClient
from app.model_settings import resolve_model_settings

SETTINGS = resolve_model_settings(os.environ)
CLIENT = OllamaMemoryExtractorClient(model_id=SETTINGS.ollama_extractor_model)
EXTRACTOR = MemoryCandidateExtractor(
    client=CLIENT,
    settings=resolve_memory_formation_settings(os.environ),
)
atexit.register(CLIENT.close)


def _case_id(context: Mapping[str, object]) -> str:
    variables = context.get("vars")
    if not isinstance(variables, Mapping):
        raise ValueError("test vars are required")
    value = variables.get("case_id")
    if not isinstance(value, str) or not value:
        raise ValueError("case_id must be a non-empty string")
    return value


def call_api(
    prompt: str,
    options: dict[str, object],
    context: dict[str, object],
) -> dict[str, object]:
    del options
    now = datetime.now(UTC)
    turn = ConversationTurn(
        turn_id=uuid4(),
        character_id="eval",
        conversation_id=uuid4(),
        user_content=prompt,
        assistant_content="synthetic evaluator response",
        status=TurnStatus.COMPLETED,
        privacy_reason_code=None,
        created_at=now,
        updated_at=now,
    )
    candidates = EXTRACTOR.extract(current_turn=turn, previous_turn=None)
    output = {
        "case_id": _case_id(context),
        "candidates": [
            {
                "memory_type": candidate.memory_type.value,
                "structured_value": candidate.structured_value.__dict__,
            }
            for candidate in candidates
        ],
    }
    return {"output": json.dumps(output, ensure_ascii=False)}
