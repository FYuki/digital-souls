"""Explicit privacy permissions, separate from inference capability and identity."""

from collections.abc import Mapping
from typing import Literal, Protocol

from .character import Profile
from .contracts import Message
from .history import Binding, Operation
from .privacy_scan import POLICY_VERSION, scan

type Permission = Literal["history", "local", "external", "memory"]


class Classifier(Protocol):
    """Classify through a managed local adapter; content never belongs in the result."""

    async def safe(self, value: object, policy_version: str) -> bool: ...


class PrivacyPolicy:
    """Operator-owned grants and deterministic history checks; default is denial.

    Replacing grants is an explicit trusted operation, never a conversation instruction.
    No assessment cache survives calls, consent changes, or model/policy changes.
    """

    def __init__(self, classifier: Classifier | None = None) -> None:
        self._generation = 0
        self._classifier_generation = 0
        self.classifier = classifier
        self._grants: dict[Binding, frozenset[Permission]] = {}

    @property
    def classifier(self) -> Classifier | None:
        return self._classifier

    @classifier.setter
    def classifier(self, value: Classifier | None) -> None:
        self._classifier = value
        self._classifier_generation += 1
        self._generation += 1

    @property
    def stamp(self) -> tuple[int, int]:
        """Content-free consent/classifier generation for adjacent no-await guards."""
        return self._generation, self._classifier_generation

    def configure(self, grants: Mapping[Binding, frozenset[Permission]]) -> None:
        """Atomically replace trusted consent; outstanding decisions become stale."""
        if any(
            not isinstance(k, Binding)
            or not isinstance(v, frozenset)
            or not v <= {"history", "local", "external", "memory"}
            for k, v in grants.items()
        ):
            raise ValueError("invalid privacy grants")
        self._grants = dict(grants)
        self._generation += 1

    def permits(self, binding: Binding, permission: Permission) -> bool:
        """Check one permission in the exact caller/character scope."""
        return permission in self._grants.get(binding, frozenset())

    def allows(self, operation: Operation, binding: Binding, messages: tuple[Message, ...]) -> bool:
        """Synchronous final history guard, safe to use adjacent to a DB commit."""
        result = scan([m.model_dump(exclude_none=True) for m in messages])
        if result.failed or result.secret:
            return False
        if operation == "export":
            return self.permits(binding, "local") or self.permits(binding, "external")
        if not self.permits(binding, "history"):
            return False
        return True

    async def authorize(self, binding: Binding, permission: Permission, value: object) -> bool:
        """Check actual content before local inference, external export or future memory."""
        generation = self._generation
        if not self.permits(binding, permission):
            return False
        result = scan(value)
        if result.failed or result.secret:
            return False
        if permission in {"external", "memory"}:
            if not self.permits(binding, "local"):
                return False
            classifier = self.classifier
            if classifier is None:
                return False
            try:
                if await classifier.safe(value, POLICY_VERSION) is not True:
                    return False
            except Exception:
                return False
            if classifier is not self.classifier:
                return False
        return generation == self._generation and self.permits(binding, permission)


def destination(profile: Profile) -> Literal["local", "external"]:
    """Only the validated, pinned llama.cpp loopback transport is local here."""
    return "local" if profile.transport == "llamacpp_chat" else "external"
