"""Input-only boundaries: no database or model may be used for invalid requests."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.semantic_answer_runtime import load_profile, runtime_identity
from digital_souls_core.semantic_evaluation_cases import parse_evaluation_inputs

from .test_semantic_retrieval_evaluation import ROOT

pytestmark = pytest.mark.it1


def provider_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "answer_provider", ROOT / "evals/semantic/provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "options,context",
    [
        ({"config": {"mode": "local_model"}}, {"vars": {"case_id": "synonym-02"}}),
        (
            {"config": {"mode": "fixture", "profile_path": "/invalid"}},
            {"vars": {"case_id": "synonym-02"}},
        ),
        ({"config": {"mode": "unknown"}}, {"vars": {"case_id": "synonym-02"}}),
        ({"config": {"unknown": "private-marker"}}, {"vars": {"case_id": "synonym-02"}}),
        (
            {"config": {"mode": "fixture"}},
            {"vars": {"case_id": "synonym-02", "gold": "private-marker"}},
        ),
    ],
)
def test_provider_invalid_configuration_is_safe_and_never_falls_back(
    options: dict[str, Any], context: dict[str, Any]
) -> None:
    assert provider_module().call_api("synonym-02", options, context) == {
        "error": "answer_evaluation_failed"
    }


def test_input_loader_never_requires_gold_and_validates_duplicate_ids() -> None:
    text = (ROOT / "evals/semantic/cases.json").read_text()
    assert len(parse_evaluation_inputs(text).cases) == 62
    malformed = json.loads(text)
    malformed["cases"][1] = malformed["cases"][0]
    with pytest.raises(ValueError):
        parse_evaluation_inputs(json.dumps(malformed))


def test_identity_does_not_open_expectations(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        assert path.name != "expectations.json"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    identity = runtime_identity("fixture", None)
    assert identity["classifier"] == "synthetic" and not identity["quality_evidence"]


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_missing_embedding_permission_rejects_profile(
    tmp_path: Path, enabled: bool
) -> None:
    chat = json.loads((ROOT / "evals/semantic/characters.json").read_text())[0]["profile"]
    embedding = json.loads((ROOT / "examples/embedding.example.json").read_text())
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"enabled": enabled, "chat": chat, "embedding": embedding}))
    with pytest.raises(ValueError, match="Invalid answer profile"):
        load_profile(str(p))
