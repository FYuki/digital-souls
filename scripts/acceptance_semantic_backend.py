"""専用受入Backendで実推論の開始・終了だけを記録する。入出力本文は記録しない。"""

from functools import wraps
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

import uvicorn

from app.inference.router import InferenceRouter


def install_activity_trace():
    if os.environ.get("DS_ENVIRONMENT_ID") != "test":
        raise RuntimeError("activity tracing requires the isolated test environment")
    root = Path(os.environ["DS_DATA_DIR"]).resolve().parent
    path = Path(os.environ["DS_ACCEPTANCE_ACTIVITY_LOG"]).resolve()
    if not root.name.startswith("ds-memory-341-") or path.parent != root:
        raise RuntimeError("activity log must remain in the owned acceptance root")
    run_id = os.environ["DS_ACCEPTANCE_RUN_ID"]
    lock = threading.Lock()

    def record(event):
        try:
            with lock, path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"run_id": run_id, **event}) + "\n")
        except OSError:
            print("ACCEPTANCE_ACTIVITY_LOG_FAILED", file=sys.stderr, flush=True)

    def instrument(name):
        original = getattr(InferenceRouter, name)

        @wraps(original)
        def measured(self, *args, **kwargs):
            target = kwargs.get("target")
            identifier = str(uuid4())
            started = time.monotonic()
            metadata = {"invocation_id": identifier, "method": name, "target": getattr(target, "value", None)}
            record({**metadata, "phase": "start", "monotonic_seconds": started})
            success = False
            try:
                result = original(self, *args, **kwargs)
                success = True
                return result
            finally:
                ended = time.monotonic()
                record({**metadata, "phase": "end", "monotonic_seconds": ended,
                        "elapsed_seconds": ended - started, "success": success})

        setattr(InferenceRouter, name, measured)

    # 戻り値を置き換えず、実運用と同じRouter・Adapter・モデルを呼ぶ。
    for method in ("generate_text", "generate_structured", "embed"):
        instrument(method)


if __name__ == "__main__":
    install_activity_trace()
    uvicorn.main()
