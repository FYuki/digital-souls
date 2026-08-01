from dataclasses import dataclass
from typing import Protocol

from app.prompting.models import PromptMessage


class TokenCounter(Protocol):
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        ...


@dataclass(frozen=True)
class TokenMeasurements:
    token_counter: TokenCounter
    counts: tuple[tuple[tuple[PromptMessage, ...], int], ...] = ()

    def measure(self, messages: tuple[PromptMessage, ...]) -> "TokenMeasurement":
        if not messages:
            return TokenMeasurement(0, self)
        cached = next(
            (count for measured, count in self.counts if measured == messages),
            None,
        )
        if cached is not None:
            return TokenMeasurement(cached, self)
        count = self.token_counter.count_input_tokens(messages)
        return TokenMeasurement(
            count,
            TokenMeasurements(self.token_counter, (*self.counts, (messages, count))),
        )


@dataclass(frozen=True)
class TokenMeasurement:
    count: int
    measurements: TokenMeasurements
