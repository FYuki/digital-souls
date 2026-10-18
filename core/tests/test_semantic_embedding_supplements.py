"""Supplement analyses never need downloaded dataset text or a real server in CI."""

import json
from pathlib import Path

import pytest

from digital_souls_core.semantic_embedding_candidates import PrefixEmbedding
from digital_souls_core.semantic_embedding_supplements import Mkqa, evaluate_supplements, load_rows

from .test_semantic_embedding_candidates import FakeEmbedding

pytestmark = pytest.mark.ut


async def test_descriptive_analysis_clips_inputs_and_never_exports_bodies(tmp_path: Path) -> None:
    manifest: dict[str, dict[str, object]] = {
        "nomiracl": {
            "revision": "ecd08778d0426a5ca28ac99763b0c9ddc2c78e68",
            "counts": {"queries": 1},
        },
        "mkqa": {"revision": "f964fe1bac4d580cee3d4928572df1f676f8e656", "counts": {"pairs": 2}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    query, body = "synthetic query " * 50, "synthetic passage " * 100
    row = dict(
        schema_version=1,
        dataset="nomiracl-ja",
        source_id="synthetic-1",
        subset="relevant",
        query=query,
        passages=[
            dict(id="a", title="Synthetic title", text=body, relevance=1),
            dict(id="b", title="Other title", text="Other synthetic passage", relevance=0),
        ],
    )
    (tmp_path / "nomiracl-ja.jsonl").write_text(json.dumps(row) + "\n")
    mkqa = [
        dict(
            schema_version=1,
            dataset="mkqa-ja-en",
            source_id=str(i),
            ja=f"架空の質問{i}",
            en=f"Synthetic question {i}",
        )
        for i in range(2)
    ]
    (tmp_path / "mkqa-ja-en.jsonl").write_text("\n".join(json.dumps(r) for r in mkqa) + "\n")
    fake = FakeEmbedding()
    result = await evaluate_supplements(tmp_path, PrefixEmbedding(fake, "query: ", "passage: "))
    assert result["nomiracl"]["auc"] == 0.5
    assert result["nomiracl"]["relevant"]["count"] == 1
    assert result["nomiracl"]["non_relevant"]["count"] == 1
    assert result["mkqa"]["ja_to_en"]["pairs"]["count"] == 2
    assert result["mkqa"]["en_to_ja"]["non_pairs"]["count"] == 2
    assert len(fake.calls[0][0]) == 128 + len("query: ")
    assert len(fake.calls[0][1]) == 200 + len("passage: ")
    assert all(t.startswith("query: ") for call in fake.calls[1:] for t in call)
    # A quote-leading non-JSON question must not be sent, across prefix choices.
    mkqa.append(
        dict(
            schema_version=1,
            dataset="mkqa-ja-en",
            source_id="blocked",
            ja='"synthetic malformed JSON',
            en="Synthetic blocked pair",
        )
    )
    manifest["mkqa"]["counts"] = {"pairs": 3}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "mkqa-ja-en.jsonl").write_text("\n".join(json.dumps(r) for r in mkqa) + "\n")
    for query_prefix, document_prefix in (("", ""), ("query: ", "passage: ")):
        second_fake = FakeEmbedding()
        filtered = await evaluate_supplements(
            tmp_path, PrefixEmbedding(second_fake, query_prefix, document_prefix)
        )
        assert filtered["sources"]["mkqa_original_pairs"] == 3
        assert filtered["sources"]["mkqa_measured_pairs"] == 2
        assert filtered["sources"]["mkqa_not_run_pairs"] == 1
        assert "NOT RUN" in filtered["status"]
        assert all("Synthetic blocked pair" not in t for call in second_fake.calls for t in call)
    output = json.dumps(result)
    assert query not in output and body not in output
    assert all(str(r["ja"]) not in output and str(r["en"]) not in output for r in mkqa)


@pytest.mark.parametrize("kind", ["empty", "duplicate", "schema", "extra"])
def test_local_supplement_loader_fails_closed(tmp_path: Path, kind: str) -> None:
    row = dict(schema_version=1, dataset="mkqa-ja-en", source_id="1", ja="合成", en="synthetic")
    rows = [row]
    if kind == "empty":
        rows = []
    elif kind == "duplicate":
        rows.append(row)
    elif kind == "schema":
        row["schema_version"] = 99
    else:
        row["unexpected"] = "synthetic"
    path = tmp_path / "mkqa.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError):
        load_rows(path, Mkqa, 1000)
