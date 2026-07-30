from abc import ABC, abstractmethod

from app.prompting.types import BuiltPrompt


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: BuiltPrompt) -> str: ...
