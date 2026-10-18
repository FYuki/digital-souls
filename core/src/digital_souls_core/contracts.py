"""Bounded public contracts; intentionally independent of provider SDKs."""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    create_model,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FunctionCall(StrictModel):
    name: Name
    arguments: str


class ToolCall(StrictModel):
    id: Annotated[str, Field(min_length=1)]
    type: Literal["function"]
    function: FunctionCall


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: Annotated[list[ToolCall], Field(min_length=1)] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role(self) -> Self:
        if self.role == "assistant":
            if self.content is None and self.tool_calls is None:
                raise ValueError("assistant requires content or tool_calls")
        elif self.content is None or self.tool_calls is not None:
            raise ValueError("this role requires text content and cannot contain tool_calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool_call_id is required only for tool messages")
        return self


class TextPart(StrictModel):
    type: Literal["text"]
    text: str


_TEXT_PARTS: TypeAdapter[list[TextPart]] = TypeAdapter(
    Annotated[list[TextPart], Field(min_length=1)]
)


def normalize_input_message(value: Any) -> Any:
    """Normalize only completion inputs; stored/provider Message stays text-only."""
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        parts = _TEXT_PARTS.validate_python(value["content"])
        return {**value, "content": "\n".join(part.text for part in parts)}
    return value


# Describe the accepted wire shape without widening the runtime Message contract.
_INPUT_MESSAGE_SCHEMA = create_model(
    "CompletionMessageInput",
    __base__=Message,
    content=(str | Annotated[list[TextPart], Field(min_length=1)] | None, None),
)

type InputMessage = Annotated[
    Message,
    BeforeValidator(normalize_input_message, json_schema_input_type=_INPUT_MESSAGE_SCHEMA),
]


class FunctionDefinition(StrictModel):
    name: Name
    description: str | None = None
    parameters: dict[str, Any]


class Tool(StrictModel):
    type: Literal["function"]
    function: FunctionDefinition


class FunctionName(StrictModel):
    name: Name


class NamedToolChoice(StrictModel):
    type: Literal["function"]
    function: FunctionName


class CompletionInput(StrictModel):
    messages: Annotated[list[InputMessage], Field(min_length=1)]
    stream: bool = False
    tools: Annotated[list[Tool], Field(min_length=1)] | None = None
    tool_choice: Literal["auto", "none", "required"] | NamedToolChoice | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    max_tokens: Annotated[int, Field(gt=0, le=32768)] | None = None
    max_completion_tokens: Annotated[int, Field(gt=0, le=32768)] | None = None

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("specify only one token limit")
        names = [tool.function.name for tool in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool names")
        if self.tool_choice is not None and not names:
            raise ValueError("tool_choice requires tools")
        if isinstance(self.tool_choice, NamedToolChoice):
            if self.tool_choice.function.name not in names:
                raise ValueError("tool_choice must name a supplied tool")
        pending: set[str] = set()
        used: set[str] = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("tool result must match an unresolved call")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("all tool results must precede another message")
                for call in message.tool_calls or []:
                    if call.id in used:
                        raise ValueError("duplicate tool call id")
                    pending.add(call.id)
                    used.add(call.id)
        if pending:
            raise ValueError("missing tool result")
        return self


class CharacterCompletion(CompletionInput):
    character_id: Name


class AliasCompletion(CompletionInput):
    model: Name
