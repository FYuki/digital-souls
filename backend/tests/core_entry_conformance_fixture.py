"""HTTP /chat・Session Text・Speech の3入口に通す共有fixture。

Issue #538 が要求する「同一fixtureを実入口3系統へ流し、共通Core境界で等価性を
確認する」ための値とfake Provider/支援部品を集約する。#422 以降の拡張が別入口を
追加するときも、このfixtureを更新すればよい。

fixtureの構成要素は計画で定義した契約 C1〜C6 に対応する（C7 は試験環境と
製品動作の維持であり、fixture の部品では確認しない）:

- C1: 同一Card・保存済み履歴・記憶参照・入力本文を3入口へ投入する
- C2: 実入口からfake Providerへ届く最終promptの重要部分を照合する
- C3: 各入口のpromptにCard・履歴・記憶参照・現在入力が含まれる
- C4: processing → completed の遷移と入口別identifierを照合する
- C5: Tool dispatch・履歴保存・形成予約の回数を入口ごとに観測する
- C6: HTTPの空input_idsとSessionの実input/response idを照合する
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import AsyncIterator, Callable
from uuid import UUID, uuid4

from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceCapability,
    InferenceMessage,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.registry import default_provider_registry
from app.inference.router import InferenceRouter
from app.inference.config import resolve_inference_settings
from app.memory.chroma_store import MemorySearchResult, RetrievalMatchKind
from app.memory.rag_service import RetrievalOutcome
from app.prompting.models import CharacterPrompt
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
)

# ---------------------------------------------------------------------------
# 共有fixture値
# ---------------------------------------------------------------------------

CHARACTER_ID = "miori"
CHARACTER_NAME = "光織"
CHARACTER_SYSTEM_PROMPT = "あなたは光織です。端正な日本語で応答します。"

# 3入口すべてに同じ発話を通す。privacy scanner にも引っかからない中立な文章。
USER_TEXT = "あの件、もう一度確認してもらえる?"

# 履歴として保存済みにするturn。privacy scanner で placeholder に置き換わる値を
# raw値として投入し、promptへはそのまま届く(履歴投影は保存時とは別段)。
RESTORED_USER = "password: restored-secret"
RESTORED_ASSISTANT = "password: restored-assistant"

# memory注入fixture。prompt builder が "## 関連する記憶" 見出しで差し込む。
MEMORY_CONTENT = "利用者は毎朝コーヒーを淹れる"
MEMORY_HEADING = "## 関連する記憶"

# 3入口で共有する会話ID。conversation_history_test_supportと揃える。
CONVERSATION_ID = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010")

# Session Text が発行する入口identifier。Speech はutterance IDがそのまま使われる。
TEXT_INPUT_ID = "text-input-538-001"

# fake Providerが返す応答本文。3入口共通で同じ返答が流れることを確認する。
LLM_REPLY = "確認しました。また連絡します。"


# ---------------------------------------------------------------------------
# Character Card
# ---------------------------------------------------------------------------


def fixture_character_document() -> dict[str, object]:
    """Card loader が読める chara_card_v3 形式のfixture文書。

    HTTP入口・Session入口の両方が load_character_card / load_tts_config を通る
    ため、共有fixtureのCardは同一文書として実ファイルへ書き出せる形で定義する。
    """
    return character_card_document(
        data=character_card_data(
            name=CHARACTER_NAME,
            description="",
            personality="",
            scenario="",
            system_prompt=CHARACTER_SYSTEM_PROMPT,
            mes_example="",
            post_history_instructions="",
            first_mes="",
            extensions={
                "digital_souls": {
                    "tts_config": {
                        "engine": "voicevox",
                        "speaker_id": 14,
                        "speaker_name": "fixture話者",
                        "style_name": "ノーマル",
                    }
                }
            },
        )
    )


def fixture_character_prompt() -> CharacterPrompt:
    """fixture Cardから導出されるプロンプト本体。"""
    return CharacterPrompt(
        description="",
        personality="",
        scenario="",
        system_prompt=CHARACTER_SYSTEM_PROMPT,
        mes_example="",
        post_history_instructions="",
    )


# ---------------------------------------------------------------------------
# fake inference Provider
# ---------------------------------------------------------------------------


class CapturingOllamaAdapter:
    """ollama provider向けfake adapter。

    generate_text / stream_text に届く最終 request.messages を保存し、
    C2 の prompt 一致検証・C3 の応答本文一致検証に使う。
    入口ごとに新しいadapterを作り、前の入口の記録を流用しない。
    """

    provider_id = "ollama"
    capabilities = frozenset(InferenceCapability)

    def __init__(self, *, reply: str = LLM_REPLY) -> None:
        self.reply = reply
        self.generate_requests: list[TextGenerationRequest] = []
        self.stream_requests: list[TextGenerationRequest] = []
        # Provider呼出中に履歴の `processing` 状態を読む観測点。
        # 呼出時点で (database_path, character_id, conversation_id, turn_id) を
        # 渡すと、その時点の turn status を list[tuple[str|None,...]] に記録する。
        self.status_probe: Callable[[], None] | None = None

    def probe(self, model_id: str, *, timeout_seconds: float) -> None:
        del model_id, timeout_seconds

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        self.generate_requests.append(request)
        if self.status_probe is not None:
            self.status_probe()
        return ProviderTextResult(text=self.reply, usage=None)

    async def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]:
        self.stream_requests.append(request)
        if self.status_probe is not None:
            self.status_probe()
        yield self.reply

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        del request
        return ProviderTextResult(text='{"answer":"ok"}', usage=None)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        del request
        return EmbeddingResult(((1.0, 2.0, 3.0),))

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        del request
        return TokenEstimate(16, TokenEstimateAccuracy.EXACT, "fixture")

    def last_messages(self) -> tuple[InferenceMessage, ...]:
        """このadapterへ届いた直近requestのmessagesを返す。"""
        if self.stream_requests:
            return self.stream_requests[-1].messages
        if self.generate_requests:
            return self.generate_requests[-1].messages
        return ()


def _fixture_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for token, model in {
        "CHAT": "gemma4:e4b",
        "PRIVACY": "gemma4:e4b",
        "MEMORY_EXTRACTION": "gemma4:e4b",
        "MEMORY_CONSOLIDATION": "gemma4:12b",
        "EMBEDDING": "nomic-embed-text:latest",
    }.items():
        environment[f"INFERENCE_TARGET_{token}"] = f"ollama/{model}"
        environment[f"INFERENCE_TARGET_{token}_MAX_INPUT_TOKENS"] = "8192"
        if token != "EMBEDDING":
            environment[f"INFERENCE_TARGET_{token}_MAX_OUTPUT_TOKENS"] = "1024"
    return environment


def build_fake_router(adapter: CapturingOllamaAdapter) -> InferenceRouter:
    """fake ProviderをバインドしたRouterを構築する。

    テスト側は `llm_router.register_inference_router(router)` /
    `llm_router.clear_inference_router(router)` で差し替える。
    """
    registry = default_provider_registry()
    registry.bind(adapter)
    settings = resolve_inference_settings(_fixture_environment(), registry)
    return InferenceRouter(settings=settings, registry=registry)


# ---------------------------------------------------------------------------
# Session入口の本番fake部品
# ---------------------------------------------------------------------------


class FixtureSyncTranscriber:
    """Session Speech入口でfake STT文字列を返す SyncTranscriber。

    start_transcription から入った音声が、WhisperSttAdapter経由で
    このtranscriberの返値として Sessionの確定utteranceになる。
    """

    def __init__(self, transcript: str = USER_TEXT) -> None:
        self.transcript = transcript
        self.calls: list[bytes] = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return self.transcript


class FixtureSpeakerSynthesizer:
    """Session入口の VoicevoxTtsAdapter に接続する SpeakerSynthesizer fake。

    実WAV(無音)を返す。音声バイナリの中身は観測対象にしない。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def synthesize(self, text: str, speaker_id: int) -> bytes:
        self.calls.append((text, speaker_id))
        import io
        import wave

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(b"\x00\x00" * 2400)
        return output.getvalue()


