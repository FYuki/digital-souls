"""Answer evaluation must dispatch the production payload and discard revoked output."""

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
    from pathlib import Path

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
