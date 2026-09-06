"""native正本を保持したまま、会話へ渡す非信頼データを制限する。"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from typing import Any

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

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        return self.private_values + tuple(
            os.environ.get(ref, "") for ref in self.secret_references
        )

    def text(self, value: str, *, maximum: int = 16_384) -> str:
        value = value[:maximum]
        for private in sorted(self.sensitive_values, key=len, reverse=True):
            if private:
                value = value.replace(private, "[非公開]")
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

    def value(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 12:
            return "[省略]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v, depth=depth + 1) for v in value[:64]]
        if isinstance(value, dict):
            return {
                self.text(str(k), maximum=200): (
                    "[非公開]"
                    if _SECRET_KEY.search(str(k))
                    else self.value(v, depth=depth + 1)
                )
                for k, v in list(value.items())[:128]
            }
        return value if value is None or isinstance(value, (bool, int, float)) else None

    def result(self, envelope: Json, *, may_change_state: bool = True) -> Json:
        outcome = envelope["outcome"]
        projected: Json = {"outcome": outcome}
        if outcome != "succeeded":
            # tool/auth/transportの生エラー本文を送らない。
            projected["error"] = envelope.get("error_category", "input_required")
            if may_change_state and envelope.get("error_category") in {
                "transport",
                "protocol",
            }:
                projected["outcome"] = "result_unknown"
            return projected
        native = envelope.get("native_payload") or {}
        texts: list[str] = []
        omitted = False
        for part in (native.get("content") or native.get("contents") or [])[:64]:
            if part.get("type", "text") == "text" and isinstance(part.get("text"), str):
                texts.append(self.text(part["text"]))
                omitted |= len(part["text"]) > 16_384
            elif part.get("type") == "resource" and isinstance(
                part.get("resource", {}).get("text"), str
            ):
                texts.append(self.text(part["resource"]["text"]))
            else:
                omitted = True
        projected.update(text=texts, omitted=omitted)
        if "structuredContent" in native:
            projected["structured"] = self.value(native["structuredContent"])
        return projected


def bounded_json(value: Json, maximum_bytes: int) -> str:
    """UTF-8 byte数をtokenの保守的上界として使う。省略を明示する。"""
    full = encode(value)
    if len(full.encode()) <= maximum_bytes:
        return full
    # 元のJSONを壊して返さず、previewとして明示する。
    wrapper: Json = {"omitted": True, "preview": ""}
    room = max(0, maximum_bytes - len(encode(wrapper).encode()) - 32)
    wrapper["preview"] = full.encode()[:room].decode("utf-8", errors="ignore")
    while len(encode(wrapper).encode()) > maximum_bytes and wrapper["preview"]:
        wrapper["preview"] = wrapper["preview"][:-1]
    return encode(wrapper)
