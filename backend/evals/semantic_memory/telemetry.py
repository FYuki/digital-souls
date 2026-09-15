"""実モデル評価中のメモリ量を観測する。共有GPUの他プロセスも含む値と区別する。"""

import csv
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.request import urlopen


class MemorySampler:
    def __init__(self, endpoint: str, output: Path, *, interval_seconds: float = 0.5):
        self.endpoint = endpoint.rstrip("/")
        self.output = output
        self.interval_seconds = interval_seconds
        self.stopped = threading.Event()
        self.thread = None
        self.samples = 0
        self.errors = {}
        self.ollama_peak_bytes = 0
        self.models = {}
        self.gpus = {}
        self.started = time.monotonic()
        executable = shutil.which("nvidia-smi")
        if executable is None and Path("/usr/lib/wsl/lib/nvidia-smi").is_file():
            executable = "/usr/lib/wsl/lib/nvidia-smi"
        self.nvidia_smi = executable

    def __enter__(self):
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="semantic-eval-memory", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        if self.thread is not None and self.thread.is_alive():
            self.errors["sampler_shutdown_timeout"] = 1
        return False

    def _run(self):
        with self.output.open("x", encoding="utf-8") as stream:
            while not self.stopped.is_set():
                sample = {"at": datetime.now(UTC).isoformat(), "elapsed_seconds": time.monotonic() - self.started}
                try:
                    with urlopen(self.endpoint + "/api/ps", timeout=2) as response:
                        models = json.load(response)["models"]
                    sample["ollama_models"] = models
                    total = sum(int(model.get("size_vram", 0)) for model in models)
                    self.ollama_peak_bytes = max(self.ollama_peak_bytes, total)
                    for model in models:
                        key = model["name"]
                        previous = self.models.setdefault(key, {"observed_peak_vram_bytes": 0, "context_lengths": []})
                        previous["observed_peak_vram_bytes"] = max(previous["observed_peak_vram_bytes"], model.get("size_vram", 0))
                        length = model.get("context_length")
                        if length not in previous["context_lengths"]:
                            previous["context_lengths"].append(length)
                except Exception as error:
                    self._error(sample, "ollama", error)
                if self.nvidia_smi:
                    try:
                        result = subprocess.run([
                            self.nvidia_smi, "--query-gpu=uuid,name,memory.used,memory.total",
                            "--format=csv,noheader,nounits",
                        ], check=True, capture_output=True, text=True, timeout=2)
                        sample["host_gpus"] = []
                        for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
                            uuid, name, used, total = row
                            used, total = int(used), int(total)
                            sample["host_gpus"].append({"uuid": uuid, "name": name, "used_mib": used, "total_mib": total})
                            previous = self.gpus.setdefault(uuid, {
                                "name": name, "total_mib": total, "first_observed_used_mib": used,
                                "observed_peak_used_mib": used,
                            })
                            previous["observed_peak_used_mib"] = max(previous["observed_peak_used_mib"], used)
                    except Exception as error:
                        self._error(sample, "nvidia_smi", error)
                self.samples += 1
                stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                stream.flush()
                self.stopped.wait(self.interval_seconds)

    def _error(self, sample, source, error):
        key = source + ":" + type(error).__name__
        self.errors[key] = self.errors.get(key, 0) + 1
        sample.setdefault("errors", []).append(key)

    def summary(self):
        return {
            "samples": self.samples, "interval_seconds": self.interval_seconds,
            "ollama_observed_peak_total_vram_bytes": self.ollama_peak_bytes if self.models else None,
            "models": self.models, "host_gpus": self.gpus, "errors": self.errors,
            "host_gpu_scope": "共有GPU全体。他プロセスを含み、この評価だけの使用量とは限らない。",
            "peak_scope": "定期観測した最大値。サンプル間の瞬間最大値は保証しない。",
            "nvidia_smi_available": self.nvidia_smi is not None,
        }
