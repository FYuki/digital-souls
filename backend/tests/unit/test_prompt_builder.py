import logging
import inspect
from dataclasses import FrozenInstanceError

import pytest


def _card_data(**overrides):
    from app.characters.models import CharacterCardData

    values = {
        "name": "光織",
        "description": "CARD_DESCRIPTION_SENTINEL",
        "personality": "CARD_PERSONALITY_SENTINEL",
        "scenario": "CARD_SCENARIO_SENTINEL",
        "first_mes": "FIRST_MESSAGE_MUST_NOT_APPEAR",
        "mes_example": "CARD_EXAMPLE_SENTINEL",
        "creator_notes": "",
        "system_prompt": "CARD_SYSTEM_SENTINEL",
        "post_history_instructions": "POST_HISTORY_SENTINEL",
        "alternate_greetings": (),
        "group_only_greetings": (),
        "creator": "",
        "character_version": "",
        "extensions": {},
    }
    values.update(overrides)
    return CharacterCardData(**values)


def _budget(**overrides):
    from app.prompting.types import PromptTokenBudget

    values = {
        "character_and_system": 1000,
        "rag": 500,
        "history": 750,
        "current_user": 400,
        "final_instructions": 200,
        "total": 2500,
    }
    values.update(overrides)
    return PromptTokenBudget(**values)


def _build(**overrides):
    from app.conversation_history.models import PersistedMaskedText
    from app.prompting.builder import PromptBuilder
    from app.prompting.types import (
        CurrentUserOriginalText,
        PersistedConversationMessage,
        RagContextText,
    )

    values = {
        "character": _card_data(),
        "rag_context": (
            RagContextText("RAG_FIRST_SENTINEL"),
            RagContextText("RAG_SECOND_SENTINEL"),
        ),
        "persisted_history": (
            PersistedConversationMessage(
                role="user",
                content=PersistedMaskedText("MASKED_USER_SENTINEL"),
            ),
            PersistedConversationMessage(
                role="assistant",
                content=PersistedMaskedText("MASKED_ASSISTANT_SENTINEL"),
            ),
        ),
        "current_user_original_text": CurrentUserOriginalText(
            "CURRENT_RAW_USER_SENTINEL"
        ),
        "post_history_instructions": "POST_HISTORY_SENTINEL",
        "token_budget": _budget(),
    }
    values.update(overrides)
    return PromptBuilder().build(**values)


class TestPromptBuilderOrdering:
    def test_builds_all_categories_in_deterministic_order(self):
        prompt = _build()

        assert [(message.role, message.content) for message in prompt.messages] == [
            (
                "system",
                "\n\n".join(
                    [
                        "CARD_DESCRIPTION_SENTINEL",
                        "CARD_PERSONALITY_SENTINEL",
                        "CARD_SCENARIO_SENTINEL",
                        "CARD_SYSTEM_SENTINEL",
                        "CARD_EXAMPLE_SENTINEL",
                    ]
                ),
            ),
            ("system", "RAG_FIRST_SENTINEL"),
            ("system", "RAG_SECOND_SENTINEL"),
            ("user", "MASKED_USER_SENTINEL"),
            ("assistant", "MASKED_ASSISTANT_SENTINEL"),
            ("user", "CURRENT_RAW_USER_SENTINEL"),
            ("system", "POST_HISTORY_SENTINEL"),
        ]

    def test_does_not_include_first_message_in_generation_prompt(self):
        prompt = _build()

        contents = tuple(message.content for message in prompt.messages)
        assert "FIRST_MESSAGE_MUST_NOT_APPEAR" not in contents
        assert contents[-2:] == (
            "CURRENT_RAW_USER_SENTINEL",
            "POST_HISTORY_SENTINEL",
        )

    def test_preserves_history_roles_and_input_order(self):
        prompt = _build()

        assert [(message.role, message.content) for message in prompt.messages[3:5]] == [
            ("user", "MASKED_USER_SENTINEL"),
            ("assistant", "MASKED_ASSISTANT_SENTINEL"),
        ]


