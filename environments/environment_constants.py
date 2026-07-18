from __future__ import annotations

DEPENDENCY_NAMES = ("frontend", "backend", "ollama", "voicevox", "whisper", "chroma")
READY_GATE_CLEANUP_TARGET = "ready_gate"
RUN_REPORT_CLEANUP_TARGET = "run_report"
CLEANUP_TARGET_NAMES = (
    *DEPENDENCY_NAMES,
    READY_GATE_CLEANUP_TARGET,
    RUN_REPORT_CLEANUP_TARGET,
)
HTTP_SERVICE_NAMES = ("frontend", "backend", "ollama", "voicevox")
RUN_REPORT_ENV = "DS_ENVIRONMENT_RUN_REPORT"
PROFILE_REPORT_ENV = "DS_PROFILE_REPORT"
READY_GATE_ENV = "DS_ENVIRONMENT_READY_URL"
DEFAULT_READY_GATE_URL = "http://127.0.0.1:4174/ready"
RUN_REPORT_SCHEMA_VERSION = 1
VOICEVOX_CONTAINER_NAME = "voicevox_engine"
VOICEVOX_SETUP_COMMAND = (
    "docker run -d --name voicevox_engine -p 50021:50021 "
    "voicevox/voicevox_engine:cpu-latest"
)
