import logging
from dataclasses import dataclass, replace

from app.characters.lore_selector import (
    CharacterLoreSelection,
    LoreSelectionDecision,
    LoreSelectionReason,
    SelectedCharacterLore,
)
from app.characters.models import CharacterLorePosition
from app.prompting.character_lore import character_lore_messages
from app.prompting.history import select_history_with_measurements, turn_messages
from app.prompting.measurement import TokenCounter, TokenMeasurement, TokenMeasurements
from app.prompting.models import (
    BuiltPrompt,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptInputLimitError,
    PromptMessage,
    PromptRole,
    PromptUsage,
    RagContext,
    RagItem,
)

logger = logging.getLogger(__name__)

CHARACTER_SECTIONS = (
    ("キャラクター概要", "description"),
    ("性格と話し方", "personality"),
    ("関係と世界観", "scenario"),
    ("応答方針", "system_prompt"),
    ("会話例", "mes_example"),
)
RAG_HEADING = "## 関連する記憶"


@dataclass(frozen=True, repr=False)
class _SelectedRegions:
    character_lore: tuple[SelectedCharacterLore, ...]
    character_lore_decisions: tuple[LoreSelectionDecision, ...]
    character: PromptMessage | None
    rag_instruction: PromptMessage | None
    rag: tuple[PromptMessage, ...]
    history: tuple[MaskedHistoryTurn, ...]
    current_user: PromptMessage
    post_history: PromptMessage | None
    omitted_rag_items: int
    omitted_history_turns: int


@dataclass(frozen=True, repr=False)
class _MeasuredRegions:
    selected: _SelectedRegions
    measurements: TokenMeasurements


