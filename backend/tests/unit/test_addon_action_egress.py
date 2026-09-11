"""外部送信は意味privacyの確定した非機微判定だけで許可する。"""

import asyncio
import json

import pytest

from app.addon_action.egress import ActionEgress
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier


@pytest.mark.parametrize("outcome", ["safe", "sensitive", "timeout", "invalid"])
def test_egress_requires_conclusive_non_sensitive_result(outcome: str) -> None:
    class Client:
        calls = 0

        def chat(self, messages, *, timeout_seconds):
            self.calls += 1
            # 15秒を超える起動待機にも判定予算を与える。再試行はしない。
            assert 15 < timeout_seconds <= 30
            if outcome == "timeout":
                raise TimeoutError
            if outcome == "invalid":
                return "invalid classifier output"
            safe = outcome == "safe"
            return json.dumps(
                {
                    "classification": "NOT_SENSITIVE" if safe else "SENSITIVE",
                    "subject_scope": "GENERAL" if safe else "SELF",
                    "category": "NONE" if safe else "HEALTH",
                    "reason_code": "NO_SENSITIVE_CONTENT"
                    if safe
                    else "SENSITIVE_CONTENT",
                }
            )

    client = Client()
    classifier = InferenceSemanticPrivacyClassifier(
        client=client,
        privacy_policy=resolved_memory_policy().privacy,
        model_id="test-model",
        model_digest="sha256:" + "a" * 64,
    )
    assert asyncio.run(ActionEgress(classifier).allowed({"content": "合成テスト"})) is (
        outcome == "safe"
    )
    assert client.calls == 1


def test_egress_without_classifier_blocks_nonempty_arguments() -> None:
    assert not asyncio.run(ActionEgress(None).allowed({"content": "合成テスト"}))
