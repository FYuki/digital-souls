"""Partial deletion contracts against the existing isolated PostgreSQL fixture."""

from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import sql

from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

from . import test_postgres_stores
from . import turn_deletion_contracts as contracts
from .memory_confirmation_contracts import Harness, make_harness
from .test_postgres_stores import BINDING, Stores
from .turn_deletion_contracts import Storage

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores

test_deleted_tail_revision_is_never_reused = contracts.test_deleted_tail_revision_is_never_reused
test_retained_accepted_confirmation_stays_ineligible_after_partial_delete = (
    contracts.test_retained_accepted_confirmation_stays_ineligible_after_partial_delete
)

test_deletion_scopes_preserve_only_untargeted_history_and_memory = (
    contracts.test_deletion_scopes_preserve_only_untargeted_history_and_memory
)
test_whole_conversation_deletion_preserves_existing_notification_contract = (
    contracts.test_whole_conversation_deletion_preserves_existing_notification_contract
)
test_later_saved_and_new_user_turns_allow_record_registration = (
    contracts.test_later_saved_and_new_user_turns_allow_record_registration
)
test_partial_deletion_does_not_admit_retained_private_or_excluded_source = (
    contracts.test_partial_deletion_does_not_admit_retained_private_or_excluded_source
)
test_tool_call_result_chain_expands_deletion_in_both_directions = (
    contracts.test_tool_call_result_chain_expands_deletion_in_both_directions
)
test_tool_identifiers_in_content_do_not_expand_deletion = (
    contracts.test_tool_identifiers_in_content_do_not_expand_deletion
)
test_deleted_request_retry_is_rejected_after_restart = (
    contracts.test_deleted_request_retry_is_rejected_after_restart
)
test_partial_deletion_rejects_same_inflight_completion = (
    contracts.test_partial_deletion_rejects_same_inflight_completion
)
test_partial_deletion_rejects_same_inflight_http_completion = (
    contracts.test_partial_deletion_rejects_same_inflight_http_completion
)
test_partial_deletion_invalidates_same_prepared_memory_context = (
    contracts.test_partial_deletion_invalidates_same_prepared_memory_context
)
test_remaining_dates_confirmation_states_and_receipts_survive_restart = (
    contracts.test_remaining_dates_confirmation_states_and_receipts_survive_restart
)
test_partial_notification_is_durable_scoped_and_contains_only_identifiers = (
    contracts.test_partial_notification_is_durable_scoped_and_contains_only_identifiers
)
test_invalid_deletion_leaves_all_state_unchanged = (
    contracts.test_invalid_deletion_leaves_all_state_unchanged
)
test_partial_deletion_remains_available_after_consent_revocation = (
    contracts.test_partial_deletion_remains_available_after_consent_revocation
)
test_failed_delete_rolls_back_revision_history_memory_and_allows_retry = (
    contracts.test_failed_delete_rolls_back_revision_history_memory_and_allows_retry
)
test_natural_language_does_not_delete_history = (
    contracts.test_natural_language_does_not_delete_history
)
test_same_tool_name_with_different_ids_does_not_join_independent_turns = (
    contracts.test_same_tool_name_with_different_ids_does_not_join_independent_turns
)
test_multiple_calls_and_results_are_deleted_together = (
    contracts.test_multiple_calls_and_results_are_deleted_together
)
test_invalid_deletion_input_is_rejected_without_changes = (
    contracts.test_invalid_deletion_input_is_rejected_without_changes
)
test_partial_deletion_cannot_change_another_binding = (
    contracts.test_partial_deletion_cannot_change_another_binding
)
test_repeated_partial_deletion_is_rejected_without_second_revision_change = (
    contracts.test_repeated_partial_deletion_is_rejected_without_second_revision_change
)
test_tombstone_keeps_request_identifier_without_content_or_fingerprint = (
    contracts.test_tombstone_keeps_request_identifier_without_content_or_fingerprint
)


@pytest.fixture
def storage(stores: Stores) -> Iterator[Storage]:
    def reopen() -> Harness:
        database = PostgresDatabase(stores.config)
        return make_harness(PostgresHistory(database), PostgresMemoryRecords(database))

    def query(statement: str, parameters: tuple[object, ...]) -> list[tuple[Any, ...]]:
        with stores.database.transaction(BINDING) as db:
            cursor = db.execute(statement.replace("?", "%s"), parameters)
            return cursor.fetchall() if cursor.description is not None else []

    def reject_delete() -> None:
        query(
            "CREATE FUNCTION reject_turn_delete() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'synthetic'; END $$",
            (),
        )
        query(
            "CREATE TRIGGER reject_turn_delete BEFORE DELETE ON turns "
            "FOR EACH ROW EXECUTE FUNCTION reject_turn_delete()",
            (),
        )

    def allow_delete() -> None:
        query("DROP TRIGGER reject_turn_delete ON turns", ())
        query("DROP FUNCTION reject_turn_delete()", ())

    def stored_values() -> tuple[object, ...]:
        with stores.database.transaction(BINDING) as db:
            tables = db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s",
                (stores.config.schema_name,),
            ).fetchall()
            return tuple(
                value
                for (table,) in tables
                for row in db.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
                for value in row
            )

    value = make_harness(stores.history, stores.records)
    with value.http:
        yield Storage(value, reopen, query, reject_delete, allow_delete, stored_values)
