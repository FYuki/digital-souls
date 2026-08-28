from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.prompting import BuiltPrompt, PromptMessage


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> str: ...

    @abstractmethod
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int: ...

    async def stream_generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> AsyncIterator[str]:
        del prompt, max_output_tokens
        raise NotImplementedError("streaming generation is not implemented")
        yield ""  # pragma: no cover
