"""予算で分割された全catalogの照合・曖昧性・停止・登録を検証する。推論はfake。"""

import json
from uuid import uuid4

import pytest

from app.memory.episodic.contracts import FiveW, RecordKind, SourceSpan, What
from app.memory.episodic.extraction_contracts import ExtractionBatch
from app.memory.episodic.quotes import InvalidExtraction
from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor, ExtractionInterrupted
from app.memory.formation.thread_chunks import split_thread
from tests.module.test_episodic_formation import Client, SETTINGS, worker
from tests.module.test_episodic_registration import (
    harness, quote, fact, episode, batch, STAMP,  # noqa: F401
)


def seeded(harness, count=18):
    old_turn = harness.turn("以前、各地を旅行した")
    snapshot = harness.snapshot()
    q = quote(snapshot, old_turn)
    source = SourceSpan(source_id=q.source_id, revision=q.revision, role=q.role,
                        start=0, end=len(q.quote), stated_at=harness.now[0])
    with harness.repository.transaction() as tx:
        for index in range(count):
            tx.create(character_id="miori", conversation_id=harness.conversation_id, kind=RecordKind.FACT,
                      five_w=FiveW(what=What(predicate="訪れた", object=f"場所{index}")),
                      sources=(source,), stamp=STAMP, receipt_id=uuid4())
    with harness.queue.guard(snapshot) as connection:
        harness.queue.finish(connection, snapshot, saved_count=0, rejected_count=0)
    new_turn = harness.turn("あの旅行先は場所17ではなく場所18だった")
    snapshot = harness.snapshot()
    chunk = split_thread(snapshot)[0]
    catalog, provenance, progress = harness.service.extraction_context(snapshot, chunk)
    q = quote(snapshot, new_turn)
    proposed = batch(episode(q), fact(q, object="場所18"), q)
    return snapshot, chunk, catalog, provenance, progress, proposed


class PagedClient(Client):
    def __init__(self, initial, target_ids=(), *, certainty="CONFIRMED", page_size=3):
        super().__init__(callback=self.respond, fits=self.fits_page)
        self.initial = initial
        self.target_ids = {str(target) for target in target_ids}
        self.certainty = certainty
        self.page_size = page_size
        self.scan_ids = []
        self.fail_page = None
        self.incomplete_above = None
        self.stop_after_page = None
        self.stopped = False

    def fits_page(self, messages, schema):
        value = json.loads(messages[1]["content"])
        return len(value["known_records"]) <= self.page_size

    def respond(self, value, index):
        phase = value.get("phase")
        if phase == "new_candidates":
            return self.initial.model_dump_json()
        if phase == "catalog_match":
            ids = [record["id"] for record in value["known_records"]]
            self.scan_ids.extend(ids)
            if self.fail_page and self.fail_page in ids:
                return "invalid"
            if self.incomplete_above is not None and len(ids) > self.incomplete_above:
                return json.dumps({"complete": False, "matches": []})
            if self.stop_after_page is not None and len(self.scan_ids) >= self.stop_after_page:
                self.stopped = True
            q = next(record for record in self.initial.records if record.kind is RecordKind.FACT).anchor
            return json.dumps({"complete": True, "matches": [
                {"key": "fact", "target": {"id": record["id"], "version": record["content_version"]},
                 "certainty": self.certainty, "operation": "UPDATE",
                 "evidence": [q.model_dump(mode="json")]}
                for record in value["known_records"] if record["id"] in self.target_ids
            ]})
        if phase == "refine_target":
            proposed = value["candidate"]
            proposed["operation"] = value["decision"]["operation"]
            proposed["target"] = value["decision"]["target"]
            proposed["changes"] = ["what"]
            return json.dumps(proposed)
        raise AssertionError("large catalog should use phased inference")


def extract(harness, setup, client):
    snapshot, chunk, catalog, provenance, progress, _ = setup
    return ThreadEpisodeExtractor(client=client, settings=SETTINGS).extract(
        snapshot=snapshot, chunk=chunk, catalog=catalog, provenance=provenance, progress=progress,
        entity_labels={"speaker:user": "ユーザー", "character:miori": "光織"}, should_stop=lambda: client.stopped)


def test_last_catalog_page_is_matched_before_atomic_update_and_all_ids_are_examined(harness):
    setup = seeded(harness)
    catalog = setup[2]
    target = catalog[-1]
    client = PagedClient(setup[-1], (target.id,))
    result = extract(harness, setup, client)
    assert set(client.scan_ids) == {str(record.id) for record in catalog}
    assert len(client.scan_ids) == len(catalog)
    operation = next(record for record in result.records if record.kind is RecordKind.FACT)
    assert operation.target.id == target.id
    assert harness.records(RecordKind.FACT) == catalog
    harness.apply(setup[0], result)
    current = {r.id: r for r in harness.records(RecordKind.FACT)}
    assert len(current) == len(catalog)
    assert current[target.id].content_version == 2
    assert current[target.id].five_w.what.object == "場所18"
    assert all(r.content_version == 1 for r in current.values() if r.id != target.id)