class PromptBuilder:
    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def build(self, prompt_input: PromptBuildInput) -> BuiltPrompt:
        measurements = TokenMeasurements(self._token_counter)
        measured = self._select_regions(prompt_input, measurements)
        measured = self._fit_total_budget(
            measured,
            prompt_input.budget.total,
        )
        messages = self._messages(measured.selected)
        usage = self._usage(measured)
        logger.debug(
            "Prompt built: messages=%d tokens=%d omitted_lore=%d omitted_rag=%d "
            "omitted_history=%d",
            len(messages),
            usage.total,
            usage.omitted_character_lore_entries,
            usage.omitted_rag_items,
            usage.omitted_history_exchanges,
        )
        return BuiltPrompt(
            messages=messages,
            usage=usage,
            character_lore_decisions=measured.selected.character_lore_decisions,
        )

    def _select_regions(
        self,
        prompt_input: PromptBuildInput,
        measurements: TokenMeasurements,
    ) -> _MeasuredRegions:
        character, measurements = self._character_message(prompt_input, measurements)
        character_lore, lore_decisions, measurements = self._select_character_lore(
            prompt_input.character_lore,
            prompt_input.budget.character_lore,
            measurements,
        )
        current_user = PromptMessage(PromptRole.USER, prompt_input.current_user.content)
        measured_user = measurements.measure((current_user,))
        self._require_within(
            "current_user",
            measured_user.count,
            prompt_input.budget.current_user,
        )
        selected_history = select_history_with_measurements(
            prompt_input.history.newest_first_factory(),
            measurements=measured_user.measurements,
            token_limit=prompt_input.budget.history,
        )
        rag_instruction, rag, omitted_rag, measurements = self._select_rag(
            prompt_input, selected_history.measurements
        )
        post_history, measurements = self._post_history_message(
            prompt_input, measurements
        )
        selected = _SelectedRegions(
            character_lore=character_lore,
            character_lore_decisions=lore_decisions,
            character=character,
            rag_instruction=rag_instruction,
            rag=rag,
            history=selected_history.history.turns,
            current_user=current_user,
            post_history=post_history,
            omitted_rag_items=omitted_rag,
            omitted_history_turns=(
                prompt_input.history.omitted_turns
                + selected_history.history.omitted_turns
            ),
        )
        return _MeasuredRegions(selected, measurements)

    def _select_character_lore(
        self,
        selection: CharacterLoreSelection,
        token_limit: int,
        measurements: TokenMeasurements,
    ) -> tuple[
        tuple[SelectedCharacterLore, ...],
        tuple[LoreSelectionDecision, ...],
        TokenMeasurements,
    ]:
        entries = selection.entries
        decisions = selection.decisions
        if not entries:
            return (), decisions, measurements
        measured = measurements.measure(character_lore_messages(entries))
        measurements = measured.measurements
        if measured.count <= token_limit:
            return entries, decisions, measurements
        remaining = list(entries)
        while remaining:
            removed = min(remaining, key=lambda entry: entry.removal_key)
            remaining.remove(removed)
            decisions = self._replace_lore_decision_reasons(
                decisions,
                (removed.source_index,),
                LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
            )
            messages = character_lore_messages(tuple(remaining))
            measured = measurements.measure(messages)
            measurements = measured.measurements
            if measured.count <= token_limit:
                return tuple(remaining), decisions, measurements
        return (), decisions, measurements

    def _character_message(
        self,
        prompt_input: PromptBuildInput,
        measurements: TokenMeasurements,
    ) -> tuple[PromptMessage | None, TokenMeasurements]:
        sections = [
            f"## {heading}\n{value.strip()}"
            for heading, field in CHARACTER_SECTIONS
            if (value := getattr(prompt_input.character, field)).strip()
        ]
        if not sections:
            return None, measurements
        message = PromptMessage(PromptRole.SYSTEM, "\n\n".join(sections))
        measured = measurements.measure((message,))
        self._require_within(
            "character",
            measured.count,
            prompt_input.budget.character,
        )
        return message, measured.measurements

    def _select_rag(
        self,
        prompt_input: PromptBuildInput,
        measurements: TokenMeasurements,
    ) -> tuple[
        PromptMessage | None,
        tuple[PromptMessage, ...],
        int,
        TokenMeasurements,
    ]:
        instruction = self._rag_instruction_message(prompt_input.rag)
        instruction_count = 0
        if instruction is not None:
            measured_instruction = measurements.measure((instruction,))
            instruction_count = measured_instruction.count
            self._require_within(
                "rag",
                instruction_count,
                prompt_input.budget.rag,
            )
            measurements = measured_instruction.measurements
        messages = tuple(self._rag_message(item) for item in prompt_input.rag.items)
        if not messages:
            return instruction, (), 0, measurements
        item_limit = prompt_input.budget.rag - instruction_count
        measured = measurements.measure(messages)
        if measured.count <= item_limit:
            return instruction, messages, 0, measured.measurements
        selected_count, measurements = self._largest_fitting_message_prefix(
            messages,
            item_limit,
            measured.measurements,
        )
        return (
            instruction,
            messages[:selected_count],
            len(messages) - selected_count,
            measurements,
        )

    def _post_history_message(
        self,
        prompt_input: PromptBuildInput,
        measurements: TokenMeasurements,
    ) -> tuple[PromptMessage | None, TokenMeasurements]:
        content = prompt_input.character.post_history_instructions.strip()
        if not content:
            return None, measurements
        message = PromptMessage(PromptRole.SYSTEM, content)
        measured = measurements.measure((message,))
        if measured.count > prompt_input.budget.post_history:
            return None, measured.measurements
        return message, measured.measurements

    def _fit_total_budget(
        self,
        measured: _MeasuredRegions,
        total_limit: int,
    ) -> _MeasuredRegions:
        total = self._measure_total(measured.selected, measured.measurements)
        measured = _MeasuredRegions(measured.selected, total.measurements)
        if total.count <= total_limit:
            return measured
        required = measured.measurements.measure(
            self._required_messages(measured.selected)
        )
        self._require_within(
            "total",
            required.count,
            total_limit,
        )
        current = self._fit_rag_to_total(
            _MeasuredRegions(measured.selected, required.measurements), total_limit
        )
        current_total = self._measure_total(current.selected, current.measurements)
        current = _MeasuredRegions(current.selected, current_total.measurements)
        if current_total.count <= total_limit:
            return current
        current = self._fit_history_to_total(current, total_limit)
        current_total = self._measure_total(current.selected, current.measurements)
        current = _MeasuredRegions(current.selected, current_total.measurements)
        if current_total.count <= total_limit:
            return current
        current = self._fit_character_lore_to_total(current, total_limit)
        current_total = self._measure_total(current.selected, current.measurements)
        current = _MeasuredRegions(current.selected, current_total.measurements)
        if current_total.count <= total_limit:
            return current
        selected = current.selected
        if selected.post_history is not None:
            selected = replace(selected, post_history=None)
        final_total = self._measure_total(selected, current.measurements)
        self._require_within(
            "total",
            final_total.count,
            total_limit,
        )
        return _MeasuredRegions(selected, final_total.measurements)

    def _fit_rag_to_total(
        self,
        measured: _MeasuredRegions,
        total_limit: int,
    ) -> _MeasuredRegions:
        selected = measured.selected
        if not selected.rag:
            return measured
        without_rag = replace(
            selected,
            rag=(),
            omitted_rag_items=selected.omitted_rag_items + len(selected.rag),
        )
        without_rag_total = self._measure_total(without_rag, measured.measurements)
        measurements = without_rag_total.measurements
        if without_rag_total.count > total_limit:
            return _MeasuredRegions(without_rag, measurements)
        lower = 0
        upper = len(selected.rag)
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            candidate = self._with_rag_prefix(selected, middle)
            candidate_total = self._measure_total(candidate, measurements)
            measurements = candidate_total.measurements
            if candidate_total.count <= total_limit:
                lower = middle
            else:
                upper = middle
        return _MeasuredRegions(self._with_rag_prefix(selected, lower), measurements)

    def _fit_history_to_total(
        self,
        measured: _MeasuredRegions,
        total_limit: int,
    ) -> _MeasuredRegions:
        selected = measured.selected
        removable = self._removable_history_indices(selected.history)
        if not removable:
            return measured
        all_removed = self._without_oldest_history(
            selected,
            removable,
            len(removable),
        )
        all_removed_total = self._measure_total(all_removed, measured.measurements)
        measurements = all_removed_total.measurements
        if all_removed_total.count > total_limit:
            return _MeasuredRegions(all_removed, measurements)
        lower = 0
        upper = len(removable)
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            candidate = self._without_oldest_history(selected, removable, middle)
            candidate_total = self._measure_total(candidate, measurements)
            measurements = candidate_total.measurements
            if candidate_total.count <= total_limit:
                upper = middle
            else:
                lower = middle
        return _MeasuredRegions(
            self._without_oldest_history(selected, removable, upper), measurements
        )

    def _fit_character_lore_to_total(
        self,
        measured: _MeasuredRegions,
        total_limit: int,
    ) -> _MeasuredRegions:
        selected = measured.selected
        if not selected.character_lore:
            return measured
        removal_order = tuple(
            sorted(selected.character_lore, key=lambda entry: entry.removal_key)
        )
        all_removed = self._without_character_lore(
            selected,
            removal_order,
            len(removal_order),
        )
        all_removed_total = self._measure_total(all_removed, measured.measurements)
        measurements = all_removed_total.measurements
        if all_removed_total.count > total_limit:
            return _MeasuredRegions(all_removed, measurements)
        lower = 0
        upper = len(removal_order)
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            candidate = self._without_character_lore(
                selected,
                removal_order,
                middle,
            )
            candidate_total = self._measure_total(candidate, measurements)
            measurements = candidate_total.measurements
            if candidate_total.count <= total_limit:
                upper = middle
            else:
                lower = middle
        return _MeasuredRegions(
            self._without_character_lore(selected, removal_order, upper),
            measurements,
        )

    @staticmethod
    def _with_rag_prefix(
        selected: _SelectedRegions,
        count: int,
    ) -> _SelectedRegions:
        removed = len(selected.rag) - count
        return replace(
            selected,
            rag=selected.rag[:count],
            omitted_rag_items=selected.omitted_rag_items + removed,
        )

    @staticmethod
    def _without_oldest_history(
        selected: _SelectedRegions,
        removable: tuple[int, ...],
        count: int,
    ) -> _SelectedRegions:
        removed = frozenset(removable[:count])
        return replace(
            selected,
            history=tuple(
                turn
                for index, turn in enumerate(selected.history)
                if index not in removed
            ),
            omitted_history_turns=selected.omitted_history_turns + count,
        )

    @staticmethod
    def _without_character_lore(
        selected: _SelectedRegions,
        removal_order: tuple[SelectedCharacterLore, ...],
        count: int,
    ) -> _SelectedRegions:
        removed_indices = frozenset(
            entry.source_index for entry in removal_order[:count]
        )
        return replace(
            selected,
            character_lore=tuple(
                entry
                for entry in selected.character_lore
                if entry.source_index not in removed_indices
            ),
            character_lore_decisions=(
                PromptBuilder._replace_lore_decision_reasons(
                    selected.character_lore_decisions,
                    removed_indices,
                    LoreSelectionReason.OMITTED_BY_TOTAL_BUDGET,
                )
            ),
        )

    @staticmethod
    def _replace_lore_decision_reasons(
        decisions: tuple[LoreSelectionDecision, ...],
        source_indices: frozenset[int] | tuple[int, ...],
        reason: LoreSelectionReason,
    ) -> tuple[LoreSelectionDecision, ...]:
        target_indices = frozenset(source_indices)
        known_indices = frozenset(decision.source_index for decision in decisions)
        missing_indices = target_indices - known_indices
        if missing_indices:
            raise ValueError(
                "character lore decisions are missing selected source indices: "
                f"{sorted(missing_indices)}"
            )
        return tuple(
            LoreSelectionDecision(decision.source_index, reason)
            if decision.source_index in target_indices
            else decision
            for decision in decisions
        )

    @staticmethod
    def _largest_fitting_message_prefix(
        messages: tuple[PromptMessage, ...],
        token_limit: int,
        measurements: TokenMeasurements,
    ) -> tuple[int, TokenMeasurements]:
        lower = 0
        upper = len(messages)
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            measured = measurements.measure(messages[:middle])
            measurements = measured.measurements
            if measured.count <= token_limit:
                lower = middle
            else:
                upper = middle
        return lower, measurements

    def _usage(
        self,
        measured: _MeasuredRegions,
    ) -> PromptUsage:
        selected = measured.selected
        lore = measured.measurements.measure(
            character_lore_messages(selected.character_lore)
        )
        character, measurements = self._measure_optional(
            selected.character, lore.measurements
        )
        rag_messages = (
            (() if selected.rag_instruction is None else (selected.rag_instruction,))
            + selected.rag
        )
        rag = measurements.measure(rag_messages)
        history = rag.measurements.measure(self._history_messages(selected.history))
        current_user = history.measurements.measure((selected.current_user,))
        post_history, measurements = self._measure_optional(
            selected.post_history, current_user.measurements
        )
        total = self._measure_total(selected, measurements)
        return PromptUsage(
            total=total.count,
            character=character,
            character_lore=lore.count,
            rag=rag.count,
            history=history.count,
            current_user=current_user.count,
            post_history=post_history,
            omitted_character_lore_entries=sum(
                decision.reason
                in {
                    LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
                    LoreSelectionReason.OMITTED_BY_TOTAL_BUDGET,
                }
                for decision in selected.character_lore_decisions
            ),
            omitted_rag_items=selected.omitted_rag_items,
            omitted_history_exchanges=selected.omitted_history_turns,
        )

    def _messages(self, selected: _SelectedRegions) -> tuple[PromptMessage, ...]:
        lore_before = character_lore_messages(
            tuple(
                entry
                for entry in selected.character_lore
                if entry.position is CharacterLorePosition.BEFORE_CHAR
            )
        )
        lore_after = character_lore_messages(
            tuple(
                entry
                for entry in selected.character_lore
                if entry.position is CharacterLorePosition.AFTER_CHAR
            )
        )
        character = () if selected.character is None else (selected.character,)
        rag_instruction = (
            ()
            if selected.rag_instruction is None
            else (selected.rag_instruction,)
        )
        post_history = (
            () if selected.post_history is None else (selected.post_history,)
        )
        return (
            *lore_before,
            *character,
            *lore_after,
            *rag_instruction,
            *selected.rag,
            *self._history_messages(selected.history),
            *post_history,
            selected.current_user,
        )

    def _required_messages(
        self,
        selected: _SelectedRegions,
    ) -> tuple[PromptMessage, ...]:
        character = () if selected.character is None else (selected.character,)
        rag_instruction = (
            ()
            if selected.rag_instruction is None
            else (selected.rag_instruction,)
        )
        latest_completed = next(
            (turn for turn in reversed(selected.history) if turn.is_completed),
            None,
        )
        completed = (
            () if latest_completed is None else turn_messages(latest_completed)
        )
        return (*character, *rag_instruction, *completed, selected.current_user)

    @staticmethod
    def _removable_history_indices(
        turns: tuple[MaskedHistoryTurn, ...],
    ) -> tuple[int, ...]:
        protected = next(
            (
                index
                for index in range(len(turns) - 1, -1, -1)
                if turns[index].is_completed
            ),
            None,
        )
        return tuple(index for index in range(len(turns)) if index != protected)

    @staticmethod
    def _history_messages(
        turns: tuple[MaskedHistoryTurn, ...],
    ) -> tuple[PromptMessage, ...]:
        return tuple(message for turn in turns for message in turn_messages(turn))

    def _measure_total(
        self,
        selected: _SelectedRegions,
        measurements: TokenMeasurements,
    ) -> TokenMeasurement:
        return measurements.measure(self._messages(selected))

    @staticmethod
    def _measure_optional(
        message: PromptMessage | None,
        measurements: TokenMeasurements,
    ) -> tuple[int, TokenMeasurements]:
        if message is None:
            return 0, measurements
        measured = measurements.measure((message,))
        return measured.count, measured.measurements

    @staticmethod
    def _rag_message(item: RagItem) -> PromptMessage:
        return PromptMessage(
            PromptRole.SYSTEM,
            f"{RAG_HEADING}\n{item.content}",
            memory_reference=item.reference,
        )

    @staticmethod
    def _rag_instruction_message(rag: RagContext) -> PromptMessage | None:
        content = rag.required_instruction.strip()
        if not content:
            return None
        return PromptMessage(PromptRole.SYSTEM, content)

    @staticmethod
    def _require_within(region: str, used: int, limit: int) -> None:
        if used > limit:
            raise PromptInputLimitError(region, used, limit)
