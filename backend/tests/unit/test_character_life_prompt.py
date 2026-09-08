from app.character_life.models import Kind, LifeState, StateStatus
from app.character_life.prompt import Context
from app.character_life.store import Store
from app.prompting import BuiltPrompt, PromptMessage, PromptRole
from app.prompting.models import PromptUsage


def test_next_response_uses_fixed_character_state_without_displacing_user(tmp_path):
    store = Store(tmp_path / "life.db")
    state = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.INTEREST,
            content="色彩への関心",
            source="user",
        )
    )
    prompt = BuiltPrompt(
        (
            PromptMessage(PromptRole.SYSTEM, "人格"),
            PromptMessage(PromptRole.USER, "最近どう？"),
        ),
        PromptUsage(10, 1, 0, 0, 0, 9, 0, 0, 0, 0),
        (),
    )
    context = Context(
        store, lambda messages: sum(len(m.content) for m in messages), 4000
    )
    fixed = context("miori", prompt)
    assert fixed.messages[-1] == prompt.messages[-1]
    assert "色彩への関心" in fixed.messages[-2].content
    assert "非信頼" in fixed.messages[-3].content
    assert context("other", prompt) is prompt
    assert Context(store, lambda _: 9999, 4000)("miori", prompt) is prompt
    store.save_state(
        state.model_copy(update={"status": StateStatus.DORMANT}), expected_revision=1
    )
    assert context("miori", prompt) is prompt
    assert "色彩への関心" in fixed.messages[-2].content