def test_matches_on_different_pages_remain_ambiguous_and_do_not_overwrite(harness):
    setup = seeded(harness)
    first, last = setup[2][0], setup[2][-1]
    client = PagedClient(setup[-1], (first.id, last.id))
    result = extract(harness, setup, client)
    assert all(record.target is None for record in result.records)
    harness.apply(setup[0], result)
    with harness.repository.read() as tx:
        assert tx.get("miori", first.id) == first
        assert tx.get("miori", last.id) == last
    assert len(harness.records(RecordKind.FACT)) == len(setup[2]) + 1


def test_one_possible_match_does_not_become_a_confirmed_update(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1], (setup[2][-1].id,), certainty="POSSIBLE")
    result = extract(harness, setup, client)
    assert all(record.target is None for record in result.records)


def test_incomplete_pages_are_subdivided_instead_of_discarding_unreturned_matches(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1], (setup[2][-1].id,), page_size=6)
    client.incomplete_above = 1
    result = extract(harness, setup, client)
    assert set(client.scan_ids) == {str(record.id) for record in setup[2]}
    assert next(r for r in result.records if r.kind is RecordKind.FACT).target.id == setup[2][-1].id


def test_late_page_failure_does_not_partially_register_an_earlier_match(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1], (setup[2][0].id,))
    client.fail_page = str(setup[2][-1].id)
    with pytest.raises(InvalidExtraction):
        extract(harness, setup, client)
    assert harness.records(RecordKind.FACT) == setup[2]


def test_cancellation_between_pages_keeps_the_job_recoverable(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1])
    client.stop_after_page = 3
    with pytest.raises(ExtractionInterrupted):
        extract(harness, setup, client)
    assert 3 <= len(client.scan_ids) < 3 + client.page_size
    assert harness.records(RecordKind.FACT) == setup[2]


def test_match_outside_its_page_is_rejected(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1])
    original = client.callback
    def forged(value, index):
        if value.get("phase") != "catalog_match":
            return original(value, index)
        q = setup[-1].records[1].anchor
        return json.dumps({"complete": True, "matches": [{
            "key": "fact", "target": {"id": str(uuid4()), "version": 1}, "certainty": "CONFIRMED",
            "operation": "UPDATE", "evidence": [q.model_dump(mode="json")],
        }]})
    client.callback = forged
    with pytest.raises(InvalidExtraction, match="offered"):
        extract(harness, setup, client)


def test_refinement_cannot_change_selected_target(harness):
    setup = seeded(harness)
    client = PagedClient(setup[-1], (setup[2][-1].id,))
    original = client.callback
    def forged(value, index):
        output = original(value, index)
        if value.get("phase") == "refine_target":
            body = json.loads(output)
            body["target"]["id"] = str(uuid4())
            return json.dumps(body)
        return output
    client.callback = forged
    with pytest.raises(InvalidExtraction, match="identity"):
        extract(harness, setup, client)


def test_long_historical_provenance_stays_in_storage_without_growing_model_input(harness):
    setup = seeded(harness, count=1)
    snapshot, chunk, catalog, provenance, progress, proposed = setup
    old = provenance[catalog[0].id][0]
    historical = tuple(old.model_copy(update={"source_id": uuid4()}) for _ in range(3000))
    client = Client(callback=lambda *_: ExtractionBatch(records=()).model_dump_json())
    ThreadEpisodeExtractor(client=client, settings=SETTINGS).extract(
        snapshot=snapshot, chunk=chunk, catalog=catalog,
        provenance={catalog[0].id: historical}, progress=progress, entity_labels={})
    known = client.requests[0]["known_records"][0]
    assert known["sources"] == []
    assert known["source_summary"]["count"] == 3000
    assert known["source_summary"]["first_stated_at"] == old.stated_at.isoformat()
    assert len(json.dumps(client.requests[0])) < 5000


def test_worker_cancellation_releases_lease_and_retry_commits_once(harness):
    setup = seeded(harness)
    harness.queue.release(setup[0].lease, failed=False)
    stopping = PagedClient(setup[-1])
    stopping.stop_after_page = 3
    assert worker(harness, stopping).process_next(should_stop=lambda: stopping.stopped)
    assert harness.queue.has_pending()
    assert harness.records(RecordKind.FACT) == setup[2]
    retry = PagedClient(setup[-1], (setup[2][-1].id,))
    assert worker(harness, retry).process_next()
    assert not harness.queue.has_pending()
    assert len(harness.records(RecordKind.FACT)) == len(setup[2])
    assert len(harness.records(RecordKind.EPISODE)) == 1
    assert not worker(harness, retry).process_next()