class TestPromptBuilderOptionalInputs:
    def test_omits_blank_character_fields_without_empty_messages(self):
        prompt = _build(
            character=_card_data(
                personality=" ",
                scenario="",
                mes_example="\n",
            )
        )

        assert prompt.messages[0].content == (
            "CARD_DESCRIPTION_SENTINEL\n\nCARD_SYSTEM_SENTINEL"
        )
        assert all(message.content.strip() for message in prompt.messages)

    def test_omits_empty_rag_and_history_without_empty_messages(self):
        prompt = _build(rag_context=(), persisted_history=())

        assert [(message.role, message.content) for message in prompt.messages] == [
            (
                "system",
                "\n\n".join(
                    [
                        "CARD_DESCRIPTION_SENTINEL",
                        "CARD_PERSONALITY_SENTINEL",
                        "CARD_SCENARIO_SENTINEL",
                        "CARD_SYSTEM_SENTINEL",
                        "CARD_EXAMPLE_SENTINEL",
                    ]
                ),
            ),
            ("user", "CURRENT_RAW_USER_SENTINEL"),
            ("system", "POST_HISTORY_SENTINEL"),
        ]

    def test_omits_blank_final_instruction(self):
        prompt = _build(post_history_instructions=" ")

        assert prompt.messages[-1].content == "CURRENT_RAW_USER_SENTINEL"
        assert prompt.messages[-1].role == "user"


class TestPromptHistoryContract:
    @pytest.mark.parametrize(
        "messages",
        [
            (("user", "orphan-user"),),
            (("assistant", "assistant-first"), ("user", "user-second")),
            (("user", "first-user"), ("user", "second-user")),
            (("user", " "), ("assistant", "assistant")),
        ],
    )
    def test_rejects_history_that_is_not_complete_user_assistant_turns(
        self,
        messages,
    ):
        from app.conversation_history.models import PersistedMaskedText
        from app.prompting.types import PersistedConversationMessage

        history = tuple(
            PersistedConversationMessage(
                role=role,
                content=PersistedMaskedText(content),
            )
            for role, content in messages
        )

        with pytest.raises(
            ValueError,
            match="persisted history must contain complete user/assistant turns",
        ):
            _build(persisted_history=history)

    def test_rejects_blank_assistant_in_complete_history(self):
        from app.conversation_history.models import PersistedMaskedText
        from app.prompting.types import PersistedConversationMessage

        history = tuple(
            PersistedConversationMessage(
                role=role,
                content=PersistedMaskedText(content),
            )
            for role, content in (
                ("user", "old-user"),
                ("assistant", "old-assistant"),
                ("user", "latest-user"),
                ("assistant", "\n"),
            )
        )

        with pytest.raises(
            ValueError,
            match="persisted history must contain complete user/assistant turns",
        ):
            _build(persisted_history=history)


