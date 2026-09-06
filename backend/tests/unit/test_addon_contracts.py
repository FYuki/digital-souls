"""#152のschema/fixture検証。MCP runtimeには依存しない。"""
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator, FormatChecker

CONTRACTS = Path(__file__).resolve().parents[3] / 'contracts/addon'

@pytest.mark.parametrize('path', sorted(CONTRACTS.glob('*.schema.json')), ids=lambda p:p.stem)
def test_schema_is_valid(path):
    Draft202012Validator.check_schema(json.loads(path.read_text()))

@pytest.mark.parametrize('path', sorted((CONTRACTS/'fixtures').glob('*/*.json')), ids=lambda p:p.stem)
def test_contract_fixture(path):
    kind = 'manifest' if path.name.startswith('manifest') else 'capability-snapshot' if path.name.startswith('snapshot') else 'execution-envelope'
    schema = json.loads((CONTRACTS/f'{kind}.schema.json').read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert validator.is_valid(json.loads(path.read_text())) == (path.parent.name == 'valid')
