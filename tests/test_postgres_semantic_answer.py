"""Answer evaluation must dispatch the production payload and discard revoked output."""

from pathlib import Path

import pytest

from digital_souls_core.semantic_evaluation_runtime import FixtureEmbedding, isolated_case

from .test_postgres_semantic_evaluation import case, config
from .test_semantic_retrieval_evaluation import DATA, ROOT, gold

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "identifier",
    ["synonym-02", "unrelated-observatory", "post-search-revocation", "post-answer-revocation"],
)
async def test_production_inference_dispatch_and_publication(identifier: str) -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer
    from digital_souls_core.semantic_answer_runtime import (
        FixtureAnswerProvider,
        answer_character,
        complete_answer,
    )

    provider = FixtureAnswerProvider(
        "架空のアオは鉛筆でメモを書きます。"
        if identifier == "synonym-02"
        else "記憶に情報がありません。"
    )
    with isolated_case(config(), case(identifier), FixtureEmbedding(DATA)) as runtime:
        character = answer_character(ROOT / "evals/semantic/characters.json", runtime.case, None)
        observed = await complete_answer(runtime, character, provider)
        assert score_answer(gold(identifier), observed)["passed"]
        assert len(provider.payloads) == int(gold(identifier).dispatch.valid)
        if provider.payloads:
            messages = provider.payloads[0]["messages"]
            assert messages[0]["role"] == "system"
            assert "## 応答方針" in messages[0]["content"]
            assert messages[-1] == {"role": "user", "content": runtime.case.query}
            assert all(r.id not in str(messages) for r in runtime.case.episodes)
            assert ("retrieved_memory_data" in str(messages)) == (
                identifier != "unrelated-observatory"
            )
        if identifier == "post-answer-revocation":
            assert observed["answer"] is None and observed["discarded"]


async def test_answer_provider_error_is_not_no_memory_success() -> None:
    from digital_souls_core.semantic_answer_runtime import (
        FixtureAnswerProvider,
        answer_character,
        complete_answer,
    )

    with isolated_case(config(), case("synonym-02"), FixtureEmbedding(DATA)) as runtime:
        with pytest.raises(ValueError, match="Answer evaluation failed"):
            await complete_answer(
                runtime,
                answer_character(ROOT / "evals/semantic/characters.json", runtime.case, None),
                FixtureAnswerProvider("", error=True),
            )


def test_promptfoo_provider_reads_no_gold_and_error_fixture_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_semantic_answer_provider import provider_module

    original = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        assert path.name != "expectations.json"
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read)
    # Restore socket changes made by the provider after this test as well.
    import socket

    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    module = provider_module()
    context = {"vars": {"case_id": "synonym-02"}}
    successful = module.call_api("synonym-02", {"config": {"mode": "fixture"}}, context)
    assert set(successful) == {"output"}
    failed = module.call_api(
        "synonym-02", {"config": {"mode": "fixture", "fixture_variant": "error"}}, context
    )
    assert failed == {"error": "answer_evaluation_failed"}


@pytest.mark.parametrize("memory_budget", [1, 65536])
async def test_answer_observation_tracks_only_memory_items_in_final_payload(
    memory_budget: int,
) -> None:
    from dataclasses import replace

    from digital_souls_core.semantic_answer_runtime import (
        FixtureAnswerProvider,
        answer_character,
        complete_answer,
    )

    provider = FixtureAnswerProvider("記憶に情報がありません。")
    with isolated_case(config(), case("synonym-02"), FixtureEmbedding(DATA)) as runtime:
        char = answer_character(ROOT / "evals/semantic/characters.json", runtime.case, None)
        char = replace(
            char,
            config=char.config.model_copy(
                update={
                    "prompt_budget": char.config.prompt_budget.model_copy(
                        update={"memory": memory_budget}
                    )
                }
            ),
        )
        observed = await complete_answer(runtime, char, provider)
        expected = runtime.retrieval.results[0] if memory_budget > 1 else ()
        assert observed["context_empty"] == (not expected)
        assert observed["dispatch_memory_ids"] == [item.identifier for item in expected]
        assert observed["dispatched"]
        assert ("retrieved_memory_data" in str(provider.payloads[0])) == bool(expected)


