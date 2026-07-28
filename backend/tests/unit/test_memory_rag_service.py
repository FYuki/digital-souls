import importlib
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest


@dataclass
class SavedRecord:
    id: str
    character: str
    role: str
    content: str
    timestamp: str


def _resolved_policy():
    memory_policy = importlib.import_module("app.memory.memory_policy")
    return memory_policy.resolved_memory_policy()


def _record_id(sequence: int) -> str:
    return f"00000000-0000-4000-8000-{sequence:012d}"


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

    def test_build_augmented_system_prompt_skips_rag_on_contract_validation_errors(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=ValueError("invalid embedding response")),
        )
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "前回は?",
            "基本人格",
            _resolved_policy(),
        )

        assert prompt == "基本人格"
        rag_service.query_memories.assert_not_called()

    def test_build_augmented_system_prompt_skips_rag_when_query_contract_fails(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
        monkeypatch.setattr(
            rag_service,
            "query_memories",
            MagicMock(side_effect=ValueError("invalid query response")),
        )

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            "前回は?",
            "基本人格",
            _resolved_policy(),
        )

        assert prompt == "基本人格"
        rag_service.query_memories.assert_called_once()

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
    def test_record_user_memory_candidate_requires_explicit_task_queue(self):
        rag_service = importlib.import_module("app.memory.rag_service")

        signature = inspect.signature(rag_service.record_user_memory_candidate)

        assert list(signature.parameters) == [
            "character",
            "user_message",
            "policy",
            "task_queue",
        ]
        assert not hasattr(rag_service, "_BACKGROUND_TASK_QUEUE")
        assert not hasattr(rag_service, "_configure_memory_task_queue")
        assert not hasattr(rag_service, "_clear_memory_task_queue")

    def test_record_user_memory_candidate_does_not_create_legacy_sqlite(
        self, tmp_path
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()

        rag_service.record_user_memory_candidate(
            "miori",
            "今日は晴れですね",
            _resolved_policy(),
            background_tasks,
        )

        assert not hasattr(rag_service, "DATA_DIR")
        assert not tmp_path.joinpath("conversations.db").exists()

    def test_record_user_memory_candidate_creates_and_enqueues_record(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        user_message = "農業日誌: トマトに水やり"
        user_record = SavedRecord(
            _record_id(1),
            "miori",
            "user",
            user_message,
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_message,
            _resolved_policy(),
            background_tasks,
        )

        assert rag_service.create_memory_candidate_record.call_args_list[0].args == (
            "miori",
            user_message,
        )
        assert rag_service.create_memory_candidate_record.call_count == 1
        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_user_memory_candidate_does_not_enqueue_temporary_chat(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        user_record = SavedRecord(
            _record_id(3),
            "miori",
            "user",
            "今日は眠いね",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            "今日は眠いね",
            _resolved_policy(),
            background_tasks,
        )

        assert rag_service.create_memory_candidate_record.call_count == 1
        background_tasks.add_task.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.HTTPError("network failure"),
            OSError("storage failure"),
            RuntimeError("runtime failure"),
            ValueError("contract failure"),
        ],
        ids=["http", "os", "runtime", "value"],
    )
    @pytest.mark.parametrize("failure_stage", ["embedding", "chroma"])
    def test_background_store_failure_is_metadata_only(
        self,
        tmp_path,
        monkeypatch,
        caplog,
        failure,
        failure_stage,
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        failed_path = tmp_path / "failed-memories.jsonl"
        record = SavedRecord(
            _record_id(7),
            "miori",
            "user",
            "ログへ残してはいけない本文",
            "2026-06-23T00:00:00+00:00",
        )
        embed_failure = failure if failure_stage == "embedding" else None
        store_failure = failure if failure_stage == "chroma" else None
        file_open = MagicMock()
        monkeypatch.setattr(Path, "open", file_open)
        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=embed_failure, return_value=[0.5]),
        )
        monkeypatch.setattr(
            rag_service,
            "add_memory",
            MagicMock(side_effect=store_failure),
        )

        with caplog.at_level(logging.WARNING, logger=rag_service.__name__):
            rag_service._embed_and_store(record)

        assert not failed_path.exists()
        file_open.assert_not_called()
        assert "ログへ残してはいけない本文" not in caplog.text
        assert _record_id(7) in caplog.text
        assert failure.__class__.__name__ in caplog.text
        if failure_stage == "embedding":
            rag_service.add_memory.assert_not_called()

    def test_background_store_does_not_recheck_candidate_policy(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")
        record = SavedRecord(
            _record_id(29),
            "miori",
            "user",
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

    def test_record_user_memory_candidate_skips_password_terms(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            "パスワードはabc",
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_not_called()
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_skips_temporary_chat_without_memory_value(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            _record_id(23),
            "miori",
            "user",
            "今日は眠いね",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_record.content,
            _resolved_policy(),
            background_tasks,
        )

        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_does_not_enqueue_uncategorized_content(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        user_record = SavedRecord(
            _record_id(10),
            "miori",
            "user",
            "この料理は材料が多いね",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_record.content,
            _resolved_policy(),
            background_tasks,
        )

        assert rag_service.create_memory_candidate_record.call_count == 1
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_enqueues_recipe_category(self, monkeypatch):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        user_record = SavedRecord(
            _record_id(13),
            "miori",
            "user",
            "レシピ: トマトソースの材料はトマトと塩",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_record.content,
            _resolved_policy(),
            background_tasks,
        )

        assert rag_service.create_memory_candidate_record.call_count == 1
        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_user_memory_candidate_does_not_enqueue_natural_records(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        records = [
            SavedRecord(
                _record_id(15),
                "miori",
                "user",
                "トマトに水やりした",
                "2026-06-23T00:00:00+00:00",
            ),
            SavedRecord(
                _record_id(17),
                "miori",
                "user",
                "材料はトマトと塩",
                "2026-06-23T00:00:02+00:00",
            ),
        ]
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(side_effect=records),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            records[0].content,
            _resolved_policy(),
            background_tasks,
        )
        rag_service.record_user_memory_candidate(
            "miori",
            records[1].content,
            _resolved_policy(),
            background_tasks,
        )

        assert rag_service.create_memory_candidate_record.call_count == 2
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_enqueues_one_user_record(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            _record_id(11),
            "miori",
            "user",
            "保存したい: トマトに水やり",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_record.content,
            _resolved_policy(),
            background_tasks,
        )

        assert background_tasks.add_task.call_count == 1
        assert background_tasks.add_task.call_args_list[0].args[1] == user_record

    def test_record_user_memory_candidate_skips_sensitive_content(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            "APIキーはabcです",
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_not_called()
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_skips_sensitive_user_message(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            "api key はabcです",
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_not_called()
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_uses_user_message_for_admission(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        user_record = SavedRecord(
            _record_id(35),
            "miori",
            "user",
            "今日は晴れですね",
            "2026-06-23T00:00:00+00:00",
        )
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(return_value=user_record),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            user_record.content,
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_called_once_with(
            "miori",
            user_record.content,
        )
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_skips_when_message_is_sensitive(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")
        background_tasks = MagicMock()
        monkeypatch.setattr(rag_service, "create_memory_candidate_record", MagicMock())

        rag_service.record_user_memory_candidate(
            "miori",
            "パスワードはabc",
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_not_called()
        background_tasks.add_task.assert_not_called()

    def test_record_user_memory_candidate_skips_negative_save_request(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        background_tasks = MagicMock()

        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(),
        )

        rag_service.record_user_memory_candidate(
            "miori",
            "この内容は保存しないで",
            _resolved_policy(),
            background_tasks,
        )

        rag_service.create_memory_candidate_record.assert_not_called()
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

    def test_non_storable_memory_reuses_sensitive_term_checker(self):
        memory_policy = importlib.import_module("app.memory.memory_policy")

        source = inspect.getsource(memory_policy.contains_non_storable_memory)

        assert "contains_sensitive_memory(content, policy)" in source
        assert "terms.sensitive_terms" not in source

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
        policy = memory_policy.resolved_memory_policy()
        monkeypatch.setattr(
            rag_service,
            "create_memory_candidate_record",
            MagicMock(),
        )
        monkeypatch.setattr(rag_service, "embed_text", MagicMock())
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        prompt = rag_service.build_augmented_system_prompt(
            "miori",
            sensitive_content,
            "基本人格",
            policy,
        )
        rag_service.record_user_memory_candidate(
            "miori",
            sensitive_content,
            policy,
            background_tasks,
        )

        assert prompt == "基本人格"
        assert memory_policy.contains_sensitive_memory(sensitive_content, policy)
        rag_service.embed_text.assert_not_called()
        rag_service.query_memories.assert_not_called()
        rag_service.create_memory_candidate_record.assert_not_called()
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
                _record_id(27),
                "miori",
                "user",
                "save-me never-store this",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                _record_id(28),
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
                _record_id(23),
                "miori",
                "user",
                "service-save peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert not memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                _record_id(24),
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
                _record_id(25),
                "miori",
                "user",
                "Journal* peppers",
                "2026-06-23T00:00:00+00:00",
            ),
            policy,
        )
        assert not memory_policy.is_long_term_memory_candidate(
            SavedRecord(
                _record_id(26),
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

    def test_additional_service_sections_are_accepted_without_public_policy_surface(
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
        assert not hasattr(policy, "services")

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
