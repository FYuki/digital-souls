import subprocess
from pathlib import Path


def test_mypy_rejects_swapped_raw_and_masked_text_types(tmp_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    fixture = tmp_path / "type_contract.py"
    fixture.write_text(
        """
from app.prompting.builder import PromptBuilder
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.models import (
    PersistedMaskedText,
    ProcessingTurnInput,
)
from app.prompting.types import (
    CurrentUserOriginalText,
    PersistedConversationMessage,
    PromptTokenBudget,
)
from app.characters.models import CharacterCardData

card: CharacterCardData
budget: PromptTokenBudget
repository: ConversationHistoryRepository
raw = CurrentUserOriginalText("raw")
masked = PersistedMaskedText("masked")
history = (PersistedConversationMessage(role="user", content=masked),)

PromptBuilder().build(
    character=card,
    rag_context=(),
    persisted_history=history,
    current_user_original_text=masked,
    post_history_instructions="",
    token_budget=budget,
)
PersistedConversationMessage(role="user", content=raw)
ProcessingTurnInput(sanitized_user_content=raw)
repository.complete_turn(
    "miori",
    repository.create_conversation("miori").conversation_id,
    repository.create_conversation("miori").conversation_id,
    sanitized_assistant_content=raw,
)
""".strip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["mypy", "--strict", "--no-error-summary", str(fixture)],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.count("[arg-type]") == 4
    assert "[import-not-found]" not in result.stdout
