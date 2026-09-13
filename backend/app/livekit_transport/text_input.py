from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import hashlib
import json
import time
from typing import Literal
from uuid import uuid4

from app.livekit_transport.delivery import TerminalProtocolError, decode_core_event


InputStatus = Literal["processing", "accepted", "rejected", "not_received"]


class TextInputRejected(Exception):
    """入力の副作用が成立する前に確定した拒否。本文を含めない。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InputReceipt:
    input_event_id: str
    status: InputStatus
    fingerprint: bytes | None = None
    response_id: str | None = None
    error_code: str | None = None


class TextInputReceiver:
    """配送ACKから独立した、session単位の入力受付と結果照合。"""

    def __init__(
        self,
        *,
        session_id: str,
        participant_id: str,
        submit: Callable[[str, str], Awaitable[str | None]],
        publish: Callable[[dict[str, object]], Awaitable[None]],
        accepting_input: Callable[[], bool],
        clock_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000,
        max_receipts: int = 256,
    ) -> None:
        if max_receipts < 1:
            raise ValueError("receipt capacity must be positive")
        self._session_id = session_id
        self._participant_id = participant_id
        self._submit = submit
        self._publish = publish
        self._accepting_input = accepting_input
        self._clock_ms = clock_ms
        self._max_receipts = max_receipts
        self._receipts: dict[str, InputReceipt] = {}
        self._lock = asyncio.Lock()

    async def receive(self, event: dict[str, object]) -> None:
        # transport以外の呼出元も同じ認可・validation境界を通す。
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        decoded = decode_core_event(payload)
        speaker = decoded.get("speaker")
        if (
            decoded["session_id"] != self._session_id
            or not isinstance(speaker, dict)
            or speaker.get("role") != "user"
            or speaker.get("participant_id") != self._participant_id
        ):
            raise TerminalProtocolError("text input session or participant mismatch")
        if decoded["type"] == "user_input_result_requested":
            await self._lookup(str(decoded["input_event_id"]))
            return
        if decoded["type"] != "user_text_submitted":
            raise TerminalProtocolError("unsupported text input event")
        input_id = str(decoded["event_id"])
        fingerprint = hashlib.sha256(payload).digest()
        async with self._lock:
            receipt = self._receipts.get(input_id)
            is_new = receipt is None
            if receipt is None:
                self._require_capacity()
                receipt = InputReceipt(input_id, "processing", fingerprint)
                if not self._accepting_input():
                    receipt = replace(receipt, status="rejected", error_code="session_unavailable")
                self._receipts[input_id] = receipt
            elif receipt.fingerprint is not None and receipt.fingerprint != fingerprint:
                raise TerminalProtocolError("input event_id has a conflicting payload")
            elif receipt.fingerprint is None:
                # 未受理照合が先着したIDは閉じたまま、初回の遅着payloadを識別する。
                receipt = replace(receipt, fingerprint=fingerprint)
                self._receipts[input_id] = receipt

        if is_new and receipt.status == "processing":
            try:
                response_id = await self._submit(input_id, str(decoded["text"]))
            except TextInputRejected as error:
                receipt = replace(receipt, status="rejected", error_code=error.code)
            except asyncio.CancelledError:
                # 取消時点で副作用の成立を断定できない。未受理へ戻さない。
                raise
            except Exception:
                # 内部例外の本文を利用者へ漏らさず、再実行による二重処理を防ぐ。
                receipt = replace(receipt, error_code="input_result_unknown")
            else:
                receipt = replace(receipt, status="accepted", response_id=response_id)
            async with self._lock:
                self._receipts[input_id] = receipt
        # 送信失敗でも先に確定した結果を残し、次の照合へ返す。
        await self._publish_receipt(receipt)

    async def _lookup(self, input_id: str) -> None:
        async with self._lock:
            receipt = self._receipts.get(input_id)
            if receipt is None:
                self._require_capacity()
                receipt = InputReceipt(input_id, "not_received")
                self._receipts[input_id] = receipt
        await self._publish_receipt(receipt)

    def _require_capacity(self) -> None:
        if len(self._receipts) >= self._max_receipts:
            # session存続中の結果を追い出して古い送信を再実行しない。
            raise TerminalProtocolError("text input receipt capacity exceeded")

    async def _publish_receipt(self, receipt: InputReceipt) -> None:
        event: dict[str, object] = {
            "type": "user_input_result",
            "protocol_version": "1.1",
            "event_id": str(uuid4()),
            "session_id": self._session_id,
            "monotonic_timestamp_ms": self._clock_ms(),
            "input_event_id": receipt.input_event_id,
            "status": receipt.status,
        }
        if receipt.response_id is not None:
            event["response_id"] = receipt.response_id
        if receipt.error_code is not None:
            event["error_code"] = receipt.error_code
        await self._publish(event)
