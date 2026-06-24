# Fix Evidence

## Test Count Source Of Truth

All report pass counts below were copied from the command results in this file.
When regenerating evidence, update every copied count in `test-report.md` in the
same change. `backend/tests/test_runtime_contract.py` does not keep fixed pass
counts; it verifies that `fix-evidence.md` and `test-report.md` stay aligned.

### Completion Condition 1/2/4 Runtime Evidence

- Command: `backend/.venv/bin/pytest -q backend/tests/test_memory_rag_runtime_evidence.py`
- Result: `3 passed, 1 warning in 1.91s`

### RAG Disabled And Embedder Subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_embedder.py backend/tests/test_chat.py backend/tests/test_chat_integration.py -q`
- Result: `27 passed, 1 warning in 0.16s`

### Memory Policy And Runtime Contract Subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_memory_rag_service.py backend/tests/test_runtime_contract.py -q`
- Result: `50 passed, 1 warning in 0.17s`

### WebSocket RAG Subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py -q`
- Result: `25 passed, 1 warning in 0.24s`

### Chat/WebSocket RAG Regression Subset

- Command: `backend/.venv/bin/python -m pytest backend/tests/test_chat.py backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py backend/tests/test_chat_integration.py backend/tests/test_memory_rag_runtime_evidence.py backend/tests/test_runtime_contract.py -q`
- Result: `70 passed, 1 warning in 1.22s`

### Regression Suite

- Command: `backend/.venv/bin/pytest -q backend/tests`
- Result: `165 passed, 1 warning in 1.44s`

### Build Check

- Command: `backend/.venv/bin/python -m compileall -q backend/app backend/tests`
- Result: success with no output.
- Command: `backend/.venv/bin/mypy --config-file backend/mypy.ini backend/app`
- Result: `Success: no issues found`
