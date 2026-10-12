#!/usr/bin/env python3
"""Download pinned data to a fresh external directory; never execute dataset code.

Run with python -I from the repository, not from the untrusted data directory.
Only counts/provenance are printed. JSONL bodies and compressed sources stay local.
"""

import argparse
import gzip
import hashlib
import json
import sys
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NOMIRACL_REVISION = "ecd08778d0426a5ca28ac99763b0c9ddc2c78e68"
MKQA_REVISION = "f964fe1bac4d580cee3d4928572df1f676f8e656"
NOMIRACL_BASE = f"https://huggingface.co/datasets/miracl/nomiracl/resolve/{NOMIRACL_REVISION}/"
MKQA_BASE = f"https://raw.githubusercontent.com/apple-aiml-research/ml-mkqa/{MKQA_REVISION}/"
MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_LINE = 1024 * 1024


def fresh_directory(path: Path) -> Path:
    """Refuse Git trees, symlinks and nonempty destinations before any download."""
    if path.is_symlink():
        raise ValueError("Invalid output directory")
    path = path.resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Output must be outside Git trees")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError("Output directory must be empty")
        path.chmod(0o700)
    else:
        path.mkdir(mode=0o700)
    return path


def download(url: str, path: Path) -> dict[str, str | int]:
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError("Download size limit exceeded")
            digest.update(chunk)
            output.write(chunk)
    return {"url": url, "sha256": digest.hexdigest(), "bytes": size}


