"""会話応答の推論Runner port。

HTTP・音声の共通Core呼出が使う、SDK非依存の会話応答portを定義する。
Target／Provider Adapterの解決、Capability検査、入力上限、cancel、error、
出力の意味は既存`InferenceRouter`が所有し、このportは`CHAT` callerへの
委譲だけを担う。#422のRunner差替えではこの実装だけを交換対象とする。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.inference.authorization import InferenceCaller
from app.inference.contracts import (
    InferenceMessage,
    InferenceTarget,
    TextGenerationResult,
    TokenEstimate,
)
from app.inference.router import InferenceRouter


@runtime_checkable
class ConversationInferenceRunner(Protocol):
    """HTTP・音声の会話応答が共有する推論実行port。"""

    def generate_text(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
    ) -> TextGenerationResult: ...

    def stream_text(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        latency_sensitive: bool = False,
    ) -> AsyncIterator[str]: ...

    def estimate_input_tokens(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
    ) -> TokenEstimate: ...


class _RouterConversationInferenceRunner(ConversationInferenceRunner):
    """登録済み`InferenceRouter`へ会話応答を委譲するRunner。"""

    def __init__(self, router: InferenceRouter) -> None:
        self._router = router

    def generate_text(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
    ) -> TextGenerationResult:
        return self._router.generate_text(
            caller=InferenceCaller.CHAT,
            target=target,
            messages=messages,
        )

    async def stream_text(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        latency_sensitive: bool = False,
    ) -> AsyncIterator[str]:
        async for delta in self._router.stream_text(
            caller=InferenceCaller.CHAT,
            target=target,
            messages=messages,
            latency_sensitive=latency_sensitive,
        ):
            yield delta

    def estimate_input_tokens(
        self,
        *,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
    ) -> TokenEstimate:
        return self._router.estimate_input_tokens(
            caller=InferenceCaller.CHAT,
            target=target,
            messages=messages,
        )


def create_conversation_inference_runner(
    router: InferenceRouter,
) -> ConversationInferenceRunner:
    """会話応答portの実装選択を集約する単一factory。

    #422のRunner差替えではこの関数だけを交換対象とし、
    利用側は`ConversationInferenceRunner`契約のみへ依存する。
    """
    return _RouterConversationInferenceRunner(router)
