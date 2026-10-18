"""Synthetic text-part inputs shared by boundary contract tests."""

from typing import Any

INVALID_CONTENT: list[Any] = [
    [],
    [{"type": "image_url", "text": "synthetic"}],
    [{"type": "input_audio", "text": "synthetic"}],
    [{"type": "file", "text": "synthetic"}],
    [{"type": "text", "text": "synthetic", "cache_control": {"type": "ephemeral"}}],
    [{"type": "text", "text": "synthetic", "unknown": True}],
    [{"type": "text", "text": 123}],
    [{"type": "text", "text": False}],
    [{"type": "text", "text": None}],
    [{"type": "text"}],
    [{"text": "synthetic"}],
    ["synthetic"],
    [None],
    [123],
    [{"type": "text", "text": "valid"}, {"type": "file", "text": "invalid"}],
    {"type": "text", "text": "synthetic"},
    123,
    True,
]


def parts(*texts: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text} for text in texts]
