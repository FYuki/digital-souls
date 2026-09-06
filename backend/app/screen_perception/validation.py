import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from app.screen_perception.generated import (
    ScreenPerceptionEvent,
    SnapshotUploadMetadata,
)


MAX_IMAGE_PIXELS = 4_194_304
TIMESTAMP_FIELDS = {
    "capture_deadline",
    "captured_at",
    "last_recognized_capture_at",
    "lease_expires_at",
    "received_at",
    "requested_at",
}
_SCREEN_PERCEPTION_EVENT_ADAPTER: TypeAdapter[ScreenPerceptionEvent] = TypeAdapter(
    ScreenPerceptionEvent
)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    repository_root = Path(__file__).resolve().parents[3]
    schema_path = (
        repository_root
        / "contracts"
        / "perception"
        / "screen"
        / "screen-perception.schema.json"
    )
    with schema_path.open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def parse_screen_perception_event(value: object) -> ScreenPerceptionEvent:
    errors = sorted(
        _validator().iter_errors(value),
        key=lambda error: tuple(str(segment) for segment in error.path),
    )
    if errors:
        raise ValueError("screen perception event does not match protocol 1.0") from errors[0]
    if isinstance(value, dict):
        for field in TIMESTAMP_FIELDS.intersection(value):
            timestamp = value[field]
            if timestamp is not None and isinstance(timestamp, str):
                try:
                    datetime.fromisoformat(timestamp)
                except ValueError as error:
                    raise ValueError(
                        "screen perception event contains an invalid UTC timestamp"
                    ) from error
    try:
        event = _SCREEN_PERCEPTION_EVENT_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise ValueError(
            "screen perception event cannot be converted to generated type"
        ) from error
    if isinstance(event, SnapshotUploadMetadata):
        if event.width * event.height > MAX_IMAGE_PIXELS:
            raise ValueError("screen snapshot exceeds the decoded pixel limit")
    return event
