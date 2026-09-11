"""接続専有・原子的削除・設定revisionの保存契約。実credentialは使わない。"""

import asyncio
import json
import sqlite3
import stat

import pytest
from pydantic import ValidationError

from app.addon_admin.connection_models import ConnectionInput, CredentialInput
from app.addon_admin.connections import ConnectionStore
from app.external_mcp.models import MCPFailure
from tests.external_mcp_test_support import manifest
from app.external_mcp import Connection


def spec(name="テスト連携", **settings):
    return ConnectionInput.model_validate(
        {
            "display_name": name,
            "settings": {
                "transport": "streamable_http",
                "endpoint": "http://127.0.0.1:9100/mcp",
                "auth": "bearer",
                **settings,
            },
        }
    )


def token(value="synthetic-private-token"):
    return CredentialInput.model_validate({"token": value})


def store_at(tmp_path):
    return ConnectionStore(tmp_path / "mcp-admin" / "connections.sqlite3")


def test_exclusive_credentials_with_duplicate_values_survive_restart(tmp_path):
    store = store_at(tmp_path)
    first, second = store.create(spec()), store.create(spec("別接続"))
    assert first.connection.manifest["core_policy"]["enabled"] is False
    assert first.secret_ref != second.secret_ref
    store.credential(first.connection.id, token())
    store.credential(second.connection.id, token())
    assert asyncio.run(store.resolve(first.secret_ref)) == "synthetic-private-token"
    store.credential(first.connection.id, token("replacement-token"))
    assert asyncio.run(store.resolve(second.secret_ref)) == "synthetic-private-token"
    store.delete(first.connection.id)
    restored = store_at(tmp_path)
    assert len(restored.records()) == 1
    assert restored.get(second.connection.id).credential_set
    assert asyncio.run(restored.resolve(second.secret_ref)) == "synthetic-private-token"
    with pytest.raises(MCPFailure, match="secret_unavailable"):
        asyncio.run(restored.resolve(first.secret_ref))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_delete_failure_rolls_back_connection_and_credential(tmp_path):
    store = store_at(tmp_path)
    record = store.create(spec())
    store.credential(record.connection.id, token())
    with store.transaction() as db:
        db.execute("""CREATE TRIGGER reject_delete BEFORE INSERT ON removed_connections
            BEGIN SELECT RAISE(ABORT, 'synthetic_failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.delete(record.connection.id)
    assert store.get(record.connection.id).credential_set
    assert asyncio.run(store.resolve(record.secret_ref)) == "synthetic-private-token"
    with store.transaction() as db:
        db.execute("DROP TRIGGER reject_delete")
    store.delete(record.connection.id)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 0


def test_success_history_belongs_to_current_settings_and_credentials(tmp_path):
    store = store_at(tmp_path)
    record = store.create(spec())
    cid = record.connection.id
    assert store.checked(cid, record.revision, error_code=None, summary={"tools": []})
    renamed = store.update(cid, spec("表示名だけ変更"))
    assert renamed.revision == record.revision
    assert renamed.last_success_at
    changed = store.update(cid, spec(endpoint="http://127.0.0.1:9101/mcp"))
    assert changed.revision > renamed.revision
    assert changed.last_success_at is None
    assert not store.checked(cid, record.revision, error_code=None)
    store.checked(cid, changed.revision, error_code="connection_failed")
    assert store.get(cid).last_success_at is None
    store.checked(cid, changed.revision, error_code=None)
    store.checked(cid, changed.revision, error_code="connection_failed")
    assert store.get(cid).last_success_at
    credential = store.credential(cid, token())
    assert credential.revision > changed.revision
    assert credential.last_success_at is None
    assert not store.checked(cid, changed.revision, error_code=None)
    store.delete(cid)
    assert not store.checked(cid, credential.revision, error_code=None)


def test_input_cannot_supply_secret_refs_policies_or_raw_manifest(tmp_path):
    store = store_at(tmp_path)
    record = store.create(spec())
    for extra in (
        {"secret_ref": record.secret_ref},
        {"manifest": {}},
        {"token": "raw-private"},
        {"desired_enabled": True},
    ):
        with pytest.raises(ValidationError):
            ConnectionInput.model_validate({**spec().model_dump(), **extra})
    for extra in (
        {"secret_ref": record.secret_ref},
        {"connection_id": record.connection.id},
    ):
        with pytest.raises(ValidationError):
            CredentialInput.model_validate({"token": "private", **extra})
    assert "synthetic-private-token" not in repr(token())
    assert "synthetic-private-token" not in record.connection._json
    with pytest.raises(MCPFailure, match="unknown_connection"):
        store.credential("not-a-connection", token())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@example.com/mcp",
        "http://example.com/mcp",
        "https://example.com/mcp?token=secret",
        "file:///etc/passwd",
        "https://example.com/mcp#secret",
    ],
)
def test_url_constraints_are_enforced_by_existing_model(tmp_path, endpoint):
    with pytest.raises(MCPFailure):
        store_at(tmp_path).create(spec(endpoint=endpoint))


def test_stdio_typed_args_and_shell_text_validation(tmp_path):
    store = store_at(tmp_path)
    valid = ConnectionInput.model_validate(
        {
            "display_name": "ローカル",
            "settings": {
                "transport": "stdio",
                "command": "python",
                "args": ["-c", "print('hello')"],
            },
        }
    )
    record = store.create(valid)
    assert record.connection.manifest["connection"]["stdio"]["args"] == [
        "-c",
        "print('hello')",
    ]
    with pytest.raises(MCPFailure, match="credential_not_applicable"):
        store.credential(record.connection.id, token())
    for command in (
        "python server.py",
        "server | cat",
        "server > out",
        "$(server)",
        "server\nother",
    ):
        with pytest.raises(ValidationError):
            ConnectionInput.model_validate(
                {
                    "display_name": "ローカル",
                    "settings": {"transport": "stdio", "command": command},
                }
            )
    with pytest.raises(ValidationError):
        ConnectionInput.model_validate(
            {
                "display_name": "ローカル",
                "settings": {
                    "transport": "stdio",
                    "command": "server",
                    "env": {"TOKEN": "private"},
                },
            }
        )


def test_import_preserves_policy_and_does_not_restore_deleted_or_edited_config(
    tmp_path,
):
    store = store_at(tmp_path)
    original = Connection.from_manifest(
        manifest(
            transport="streamable_http",
            auth={"type": "bearer", "secret_ref": "OLD_SHARED_REF"},
            binding=True,
            trusted=True,
        )
    )
    store.import_connection(original, "既存接続", "legacy-token")
    imported = store.get(original.id)
    assert (
        imported.connection.manifest["core_policy"] == original.manifest["core_policy"]
    )
    assert (
        imported.connection.manifest["connection"]["trust"]
        == original.manifest["connection"]["trust"]
    )
    assert imported.secret_ref != "OLD_SHARED_REF"
    assert asyncio.run(store.resolve(imported.secret_ref)) == "legacy-token"
    store.update(original.id, spec("編集済み"))
    store.import_connection(original, "既存接続", "overwritten-token")
    assert store.get(original.id).spec.display_name == "編集済み"
    store.delete(original.id)
    store_at(tmp_path).import_connection(original, "既存接続", "legacy-token")
    assert store.records() == ()


def test_auth_none_removes_stored_credential_atomically(tmp_path):
    store = store_at(tmp_path)
    record = store.create(spec())
    store.credential(record.connection.id, token())
    updated = store.update(record.connection.id, spec(auth="none"))
    assert updated.credential_set is False
    with pytest.raises(MCPFailure, match="secret_unavailable"):
        asyncio.run(store.resolve(record.secret_ref))
    assert "synthetic-private-token" in store.private_values()
    assert "synthetic-private-token" not in json.dumps(updated.spec.model_dump())


def test_legacy_manifest_stdio_is_preserved_without_new_form_limits(tmp_path):
    value = manifest()
    value["connection"]["stdio"] = {
        "command": "/opt/legacy $tool",
        "args": ["line1\nline2", "x" * 9000] + ["argument"] * 129,
    }
    connection = Connection.from_manifest(value)
    store = store_at(tmp_path)
    store.import_connection(connection, "既存のstdio", None)
    restored = store_at(tmp_path).get(connection.id)
    assert (
        restored.connection.manifest["connection"]["stdio"]
        == value["connection"]["stdio"]
    )
    assert restored.spec.settings.args == value["connection"]["stdio"]["args"]
    with pytest.raises(ValidationError):
        ConnectionInput.model_validate(restored.spec.model_dump())


def test_reads_do_not_acquire_writer_lock_and_resolve_does_not_block_loop(tmp_path):
    store = store_at(tmp_path)
    record = store.create(spec())
    store.credential(record.connection.id, token())
    with store.transaction():
        # 同時にRESERVED lockがあってもreadは既存snapshotを参照できる。
        assert store.get(record.connection.id).credential_set
        assert len(store.records()) == 1

    async def run():
        blocker = sqlite3.connect(store.path)
        blocker.execute("BEGIN EXCLUSIVE")
        pending = asyncio.create_task(store.resolve(record.secret_ref))
        try:
            await asyncio.sleep(0.05)
            # SQLiteのlock待機中にもevent loopは進む。
            assert not pending.done()
        finally:
            blocker.rollback()
            blocker.close()
        assert await asyncio.wait_for(pending, 1) == "synthetic-private-token"

    asyncio.run(run())
