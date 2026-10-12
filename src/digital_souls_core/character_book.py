"""Embedded CCV3 Book validation and deterministic Lore selection (ADR 0028)."""

import inspect
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from .contracts import Message
from .prompt_builder import LoreDiagnostic, SelectedLore, TokenCounter


class CharacterCardValidationError(ValueError):
    """Contains only a known schema path, never an input value."""


class BookEntry(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    keys: tuple[StrictStr, ...] = Field(repr=False)
    content: StrictStr = Field(repr=False)
    extensions: dict[str, Any] = Field(repr=False)
    enabled: StrictBool
    insertion_order: StrictInt
    use_regex: StrictBool
    case_sensitive: StrictBool | None = None
    constant: StrictBool | None = None
    name: StrictStr | None = Field(default=None, repr=False)
    priority: StrictInt | None = None
    id: StrictInt | StrictStr | None = Field(default=None, repr=False)
    comment: StrictStr | None = Field(default=None, repr=False)
    selective: StrictBool | None = None
    secondary_keys: tuple[StrictStr, ...] | None = Field(default=None, repr=False)
    position: Literal["before_char", "after_char"] | None = None

    @property
    def extra_fields(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    def __repr_args__(self) -> Any:
        # Pydantic's default repr includes extras; extras can contain card bodies.
        return (
            (name, getattr(self, name))
            for name, info in type(self).model_fields.items()
            if info.repr
        )


class CharacterBook(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    extensions: dict[str, Any] = Field(repr=False)
    entries: tuple[BookEntry, ...] = Field(repr=False)
    name: StrictStr | None = Field(default=None, repr=False)
    description: StrictStr | None = Field(default=None, repr=False)
    scan_depth: StrictInt | None = Field(default=None, ge=0)
    token_budget: StrictInt | None = Field(default=None, ge=0)
    recursive_scanning: StrictBool | None = None

    @property
    def extra_fields(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    def __repr_args__(self) -> Any:
        return (
            (name, getattr(self, name))
            for name, info in type(self).model_fields.items()
            if info.repr
        )


def load_book(raw: object) -> CharacterBook:
    """JSON-shaped containers only; strict scalars with safe error translation."""
    try:
        if not isinstance(raw, dict):
            raise CharacterCardValidationError("Invalid card field: data.character_book")
        if not isinstance(raw.get("entries"), list):
            raise CharacterCardValidationError("Invalid card field: data.character_book.entries")
        for index, entry in enumerate(raw["entries"]):
            if isinstance(entry, dict):
                for name in ("keys", "secondary_keys"):
                    if (
                        name in entry
                        and entry[name] is not None
                        and not isinstance(entry[name], list)
                    ):
                        raise CharacterCardValidationError(
                            f"Invalid card field: data.character_book.entries[{index}].{name}"
                        )
        return CharacterBook.model_validate(raw)
    except ValidationError as error:
        location = error.errors(include_input=False, include_context=False, include_url=False)[0][
            "loc"
        ]
        # No union variant labels or unknown keys from values enter diagnostics.
        path = "data.character_book"
        allowed = set(CharacterBook.model_fields) | set(BookEntry.model_fields)
        for part in location:
            if isinstance(part, int):
                path += f"[{part}]"
            elif part in allowed:
                path += "." + part
        raise CharacterCardValidationError("Invalid card field: " + path) from None


@dataclass(frozen=True)
class LoreSelection:
    entries: tuple[SelectedLore, ...] = field(repr=False)
    diagnostics: tuple[LoreDiagnostic, ...]
    tokens: int


def _view(text: str, sensitive: bool) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return normalized if sensitive else normalized.casefold()


def _matches(keys: Sequence[str], messages: Sequence[str], sensitive: bool) -> bool:
    return any(
        view and view in _view(message, sensitive)
        for key in keys
        if (view := _view(key, sensitive))
        for message in messages
    )


async def select_lore(
    book: CharacterBook | None, current: str, history: Sequence[Message], counter: TokenCounter
) -> LoreSelection:
    if book is None:
        return LoreSelection((), (), 0)
    depth = book.scan_depth if book.scan_depth is not None else 1
    messages = (
        current,
        *(m.content or "" for m in reversed(history) if m.role in ("user", "assistant")),
    )[:depth]
    selected: list[SelectedLore] = []
    diagnostics: list[LoreDiagnostic] = []
    for index, entry in enumerate(book.entries):
        if not entry.enabled:
            reason = "disabled"
        elif not entry.content.strip():
            reason = "empty_content"
        elif any(line.lstrip().startswith("@@") for line in entry.content.splitlines()):
            reason = "unsupported_decorator"
        elif entry.use_regex:
            reason = "unsupported_regex"
        elif entry.constant:
            reason = "selected_constant"
        elif not _matches(entry.keys, messages, bool(entry.case_sensitive)):
            reason = "primary_key_miss"
        elif entry.selective and not _matches(
            entry.secondary_keys or (), messages, bool(entry.case_sensitive)
        ):
            reason = "secondary_key_miss"
        else:
            reason = "selected_selective" if entry.selective else "selected_primary"
        position = entry.position or "after_char"
        diagnostics.append(LoreDiagnostic(index, reason, position))
        if reason.startswith("selected_"):
            selected.append(
                SelectedLore(
                    entry.content,
                    position,
                    entry.insertion_order,
                    source_index=index,
                    effective_priority=entry.priority
                    if entry.priority is not None
                    else entry.insertion_order,
                    activation_kind=reason.removeprefix("selected_"),
                )
            )
    selected.sort(
        key=lambda item: (item.position != "before_char", item.insertion_order, item.source_index)
    )

    async def count() -> int:
        if not selected:
            return 0
        value = counter(tuple(Message(role="system", content=item.render()) for item in selected))
        counted = await value if inspect.isawaitable(value) else value
        if type(counted) is not int or counted < 0:
            raise ValueError("Invalid prompt token count")
        return counted

    tokens = await count()
    while (
        selected
        and book.token_budget is not None
        and (book.token_budget == 0 or tokens > book.token_budget)
    ):
        removed = min(selected, key=lambda item: item.removal_key)
        selected.remove(removed)
        diagnostics[removed.source_index] = LoreDiagnostic(
            removed.source_index, "omitted_by_lore_budget", removed.position
        )
        tokens = await count()
    return LoreSelection(tuple(selected), tuple(diagnostics), tokens)
