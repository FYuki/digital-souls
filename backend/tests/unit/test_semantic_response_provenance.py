"""ユーザーの明示確認を新しい根拠とする意味記憶の応答出典。"""

from uuid import uuid4

from app.memory.episodic.response_provenance import record_response
from app.memory.semantic.contracts import FormationType, Proposition, SemanticCandidate, SemanticOperation, SemanticSource
from app.memory.semantic.repository import SemanticRepository
from tests.unit.test_response_provenance import NOW, STAMP, setup as setup, span


def test_confirmed_reaffirmation_is_independent_of_the_previous_response_memory_version(setup):
    episodes, paths, _ = setup
    semantics = SemanticRepository(paths.persona_memory_sqlite_path)
    conversation, first_user, old_response, confirmation, new_response = (uuid4() for _ in range(5))

    def evidence(turn, role="user"):
        return SemanticSource(kind="CONVERSATION", source_id=turn, revision=1,
                              conversation_id=conversation, span=span(turn, role))

    proposition = Proposition(subject="ユーザー", predicate="居住地", value="大阪",
                              content="ユーザーの居住地は大阪", mutability="CHANGEABLE", self_report=True)
    with semantics.transaction(now=NOW) as tx:
        original = tx.apply(character_id="miori", receipt_key="first", stamp=STAMP,
            candidate=SemanticCandidate(formation_type=FormationType.DIRECT_EXTRACTION,
                proposition=proposition, sources=(evidence(first_user),), confidence=1))
    with episodes.transaction() as tx:
        record_response(tx._connection, character_id="miori", conversation_id=conversation,
            turn_id=old_response, references=((original.id, 1),), history_turn_ids=(), created_at=NOW.isoformat())
    with semantics.transaction(now=NOW) as tx:
        reaffirmed = tx.apply(character_id="miori", receipt_key="confirmation", stamp=STAMP,
            operation=SemanticOperation.REAFFIRM, target_id=original.id, target_version=1,
            candidate=SemanticCandidate(formation_type=FormationType.DIRECT_EXTRACTION,
                proposition=proposition, sources=(evidence(confirmation), evidence(old_response, "assistant")),
                confidence=1))
    with episodes.transaction() as tx:
        record_response(tx._connection, character_id="miori", conversation_id=conversation,
            turn_id=new_response, references=((reaffirmed.id, reaffirmed.content_version),),
            history_turn_ids=(), created_at=NOW.isoformat())
        assert new_response not in tx.invalid_response_ids("miori")
        # 引用したassistant発言自体が失効した場合の検証は省略しない。
        invalid = tx.invalid_response_ids("miori", source_validator=lambda _conversation, sources:
                                         not any(source.source_id == old_response for source in sources))
        assert new_response in invalid
