from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol


POLICY_VERSION = "2026-08-wave2-v1"
SANITIZER_VERSION = "history-sanitizer-v1"


@dataclass(frozen=True)
class ConformanceCase:
    case_id: str
    text: str
    category: str
    matched_text: str
    placeholder: str | None
    storage_scope: str | None
    recognizer_version: str
    policy_version: str

    @property
    def expected_span(self) -> tuple[int, int]:
        start = self.text.index(self.matched_text)
        return start, start + len(self.matched_text)


def load_conformance_cases() -> tuple[ConformanceCase, ...]:
    path = Path(__file__).with_name("fixtures") / "privacy_conformance.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return tuple(ConformanceCase(**item) for item in loaded)


def policy_config() -> dict[str, object]:
    return {
        "policy_version": POLICY_VERSION,
        "retrieval_compatible_policy_versions": [POLICY_VERSION],
        "common": {
            "do_not_store_terms": ["保存しないで"],
            "explicit_memory_terms": ["覚えて"],
            "long_term_memory_markers": ["重要"],
        },
        "services": {"rag_service": {"max_retrieved_memories": 5}},
        "privacy": {
            "required_recognizers": [
                "credentials",
                "keys",
                "financial",
                "contact",
                "government",
                "location",
                "configured",
            ],
            "absolute_deny_categories": [
                "API_KEY",
                "ACCESS_TOKEN",
                "SESSION_COOKIE",
                "RECOVERY_CODE",
                "PASSWORD",
                "PIN",
                "PRIVATE_KEY",
                "CRYPTO_PRIVATE_KEY",
                "SEED_PHRASE",
                "PAYMENT_CARD",
                "CVV",
                "BANK_ACCOUNT",
                "BANK_CREDENTIAL",
                "EMAIL",
                "PHONE",
                "PRIVATE_CONTACT",
                "GOVERNMENT_ID",
                "PRECISE_ADDRESS",
                "PRECISE_LOCATION",
                "POLICY_ADDED_SENSITIVE",
            ],
            "placeholders": {
                "API_KEY": "[API_KEY]",
                "ACCESS_TOKEN": "[ACCESS_TOKEN]",
                "SESSION_COOKIE": "[ACCESS_TOKEN]",
                "RECOVERY_CODE": "[SENSITIVE]",
                "PASSWORD": "[PASSWORD]",
                "PIN": "[PASSWORD]",
                "PRIVATE_KEY": "[PRIVATE_KEY]",
                "CRYPTO_PRIVATE_KEY": "[PRIVATE_KEY]",
                "SEED_PHRASE": "[PRIVATE_KEY]",
                "PAYMENT_CARD": "[PAYMENT_CARD]",
                "CVV": "[PAYMENT_CARD]",
                "BANK_ACCOUNT": "[BANK_ACCOUNT]",
                "BANK_CREDENTIAL": "[BANK_ACCOUNT]",
                "EMAIL": "[EMAIL]",
                "PHONE": "[PHONE]",
                "PRIVATE_CONTACT": "[PHONE]",
                "GOVERNMENT_ID": "[GOVERNMENT_ID]",
                "PRECISE_ADDRESS": "[ADDRESS]",
                "PRECISE_LOCATION": "[LOCATION]",
                "POLICY_ADDED_SENSITIVE": "[SENSITIVE]",
            },
            "storage_opt_out_rules": [
                {"scope": "RAG", "phrases": ["覚えないで"], "patterns": []},
                {
                    "scope": "BOTH",
                    "phrases": [
                        "履歴に残さないで",
                        "履歴にも残さないで",
                        "保存しないで",
                        "記録しないで",
                    ],
                    "patterns": [],
                },
            ],
            "regional_patterns": [
                {
                    "name": "synthetic_us_driver_license",
                    "category": "GOVERNMENT_ID",
                    "recognizer": "government",
                    "pattern": r"CA DL: (?P<value>Z0000000)",
                    "view": "casefold",
                },
                {
                    "name": "jp_phone",
                    "category": "PHONE",
                    "recognizer": "contact",
                    "pattern": (
                        r"(?<!\d)(?P<value>(?:\+81(?:0)?[1-9]\d{8,9}|"
                        r"0[1-9]\d{8,9}))(?!\d)"
                    ),
                    "view": "compact_phone",
                },
                {
                    "name": "us_phone",
                    "category": "PHONE",
                    "recognizer": "contact",
                    "pattern": r"(?<!\d)(?P<value>(?:\+?1)?[2-9]\d{9})(?!\d)",
                    "view": "compact_phone",
                },
                {
                    "name": "jp_bank_account",
                    "category": "BANK_ACCOUNT",
                    "recognizer": "financial",
                    "pattern": r"銀行口座\s*[:=]\s*(?P<value>\d{7,12})",
                    "view": "casefold",
                },
                {
                    "name": "us_routing_number",
                    "category": "BANK_ACCOUNT",
                    "recognizer": "financial",
                    "pattern": r"routing number\s*[:=]\s*(?P<value>\d{9})",
                    "view": "casefold",
                },
                {
                    "name": "us_account_number",
                    "category": "BANK_ACCOUNT",
                    "recognizer": "financial",
                    "pattern": r"account number\s*[:=]\s*(?P<value>\d{6,17})",
                    "view": "casefold",
                },
                {
                    "name": "jp_my_number",
                    "category": "GOVERNMENT_ID",
                    "recognizer": "government",
                    "pattern": r"マイナンバー\s*[:=]\s*(?P<value>\d{12})",
                    "view": "casefold",
                },
                {
                    "name": "jp_driver_license",
                    "category": "GOVERNMENT_ID",
                    "recognizer": "government",
                    "pattern": r"運転免許証番号\s*[:=]\s*(?P<value>\d{12})",
                    "view": "casefold",
                },
                {
                    "name": "us_ssn",
                    "category": "GOVERNMENT_ID",
                    "recognizer": "government",
                    "pattern": r"\bssn\s*[:=]\s*(?P<value>\d{3}-\d{2}-\d{4})",
                    "view": "casefold",
                },
                {
                    "name": "jp_postal_address",
                    "category": "PRECISE_ADDRESS",
                    "recognizer": "location",
                    "pattern": (
                        r"(?:住所は\s*)?(?P<value>〒?\d{3}-\d{4}\s+"
                        r"(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県)\S+)"
                    ),
                    "view": "casefold",
                },
                {
                    "name": "us_street_address",
                    "category": "PRECISE_ADDRESS",
                    "recognizer": "location",
                    "pattern": (
                        r"address\s*:\s*(?P<value>\d+\s+[a-z]+\s+"
                        r"(?:street|st|avenue|ave),\s*[a-z]+,\s*[a-z]{2}\s+"
                        r"\d{5}(?:-\d{4})?)"
                    ),
                    "view": "casefold",
                },
            ],
            "additional_sensitive_patterns": [
                {
                    "name": "synthetic_project_secret",
                    "pattern": r"PROJECT-SECRET-[0-9]{4}",
                    "view": "normalized",
                }
            ],
        },
    }


def write_policy_config(path: Path, config: dict[str, object]) -> None:
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def config_with(
    config: dict[str, object],
    *,
    root: dict[str, object] | None = None,
    privacy: dict[str, object] | None = None,
) -> dict[str, object]:
    copied = json.loads(json.dumps(config))
    if root is not None:
        copied.update(root)
    if privacy is not None:
        privacy_section = copied["privacy"]
        assert isinstance(privacy_section, dict)
        privacy_section.update(privacy)
    return copied


class ScannerLike(Protocol):
    def scan(self, text: str) -> object:
        ...


@dataclass
class StubScanner:
    result: object
    calls: list[str]

    def scan(self, text: str) -> object:
        self.calls.append(text)
        return self.result

    def with_result(self, result: object) -> StubScanner:
        return replace(self, result=result, calls=[])
