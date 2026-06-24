# Test Report

## Runtime Evidence

### Completion Condition 1: memory is persisted

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 1.91s`
- Evidence: `TestRagRuntimeEvidence.test_real_chat_store_chroma_query_and_prompt_injection_reach_llm` posts a chat message through `/chat`, then reads the real Chroma `PersistentClient` collection with `query_memories()` and asserts that the stored conversation content is returned.

### Completion Condition 2: retrieved memory reaches the LLM prompt

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 1.91s`
- Evidence: `TestRagRuntimeEvidence.test_real_chat_store_chroma_query_and_prompt_injection_reach_llm` uses the real Ollama embeddings API and real Chroma query path, captures the LLM call, and asserts that the augmented system prompt contains `過去の記憶:` and the retrieved stored memory.

### Completion Condition 3: RAG can be disabled

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_embedder.py backend/tests/test_chat.py backend/tests/test_chat_integration.py -q`
- Result: `27 passed, 1 warning in 0.16s`
- Evidence: `TestChatEndpoint.test_rag_disabled_does_not_resolve_memory_policy_or_record` verifies the disabled runtime path through `/chat` with `RAG_ENABLED=false`.

### Completion Condition 4: storage failure fallback

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 1.91s`
- Evidence: `TestRagRuntimeEvidence.test_real_storage_failure_chat_continues_and_failed_memory_is_written` injects a Chroma `add` failure while keeping the chat flow through `/chat`, asserts the chat response remains `200`, and verifies the failed user memory is written to `failed-memories.jsonl`.

### Embedding model environment override

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_embedder.py backend/tests/test_chat.py backend/tests/test_chat_integration.py -q`
- Result: `27 passed, 1 warning in 0.16s`
- Evidence: `TestEmbedder.test_embed_text_uses_embedding_model_environment_override` verifies `OLLAMA_EMBEDDING_MODEL` is passed to the Ollama embeddings request.

### Memory policy config refresh

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_rag_service.py backend/tests/test_runtime_contract.py -q`
- Result: `50 passed, 1 warning in 0.17s`
- Evidence: `TestMemoryPolicyConfiguration.test_same_config_path_update_is_reflected_without_process_restart` rewrites the same `memory_policy.json` path and verifies the new sensitive term is used while the old term is no longer matched. `TestMemoryPolicyConfiguration.test_missing_rag_service_limit_raises_instead_of_runtime_fallback` verifies `services.rag_service.max_retrieved_memories` is validated at config load time. `TestMemoryPolicyConfiguration.test_additional_service_sections_are_allowed_as_override_objects` verifies non-`rag_service` service override objects are accepted. `TestMemoryPolicyConfiguration.test_default_sensitive_terms_cover_cautious_policy_categories` verifies cautious information categories are treated as sensitive and skipped for both lookup and storage enqueue.

### WebSocket RAG async persistence and runtime wiring

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py -q`
- Result: `25 passed, 1 warning in 0.24s`
- Evidence: `TestChatServiceErrorContract.test_create_chat_session_does_not_require_event_loop_argument` verifies the WebSocket session factory no longer exposes event loop wiring in its public call contract. `TestChatServiceErrorContract.test_generate_chat_reply_does_not_expose_task_queue_argument` verifies the HTTP reply function no longer exposes private task queue wiring in its public call contract. `TestWebSocketFlowIntegration.test_rag_search_injection_and_recording_flow_through_websocket` sends messages through `/ws/miori` with `RAG_ENABLED=true` and verifies the real `rag_service` prompt-injection and record flow. `TestWebSocketFlowIntegration.test_rag_storage_failure_does_not_block_websocket_response_and_writes_fallback` blocks the storage task, receives the WebSocket response before unblocking it, then verifies the failed memory JSONL fallback is written.

### Chat/WebSocket RAG regression subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat.py backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py backend/tests/test_chat_integration.py backend/tests/test_memory_rag_runtime_evidence.py backend/tests/test_runtime_contract.py -q`
- Result: `70 passed, 1 warning in 1.22s`
- Evidence: The combined chat, WebSocket, runtime evidence, and runtime contract subset passed after the shared async task queue change.

### Regression suite

- Command: `backend/.venv/bin/pytest -q backend/tests`
- Result: `165 passed, 1 warning in 1.44s`
- Evidence: Full backend test suite passed after the memory policy refresh, service config surface, sensitive term coverage, RAG fallback boundary, chat service integration, WebSocket async persistence coverage, documentation, and evidence report fixes.

### Build check

- Command: `backend/.venv/bin/python -m compileall -q backend/app backend/tests`
- Result: success with no output.
- Command: `backend/.venv/bin/mypy --config-file backend/mypy.ini backend/app`
- Result: `Success: no issues found`

### Ollama Model Availability

- Command: `ollama list`
- Result: `nomic-embed-text:latest` is present.