class FixtureDelivery:
    """ConversationCoreSession へ渡す最小 DeliveryPort。

    発行されたCoreEventを順序付きで記録する。response_started から
    完了イベントまでの入口別イベント列を観測するために使う。
    """

    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# 履歴 / memory fixture
# ---------------------------------------------------------------------------


def insert_completed_turn(
    database_path: Path,
    *,
    character_id: str,
    conversation_id: UUID,
    user_content: str,
    assistant_content: str,
    created_at: str = "2026-01-01T00:00:00.000000Z",
) -> None:
    """既存完了turnを SQLite に直接挿入する。

    新規turnより前に並ぶよう、created_at は十分過去の固定値を使う。
    """
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, character_id, conversation_id, status, "
            "user_content, assistant_content, created_at, updated_at) "
            "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)",
            (
                str(uuid4()),
                character_id,
                str(conversation_id),
                user_content,
                assistant_content,
                created_at,
                created_at,
            ),
        )


def fetch_turns(
    database_path: Path, character_id: str, conversation_id: UUID
) -> list[sqlite3.Row]:
    """対象会話のturnを永続化順で返す。"""
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT turn_id, status, user_content, assistant_content "
            "FROM conversation_turns "
            "WHERE character_id = ? AND conversation_id = ? "
            "ORDER BY created_at, turn_id",
            (character_id, str(conversation_id)),
        ).fetchall()


