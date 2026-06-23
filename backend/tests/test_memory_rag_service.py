import importlib
import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@dataclass
class SavedRecord:
    id: int
    character: str
    role: str
    content: str
    timestamp: str


def _resolved_policy():
    memory_policy = importlib.import_module("app.memory.memory_policy")
    return memory_policy.resolved_memory_policy()


class TestRagServicePrompt:
    def test_build_augmented_system_prompt_appends_retrieved_memories(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
        monkeypatch.setattr(
            rag_service,
            "query_memories",
            MagicMock(
                return_value=[
                    rag_service.MemorySearchResult(
                        content="前回は畑の土壌について話した",
                        timestamp="2026-06-20T00:00:00+00:00",
                        role="user",
                    ),
                    rag_service.MemorySearchResult(
                        content="雨量を確認した",
                        timestamp="2026-06-21T00:00:00+00:00",
                        role="assistant",
                    ),
                ]
            ),
        )

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "前回は?",
            "基本人格",
            _resolved_policy(),
        )

        assert prompt.startswith("基本人格")
        assert "[2026-06-20T00:00:00+00:00] (user)" in prompt
        assert "前回は畑の土壌について話した" in prompt
        assert "[2026-06-21T00:00:00+00:00] (assistant)" in prompt
        assert "雨量を確認した" in prompt
        rag_service.query_memories.assert_called_once_with("miori", [0.1], n_results=5)

    def test_build_augmented_system_prompt_uses_passed_policy_once(
        self, monkeypatch, tmp_path
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {"rag_service": {"max_retrieved_memories": 2}},
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )
        policy = memory_policy.resolved_memory_policy()

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
        monkeypatch.setattr(
            rag_service,
            "query_memories",
            MagicMock(
                return_value=[
                    rag_service.MemorySearchResult(
                        content="前回は畑の土壌について話した",
                        timestamp="2026-06-20T00:00:00+00:00",
                        role="user",
                    ),
                    rag_service.MemorySearchResult(
                        content="雨量を確認した",
                        timestamp="2026-06-21T00:00:00+00:00",
                        role="assistant",
                    ),
                ]
            ),
        )
        monkeypatch.setattr(
            rag_service,
            "rag_service_policy",
            MagicMock(wraps=rag_service.rag_service_policy),
        )

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "前回は?",
            "基本人格",
            policy,
        )

        assert prompt.startswith("基本人格")
        rag_service.query_memories.assert_called_once_with("miori", [0.1], n_results=2)
        rag_service.rag_service_policy.assert_called_once_with(policy)

    def test_build_augmented_system_prompt_skips_rag_when_search_fails(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(side_effect=RuntimeError))
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "前回は?",
            "基本人格",
            _resolved_policy(),
        )

        assert prompt == "基本人格"
        rag_service.query_memories.assert_not_called()

    def test_build_augmented_system_prompt_raises_contract_validation_errors(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=ValueError("invalid embedding response")),
        )
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        with pytest.raises(ValueError, match="invalid embedding response"):
            rag_service.build_augmented_system_prompt(
                "miori",
                "前回は?",
                "基本人格",
                _resolved_policy(),
            )

        rag_service.query_memories.assert_not_called()

    def test_build_augmented_system_prompt_skips_sensitive_query_embedding(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock())
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "APIキーはabcです",
            "基本人格",
            _resolved_policy(),
        )

        assert prompt == "基本人格"
        rag_service.embed_text.assert_not_called()
        rag_service.query_memories.assert_not_called()


