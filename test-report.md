# Test Report

## Runtime Evidence

### Completion Condition 1: memory is persisted

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 2.21s`
- Evidence: `TestRagRuntimeEvidence.test_real_chat_store_chroma_query_and_prompt_injection_reach_llm` posts a chat message through `/chat`, then reads the real Chroma `PersistentClient` collection with `query_memories()` and asserts that the stored conversation content is returned.

### Completion Condition 2: retrieved memory reaches the LLM prompt

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 2.21s`
- Evidence: `TestRagRuntimeEvidence.test_real_chat_store_chroma_query_and_prompt_injection_reach_llm` uses the real Ollama embeddings API and real Chroma query path, captures the LLM call, and asserts that the augmented system prompt contains `過去の記憶:` and the retrieved stored memory.

### Completion Condition 3: RAG can be disabled

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_embedder.py backend/tests/test_chat.py backend/tests/test_chat_integration.py -q`
- Result: `28 passed, 1 warning in 0.24s`
- Evidence: `TestChatEndpoint.test_rag_disabled_does_not_resolve_memory_policy_or_record` verifies the disabled runtime path through `/chat` with `RAG_ENABLED=false`.

### Completion Condition 4: storage failure fallback

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 2.21s`
- Evidence: `TestRagRuntimeEvidence.test_real_storage_failure_chat_continues_and_failed_memory_is_written` injects a Chroma `add` failure while keeping the chat flow through `/chat`, asserts the chat response remains `200`, and verifies the failed user memory is written to `failed-memories.jsonl`.

### Embedding model environment override

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_embedder.py backend/tests/test_chat.py backend/tests/test_chat_integration.py -q`
- Result: `28 passed, 1 warning in 0.24s`
- Evidence: `TestEmbedder.test_embed_text_uses_embedding_model_environment_override` verifies `OLLAMA_EMBEDDING_MODEL` is passed to the Ollama embeddings request.

### Memory policy config refresh

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_rag_service.py backend/tests/test_runtime_contract.py -q`
- Result: `65 passed, 1 warning in 0.13s`
- Evidence: `TestMemoryPolicyConfiguration.test_same_config_path_update_is_reflected_without_process_restart` rewrites the same `memory_policy.json` path and verifies the new sensitive term is used while the old term is no longer matched. `TestMemoryPolicyConfiguration.test_missing_rag_service_limit_raises_instead_of_runtime_fallback` verifies `services.rag_service.max_retrieved_memories` is validated at config load time. `TestMemoryPolicyConfiguration.test_additional_service_sections_are_accepted_without_public_policy_surface` verifies non-`rag_service` service override objects are accepted without exposing raw `services` on `MemoryPolicy`. `TestRuntimeConfiguration.test_memory_policy_does_not_expose_raw_service_sections` verifies the same public contract at runtime. `TestRuntimeConfiguration.test_chat_and_ws_routes_delegate_to_same_app_chat_service` verifies `/chat` and `/ws` delegate to the same `app.state.chat_service` boundary. `TestRuntimeConfiguration.test_main_lifespan_shuts_down_chat_service_task_queue_executor` verifies FastAPI lifespan shuts down the task queue executor. `TestRuntimeConfiguration.test_main_lifespan_cleans_runtime_when_config_resolution_fails` verifies startup failure during runtime config resolution still shuts down the executor and leaves no app-state service or public resolver behind. `TestRuntimeConfiguration.test_main_lifespan_owns_chat_service_state_and_cleans_it_up` verifies FastAPI app state exposes the chat service only during lifespan and removes it after shutdown. `TestRuntimeConfiguration.test_main_lifespan_registers_module_entrypoints_to_app_chat_service` verifies the module-level public entrypoints resolve the current lifespan-owned `app.state.chat_service` instance. `TestMemoryPolicyConfiguration.test_default_sensitive_terms_cover_cautious_policy_categories` verifies cautious information categories are treated as sensitive and skipped for both lookup and storage enqueue. `TestRagServicePrompt.test_build_augmented_system_prompt_skips_rag_on_contract_validation_errors` and `TestRagServicePrompt.test_build_augmented_system_prompt_skips_rag_when_query_contract_fails` verify invalid embedding/search responses fall back to the base prompt. `TestRagServiceRecording.test_record_chat_turn_requires_explicit_task_queue` verifies the recording API requires explicit task queue wiring and no longer depends on a module-global queue. `TestRagServiceRecording.test_background_store_dumps_failed_record_for_contract_validation_errors` verifies invalid storage embedding responses write failed memory JSONL.

