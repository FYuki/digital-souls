"""実SQLiteの永続予約から、構造化推論境界・登録・回復までを結合する。推論のみfake。"""

import asyncio
import json

import pytest

from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.episodic.extraction_contracts import ExtractionBatch, SourceQuote
from app.memory.episodic.quotes import InvalidExtraction
from app.memory.formation.config import MemoryFormationSettings
from app.memory.formation.durable_scheduler import DurableMemoryFormationScheduler
from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor, ExtractionInputTooLarge
from app.memory.formation.episodic_worker import EpisodicFormationWorker
from app.memory.formation.thread_chunks import split_thread
from tests.module.test_episodic_registration import harness, episode, fact, batch  # noqa: F401

SETTINGS = MemoryFormationSettings(5, 2, 15, 300, 100, 4096)


class Client:
    def __init__(self, callback=None, fits=None):
        self.callback = callback or self.valid_response
        self.fit_callback = fits
        self.requests = []

    def fits(self, messages, json_schema):
        return True if self.fit_callback is None else self.fit_callback(messages, json_schema)

    def chat(self, messages, *, json_schema, timeout_seconds, max_output_tokens):
        value = json.loads(messages[1]["content"])
        self.requests.append(value)
        return self.callback(value, len(self.requests))

    @staticmethod
    def valid_response(value, index):
        part = next((p for p in value["fragments"] if p["role"] == "user" and p["ownership"] == "primary"), None)
        if part is None:
            return ExtractionBatch(records=()).model_dump_json()
        q = SourceQuote(source_id=part["source_id"], revision=part["revision"], role="user",
                        quote=part["text"], start=part["start"])
        return batch(episode(q), fact(q), q).model_dump_json()


def worker(harness, client, *, budget=4000):
    return EpisodicFormationWorker(
        queue=harness.queue, extractor=ThreadEpisodeExtractor(client=client, settings=SETTINGS),
        registration=harness.service, entity_labels=lambda _: {"speaker:user": "ユーザー"},
        extraction_identity=lambda: ExtractionIdentity(
            provider_id="fake", model_id="test", model_digest="test-digest", prompt_version="test-v1"),
        chunk_characters=budget, context_characters=0,
    )


def test_persisted_job_runs_without_an_in_memory_submit_and_saves_provenance(harness):
    harness.turn("うどんを食べた")
    client = Client()
    assert worker(harness, client).process_next()
    assert len(harness.records()) == 2
    assert not harness.queue.has_pending()
    assert client.requests[0]["character_id"] == "miori"
    assert client.requests[0]["fragments"][0]["revision"] == 2
    assert client.requests[0]["known_records"] == []


def test_invalid_model_output_is_failure_and_never_completed_as_empty(harness):
    harness.turn("うどんを食べた")
    client = Client(callback=lambda *_: '{"records": "invalid"}')
    with pytest.raises(InvalidExtraction):
        worker(harness, client).process_next()
    assert len(client.requests) == 2
    assert harness.queue.has_pending()
    assert harness.queue.claim() is None
    assert harness.records() == ()


def test_partial_chunk_success_survives_restart_without_duplicating_prefix(harness):
    from datetime import timedelta
    first = harness.turn("うどんを食べた")
    harness.turn("そばを食べた")
    budget = len(first.user_content) + len(first.assistant_content)
    def fail_second(value, index):
        return Client.valid_response(value, index) if index == 1 else "invalid"
    with pytest.raises(InvalidExtraction):
        worker(harness, Client(callback=fail_second), budget=budget).process_next()
    first_ids = {record.id for record in harness.records()}
    assert len(first_ids) == 2
    assert harness.queue.has_pending()
    harness.now[0] += timedelta(seconds=6)
    restored_client = Client()
    worker(harness, restored_client, budget=budget).process_next()
    assert len(harness.records()) == 4
    assert first_ids <= {record.id for record in harness.records()}
    assert not harness.queue.has_pending()
    assert any(part["processed_ranges"] for part in restored_client.requests[0]["fragments"])


def test_input_budget_bisection_covers_every_source_character(harness):
    harness.turn("あいうえおかきくけこさしすせそたちつてと")
    def fits(messages, _):
        value = json.loads(messages[1]["content"])
        return sum(len(p["text"]) for p in value["fragments"]) <= 8
    client = Client(callback=lambda *_: ExtractionBatch(records=()).model_dump_json(), fits=fits)
    worker(harness, client).process_next()
    assert not harness.queue.has_pending()
    spans = {}
    for request in client.requests:
        for part in request["fragments"]:
            if part["ownership"] == "primary":
                key = (part["source_id"], part["role"])
                spans.setdefault(key, []).extend(range(part["start"], part["end"]))
    assert spans
    for positions in spans.values():
        assert len(positions) == len(set(positions))
        assert set(positions) == set(range(max(positions) + 1))


def test_model_declared_incomplete_output_requests_smaller_chunks(harness):
    turn = harness.turn("うどんを食べた")
    snapshot = harness.snapshot()
    client = Client(callback=lambda *_: '{"complete": false, "records":[]}')
    with pytest.raises(ExtractionInputTooLarge):
        ThreadEpisodeExtractor(client=client, settings=SETTINGS).extract(
            snapshot=snapshot, chunk=split_thread(snapshot)[0], catalog=(), provenance={},
            progress={}, entity_labels={"speaker:user": "ユーザー"})
    assert turn.turn_id
    assert harness.records() == ()


def test_shutdown_retains_unprocessed_reservation(harness):
    harness.turn("うどんを食べた")
    stopped = worker(harness, Client()).process_next(should_stop=lambda: True)
    assert stopped
    assert harness.queue.has_pending()
    assert harness.queue.claim() is not None
    assert harness.records() == ()


@pytest.mark.anyio
async def test_durable_scheduler_recovers_existing_reservations_on_start(harness):
    harness.turn("うどんを食べた")
    scheduler = DurableMemoryFormationScheduler(worker=worker(harness, Client()), queue=harness.queue,
                                                 poll_seconds=0.01)
    await scheduler.start()
    try:
        async def complete():
            while harness.queue.has_pending():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(complete(), timeout=5)
        assert len(harness.records()) == 2
    finally:
        await scheduler.stop()