def json_lines(path: Path) -> Iterator[dict[str, Any]]:
    total = 0
    with gzip.open(path, "rb") as source:
        while line := source.readline(MAX_LINE + 1):
            total += len(line)
            if len(line) > MAX_LINE or total > MAX_EXPANDED:
                raise ValueError("Expanded data limit exceeded")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Invalid data row")
            yield value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid text field")
    return value


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("No selected rows")
    with path.open("x", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def convert_nomiracl(directory: Path, count: int) -> dict[str, int]:
    selections: list[dict[str, Any]] = []
    wanted: set[str] = set()
    for subset in ("relevant", "non_relevant"):
        topics = (directory / f"topics-{subset}.tsv").read_text(encoding="utf-8").splitlines()
        queries: dict[str, str] = {}
        for line in topics:
            identifier, query = line.split("\t", 1)
            if identifier in queries:
                raise ValueError("Duplicate query")
            queries[text(identifier)] = text(query)
        selected = list(queries)[:count]
        if len(selected) != count:
            raise ValueError("Insufficient topics")
        labels: dict[str, dict[str, int]] = {qid: {} for qid in selected}
        for line in (directory / f"qrels-{subset}.tsv").read_text(encoding="utf-8").splitlines():
            qid, _, docid, label = line.split()
            if qid in labels:
                if label not in {"0", "1"} or docid in labels[qid]:
                    raise ValueError("Invalid relevance label")
                labels[qid][text(docid)] = int(label)
        for qid in selected:
            judgments = labels[qid]
            if (
                not judgments
                or len(judgments) > 100
                or bool(any(judgments.values())) != (subset == "relevant")
            ):
                raise ValueError("Inconsistent relevance subset")
            selections.append(
                {"source_id": qid, "subset": subset, "query": queries[qid], "labels": judgments}
            )
            wanted.update(judgments)
    documents: dict[str, dict[str, str]] = {}
    for row in json_lines(directory / "nomiracl-corpus.jsonl.gz"):
        docid = text(row["docid"])
        if docid in wanted:
            document = {"title": text(row["title"]), "text": text(row["text"])}
            if docid in documents and documents[docid] != document:
                raise ValueError("Conflicting passage")
            documents[docid] = document
    if set(documents) != wanted:
        raise ValueError("Missing passage")
    rows: list[dict[str, Any]] = []
    for selection in selections:
        labels = selection.pop("labels")
        selection["passages"] = [
            {"id": docid, **documents[docid], "relevance": label} for docid, label in labels.items()
        ]
        rows.append({"schema_version": 1, "dataset": "nomiracl-ja", **selection})
    write_rows(directory / "nomiracl-ja.jsonl", rows)
    return {
        "queries": len(rows),
        "relevant_queries": count,
        "non_relevant_queries": count,
        "passage_judgments": sum(len(row["passages"]) for row in rows),
        "unique_passages": len(documents),
    }


def convert_mkqa(directory: Path, count: int) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    scanned = 0
    for row in json_lines(directory / "mkqa.jsonl.gz"):
        scanned += 1
        identifier = row["example_id"]
        if type(identifier) is not int or identifier in seen:
            raise ValueError("Invalid example ID")
        seen.add(identifier)
        ja, en = text(row["queries"]["ja"]), text(row["queries"]["en"])
        if len(rows) < count:
            rows.append(
                {
                    "schema_version": 1,
                    "dataset": "mkqa-ja-en",
                    "source_id": str(identifier),
                    "ja": ja,
                    "en": en,
                }
            )
    if len(rows) != count:
        raise ValueError("Insufficient bilingual pairs")
    write_rows(directory / "mkqa-ja-en.jsonl", rows)
    return {"pairs": len(rows), "source_rows": scanned}


def prepare(directory: Path, nomiracl_count: int, mkqa_count: int) -> dict[str, Any]:
    if not 1 <= nomiracl_count <= 200 or not 1 <= mkqa_count <= 1000:
        raise ValueError("Invalid sample count")
    directory = fresh_directory(directory)
    provenance = {}
    downloads = {
        "nomiracl-card.md": NOMIRACL_BASE + "README.md",
        "mkqa-card.md": MKQA_BASE + "README.md",
        "nomiracl-corpus.jsonl.gz": NOMIRACL_BASE + "data/japanese/corpus.jsonl.gz",
        "mkqa.jsonl.gz": MKQA_BASE + "dataset/mkqa.jsonl.gz",
    }
    for subset in ("relevant", "non_relevant"):
        for kind in ("topics", "qrels"):
            downloads[f"{kind}-{subset}.tsv"] = (
                NOMIRACL_BASE + f"data/japanese/{kind}/dev.{subset}.tsv"
            )
    for filename, url in downloads.items():
        provenance[filename] = download(url, directory / filename)
    # Fail if pinned cards no longer contain the audited license declarations.
    if "apache-2.0" not in (directory / "nomiracl-card.md").read_text(encoding="utf-8"):
        raise ValueError("Unverified NoMIRACL license")
    if "Creative Commons Attribution-ShareAlike 3.0 Unported" not in (
        directory / "mkqa-card.md"
    ).read_text(encoding="utf-8"):
        raise ValueError("Unverified MKQA license")
    manifest = {
        "schema_version": 1,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "selection": "first rows in pinned file order; no model-based selection",
        "nomiracl": {
            "revision": NOMIRACL_REVISION,
            "card_license": "Apache-2.0",
            "split": "dev",
            "counts": convert_nomiracl(directory, nomiracl_count),
        },
        "mkqa": {
            "revision": MKQA_REVISION,
            "data_license": "CC-BY-SA-3.0",
            "counts": convert_mkqa(directory, mkqa_count),
        },
        "sources": provenance,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nomiracl-per-subset", type=int, default=50)
    parser.add_argument("--mkqa-pairs", type=int, default=100)
    args = parser.parse_args()
    if not sys.flags.isolated:
        print("Use python -I to isolate untrusted data.", file=sys.stderr)
        return 1
    try:
        manifest = prepare(args.output, args.nomiracl_per_subset, args.mkqa_pairs)
    except Exception:
        print(
            "Supplement preparation failed; partial files are NOT a completed dataset.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({k: v for k, v in manifest.items() if k != "sources"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
