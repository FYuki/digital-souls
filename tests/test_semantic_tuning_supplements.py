"""Local conversion of synthetic stand-ins; never load real external dataset bodies."""

import gzip
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.ut
SCRIPT = Path(__file__).resolve().parents[1] / "tools/prepare-semantic-tuning-supplements.py"
FUNCTIONS = runpy.run_path(str(SCRIPT))


def compressed(path: Path, rows: list[Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row) + "\n")


def nomiracl_inputs(path: Path) -> None:
    for subset, qid, docid, label in (("relevant", "q1", "d1", 1), ("non_relevant", "q2", "d2", 0)):
        (path / f"topics-{subset}.tsv").write_text(f"{qid}\tSynthetic query {qid}\n")
        (path / f"qrels-{subset}.tsv").write_text(f"{qid}\tQ0\t{docid}\t{label}\n")
    compressed(
        path / "nomiracl-corpus.jsonl.gz",
        [
            {"docid": "d1", "title": "Synthetic A", "text": "Synthetic relevant passage"},
            {"docid": "d2", "title": "Synthetic B", "text": "Synthetic unrelated passage"},
            {"docid": "unused", "title": "Synthetic C", "text": "Not selected"},
        ],
    )


def test_nomiracl_keeps_judgments_and_only_selected_passages(tmp_path: Path) -> None:
    nomiracl_inputs(tmp_path)
    counts = FUNCTIONS["convert_nomiracl"](tmp_path, 1)
    assert counts == {
        "queries": 2,
        "relevant_queries": 1,
        "non_relevant_queries": 1,
        "passage_judgments": 2,
        "unique_passages": 2,
    }
    rows = [json.loads(line) for line in (tmp_path / "nomiracl-ja.jsonl").read_text().splitlines()]
    assert [row["passages"][0]["relevance"] for row in rows] == [1, 0]
    assert [row["source_id"] for row in rows] == ["q1", "q2"]
    assert "Not selected" not in (tmp_path / "nomiracl-ja.jsonl").read_text()


@pytest.mark.parametrize("invalid", ["missing", "label", "subset", "duplicate", "count"])
def test_nomiracl_rejects_missing_or_inconsistent_data(tmp_path: Path, invalid: str) -> None:
    nomiracl_inputs(tmp_path)
    if invalid == "missing":
        compressed(tmp_path / "nomiracl-corpus.jsonl.gz", [])
    elif invalid == "label":
        (tmp_path / "qrels-relevant.tsv").write_text("q1 Q0 d1 2\n")
    elif invalid == "subset":
        (tmp_path / "qrels-non_relevant.tsv").write_text("q2 Q0 d2 1\n")
    elif invalid == "duplicate":
        (tmp_path / "topics-relevant.tsv").write_text("q1\tSynthetic A\nq1\tSynthetic B\n")
    with pytest.raises(ValueError):
        FUNCTIONS["convert_nomiracl"](tmp_path, 2 if invalid == "count" else 1)
    assert not (tmp_path / "nomiracl-ja.jsonl").exists()


def test_mkqa_pairs_preserve_alignment_in_file_order_without_answers(tmp_path: Path) -> None:
    compressed(
        tmp_path / "mkqa.jsonl.gz",
        [
            {"example_id": 11, "queries": {"ja": "合成の問い甲", "en": "Synthetic question A"}},
            {"example_id": 22, "queries": {"ja": "合成の問い乙", "en": "Synthetic question B"}},
        ],
    )
    assert FUNCTIONS["convert_mkqa"](tmp_path, 1) == {"pairs": 1, "source_rows": 2}
    rows = [json.loads(line) for line in (tmp_path / "mkqa-ja-en.jsonl").read_text().splitlines()]
    assert rows == [
        {
            "schema_version": 1,
            "dataset": "mkqa-ja-en",
            "source_id": "11",
            "ja": "合成の問い甲",
            "en": "Synthetic question A",
        }
    ]


@pytest.mark.parametrize("invalid", ["missing-language", "duplicate", "empty", "count"])
def test_mkqa_invalid_pairs_do_not_produce_success(tmp_path: Path, invalid: str) -> None:
    row = {"example_id": 1, "queries": {"ja": "合成の問い", "en": "Synthetic question"}}
    if invalid == "missing-language":
        row["queries"] = {"ja": "合成の問い"}
    elif invalid == "empty":
        row["queries"] = {"ja": "", "en": "Synthetic question"}
    compressed(tmp_path / "mkqa.jsonl.gz", [row, row] if invalid == "duplicate" else [row])
    with pytest.raises((ValueError, KeyError)):
        FUNCTIONS["convert_mkqa"](tmp_path, 2 if invalid == "count" else 1)
    assert not (tmp_path / "mkqa-ja-en.jsonl").exists()


@pytest.mark.parametrize("invalid", ["nonempty", "symlink", "git"])
def test_destination_refuses_nonempty_symlink_or_git_tree(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "data"
    path.mkdir()
    if invalid == "nonempty":
        (path / "untrusted.py").write_text("raise RuntimeError('must never execute')")
    elif invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    else:
        (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError):
        FUNCTIONS["fresh_directory"](path)


def test_fresh_directory_has_private_permissions(tmp_path: Path) -> None:
    path = FUNCTIONS["fresh_directory"](tmp_path / "data")
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("row", [["not-an-object"], {"text": "x" * (1024 * 1024)}])
def test_untrusted_json_shape_and_expansion_limit(tmp_path: Path, row: Any) -> None:
    compressed(tmp_path / "oversized.jsonl.gz", [row])
    with pytest.raises(ValueError):
        list(FUNCTIONS["json_lines"](tmp_path / "oversized.jsonl.gz"))


def test_cli_refuses_missing_isolation_before_network_or_output(tmp_path: Path) -> None:
    output = tmp_path / "data"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert process.returncode == 1
    assert process.stdout == "" and process.stderr == "Use python -I to isolate untrusted data.\n"
    assert not output.exists()


def test_nomiracl_retains_extra_judged_positives_beyond_ten(tmp_path: Path) -> None:
    nomiracl_inputs(tmp_path)
    (tmp_path / "qrels-relevant.tsv").write_text("".join(f"q1 Q0 d{i} 1\n" for i in range(1, 17)))
    compressed(
        tmp_path / "nomiracl-corpus.jsonl.gz",
        [
            {"docid": f"d{i}", "title": "Synthetic", "text": "Synthetic passage"}
            for i in range(1, 17)
        ],
    )
    counts = FUNCTIONS["convert_nomiracl"](tmp_path, 1)
    assert counts["passage_judgments"] == 17
    rows = [json.loads(line) for line in (tmp_path / "nomiracl-ja.jsonl").read_text().splitlines()]
    assert len(rows[0]["passages"]) == 16


@pytest.mark.parametrize("conflicting", [False, True])
def test_nomiracl_collapses_identical_repeats_but_rejects_conflicts(
    tmp_path: Path, conflicting: bool
) -> None:
    nomiracl_inputs(tmp_path)
    document = {"docid": "d1", "title": "Synthetic A", "text": "Synthetic relevant passage"}
    compressed(
        tmp_path / "nomiracl-corpus.jsonl.gz",
        [
            document,
            {**document, "text": "Conflict"} if conflicting else document,
            {"docid": "d2", "title": "Synthetic B", "text": "Synthetic unrelated passage"},
        ],
    )
    if conflicting:
        with pytest.raises(ValueError, match="Conflicting passage"):
            FUNCTIONS["convert_nomiracl"](tmp_path, 1)
    else:
        assert FUNCTIONS["convert_nomiracl"](tmp_path, 1)["unique_passages"] == 2
