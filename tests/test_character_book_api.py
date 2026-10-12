"""Both HTTP APIs use the same privacy-processed scan source before trimming."""

from dataclasses import replace
from pathlib import Path

import pytest

from digital_souls_core.character_book import load_book
from digital_souls_core.contracts import Message

from .test_character_book import entry
from .test_prompt_assembly_api import REGIONS, configured, http_setup, post, seeded_turns

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 99])
@pytest.mark.parametrize("api", ["chat", "history"])
def test_shared_scan_context_and_pre_budget_history(tmp_path: Path, depth: int, api: str) -> None:
    limits = dict.fromkeys(REGIONS, 10000)
    limits["history"] = 150
    card = configured(tmp_path, prompt_budget=limits)
    card = replace(
        card,
        character_book=load_book(
            {
                "extensions": {},
                "scan_depth": depth,
                "entries": [
                    entry(keys=["recent-answer"], content="assistant-lore"),
                    entry(keys=["recent-user-key"], content="user-lore"),
                    entry(keys=["old"], content="pruned-lore"),
                    entry(keys=["tool-result"], content="never-tool-lore"),
                    entry(
                        keys=["caller", "persona", "memory-0", "post"],
                        content="never-instruction-lore",
                    ),
                ],
            }
        ),
    )
    seeded = tuple(
        m.model_copy(update={"content": "recent-user-key"})
        if m.role == "user" and m.content == "recent"
        else m
        for m in seeded_turns()
    )
    http, provider, inference, _ = http_setup(card, seeded)
    current = [{"role": "user", "content": "now"}]
    response = post(http, api, seeded, current)
    assert response.status_code == 200
    system = provider.calls[0][1]["messages"][0]["content"]
    assert ("assistant-lore" in system) == (depth >= 2)
    # Assistant tool-call is a separate message: null body still consumes depth.
    assert ("user-lore" in system) == (depth >= 4)
    assert ("pruned-lore" in system) == (depth == 99)
    assert "never-tool-lore" not in system and "never-instruction-lore" not in system
    assert inference.prepared.usage.dropped["history"] == 2
    assert "old" not in [m.get("content") for m in provider.calls[0][1]["messages"]]


def test_history_uses_sanitized_snapshot_and_conversation_isolation(tmp_path: Path) -> None:
    card = replace(
        configured(tmp_path),
        character_book=load_book(
            {
                "extensions": {},
                "scan_depth": 99,
                "entries": [
                    entry(keys=["raw-private"], content="forbidden-lore"),
                    entry(keys=["masked"], content="safe-lore"),
                ],
            }
        ),
    )
    stored = (Message(role="user", content="masked"), Message(role="assistant", content="safe"))
    http, provider, _, _ = http_setup(card, stored)
    assert post(http, "history", stored, [{"role": "user", "content": "now"}]).status_code == 200
    system = provider.calls[0][1]["messages"][0]["content"]
    assert "safe-lore" in system and "forbidden-lore" not in system
    fresh_http, fresh_provider, _, _ = http_setup(card, ())
    assert post(fresh_http, "history", (), [{"role": "user", "content": "now"}]).status_code == 200
    assert "safe-lore" not in fresh_provider.calls[0][1]["messages"][0]["content"]


def test_prepared_usage_and_repr_hide_book_fields(tmp_path: Path) -> None:
    private = "synthetic-private-marker"
    card = replace(
        configured(tmp_path),
        character_book=load_book(
            {
                "extensions": {},
                "token_budget": 0,
                "entries": [
                    entry(
                        keys=[private], name=private, id=private, comment=private, content=private
                    )
                ],
            }
        ),
    )
    http, _, inference, _ = http_setup(card, ())
    assert post(http, "chat", (), [{"role": "user", "content": private}]).status_code == 200
    assert private not in repr(inference.prepared.usage)
    assert private not in repr(inference.prepared)
    assert inference.prepared.usage.dropped["lore"] == 1
