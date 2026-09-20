"""参照cacheの診断で欠落出力と偏った順序を見逃さない。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts/voice_quality/probe_irodori_reference_cache.py"
spec = importlib.util.spec_from_file_location("voice_reference_probe_tested", SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("actual,result", [((1, 2), (1,)), ((1,), (1, 2)), ((1, 2), (1, 3))])
def test_missing_extra_or_changed_reference_output_cannot_pass(actual, result):
    with pytest.raises(AssertionError, match="reference_output"):
        probe.verify_reference_output(actual, result, lambda left, right: left == right)


def test_equal_outputs_and_alternating_order_keep_every_text_repetition():
    probe.verify_reference_output((1, 2), (1, 2), lambda left, right: left == right)
    texts = tuple(str(i) for i in range(5))
    pairs = list(probe.comparison_pairs(texts, 3))
    assert len(pairs) == 15
    assert sum(order[0] == "baseline" for _, _, _, order in pairs) == 8
    assert sum(order[0] == "reference_reuse" for _, _, _, order in pairs) == 7
    assert {(repetition, text) for _, repetition, text, _ in pairs} == {(r, t) for r in range(3) for t in texts}
    assert all(pairs[i][3] != pairs[i+1][3] for i in range(len(pairs)-1))
