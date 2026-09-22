"""明示された専用Irodoriコンテナの稼働中GPU workerだけへ故障を注入する。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def validate_target(target: dict, container: dict) -> None:
    owner = target.get("owner", "")
    identity = target.get("container_id", "")
    image = target.get("image", "")
    if not isinstance(owner, str) or not re.fullmatch(r"voice-quality-423-recovery-[a-z0-9-]+", owner):
        raise ValueError("dedicated owner required")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("exact container identity required")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable candidate image required")
    if target.get("base_url") != "http://127.0.0.1:50026":
        raise ValueError("dedicated loopback endpoint required")
    if (
        container.get("Id") != identity or container.get("Image") != image
        or container.get("Name") != "/ds-" + owner
        or not container.get("State", {}).get("Running")
        or container.get("Config", {}).get("Labels", {}).get("digital-souls.owner") != owner
        or container.get("HostConfig", {}).get("NetworkMode") != "host"
    ):
        raise ValueError("candidate identity or ownership mismatch")
    command = container.get("Config", {}).get("Cmd", [])
    if command != ["--host", "127.0.0.1", "--port", "50026", "--workers", "1", "--no-access-log"]:
        raise ValueError("dedicated service command required")


# container内のPID namespaceで、PID 1の単一spawn workerだけを選ぶ。
# request本文・環境変数・model path等は取得も出力もしない。
KILL_WORKER = r"""
from pathlib import Path
import os,signal,json
parent=Path('/proc/1/cmdline').read_bytes().split(b'\0')
assert b'irodori_service.app:app' in parent
workers=[]
for entry in Path('/proc').iterdir():
 if not entry.name.isdigit(): continue
 try:
  args=(entry/'cmdline').read_bytes().split(b'\0')
  status=(entry/'status').read_text().splitlines()
  ppid=int(next(s.split()[1] for s in status if s.startswith('PPid:')))
 except (FileNotFoundError,ProcessLookupError): continue
 if ppid==1 and b'--multiprocessing-fork' in args and any(b'from multiprocessing.spawn import spawn_main' in arg for arg in args):
  workers.append(int(entry.name))
assert len(workers)==1, 'single_owned_gpu_worker_required'
os.kill(workers[0],signal.SIGTERM)
print(json.dumps({'worker_pid':workers[0],'signal':'SIGTERM'}))
"""


def run(target: dict) -> dict:
    # 任意shell・Docker remote指定は受け付けない。
    distro = target.get("docker_distro")
    if distro not in (None, "Ubuntu-dogfood"):
        raise ValueError("unsupported Docker distribution")
    prefix = (["/mnt/c/Windows/System32/wsl.exe", "-d", distro, "--"] if distro else []) + ["docker"]
    def docker(*args):
        result = subprocess.run(prefix + list(args), capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError("owned container operation failed")
        return result.stdout
    def inspect():
        identity = target.get("container_id")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("exact container identity required")
        current = json.loads(docker("inspect", identity))
        if not isinstance(current, list) or len(current) != 1:
            raise ValueError("single container required")
        validate_target(target, current[0])
    inspect()
    started = time.monotonic()
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() - started < 30:
        with opener.open(target["base_url"] + "/version", timeout=2) as response:
            version = json.load(response)
        if type(version.get("active")) is int and version["active"] == 1:
            inspect()
            # activeの観測から実際の失敗までの相関はブラウザ側でも検査する。
            result = json.loads(docker("exec", target["container_id"], "/app/.venv/bin/python", "-c", KILL_WORKER))
            return {"active_observed": 1, "wait_seconds": time.monotonic() - started, **result}
        time.sleep(0.02)
    raise TimeoutError("no_active_synthesis_observed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    args = parser.parse_args()
    target = json.loads(args.target_file.read_text())
    print(json.dumps(run(target)))


if __name__ == "__main__":
    main()
