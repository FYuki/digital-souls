import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.ut


def test_request_fingerprint_survives_process_hash_seed() -> None:
    script = """
from digital_souls_core.conversations import request_fingerprint
from digital_souls_core.history import TurnInput
from digital_souls_core.contracts import Message
from tests.support import character
body = TurnInput(request_id="retry", expected_revision=0,
                 messages=[Message(role="user", content="Synthetic")])
print(request_fingerprint(body, character("synthetic").config))
"""
    values = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        ).strip()
        for seed in ("1", "2")
    ]
    assert len(values[0]) == 64
    assert values[0] == values[1]
