"""promptfoo 0.117.2 Python provider. The model input side never opens gold."""

import asyncio
import io
import json
import os
import socket
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from digital_souls_core.application import Provider
from digital_souls_core.local_embedding import LocalEmbedding
from digital_souls_core.memory_ranking import MemoryEmbedding
from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.semantic_answer_codex import CodexSubscriptionAnswerProvider
from digital_souls_core.semantic_answer_external import ExternalAnswerProfile, external_credentials
from digital_souls_core.semantic_answer_runtime import (
    FixtureAnswerProvider,
    answer_character,
    complete_answer,
    load_profile,
    runtime_identity,
)
from digital_souls_core.semantic_evaluation_cases import parse_evaluation_inputs
from digital_souls_core.semantic_evaluation_runtime import FixtureEmbedding, isolated_case

EVAL = Path(__file__).resolve().parent


def deny_fixture_network() -> None:
    original = socket.socket.connect

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family != socket.AF_UNIX:
            raise RuntimeError("Fixture IP network disabled")
        return original(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign, assignment]


def _call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        config = options.get("config", {})
        if set(config) - {
            "mode",
            "profile_path",
            "fixture_variant",
            "basePath",
            "pythonExecutable",
        }:
            raise ValueError
        variables = context.get("vars", {})
        if set(variables) != {"case_id"} or prompt != variables["case_id"]:
            raise ValueError
        mode = config.get("mode", "fixture")
        profile_path = config.get("profile_path")
        identity = runtime_identity(mode, profile_path)
        inputs = parse_evaluation_inputs((EVAL / "cases.json").read_text(encoding="utf-8"))
        case = next(c for c in inputs.cases if c.id == prompt)
        profile = load_profile(profile_path, mode)
        provider: Provider
        embedding: MemoryEmbedding
        if mode == "fixture":
            deny_fixture_network()
            fixture = json.loads((EVAL / "answer-fixtures.json").read_text(encoding="utf-8"))
            if fixture["schema_version"] != 1 or set(fixture["responses"]) != {
                c.id for c in inputs.cases
            }:
                raise ValueError
            response = fixture["responses"][case.id]
            variant = config.get("fixture_variant")
            if variant is not None:
                invalid = json.loads((EVAL / "invalid-answer-fixtures.json").read_text())
                response = invalid["overrides"][variant].get(case.id, response)
            if (
                set(response) - {"answer", "error"}
                or type(response.get("answer", "")) is not str
                or type(response.get("error", False)) is not bool
            ):
                raise ValueError
            provider = FixtureAnswerProvider(
                response.get("answer", ""), error=response.get("error", False)
            )
            embedding = FixtureEmbedding(inputs)
        else:
            if profile is None or config.get("fixture_variant") is not None:
                raise ValueError
            from digital_souls_core.provider import LiteLLMProvider

            provider = (
                CodexSubscriptionAnswerProvider(profile)
                if isinstance(profile, ExternalAnswerProfile)
                and profile.answer_provider == "codex_subscription"
                else LiteLLMProvider()
            )
            embedding = LocalEmbedding(profile.embedding)
        database = PostgresConfig(
            host=os.environ["DSC_TEST_POSTGRES_SOCKET"],
            port=int(os.environ["DSC_TEST_POSTGRES_PORT"]),
            database=os.environ["DSC_TEST_POSTGRES_DATABASE"],
            user=os.environ["DSC_TEST_POSTGRES_USER"],
        )
        if not database.host.startswith("/"):
            raise ValueError
        with isolated_case(database, case, embedding, external=mode == "external_model") as runtime:
            observation = asyncio.run(
                complete_answer(
                    runtime, answer_character(EVAL / "characters.json", case, profile), provider
                )
            )
        if isinstance(provider, CodexSubscriptionAnswerProvider):
            identity["codex_cli_version"] = provider.cli_version
        return {
            "output": json.dumps(
                {"observation": observation, "identity": identity},
                ensure_ascii=False,
                allow_nan=False,
            )
        }
    except Exception:
        return {"error": "answer_evaluation_failed"}


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if options.get("config", {}).get("mode") != "external_model":
        return _call_api(prompt, options, context)
    # Third-party SDK output is private even when it prints before raising.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            if os.environ.get("DSC_EXECUTE_EXTERNAL_MODEL") != "1":
                raise ValueError
            profile = load_profile(options["config"].get("profile_path"), "external_model")
            assert isinstance(profile, ExternalAnswerProfile)
            with external_credentials(profile):
                return _call_api(prompt, options, context)
        except Exception:
            return {"error": "answer_evaluation_failed"}
