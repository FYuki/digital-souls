"""共有Irodoriの起動前に、解決済みComposeのimmutable imageを検証する。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def validate_image(image: object) -> None:
    # ローカル検証でdocker loadしたimage IDも内容固定として許可する。
    if not isinstance(image, str) or re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("DOGFOOD_IRODORI_IMAGE requires a repository digest or local sha256 image ID")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    args = parser.parse_args()
    resolved = subprocess.check_output([
        "docker", "compose", "--env-file", str(args.env_file),
        "--file", str(args.compose_file), "config", "--format", "json",
    ], text=True)
    document = json.loads(resolved)
    validate_image(document["services"]["irodori"]["image"])
    print("Irodori deployment image is immutable")


if __name__ == "__main__":
    main()
