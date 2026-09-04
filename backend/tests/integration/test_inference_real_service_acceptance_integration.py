from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import subprocess
from collections.abc import Callable
from typing import TypeVar

import pytest

from app.inference import InferenceCaller, InferenceMessage, InferenceTarget
from app.inference.runtime import create_inference_runtime


pytestmark = pytest.mark.inference_real
_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
_MESSAGE = (InferenceMessage("user", "Return a minimal successful result."),)
_Result = TypeVar("_Result")


def _enabled() -> bool:
    return os.environ.get("RUN_INFERENCE_REAL_SERVICE_TESTS") == "true"


def _commit_sha() -> str:
    configured = os.environ.get("INFERENCE_ACCEPTANCE_COMMIT_SHA")
    if configured:
        return configured
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _record(
    evidence: list[dict[str, str]],
    *,
    provider: str,
    model: str,
    capability: str,
    result: str,
) -> None:
    evidence.append(
        {
            "executed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "commit_sha": _commit_sha(),
            "environment": os.environ.get("INFERENCE_ACCEPTANCE_ENVIRONMENT", "dev"),
            "provider": provider,
            "model": model,
            "capability": capability,
            "result": result,
        }
    )


def _attempt(
    evidence: list[dict[str, str]],
    *,
    provider: str,
    model: str,
    capability: str,
    operation: Callable[[], _Result],
) -> _Result:
    try:
        value = operation()
    except Exception:
        _record(
            evidence,
            provider=provider,
            model=model,
            capability=capability,
            result="failure",
        )
        raise
    _record(
        evidence,
        provider=provider,
        model=model,
        capability=capability,
        result="success",
    )
    return value


def test_configured_provider_real_service_capabilities() -> None:
    if not _enabled():
        pytest.skip("RUN_INFERENCE_REAL_SERVICE_TESTS=true の明示時だけ実行する")
    provider = os.environ.get("INFERENCE_ACCEPTANCE_PROVIDER")
    if provider not in {"ollama", "openai-api", "openai-codex"}:
        pytest.fail("INFERENCE_ACCEPTANCE_PROVIDER must select a supported provider")

    runtime = create_inference_runtime(os.environ)
    evidence: list[dict[str, str]] = []
    try:
        runtime.probe_startup()
        configured = {
            target: resolved
            for target, resolved in runtime.settings.targets.items()
            if resolved.reference.provider_id == provider
        }

        if provider == "ollama":
            chat = configured.get(InferenceTarget.CHAT)
            structured = configured.get(InferenceTarget.MEMORY_EXTRACTION)
            embedding = configured.get(InferenceTarget.EMBEDDING)
            if chat is None or structured is None or embedding is None:
                pytest.fail("Ollama acceptance requires chat, memory-extraction, embedding")
            _attempt(
                evidence,
                provider=provider,
                model=chat.reference.model_id,
                capability="estimate_input_tokens",
                operation=lambda: runtime.router.estimate_input_tokens(
                    caller=InferenceCaller.CHAT,
                    target=InferenceTarget.CHAT,
                    messages=_MESSAGE,
                ),
            )
            _attempt(
                evidence,
                provider=provider,
                model=chat.reference.model_id,
                capability="generate_text",
                operation=lambda: runtime.router.generate_text(
                    caller=InferenceCaller.CHAT,
                    target=InferenceTarget.CHAT,
                    messages=_MESSAGE,
                ),
            )

            async def stream() -> None:
                chunks = [
                    chunk
                    async for chunk in runtime.router.stream_text(
                        caller=InferenceCaller.CHAT,
                        target=InferenceTarget.CHAT,
                        messages=_MESSAGE,
                    )
                ]
                assert chunks

            _attempt(
                evidence,
                provider=provider,
                model=chat.reference.model_id,
                capability="stream_text",
                operation=lambda: asyncio.run(stream()),
            )
            _attempt(
                evidence,
                provider=provider,
                model=structured.reference.model_id,
                capability="generate_structured",
                operation=lambda: runtime.router.generate_structured(
                    caller=InferenceCaller.MEMORY_EXTRACTION,
                    target=InferenceTarget.MEMORY_EXTRACTION,
                    messages=_MESSAGE,
                    response_schema=_SCHEMA,
                ),
            )
            _attempt(
                evidence,
                provider=provider,
                model=embedding.reference.model_id,
                capability="embed",
                operation=lambda: runtime.router.embed(
                    caller=InferenceCaller.MEMORY_INDEX,
                    target=InferenceTarget.EMBEDDING,
                    inputs=("synthetic acceptance input",),
                ),
            )
        elif provider == "openai-api":
            text = configured.get(InferenceTarget.HEAVY_REASONING)
            structured = configured.get(InferenceTarget.MEMORY_EXTRACTION)
            if text is None or structured is None:
                pytest.fail("OpenAI API acceptance requires heavy-reasoning and memory-extraction")
            _attempt(
                evidence,
                provider=provider,
                model=text.reference.model_id,
                capability="generate_text",
                operation=lambda: runtime.router.generate_text(
                    caller=InferenceCaller.HEAVY_REASONING,
                    target=InferenceTarget.HEAVY_REASONING,
                    messages=_MESSAGE,
                ),
            )
            _attempt(
                evidence,
                provider=provider,
                model=structured.reference.model_id,
                capability="generate_structured",
                operation=lambda: runtime.router.generate_structured(
                    caller=InferenceCaller.MEMORY_EXTRACTION,
                    target=InferenceTarget.MEMORY_EXTRACTION,
                    messages=_MESSAGE,
                    response_schema=_SCHEMA,
                ),
            )
            embedding = configured.get(InferenceTarget.EMBEDDING)
            if embedding is not None:
                _attempt(
                    evidence,
                    provider=provider,
                    model=embedding.reference.model_id,
                    capability="embed",
                    operation=lambda: runtime.router.embed(
                        caller=InferenceCaller.MEMORY_INDEX,
                        target=InferenceTarget.EMBEDDING,
                        inputs=("synthetic acceptance input",),
                    ),
                )
        else:
            text = configured.get(InferenceTarget.HEAVY_REASONING)
            if text is None:
                pytest.fail("Codex acceptance requires heavy-reasoning")
            _attempt(
                evidence,
                provider=provider,
                model=text.reference.model_id,
                capability="generate_text",
                operation=lambda: runtime.router.generate_text(
                    caller=InferenceCaller.HEAVY_REASONING,
                    target=InferenceTarget.HEAVY_REASONING,
                    messages=_MESSAGE,
                ),
            )
    finally:
        runtime.close()
        print(json.dumps({"inference_acceptance": evidence}, ensure_ascii=False))