### WebSocket RAG async persistence and runtime wiring

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py -q`
- Result: `32 passed, 1 warning in 0.26s`
- Evidence: `TestChatServiceErrorContract.test_infra_functions_are_not_public_api` verifies the public module surface exposes `generate_chat_reply()` / `create_chat_session()` while keeping raw RAG queue parameters private. `TestChatServiceErrorContract.test_public_generate_chat_reply_delegates_to_configured_service` and `TestChatServiceErrorContract.test_public_create_chat_session_delegates_to_configured_service` verify the module-level entrypoints work through registered runtime service delegation. `TestChatServiceErrorContract.test_public_entrypoints_fail_fast_without_registered_service` verifies the public functions do not create a hidden standalone runtime when no app resolver is registered. `TestChatServiceErrorContract.test_public_entrypoints_follow_registered_app_state_service` verifies module-level entrypoints resolve the current registered app-state service instead of a split instance. `TestChatServiceErrorContract.test_public_entrypoints_restore_previous_resolver_after_nested_clear` verifies nested resolver teardown restores the previous app service instead of losing it. `TestChatServiceRagContract.test_thread_pool_memory_task_queue_shutdown_waits_for_pending_tasks` verifies the queue can drain pending storage work before shutdown. `TestChatServiceRagContract.test_runtime_config_fails_fast_for_inconsistent_rag_policy` verifies invalid boundary configuration fails before serving. `TestChatServiceRagContract.test_chat_session_uses_same_per_message_resolution_as_http_reply` verifies WebSocket sessions use the same per-message prompt resolution path as HTTP replies. `TestWebSocketEndpoint.test_returns_404_when_character_disappears_after_session_open` verifies per-message character lookup failures return a 404 error and close cleanly. `TestWebSocketFlowIntegration.test_rag_search_injection_and_recording_flow_through_websocket` sends messages through `/ws/miori` with `RAG_ENABLED=true` and verifies the real `rag_service` prompt-injection and record flow. `TestWebSocketFlowIntegration.test_rag_storage_failure_does_not_block_websocket_response_and_writes_fallback` blocks the storage task, receives the WebSocket response before unblocking it, then verifies the failed memory JSONL fallback is written.

### Chat/WebSocket RAG regression subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat.py backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py backend/tests/test_chat_integration.py backend/tests/test_memory_rag_runtime_evidence.py backend/tests/test_runtime_contract.py -q`
- Result: `91 passed, 1 warning in 2.22s`
- Evidence: The combined chat, WebSocket, runtime evidence, and runtime contract subset passed after the single chat service entrypoint change.

### Regression suite

- Command: `backend/.venv/bin/pytest -q backend/tests`
- Result: `191 passed, 1 warning in 1.53s`
- Evidence: Full backend test suite passed after the memory policy refresh, private service config surface, single chat service RAG task queue lifecycle, app-state public entrypoint delegation, WebSocket per-message 404 handling, sensitive term coverage, RAG fallback boundary, chat service integration, WebSocket async persistence coverage, documentation, and evidence report fixes.

### Build check

- Command: `backend/.venv/bin/python -m compileall -q backend/app backend/tests`
- Result: success with no output.
- Command: `backend/.venv/bin/python -m mypy backend/app backend/tests`
- Result: `Success: no issues found in 41 source files`

### Ollama Model Availability

- Command: `ollama list`
- Result: `nomic-embed-text:latest` is present.
