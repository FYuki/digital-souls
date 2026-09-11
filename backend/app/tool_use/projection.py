"""native正本を保持したまま、会話へ渡す非信頼データを制限する。"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from app.external_mcp.models import Json, encode
from app.privacy.contracts import PrivacyScanner, ScanSuccess

_SECRET_KEY = re.compile(
    r"password|passwd|secret|credential|authorization|api.?key|access.?token|"
    r"refresh.?token|cookie|private.?key|requeststate|endpoint",
    re.I,
)
_URL = re.compile(r"(?:https?|wss?)://[^\s<>\"']+", re.I)


@dataclass(frozen=True)
class Sanitizer:
    scanner: PrivacyScanner
    private_values: tuple[str, ...] = field(default=(), repr=False)
    secret_references: tuple[str, ...] = field(default=(), repr=False)

    dynamic_private_values: Callable[[], tuple[str, ...]] | None = field(
        default=None, repr=False
    )

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        return (
            self.private_values
            + tuple(os.environ.get(ref, "") for ref in self.secret_references)
            + (self.dynamic_private_values() if self.dynamic_private_values else ())
        )

    def text(self, value: str, *, maximum: int = 16_384) -> str:
        for private in sorted(self.sensitive_values, key=len, reverse=True):
            if private:
                value = value.replace(private, "[非公開]")
        # secretを途中で切って断片を残さないよう、伏せた後で表示量を制限する。
        value = value[:maximum]
        # URLは表示用出典へ流用しない。Coreが管理する出典ラベルを使う。
        value = _URL.sub("[外部参照]", value)
        result = self.scanner.scan(value)
        if not isinstance(result, ScanSuccess):
            return "[内容を安全に表示できません]"
        spans: list[tuple[int, int]] = []
        for finding in sorted(result.findings, key=lambda f: f.start):
            if not 0 <= finding.start < finding.end <= len(value):
                return "[内容を安全に表示できません]"
            if spans and finding.start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], finding.end))
            else:
                spans.append((finding.start, finding.end))
        for start, end in reversed(spans):
            value = value[:start] + "[非公開]" + value[end:]
        return "".join(c for c in value if c in "\n\t" or ord(c) >= 32)

    def value(
        self, value: Any, *, depth: int = 0, omitted: list[bool] | None = None
    ) -> Any:
        omitted = omitted if omitted is not None else [False]
        if depth > 12:
            omitted[0] = True
            return "[省略]"
        if isinstance(value, str):
            omitted[0] |= len(value) > 16_384
            return self.text(value)
        if isinstance(value, list):
            omitted[0] |= len(value) > 64
            return [self.value(v, depth=depth + 1, omitted=omitted) for v in value[:64]]
        if isinstance(value, dict):
            omitted[0] |= len(value) > 128 or any(len(str(k)) > 200 for k in value)
            return {
                self.text(str(k), maximum=200): (
                    "[非公開]"
                    if _SECRET_KEY.search(str(k))
                    else self.value(v, depth=depth + 1, omitted=omitted)
                )
                for k, v in list(value.items())[:128]
            }
        return value if value is None or isinstance(value, (bool, int, float)) else None

    def arguments_allowed(self, value: Json) -> bool:
        """公開URLは引数に使えるが、秘密値・credential欄・MCP接続先は送らない。"""
        serialized = encode(value)
        if any(private and private in serialized for private in self.sensitive_values):
            return False

        def secret_key(item: Any) -> bool:
            if isinstance(item, dict):
                return any(
                    _SECRET_KEY.search(str(k)) or secret_key(v) for k, v in item.items()
                )
            return isinstance(item, list) and any(secret_key(v) for v in item)

        scan = self.scanner.scan(serialized)
        return (
            isinstance(scan, ScanSuccess)
            and not scan.findings
            and not secret_key(value)
        )

    def result(self, envelope: Json, *, may_change_state: bool = True) -> Json:
        outcome = envelope["outcome"]
        projected: Json = {"outcome": outcome}
        if "result_projection" in envelope:
            truncation = [False]
            projection = self.value(envelope.get("result_projection") or {}, omitted=truncation)
            return {
                **projection,
                "outcome": outcome,
                **({"omitted": True} if truncation[0] else {}),
                **({"replayed": True} if envelope.get("replayed") else {}),
            }
        if outcome != "succeeded":
            # tool/auth/transportの生エラー本文を送らない。
            projected["error"] = envelope.get("error_category", "input_required")
            if (
                may_change_state
                and envelope.get("dispatch_started") is not False
                and envelope.get("error_category")
                in {
                    "transport",
                    "protocol",
                }
            ):
                projected["outcome"] = "result_unknown"
            return projected
        native = envelope.get("native_payload") or {}
        if not isinstance(native, dict):
            return {**projected, "text": [], "omitted": True}
        texts: list[str] = []
        parts = native["content"] if "content" in native else native.get("contents", [])
        omitted = not isinstance(parts, list) or len(parts) > 64
        for part in parts[:64] if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                omitted = True
                continue
            if part.get("type", "text") == "text" and isinstance(part.get("text"), str):
                texts.append(self.text(part["text"]))
                omitted |= len(part["text"]) > 16_384
            elif (
                part.get("type") == "resource"
                and isinstance(part.get("resource"), dict)
                and isinstance(part.get("resource", {}).get("text"), str)
            ):
                texts.append(self.text(part["resource"]["text"]))
                omitted |= len(part["resource"]["text"]) > 16_384
            else:
                omitted = True
        truncation = [omitted]
        if "structuredContent" in native:
            projected["structured"] = self.value(
                native["structuredContent"], omitted=truncation
            )
        projected.update(text=texts, omitted=truncation[0])
        return projected


def bounded_json(value: Json, maximum_bytes: int) -> str:
    """UTF-8 byte数をtokenの保守的上界として使う。省略を明示する。"""
    full = encode(value)
    if len(full.encode()) <= maximum_bytes:
        return full
    # 元のJSONを壊して返さず、previewとして明示する。
    wrapper: Json = {"omitted": True, "preview": ""}
    if maximum_bytes >= 128 and "outcome" in value:
        wrapper["outcome"] = value["outcome"]
    room = max(0, maximum_bytes - len(encode(wrapper).encode()) - 32)
    wrapper["preview"] = full.encode()[:room].decode("utf-8", errors="ignore")
    while len(encode(wrapper).encode()) > maximum_bytes and wrapper["preview"]:
        wrapper["preview"] = wrapper["preview"][:-1]
    return encode(wrapper)
