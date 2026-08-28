import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.characters.models import (
    CharacterBook,
    CharacterBookEntry,
    CharacterLorePosition,
)


class LoreActivationKind(str, Enum):
    CONSTANT = "constant"
    PRIMARY = "primary"
    SELECTIVE = "selective"


class LoreSelectionReason(str, Enum):
    DISABLED = "disabled"
    EMPTY_CONTENT = "empty_content"
    UNSUPPORTED_DECORATOR = "unsupported_decorator"
    UNSUPPORTED_REGEX = "unsupported_regex"
    PRIMARY_KEY_MISS = "primary_key_miss"
    SECONDARY_KEY_MISS = "secondary_key_miss"
    SELECTED_CONSTANT = "selected_constant"
    SELECTED_PRIMARY = "selected_primary"
    SELECTED_SELECTIVE = "selected_selective"
    OMITTED_BY_LORE_BUDGET = "omitted_by_lore_budget"
    OMITTED_BY_TOTAL_BUDGET = "omitted_by_total_budget"


@dataclass(frozen=True, repr=False)
class SelectedCharacterLore:
    source_index: int
    content: str
    position: CharacterLorePosition
    insertion_order: int
    effective_priority: int
    activation_kind: LoreActivationKind

    @property
    def removal_key(self) -> tuple[int, int, int]:
        return (
            self.effective_priority,
            self.insertion_order,
            self.source_index,
        )


@dataclass(frozen=True)
class LoreSelectionDecision:
    source_index: int
    reason: LoreSelectionReason


@dataclass(frozen=True, repr=False)
class CharacterLoreSelection:
    entries: tuple[SelectedCharacterLore, ...]
    decisions: tuple[LoreSelectionDecision, ...]
    lore_budget_tokens: int | None
    recursive_scanning_ignored: bool

    @property
    def omitted_by_budget(self) -> int:
        return sum(
            decision.reason is LoreSelectionReason.OMITTED_BY_LORE_BUDGET
            for decision in self.decisions
        )


class CharacterLoreTokenCounter(Protocol):
    def count_lore_tokens(
        self,
        entries: tuple[SelectedCharacterLore, ...],
    ) -> int:
        ...


def select_character_lore(
    book: CharacterBook | None,
    messages_newest_first: tuple[str, ...],
    token_counter: CharacterLoreTokenCounter,
) -> CharacterLoreSelection:
    if book is None:
        return CharacterLoreSelection((), (), None, False)

    scan_depth = 1 if book.scan_depth is None else book.scan_depth
    scan_messages = messages_newest_first[:scan_depth]
    decisions: dict[int, LoreSelectionReason] = {}
    candidates: list[SelectedCharacterLore] = []
    for source_index, entry in enumerate(book.entries):
        selected, reason = _match_entry(entry, source_index, scan_messages)
        decisions[source_index] = reason
        if selected is not None:
            candidates.append(selected)

    budget_tokens: int | None = None
    if book.token_budget is not None:
        candidates, decisions, budget_tokens = _fit_lore_budget(
            candidates,
            decisions,
            book.token_budget,
            token_counter,
        )

    return CharacterLoreSelection(
        entries=_prompt_order(tuple(candidates)),
        decisions=tuple(
            LoreSelectionDecision(source_index, decisions[source_index])
            for source_index in sorted(decisions)
        ),
        lore_budget_tokens=budget_tokens,
        recursive_scanning_ignored=book.recursive_scanning is True,
    )


def _match_entry(
    entry: CharacterBookEntry,
    source_index: int,
    scan_messages: tuple[str, ...],
) -> tuple[SelectedCharacterLore | None, LoreSelectionReason]:
    if not entry.enabled:
        return None, LoreSelectionReason.DISABLED
    content = entry.content.strip()
    if not content:
        return None, LoreSelectionReason.EMPTY_CONTENT
    if _contains_decorator(entry.content):
        return None, LoreSelectionReason.UNSUPPORTED_DECORATOR
    if entry.use_regex:
        return None, LoreSelectionReason.UNSUPPORTED_REGEX

    if entry.constant is True:
        return (
            _selected_entry(
                entry,
                source_index,
                content,
                LoreActivationKind.CONSTANT,
            ),
            LoreSelectionReason.SELECTED_CONSTANT,
        )

    if not _matches_any(entry.keys, scan_messages, entry.case_sensitive is True):
        return None, LoreSelectionReason.PRIMARY_KEY_MISS
    if entry.selective is True:
        secondary_keys = () if entry.secondary_keys is None else entry.secondary_keys
        if not _matches_any(
            secondary_keys,
            scan_messages,
            entry.case_sensitive is True,
        ):
            return None, LoreSelectionReason.SECONDARY_KEY_MISS
        return (
            _selected_entry(
                entry,
                source_index,
                content,
                LoreActivationKind.SELECTIVE,
            ),
            LoreSelectionReason.SELECTED_SELECTIVE,
        )
    return (
        _selected_entry(
            entry,
            source_index,
            content,
            LoreActivationKind.PRIMARY,
        ),
        LoreSelectionReason.SELECTED_PRIMARY,
    )


def _selected_entry(
    entry: CharacterBookEntry,
    source_index: int,
    content: str,
    activation_kind: LoreActivationKind,
) -> SelectedCharacterLore:
    return SelectedCharacterLore(
        source_index=source_index,
        content=content,
        position=(
            CharacterLorePosition.AFTER_CHAR
            if entry.position is None
            else entry.position
        ),
        insertion_order=entry.insertion_order,
        effective_priority=(
            entry.insertion_order if entry.priority is None else entry.priority
        ),
        activation_kind=activation_kind,
    )


def _contains_decorator(content: str) -> bool:
    return any(line.lstrip().startswith("@@") for line in content.splitlines())


def _matches_any(
    keys: tuple[str, ...],
    messages: tuple[str, ...],
    case_sensitive: bool,
) -> bool:
    normalized_messages = tuple(
        _normalized_view(message, case_sensitive) for message in messages
    )
    return any(
        normalized_key in message
        for key in keys
        if (normalized_key := _normalized_view(key, case_sensitive))
        for message in normalized_messages
    )


def _normalized_view(value: str, case_sensitive: bool) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return normalized if case_sensitive else normalized.casefold()


def _fit_lore_budget(
    candidates: list[SelectedCharacterLore],
    decisions: dict[int, LoreSelectionReason],
    token_budget: int,
    token_counter: CharacterLoreTokenCounter,
) -> tuple[
    list[SelectedCharacterLore],
    dict[int, LoreSelectionReason],
    int,
]:
    remaining = list(candidates)
    token_count = _count_tokens(remaining, token_counter)
    while remaining and token_count > token_budget:
        removed = min(remaining, key=lambda entry: entry.removal_key)
        remaining.remove(removed)
        decisions[removed.source_index] = LoreSelectionReason.OMITTED_BY_LORE_BUDGET
        token_count = _count_tokens(remaining, token_counter)
    return remaining, decisions, token_count


def _count_tokens(
    entries: list[SelectedCharacterLore],
    token_counter: CharacterLoreTokenCounter,
) -> int:
    if not entries:
        return 0
    count = token_counter.count_lore_tokens(_prompt_order(tuple(entries)))
    if type(count) is not int or count < 0:
        raise ValueError("character lore token count must be a non-negative integer")
    return count


def _prompt_order(
    entries: tuple[SelectedCharacterLore, ...],
) -> tuple[SelectedCharacterLore, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0
                if entry.position is CharacterLorePosition.BEFORE_CHAR
                else 1,
                entry.insertion_order,
                entry.source_index,
            ),
        )
    )
