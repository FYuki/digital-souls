from typing import Protocol


class SpeechSynthesisError(RuntimeError):
    """Speech synthesis transport failed."""


class SpeechSynthesizer(Protocol):
    def synthesize(self, text: str, speaker_id: int) -> bytes:
        ...

    def close(self) -> None:
        ...