async def test_external_dry_run_prepares_all_cases_with_local_embedding_without_answer_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from typing import Any

    from digital_souls_core.local_embedding import LocalEmbedding
    from digital_souls_core.provider import LiteLLMProvider
    from digital_souls_core.semantic_answer_bridge import dry_run

    from .test_semantic_answer_external import external_data

    path = tmp_path / "external.json"
    path.write_text(json.dumps(external_data()))
    embedding_calls: list[str] = []

    async def local_embed(
        self: LocalEmbedding, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        assert self._profile.api_base.startswith("http://127.0.0.1:")
        embedding_calls.extend(texts)
        vectors = await FixtureEmbedding(DATA).embed(texts)
        return tuple(v + (0.0,) * (self.space.dimensions - len(v)) for v in vectors)

    async def no_send(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Dry-run must never invoke answer transport")

    monkeypatch.setattr(LocalEmbedding, "embed", local_embed)
    monkeypatch.setattr(LiteLLMProvider, "complete", no_send)
    result = await dry_run({"profile": str(path)})
    assert result["cases"] == 62
    assert result["calls_per_run"] == sum(g.dispatch.valid for g in DATA.expectations.cases)
    assert result["input_tokens_per_run"] > 0
    assert embedding_calls
    assert all(c.query not in json.dumps(result) for c in DATA.cases.cases)


def test_external_promptfoo_provider_uses_production_litellm_with_only_local_auxiliaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import os
    from typing import Any

    import litellm

    from digital_souls_core.local_embedding import LocalEmbedding

    from .support import completion
    from .test_semantic_answer_external import external_data
    from .test_semantic_answer_provider import provider_module

    path = tmp_path / "external.json"
    path.write_text(json.dumps(external_data()))
    monkeypatch.setenv("DSC_EXECUTE_EXTERNAL_MODEL", "1")
    monkeypatch.setenv("SYNTHETIC_ANSWER_KEY", "synthetic-key-sentinel")
    monkeypatch.setenv(
        "SYNTHETIC_ANSWER_BASE", "https://user:synthetic-url@host.invalid/private?secret"
    )
    calls: list[dict[str, Any]] = []
    embeddings: list[object] = []

    async def local_embed(
        self: LocalEmbedding, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        embeddings.append(self._profile)
        vectors = await FixtureEmbedding(DATA).embed(texts)
        return tuple(v + (0.0,) * (self.space.dimensions - len(v)) for v in vectors)

    async def fake_sdk(**kwargs: Any) -> Any:
        assert os.environ["OPENAI_API_KEY"] == "synthetic-key-sentinel"
        assert os.environ["OPENAI_BASE_URL"].endswith("/private?secret")
        assert "api_base" not in kwargs and "api_key" not in kwargs
        assert kwargs["model"] == "openai/synthetic-model"
        assert kwargs["messages"][-1]["content"] == case("synonym-02").query
        assert "retrieved_memory_data" in str(kwargs["messages"])
        calls.append(kwargs)
        print("synthetic-key-sentinel synthetic-body-sentinel")
        return litellm.ModelResponse(**completion("synthetic-model"))

    monkeypatch.setattr(LocalEmbedding, "embed", local_embed)
    monkeypatch.setattr(litellm, "acompletion", fake_sdk)
    result = provider_module().call_api(
        "synonym-02",
        {"config": {"mode": "external_model", "profile_path": str(path)}},
        {"vars": {"case_id": "synonym-02"}},
    )
    assert set(result) == {"output"}
    parsed = json.loads(result["output"])
    assert parsed["observation"]["dispatched"] is True
    assert parsed["identity"]["transport"] == "sdk"
    assert len(calls) == 1 and embeddings
    assert capsys.readouterr() == ("", "")
