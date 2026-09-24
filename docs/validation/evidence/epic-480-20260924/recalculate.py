"""Epic #480の6試行を保存したJSONLから再集計する。外部サービスは起動しない。"""
from __future__ import annotations
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROWS = json.loads((ROOT.parent.parent / "epic-480-paired-performance-20260924.json").read_text())
FIELDS = (
    "text_from_finalized_ms", "text_from_vad_end_ms",
    "playback_from_send_ms", "playback_from_speech_end_ms",
    "backend_cpu_seconds", "backend_cpu_percent", "backend_peak_rss_mib",
)

def trial_values(folder: Path) -> dict[str, float]:
    manifest = json.loads((folder / "trial-manifest.json").read_text())
    assert len(manifest["trials"]) == 1
    trial = manifest["trials"][0]
    events = [json.loads(line) for line in (folder / "controlled-trace.jsonl").read_text().splitlines()]
    def event_time(name: str) -> int:
        matches = [event for event in events if event["name"] == name]
        assert len(matches) == 1, (name, len(matches))
        assert matches[0]["clock_domain"] == "server_monotonic"
        assert matches[0]["unit"] == "nanosecond"
        return matches[0]["timestamp"]
    resources = [json.loads(line) for line in (folder / "backend-resources.jsonl").read_text().splitlines()]
    assert len(resources) >= 2
    ticks = resources[-1]["cpu_ticks"] - resources[0]["cpu_ticks"]
    cpu_seconds = ticks / resources[0]["clock_ticks"]
    elapsed_seconds = (resources[-1]["monotonic_ns"] - resources[0]["monotonic_ns"]) / 1e9
    return {
        "text_from_finalized_ms": (event_time("first_text_delta") - event_time("utterance_finalized")) / 1e6,
        "text_from_vad_end_ms": (event_time("first_text_delta") - event_time("vad_speech_end")) / 1e6,
        "playback_from_send_ms": trial["startedAt"] - trial["sendAt"],
        "playback_from_speech_end_ms": trial["startedAt"] - trial["fixture_speech_end_client_ms"],
        "backend_cpu_seconds": cpu_seconds,
        "backend_cpu_percent": cpu_seconds / elapsed_seconds * 100,
        "backend_peak_rss_mib": max(row["rss_pages"] * row["page_bytes"] for row in resources) / 1048576,
    }

groups: dict[str, list[dict[str, float]]] = {"baseline": [], "updated": []}
for row in ROWS:
    folder = ROOT.parent.parent.parent.parent / row["evidence"]
    assert folder.is_dir(), folder
    values = trial_values(folder)
    manifest = json.loads((folder / "trial-manifest.json").read_text())
    assert row["initial_state_hash"] == manifest["initial_state_hash"]
    for key in FIELDS:
        assert abs(values[key] - row[key]) < 0.000001, (folder, key, values[key], row[key])
    groups[folder.parent.name].append(values)
assert all(len(rows) == 3 for rows in groups.values())
for group, rows in groups.items():
    print(group, json.dumps({key:round(statistics.median(row[key] for row in rows), 2) for key in FIELDS},ensure_ascii=False))
