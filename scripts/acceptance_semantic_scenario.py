"""通常会話で生活属性の変化・矛盾・再起動用未処理を作る。"""

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

p = argparse.ArgumentParser()
p.add_argument("root", type=Path)
p.add_argument("label")
p.add_argument("message")
p.add_argument("--stop-after-reply", action="store_true")
args = p.parse_args()
if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.label):
    raise ValueError("label must be a short filename-safe identifier")
root = args.root.resolve(strict=True)
if (
    os.environ.get("DS_ENVIRONMENT_ID") == "dogfood"
    or root.parent != Path("/tmp")
    or not root.name.startswith("ds-memory-341-")
):
    raise RuntimeError("owned test root required")
manifest = json.loads((root / "runtime-manifest.json").read_text())
if manifest["status"] != "ready" or manifest["environmentId"] != "test":
    raise RuntimeError("ready test runtime required")
assert (
    manifest["dataRoot"] == str(root / "data")
    and (root / "data").resolve() == root / "data"
)
url = urlsplit(manifest["backend"])
if (
    url.scheme != "http"
    or url.hostname not in {"127.0.0.1", "localhost", "::1"}
    or url.port in {18000, 15173, 14174}
):
    raise RuntimeError("test backend endpoint required")
if (root / ("lifecycle-" + args.label + ".json")).exists():
    raise RuntimeError("use a new label to preserve prior evidence")
client = httpx.Client(base_url=manifest["backend"], timeout=180)
r = client.post("/characters/miori/conversations")
r.raise_for_status()
conversation = r.json()["conversation_id"]
started = time.monotonic()
r = client.post(
    "/chat",
    json={
        "character": "miori",
        "conversation_id": conversation,
        "message": args.message,
    },
)
r.raise_for_status()
reply = r.json()
assert reply["turn"]["kind"] == "content"
event = {
    "run_id": manifest["runId"],
    "label": args.label,
    "conversation_id": conversation,
    "reply": reply,
    "elapsed_seconds": time.monotonic() - started,
}
with (root / "lifecycle-events.jsonl").open("a", encoding="utf8") as f:
    f.write(json.dumps(event, ensure_ascii=False) + "\n")
print(
    json.dumps(
        {
            "label": args.label,
            "conversation_id": conversation,
            "turn_id": reply["turn"]["turn_id"],
        }
    ),
    flush=True,
)
if args.stop_after_reply:
    (root / "stop").write_text("owned restart acceptance")
    raise SystemExit(0)
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    with sqlite3.connect(f"file:{root}/data/persona-memory.db?mode=ro", uri=True) as db:
        done = db.execute(
            "SELECT COUNT(*) FROM semantic_processed_sources WHERE character_id=? AND conversation_id=? AND source_id=?",
            ("miori", conversation, reply["turn"]["turn_id"]),
        ).fetchone()[0]
    if done:
        r = client.get("/characters/miori/semantic-memories")
        r.raise_for_status()
        records = r.json()
        (root / ("lifecycle-" + args.label + ".json")).write_text(
            json.dumps(
                {"event": event, "records": records}, ensure_ascii=False, indent=2
            )
        )
        print(
            json.dumps(
                {
                    "label": args.label,
                    "processed": True,
                    "records": [
                        {
                            "id": r["id"],
                            "version": r["content_version"],
                            "status": r["status"],
                        }
                        for r in records
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        break
    time.sleep(1)
else:
    raise RuntimeError("semantic source did not complete")
