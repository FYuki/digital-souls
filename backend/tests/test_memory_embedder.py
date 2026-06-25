import importlib
from unittest.mock import MagicMock

import pytest


def _embedding_response(body: object) -> MagicMock:
    response = MagicMock()
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


class TestEmbedder:
    def test_embed_text_requests_default_nomic_embed_text_embedding(self, monkeypatch):
        embedder = importlib.import_module("app.memory.embedder")

        monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.example")
        monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
        mock_post = MagicMock(
            return_value=_embedding_response({"embedding": [0.1, 0.2, 0.3]})
        )
        monkeypatch.setattr(embedder.httpx, "post", mock_post)

        embedding = embedder.embed_text("検索したい発話")

        assert embedding == [0.1, 0.2, 0.3]
        mock_post.assert_called_once()
        assert mock_post.call_args.args == ("http://ollama.example/api/embeddings",)
        assert mock_post.call_args.kwargs["json"] == {
            "model": "nomic-embed-text:latest",
            "prompt": "検索したい発話",
        }

    def test_embed_text_uses_embedding_model_environment_override(
        self, monkeypatch
    ):
        embedder = importlib.import_module("app.memory.embedder")

        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "mxbai-embed-large:latest")
        mock_post = MagicMock(return_value=_embedding_response({"embedding": [0.4]}))
        monkeypatch.setattr(embedder.httpx, "post", mock_post)

        embedding = embedder.embed_text("検索したい発話")

        assert embedding == [0.4]
        assert mock_post.call_args.kwargs["json"] == {
            "model": "mxbai-embed-large:latest",
            "prompt": "検索したい発話",
        }

    def test_embed_text_raises_when_embedding_field_is_missing(self, monkeypatch):
        embedder = importlib.import_module("app.memory.embedder")

        mock_post = MagicMock(return_value=_embedding_response({"model": "nomic"}))
        monkeypatch.setattr(embedder.httpx, "post", mock_post)

        with pytest.raises(ValueError):
            embedder.embed_text("検索したい発話")
