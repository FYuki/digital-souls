"""contracts/以下のJSON Schemaを使うイベント検証の共通部品。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


_CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts"


@lru_cache(maxsize=None)
def contract_validator(schema_relative_path: str) -> Draft202012Validator:
    """contracts/以下のschemaを読み込み、check_schema済みvalidatorを返す。"""
    schema_path = _CONTRACTS_ROOT / schema_relative_path
    with schema_path.open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def contract_errors(
    validator: Draft202012Validator, value: object
) -> list[ValidationError]:
    """path順に整列した検証error一覧を返す。"""
    return sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(segment) for segment in error.path),
    )
