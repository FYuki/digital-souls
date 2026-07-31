from abc import ABC, abstractmethod

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
