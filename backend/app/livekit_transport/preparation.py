from __future__ import annotations


class VoiceModelPreparationError(RuntimeError):
    """開始時の失敗段階と、安全な固定codeのみをUIへ渡す。"""

    def __init__(self, *, stage: str, code: str) -> None:
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}: {code}")
