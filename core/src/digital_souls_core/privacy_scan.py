"""Bounded deterministic screening; never returns matched values or source spans."""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

POLICY_VERSION = "core-privacy-v1"
SCANNER_VERSION = "core-scanner-v3"
MAX_SCAN_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Scan:
    """Content-free findings; no claim of exhaustive secret or PII detection."""

    secret: bool = False
    no_history: bool = False
    no_memory: bool = False
    failed: bool = False
    version: str = SCANNER_VERSION


_SECRET = re.compile(
    r"(?:\b(?:sk-(?:proj-|ant-)?[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}|"
    r"github_pat_[a-z0-9_]{20,}|akia[a-z0-9]{16}|xox[baprs]-[a-z0-9-]{12,})\b)"
    r"|-----begin (?:[a-z0-9]+ )?private key-----"
    r"|\bbearer\s+[a-z0-9._~+/-]{8,}"
    r"|(?:password|passwd|api[ _-]?key|access[ _-]?token|session[ _-]?cookie|"
    r"recovery[ _-]?code|seed[ _-]?phrase|private[ _-]?key|パスワード|暗証番号|秘密鍵)"
    r"\s*[\"']?\s*[:=：]\s*\S+"
    r"|[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)+"
    r"|(?:住所|自宅|現在地|address|ssn|social security|マイナンバー|bank account|口座番号)"
    r"\s*[:=：]\s*\S+"
)
_HISTORY = re.compile(
    r"保存しないで|記録しないで|履歴に(?:も)?残さないで|保存禁止"
    r"|\b(?:do not|don't) (?:save|store|record)\b"
)
_MEMORY = re.compile(
    r"覚えないで|記憶しないで|記憶に(?:は)?残さないで"
    r"|\b(?:do not|don't) remember\b"
)


def normalized(text: str) -> str:
    """Use PoC's NFKC/casefold/format-removal principle without retaining source maps."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Cf").split())


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char) * (2 if index % 2 else 1)
        total += digit - 9 if digit > 9 else digit
    return total % 10 == 0


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError("nonfinite JSON constant")


def scan(value: object) -> Scan:
    """Inspect nested JSON and decoded strings fail-closed, with bounded depth/work."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode("utf-8")) > MAX_SCAN_BYTES:
            return Scan(failed=True)
        pending: list[tuple[object, int]] = [(value, 0)]
        texts: list[str] = []
        visited = 0
        keyed_secret = False
        while pending:
            item, depth = pending.pop()
            visited += 1
            if depth > 12 or visited > 8192:
                return Scan(failed=True)
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        return Scan(failed=True)
                    if (
                        _SECRET.search(normalized(key) + ":x")
                        or normalized(key)
                        in {
                            "password",
                            "passwd",
                            "api_key",
                            "apikey",
                            "api-key",
                            "access_token",
                            "private_key",
                            "secret",
                            "session_cookie",
                            "recovery_code",
                            "seed_phrase",
                            "パスワード",
                            "秘密鍵",
                            "暗証番号",
                            "住所",
                            "自宅",
                            "現在地",
                            "address",
                            "ssn",
                            "social security",
                            "マイナンバー",
                            "bank account",
                            "口座番号",
                        }
                    ) and child not in (None, "", [], {}):
                        keyed_secret = True
                    texts.append(key)
                    pending.append((child, depth + 1))
            elif isinstance(item, (list, tuple)):
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is int:
                texts.append(str(item))
            elif type(item) is float or isinstance(item, Decimal):
                # High-magnitude binary floats may already have lost identifier
                # digits before this boundary. Do not guess the original token.
                if isinstance(item, float):
                    if not math.isfinite(item) or abs(item) >= 1e13:
                        return Scan(failed=True)
                    number = Decimal(str(item))
                else:
                    number = item
                if not number.is_finite() or not -1024 <= number.adjusted() <= 308:
                    return Scan(failed=True)
                # Decimal parsing preserves JSON digits and expands exponents
                # without binary rounding or context-sensitive normalization.
                integer, _, fractional = format(number, "f").partition(".")
                texts.append(integer)
                # Fractional place-value padding is not an identifier. Inspect
                # significant fractional digits separately, without joining.
                if significant := fractional.lstrip("0"):
                    texts.append(significant)
            elif isinstance(item, str):
                texts.append(item)
                try:
                    decoded = json.loads(
                        item,
                        object_pairs_hook=_unique_object,
                        parse_constant=_invalid_constant,
                        parse_float=Decimal,
                    )
                except json.JSONDecodeError:
                    if item.lstrip().startswith(("{", "[", '"')):
                        return Scan(failed=True)
                    continue
                except (ValueError, RecursionError):
                    return Scan(failed=True)
                if isinstance(decoded, (str, dict, list, int, Decimal)) and decoded != item:
                    # The decoded leaves are checked below; the original JSON
                    # would reintroduce numeric padding and duplicate fields.
                    texts.pop()
                    pending.append((decoded, depth + 1))
        secret = keyed_secret
        no_history = no_memory = False
        # Normalize each key/value independently. JSON/array boundaries are not
        # whitespace: removing them can merge unrelated numbers and hide findings.
        for source in texts:
            text = normalized(source)
            compact = re.sub(r"[\s()\-]", "", text)
            secret |= bool(
                _SECRET.search(text)
                or _SECRET.search(compact)
                or _SECRET.search(re.sub(r"\s", "", text))
            )
            secret |= bool(
                re.search(r"(?<!\d)(?:\+81[1-9]\d{8,9}|0[789]0\d{8}|\+1[2-9]\d{9})(?!\d)", compact)
            )
            secret |= any(_luhn(m.group()) for m in re.finditer(r"(?<!\d)\d{13,19}(?!\d)", compact))
            no_history |= bool(_HISTORY.search(text))
            no_memory |= bool(_MEMORY.search(text))
        return Scan(secret=secret, no_history=no_history, no_memory=no_history or no_memory)
    except Exception:
        return Scan(failed=True)
