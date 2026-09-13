"""固定正解と構造化出力を照合する。モデルによる採点・実行後の正解変更は行わない。"""
import json

from app.memory.episodic.extraction_contracts import SourceQuote
from app.memory.episodic.quotes import InvalidExtraction, resolve_quote, owns_anchor
from evals.episodic_quality.provider import build_input, source_context


def at(value, path):
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def matches(value, spec):
    for path, expected in spec.items():
        actual = at(value, path)
        if isinstance(expected, dict) and "contains_any" in expected:
            if not isinstance(actual, str) or not any(x in actual for x in expected["contains_any"]):
                return False
        elif isinstance(expected, dict) and "one_of" in expected:
            if actual not in expected["one_of"]:
                return False
        elif actual != expected:
            return False
    return True


def quotes_valid(batch, payload):
    """引用の表記だけでなく、提示出典・版・role・位置・所有範囲も検証する。"""
    snapshot, chunk = source_context(payload)
    def valid(quote, primary=False):
        try:
            resolved = resolve_quote(SourceQuote.model_validate(quote), snapshot, chunk)
            return not primary or owns_anchor(chunk, resolved)
        except (InvalidExtraction, ValueError, TypeError):
            return False
    for r in batch.get("records", []):
        if not valid(r["anchor"], True) or not all(valid(q) for q in r["sources"]):
            return False
        if r.get("time_source") and not valid(r["time_source"]):
            return False
    for relation in batch.get("links", []) + batch.get("merges", []):
        if not all(valid(q) for q in relation.get("sources", relation.get("evidence", []))):
            return False
    return True


def evaluate(output, variables):
    if not isinstance(output, dict) or output.get("error_type"):
        return False
    truth = json.loads(variables["expected_json"])
    data = json.loads(variables["input_json"])
    if data["stage"] == "privacy":
        return output.get("classification") == truth["classification"]
    _, _, payload = build_input(variables["input_json"])
    if data["stage"] == "catalog":
        result = output.get("catalog", {})
        if not result.get("complete") or any(m["key"] != "fact" for m in result.get("matches", [])):
            return False
        got = sorted((m["target"]["id"], m["target"]["version"], m["operation"], m["certainty"])
                     for m in result.get("matches", []))
        expected = sorted(tuple(x) for x in truth["matches"])
        # 照合根拠は提示会話の引用でなければならない。
        evidence = {"records": [], "links": [
            {"sources": m["evidence"]} for m in result.get("matches", [])]}
        return got == expected and quotes_valid(evidence, payload)
    batch = output.get("batch", {})
    if not batch.get("complete") or not isinstance(batch.get("records"), list):
        return False
    if not quotes_valid(batch, payload):
        return False
    records = batch["records"]
    selected = [r for r in records if r["kind"] == truth.get("kind", "FACT")]
    if "count" in truth and len(selected) != truth["count"]:
        return False
    if "records" in truth:
        remaining = list(selected)
        for spec in truth["records"]:
            choices = [i for i, record in enumerate(remaining) if matches(record, spec)]
            if not choices:
                return False
            remaining.pop(choices[0])
        if remaining:
            return False
    if "fields" in truth and (len(selected) != 1 or not matches(selected[0], truth["fields"])):
        return False
    if truth.get("nonempty_fact") and not selected:
        return False
    if "merge_targets" in truth:
        required = set(truth["merge_targets"])
        by_key = {r["key"]: r for r in selected}
        pairs = [{(by_key[m["source"]].get("target") or {}).get("id"),
                  (by_key[m["target"]].get("target") or {}).get("id")} for m in batch["merges"]]
        if len(pairs) != 1 or pairs[0] != required:
            return False
    if truth.get("linked"):
        facts = {r["key"] for r in records if r["kind"] == "FACT"}
        if not facts or {link["fact"] for link in batch["links"]} != facts:
            return False
        for link in batch["links"]:
            episode = next(r for r in records if r["key"] == link["episode"])
            fact = next(r for r in records if r["key"] == link["fact"])
            topic = fact["five_w"]["what"].get("object") or ""
            # 誤った経験への関連付けを、単にリンクがあるだけで合格にしない。
            tokens = truth.get("link_topics") or [
                token for spec in truth.get("records", [])
                for token in spec.get("five_w.what.object", {}).get("contains_any", [])
            ]
            if tokens and not any(
                token in (episode["five_w"]["what"].get("object") or "")
                and token in topic for token in tokens
            ):
                return False
    for r in selected:
        if truth.get("evidence_tokens") and not any(
            token in r["anchor"]["quote"] for token in truth["evidence_tokens"]
        ):
            return False
    if truth.get("no_merges") and batch["merges"]:
        return False
    return True


def get_assert(output, context):
    try:
        passed = evaluate(json.loads(output), context["vars"])
    except (ValueError, KeyError, TypeError, StopIteration):
        passed = False
    return {"pass": passed, "score": int(passed),
            "reason": "fixed_truth_match" if passed else "fixed_truth_mismatch_or_error"}