class TestRagServiceRecording:
    def test_record_chat_turn_saves_both_roles_and_enqueues_user_memory_record(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_message = "農業日誌: トマトに水やり"
        assistant_reply = "作業ログとして保存しました: トマトに水やり"
        user_record = SavedRecord(
            1,
            "miori",
            "user",
            user_message,
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            2,
            "miori",
            "assistant",
            assistant_reply,
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_message,
            assistant_reply,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_args_list[0].args == (
            "miori",
            "user",
            user_message,
        )
        assert rag_service.save_message.call_args_list[1].args == (
            "miori",
            "assistant",
            assistant_reply,
        )
        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_chat_turn_does_not_enqueue_temporary_chat_as_long_term_memory(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_record = SavedRecord(
            3,
            "miori",
            "user",
            "今日は眠いね",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            4,
            "miori",
            "assistant",
            "そうですね",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            "今日は眠いね",
            "そうですね",
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 2
        background_tasks.add_task.assert_not_called()

    def test_background_store_dumps_failed_record_for_retry(self, tmp_path, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")

        failed_path = tmp_path / "failed-memories.jsonl"
        record = SavedRecord(
            7,
            "miori",
            "user",
            "保存したい内容",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(rag_service, "FAILED_MEMORY_LOG_PATH", failed_path)
        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.5]))
        monkeypatch.setattr(rag_service, "add_memory", MagicMock(side_effect=RuntimeError))

        rag_service._embed_and_store(record)

        dumped = json.loads(failed_path.read_text(encoding="utf-8").splitlines()[0])
        assert dumped == {
            "id": 7,
            "character": "miori",
            "role": "user",
            "content": "保存したい内容",
            "timestamp": "2026-06-23T00:00:00+00:00",
        }

    def test_background_store_raises_contract_validation_errors(
        self, tmp_path, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        failed_path = tmp_path / "failed-memories.jsonl"
        record = SavedRecord(
            31,
            "miori",
            "user",
            "農業日誌: invalid payload",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(rag_service, "FAILED_MEMORY_LOG_PATH", failed_path)
        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=ValueError("invalid embedding response")),
        )
        monkeypatch.setattr(rag_service, "add_memory", MagicMock())

        with pytest.raises(ValueError, match="invalid embedding response"):
            rag_service._embed_and_store(record)

        assert not failed_path.exists()
        rag_service.add_memory.assert_not_called()

    def test_background_store_does_not_recheck_candidate_policy(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")
        record = SavedRecord(
            29,
            "miori",
            "assistant",
            "worker receives records after candidate filtering",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.5]))
        monkeypatch.setattr(rag_service, "add_memory", MagicMock())
        monkeypatch.setattr(
            rag_service,
            "is_long_term_memory_candidate",
            MagicMock(side_effect=AssertionError("worker must not recheck policy")),
        )

        rag_service._embed_and_store(record)

        rag_service.add_memory.assert_called_once()
        rag_service.is_long_term_memory_candidate.assert_not_called()

    def test_record_chat_turn_skips_password_memory_policy_terms(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            8,
            "miori",
            "user",
            "パスワードはabc",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            9,
            "miori",
            "assistant",
            "受け取りました",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        background_tasks.add_task.assert_not_called()

    def test_record_chat_turn_skips_temporary_chat_without_memory_value(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            23,
            "miori",
            "user",
            "今日は眠いね",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            24,
            "miori",
            "assistant",
            "そうですね",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        background_tasks.add_task.assert_not_called()

    def test_record_chat_turn_does_not_enqueue_uncategorized_conversation(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_record = SavedRecord(
            10,
            "miori",
            "user",
            "この料理は材料が多いね",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            11,
            "miori",
            "assistant",
            "手順を整理しましょう",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 2
        background_tasks.add_task.assert_not_called()

    def test_record_chat_turn_enqueues_recipe_memory_category(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_record = SavedRecord(
            13,
            "miori",
            "user",
            "レシピ: トマトソースの材料はトマトと塩",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            14,
            "miori",
            "assistant",
            "レシピ記録として保存しました",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 2
        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_chat_turn_does_not_enqueue_policy_candidate_natural_records(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        records = [
            SavedRecord(
                15,
                "miori",
                "user",
                "トマトに水やりした",
                "2026-06-23T00:00:00+00:00",
            ),
            SavedRecord(
                16,
                "miori",
                "assistant",
                "前回の追肥は6月上旬です",
                "2026-06-23T00:00:01+00:00",
            ),
            SavedRecord(
                17,
                "miori",
                "user",
                "材料はトマトと塩",
                "2026-06-23T00:00:02+00:00",
            ),
            SavedRecord(
                18,
                "miori",
                "assistant",
                "手順は煮詰めて味を調整します",
                "2026-06-23T00:00:03+00:00",
            ),
        ]
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=records),
        )

        rag_service.record_chat_turn(
            "miori",
            records[0].content,
            records[1].content,
            background_tasks,
            _resolved_policy(),
        )
        rag_service.record_chat_turn(
            "miori",
            records[2].content,
            records[3].content,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 4
        background_tasks.add_task.assert_not_called()

    def test_record_chat_turn_skips_assistant_reply_without_user_save_request(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            11,
            "miori",
            "user",
            "保存したい: トマトに水やり",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            12,
            "miori",
            "assistant",
            "保存しました: トマトに水やり",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_chat_turn_logs_sensitive_content_without_chroma_enqueue(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_record = SavedRecord(
            19,
            "miori",
            "user",
            "APIキーはabcです",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            20,
            "miori",
            "assistant",
            "受け取りました",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 2
        background_tasks.add_task.assert_not_called()

    def test_record_chat_turn_logs_negative_save_request_without_chroma_enqueue(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()
        user_record = SavedRecord(
            21,
            "miori",
            "user",
            "この内容は保存しないで",
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            22,
            "miori",
            "assistant",
            "保存しません",
            "2026-06-23T00:00:01+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )

        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            _resolved_policy(),
        )

        assert rag_service.save_message.call_count == 2
        background_tasks.add_task.assert_not_called()


def _write_memory_policy_config(config_path, common, services):
    config_path.write_text(
        json.dumps(
            {
                "common": common,
                "services": services,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TestMemoryPolicyConfiguration:
    def test_sensitive_terms_are_loaded_from_config_file(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": ["vault-token"],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {"rag_service": {"max_retrieved_memories": 5}},
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )
        policy = memory_policy.resolved_memory_policy()

        assert memory_policy.contains_sensitive_memory("vault-token は秘密", policy)
        assert not memory_policy.contains_sensitive_memory(
            "password は既定値ではない",
            policy,
        )

    @pytest.mark.parametrize(
        "sensitive_content",
        [
            "個人情報を保存して",
            "健康情報を保存して",
            "金銭情報を保存して",
            "住所を保存して",
            "連絡先を保存して",
            "他者のプライベート情報を保存して",
        ],
    )
    def test_default_sensitive_terms_cover_cautious_policy_categories(
        self, sensitive_content, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        memory_policy = importlib.import_module("app.memory.memory_policy")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            32,
            "miori",
            "user",
            sensitive_content,
            "2026-06-23T00:00:00+00:00",
        )
        assistant_record = SavedRecord(
            33,
            "miori",
            "assistant",
            "受け取りました",
            "2026-06-23T00:00:01+00:00",
        )
        policy = memory_policy.resolved_memory_policy()
        monkeypatch.setattr(
            rag_service,
            "save_message",
            MagicMock(side_effect=[user_record, assistant_record]),
        )
        monkeypatch.setattr(rag_service, "embed_text", MagicMock())
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            sensitive_content,
            "基本人格",
            policy,
        )
        rag_service.record_chat_turn(
            "miori",
            user_record.content,
            assistant_record.content,
            background_tasks,
            policy,
        )

        assert prompt == "基本人格"
        assert memory_policy.contains_sensitive_memory(sensitive_content, policy)
        rag_service.embed_text.assert_not_called()
        rag_service.query_memories.assert_not_called()
        background_tasks.add_task.assert_not_called()

    def test_do_not_store_terms_are_loaded_from_config_file(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": ["never-store"],
                "explicit_memory_terms": ["save-me"],
                "long_term_memory_markers": [],
            },
            {"rag_service": {"max_retrieved_memories": 5}},
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )
        policy = memory_policy.resolved_memory_policy()

        assert not memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                27,
                "miori",
                "user",
                "save-me never-store this",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                28,
                "miori",
                "user",
                "save-me ordinary memory",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )

    def test_rag_service_override_replaces_common_explicit_terms(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": [],
                "explicit_memory_terms": ["common-save"],
                "long_term_memory_markers": [],
            },
            {
                "rag_service": {
                    "explicit_memory_terms": ["service-save"],
                    "max_retrieved_memories": 5,
                }
            },
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )
        policy = memory_policy.resolved_memory_policy()

        assert memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                23,
                "miori",
                "user",
                "service-save peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert not memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                24,
                "miori",
                "user",
                "common-save peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )

    def test_long_term_markers_are_loaded_from_config_file(self, tmp_path, monkeypatch):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": ["Journal*"],
            },
            {"rag_service": {"max_retrieved_memories": 5}},
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )
        policy = memory_policy.resolved_memory_policy()

        assert memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                25,
                "miori",
                "user",
                "Journal* peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert not memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                26,
                "miori",
                "user",
                "レシピ: peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )

    def test_missing_config_file_raises_instead_of_using_hardcoded_fallback(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            tmp_path / "missing.json",
            raising=False,
        )

        with pytest.raises(FileNotFoundError):
            memory_policy.resolved_memory_policy()

    def test_missing_rag_service_limit_raises_instead_of_runtime_fallback(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {"rag_service": {}},
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )

        with pytest.raises(ValueError, match="max_retrieved_memories"):
            memory_policy.resolved_memory_policy()

    def test_additional_service_sections_are_allowed_as_override_objects(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": [],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {
                "rag_service": {"max_retrieved_memories": 3},
                "embedder": {"batch_size": 16},
                "chroma_store": {"collection_prefix": "test"},
            },
        )
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )

        policy = memory_policy.resolved_memory_policy()

        assert policy.rag_service.max_retrieved_memories == 3
        assert policy.services["embedder"] == {"batch_size": 16}
        assert policy.services["chroma_store"] == {"collection_prefix": "test"}

    def test_same_config_path_update_is_reflected_without_process_restart(
        self, tmp_path, monkeypatch
    ):
        memory_policy = importlib.import_module("app.memory.memory_policy")
        config_path = tmp_path / "memory_policy.json"
        monkeypatch.setattr(
            memory_policy,
            "MEMORY_POLICY_CONFIG_PATH",
            config_path,
            raising=False,
        )

        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": ["alpha-secret"],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {"rag_service": {"max_retrieved_memories": 5}},
        )
        first_policy = memory_policy.resolved_memory_policy()
        assert memory_policy.contains_sensitive_memory("alpha-secret", first_policy)

        _write_memory_policy_config(
            config_path,
            {
                "sensitive_terms": ["beta-secret"],
                "do_not_store_terms": [],
                "explicit_memory_terms": [],
                "long_term_memory_markers": [],
            },
            {"rag_service": {"max_retrieved_memories": 5}},
        )

        second_policy = memory_policy.resolved_memory_policy()
        assert memory_policy.contains_sensitive_memory("beta-secret", second_policy)
        assert not memory_policy.contains_sensitive_memory("alpha-secret", second_policy)
