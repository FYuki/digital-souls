from abc import ABC, abstractmethod


class AudioTransport(ABC):
    @abstractmethod
    def receive_audio(self) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def send_audio(self, audio: bytes) -> None:
        raise NotImplementedError
