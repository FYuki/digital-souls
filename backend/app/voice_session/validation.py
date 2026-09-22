from pydantic import ValidationError

from app.contract_validation import contract_errors, contract_validator
from app.voice_session.generated import VoiceSessionEvent


def parse_voice_session_event(value: object) -> VoiceSessionEvent:
    errors = contract_errors(
        contract_validator("voice-session/voice-session.schema.json"),
        value,
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
