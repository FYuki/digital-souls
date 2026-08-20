import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.privacy_test_support import policy_config


_CHROMA_PATH = Path("/test/runtime-data/chroma")


def _resolved_policy():
    memory_policy = importlib.import_module("app.memory.memory_policy")
    return memory_policy.resolved_memory_policy()


class TestRagServicePrompt:
    def test_retrieve_prompt_memories_returns_retrieved_memories(
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

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "前回は?",
            _resolved_policy(),
            chroma_path=_CHROMA_PATH,
        )

        assert [memory.content for memory in memories] == [
            "前回は畑の土壌について話した",
            "雨量を確認した",
        ]
        assert [memory.role for memory in memories] == ["user", "assistant"]
        rag_service.query_memories.assert_called_once_with(
            "miori", [0.1], n_results=5, chroma_path=_CHROMA_PATH
        )

    def test_retrieve_prompt_memories_uses_passed_policy_once(
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

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "前回は?",
            policy,
            chroma_path=_CHROMA_PATH,
        )

        assert len(memories) == 2
        rag_service.query_memories.assert_called_once_with(
            "miori", [0.1], n_results=2, chroma_path=_CHROMA_PATH
        )
        rag_service.rag_service_policy.assert_called_once_with(policy)

    def test_retrieve_prompt_memories_returns_empty_when_search_fails(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(side_effect=RuntimeError))
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "前回は?",
            _resolved_policy(),
            chroma_path=_CHROMA_PATH,
        )

        assert memories == ()
        rag_service.query_memories.assert_not_called()

    def test_retrieve_prompt_memories_returns_empty_on_contract_validation_errors(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=ValueError("invalid embedding response")),
        )
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "前回は?",
            _resolved_policy(),
            chroma_path=_CHROMA_PATH,
        )

        assert memories == ()
        rag_service.query_memories.assert_not_called()

    def test_retrieve_prompt_memories_returns_empty_when_query_contract_fails(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
        monkeypatch.setattr(
            rag_service,
            "query_memories",
            MagicMock(side_effect=ValueError("invalid query response")),
        )

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "前回は?",
            _resolved_policy(),
            chroma_path=_CHROMA_PATH,
        )

        assert memories == ()
        rag_service.query_memories.assert_called_once()

    def test_retrieve_prompt_memories_skips_sensitive_query_embedding(
        self, monkeypatch
    ):
        rag_service = importlib.import_module("app.memory.rag_service")

        monkeypatch.setattr(rag_service, "embed_text", MagicMock())
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            "APIキーはabcです",
            _resolved_policy(),
            chroma_path=_CHROMA_PATH,
        )

        assert memories == ()
        rag_service.embed_text.assert_not_called()
        rag_service.query_memories.assert_not_called()


def _write_memory_policy_config(config_path, common, services):
    config = policy_config()
    config["common"] = common
    config["services"] = services
    config_path.write_text(
        json.dumps(
            config,
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
        policy = memory_policy.resolved_memory_policy()
        monkeypatch.setattr(rag_service, "embed_text", MagicMock())
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        memories = rag_service.retrieve_prompt_memories(
            "miori",
            sensitive_content,
            policy,
            chroma_path=_CHROMA_PATH,
        )
        assert memories == ()
        assert memory_policy.contains_sensitive_memory(sensitive_content, policy)
        rag_service.embed_text.assert_not_called()
        rag_service.query_memories.assert_not_called()

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