def turn_status(
    database_path: Path, character_id: str, conversation_id: UUID, turn_id: str
) -> str | None:
    """同一turn IDの現在statusを返す。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM conversation_turns "
            "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
            (character_id, str(conversation_id), turn_id),
        ).fetchone()
    return None if row is None else str(row[0])


def fixture_retrieval_outcome(*, memory_id: str | None = None) -> RetrievalOutcome:
    """memory注入1件を返す fake RetrievalOutcome。

    provenance recorder が存在確認を行う場合は、実DB保存済みの memory_id を
    渡して参照整合を保つ。
    """
    return RetrievalOutcome(
        memories=(
            MemorySearchResult(
                memory_id=memory_id or "53800000-0000-4000-8000-000000000001",
                normalized_text=MEMORY_CONTENT,
                occurred_at="2026-09-01T00:00:00.000000Z",
                memory_type="USER_PREFERENCE",
                raw_distance=0.5,
                match_kind=RetrievalMatchKind.SEMANTIC,
                content_version=1,
            ),
        ),
        no_match=False,
        response_cautions=(),
    )


# ---------------------------------------------------------------------------
# 形成予約の記録
# ---------------------------------------------------------------------------


class RecordingFormationScheduler:
    """submit回数だけ記録する最小fake scheduler。"""

    def __init__(self) -> None:
        self.jobs: list[object] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    def submit(self, job: object) -> None:
        self.jobs.append(job)

    def is_busy(self) -> bool:
        return False

    async def stop(self) -> None:
        self.stopped = True


def make_formation_observer(
    scheduler: RecordingFormationScheduler,
    repository,
) -> Callable[[object], None]:
    """submit_completed_turn を通じて formation job を記録する observer。

    Session入口側の completed_turn_observer に接続する。
    """
    from app.core_invocation import submit_completed_turn
    from app.conversation_history.models import ConversationTurn

    def observer(turn: object) -> None:
        if not isinstance(turn, ConversationTurn):
            return
        submit_completed_turn(
            turn,
            screen_derived=repository.is_screen_derived(
                turn.character_id, turn.conversation_id, turn.turn_id
            ),
            submitter=scheduler,
        )

    return observer