class TestPromptBudgetContract:
    def test_preserves_all_element_limits_on_built_prompt(self):
        budget = _budget()

        prompt = _build(token_budget=budget)

        assert prompt.token_budget is budget
        assert (
            budget.character_and_system,
            budget.rag,
            budget.history,
            budget.current_user,
            budget.final_instructions,
            budget.total,
        ) == (1000, 500, 750, 400, 200, 2500)

    def test_exposes_required_retention_and_reduction_priorities(self):
        budget = _budget()

        assert budget.retention_priority == (
            "character_and_system",
            "current_user",
            "latest_exchange",
        )
        assert budget.reduction_priority == ("rag", "oldest_history")

    @pytest.mark.parametrize("field", ["character_and_system", "total"])
    def test_rejects_non_positive_limits(self, field):
        from app.prompting.types import PromptTokenBudget

        values = {
            "character_and_system": 100,
            "rag": 50,
            "history": 75,
            "current_user": 40,
            "final_instructions": 20,
            "total": 250,
        }
        values[field] = 0

        with pytest.raises(ValueError, match=field):
            PromptTokenBudget(**values)

    def test_required_input_limit_error_does_not_expose_content(self):
        from app.prompting.types import PromptInputLimitError

        error = PromptInputLimitError(element="current_user", limit=10)

        assert tuple(inspect.signature(PromptInputLimitError).parameters) == (
            "element",
            "limit",
        )
        assert error.element == "current_user"
        assert error.limit == 10

    def test_does_not_apply_element_budgets_to_messages(self):
        budget = _budget(
            character_and_system=1,
            rag=1,
            history=1,
            current_user=1,
            final_instructions=1,
        )

        prompt = _build(token_budget=budget)

        assert [(message.role, message.content) for message in prompt.messages] == [
            (
                "system",
                "\n\n".join(
                    [
                        "CARD_DESCRIPTION_SENTINEL",
                        "CARD_PERSONALITY_SENTINEL",
                        "CARD_SCENARIO_SENTINEL",
                        "CARD_SYSTEM_SENTINEL",
                        "CARD_EXAMPLE_SENTINEL",
                    ]
                ),
            ),
            ("system", "RAG_FIRST_SENTINEL"),
            ("system", "RAG_SECOND_SENTINEL"),
            ("user", "MASKED_USER_SENTINEL"),
            ("assistant", "MASKED_ASSISTANT_SENTINEL"),
            ("user", "CURRENT_RAW_USER_SENTINEL"),
            ("system", "POST_HISTORY_SENTINEL"),
        ]

    def test_does_not_apply_total_budget_to_rag_or_history(self):
        from app.conversation_history.models import PersistedMaskedText
        from app.prompting.types import (
            PersistedConversationMessage,
            RagContextText,
        )

        history = tuple(
            PersistedConversationMessage(
                role=role,
                content=PersistedMaskedText(content),
            )
            for role, content in (
                ("user", "OLD_MASKED_USER"),
                ("assistant", "OLD_MASKED_ASSISTANT"),
                ("user", "LATEST_MASKED_USER"),
                ("assistant", "LATEST_MASKED_ASSISTANT"),
            )
        )

        prompt = _build(
            rag_context=(
                RagContextText("FIRST_RAG_MEMORY"),
                RagContextText("SECOND_RAG_MEMORY"),
            ),
            persisted_history=history,
            token_budget=_budget(total=1),
        )

        assert [(message.role, message.content) for message in prompt.messages[1:]] == [
            ("system", "FIRST_RAG_MEMORY"),
            ("system", "SECOND_RAG_MEMORY"),
            ("user", "OLD_MASKED_USER"),
            ("assistant", "OLD_MASKED_ASSISTANT"),
            ("user", "LATEST_MASKED_USER"),
            ("assistant", "LATEST_MASKED_ASSISTANT"),
            ("user", "CURRENT_RAW_USER_SENTINEL"),
            ("system", "POST_HISTORY_SENTINEL"),
        ]


class TestPromptPrivacyContract:
    def test_sensitive_content_is_hidden_from_repr(self):
        prompt = _build()

        rendered = repr(prompt)

        for sentinel in (
            "CARD_DESCRIPTION_SENTINEL",
            "RAG_FIRST_SENTINEL",
            "MASKED_USER_SENTINEL",
            "CURRENT_RAW_USER_SENTINEL",
        ):
            assert sentinel not in rendered

    def test_builder_logs_metadata_only(self, caplog):
        caplog.set_level(logging.DEBUG)

        _build()

        rendered = "\n".join(record.getMessage() for record in caplog.records)
        for sentinel in (
            "CARD_DESCRIPTION_SENTINEL",
            "RAG_FIRST_SENTINEL",
            "MASKED_USER_SENTINEL",
            "CURRENT_RAW_USER_SENTINEL",
        ):
            assert sentinel not in rendered

    def test_built_prompt_is_immutable(self):
        prompt = _build()

        with pytest.raises(FrozenInstanceError):
            prompt.messages = ()
