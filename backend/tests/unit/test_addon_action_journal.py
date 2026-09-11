"""副作用claimの永続化と、runtime再開・並行実行時の二重送信防止。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from app.addon_action.journal import (
    ActionIdentity,
    ActionJournal,
    ActionOutcome,
    UnresolvedAction,
)
from app.addon_action.models import ExecutionScene
from app.addon_action.store import ActionStore
from app.external_mcp.models import MCPFailure


def identity(**changes):
    return replace(
        ActionIdentity(
            "activity-1",
            "digest",
            "connection",
            "identity",
            "miori",
            "session",
            ExecutionScene.AUTONOMOUS,
            "write",
            "definition",
        ),
        **changes,
    )


def test_runtime_replay_and_parallel_claim_do_not_repeat_dispatch(tmp_path):
    path = tmp_path / "actions.sqlite3"
    journals = [ActionJournal(ActionStore(path)) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: j.begin(identity(), str(uuid4())), journals))
    assert sum(claimed for _, claimed in results) == 1
    assert len({r.execution_id for r, _ in results}) == 1
    restored = ActionJournal(ActionStore(path))
    restored.detach_dispatches()
    record, claimed = restored.begin(identity(), str(uuid4()))
    assert not claimed and record.outcome == ActionOutcome.RESULT_UNKNOWN
    with pytest.raises(UnresolvedAction):
        restored.begin(identity(fingerprint="modified-arguments"), str(uuid4()))
    assert restored.begin(identity(scope="explicit-new-request"), str(uuid4()))[1]


def test_identity_change_is_not_a_new_action_and_known_result_survives_restart(
    tmp_path,
):
    journal = ActionJournal(ActionStore(tmp_path / "actions.sqlite3"))
    first, _ = journal.begin(identity(), str(uuid4()))
    with pytest.raises(MCPFailure, match="action_identity_changed"):
        journal.begin(identity(definition_digest="changed"), str(uuid4()))
    journal.finish(
        first.execution_id, ActionOutcome.APPLIED, projection={"outcome": "succeeded"}
    )
    journal.detach_dispatches()
    late = journal.finish(first.execution_id, ActionOutcome.CANCELLED)
    assert late.outcome == ActionOutcome.APPLIED
    previous, claimed = journal.begin(identity(), str(uuid4()))
    assert not claimed and previous.projection == {"outcome": "succeeded"}


def test_cancel_request_does_not_claim_external_stop(tmp_path):
    journal = ActionJournal(ActionStore(tmp_path / "actions.sqlite3"))
    record, _ = journal.begin(identity(), str(uuid4()))
    journal.finish(record.execution_id, ActionOutcome.RUNNING, task_id="opaque-job")
    requested = journal.request_cancel(record.execution_id)
    assert requested.outcome == ActionOutcome.RUNNING and requested.cancel_requested
    assert journal.pending()[0].task_id == "opaque-job"
    journal.finish(record.execution_id, ActionOutcome.CANCEL_REQUESTED)
    journal.detach_dispatches()
    assert journal.get(record.execution_id).outcome == ActionOutcome.CANCEL_REQUESTED
    assert (
        journal.finish(record.execution_id, ActionOutcome.CANCELLED).outcome
        == ActionOutcome.CANCELLED
    )
    assert not journal.pending()
