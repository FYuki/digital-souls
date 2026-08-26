import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.voice_session.generated import VoiceSessionEvent


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    repository_root = Path(__file__).resolve().parents[3]
    schema_path = (
        repository_root / "contracts" / "voice-session" / "voice-session.schema.json"
    )
    with schema_path.open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def parse_voice_session_event(value: object) -> VoiceSessionEvent:
    errors = sorted(
        _validator().iter_errors(value),
        key=lambda error: tuple(str(segment) for segment in error.path),
    )
    if errors:
        raise ValueError("voice session event does not match protocol 1.0") from errors[0]
    try:
        event = VoiceSessionEvent.model_validate(value)
    except ValidationError as error:
        raise ValueError("voice session event cannot be converted to generated type") from error
    if event.text_range is not None and event.text_range.start > event.text_range.end:
        raise ValueError("voice session event has an invalid text range")
    return event
