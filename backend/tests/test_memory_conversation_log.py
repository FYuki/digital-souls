import importlib
from pathlib import Path


def _use_temp_database(monkeypatch, module, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(module, "DATA_DIR", data_dir)
    monkeypatch.setattr(module, "DB_PATH", data_dir / "conversations.db")


class TestConversationLog:
    def test_save_message_persists_required_fields(self, tmp_path, monkeypatch):
        conversation_log = importlib.import_module("app.memory.conversation_log")

        _use_temp_database(monkeypatch, conversation_log, tmp_path)

        saved = conversation_log.save_message("miori", "user", "前回の話を覚えて")
        records = conversation_log.get_messages("miori")

        assert len(records) == 1
        assert records[0].id == saved.id
        assert records[0].character == "miori"
        assert records[0].role == "user"
        assert records[0].content == "前回の話を覚えて"
        assert records[0].timestamp == saved.timestamp

    def test_get_messages_filters_by_character(self, tmp_path, monkeypatch):
        conversation_log = importlib.import_module("app.memory.conversation_log")

        _use_temp_database(monkeypatch, conversation_log, tmp_path)

        conversation_log.save_message("miori", "user", "光織の記憶")
        conversation_log.save_message("akira", "user", "別キャラクターの記憶")

        records = conversation_log.get_messages("miori")

        assert [record.content for record in records] == ["光織の記憶"]

    def test_get_messages_respects_limit(self, tmp_path, monkeypatch):
        conversation_log = importlib.import_module("app.memory.conversation_log")

        _use_temp_database(monkeypatch, conversation_log, tmp_path)
        conversation_log.save_message("miori", "user", "1件目")
        conversation_log.save_message("miori", "assistant", "2件目")
        conversation_log.save_message("miori", "user", "3件目")

        records = conversation_log.get_messages("miori", limit=2)

        assert [record.content for record in records] == ["2件目", "3件目"]
