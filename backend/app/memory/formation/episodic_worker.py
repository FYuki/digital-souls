"""永続予約から分割抽出・登録を実行し、失敗と停止を未処理として回復する。"""

from collections import deque
from collections.abc import Callable, Mapping
import threading

from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.episodic.registration import EpisodicRegistrationService
from app.memory.formation.episodic_extractor import ExtractionInputTooLarge, ThreadEpisodeExtractor
from app.memory.formation.thread_chunks import bisect_chunk, split_thread
from app.memory.formation.thread_queue import ThreadFormationQueue, StaleThreadSnapshot


class EpisodicFormationWorker:
    def __init__(
        self, *, queue: ThreadFormationQueue, extractor: ThreadEpisodeExtractor,
        registration: EpisodicRegistrationService, entity_labels: Callable[[str], Mapping[str, str]],
        extraction_identity: Callable[[], ExtractionIdentity],
        chunk_characters: int = 4000, context_characters: int = 800,
    ) -> None:
        self._queue = queue
        self._extractor = extractor
        self._registration = registration
        self._entity_labels = entity_labels
        self._extraction_identity = extraction_identity
        self._chunk_characters = chunk_characters
        self._context_characters = context_characters

    def process_next(self, *, should_stop: Callable[[], bool] = lambda: False) -> bool:
        lease = self._queue.claim()
        if lease is None:
            return False
        finished = threading.Event()
        lost = threading.Event()

        def renew() -> None:
            while not finished.wait(self._queue.renewal_interval_seconds):
                try:
                    if not self._queue.renew(lease):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        renewal = threading.Thread(target=renew, name="episode-lease-renewal", daemon=True)
        renewal.start()
        try:
            snapshot = self._queue.snapshot(lease)
            self._registration.reconcile_sources(snapshot)
            pending = deque(split_thread(snapshot, max_characters=self._chunk_characters,
                                         context_characters=self._context_characters))
            saved = rejected = 0
            while pending:
                if should_stop() or lost.is_set():
                    self._queue.release(lease, failed=False)
                    return True
                chunk = pending.popleft()
                catalog, provenance, progress = self._registration.extraction_context(snapshot, chunk)
                labels = self._entity_labels(lease.character_id)
                identity = self._extraction_identity()
                try:
                    batch = self._extractor.extract(
                        snapshot=snapshot, chunk=chunk, catalog=catalog, provenance=provenance,
                        progress=progress, entity_labels=labels,
                    )
                except ExtractionInputTooLarge:
                    left, right = bisect_chunk(chunk)
                    pending.extendleft((right, left))
                    continue
                if self._extraction_identity() != identity:
                    raise RuntimeError("extraction model identity changed")
                prepared = self._registration.prepare(
                    snapshot, chunk, batch, catalog, entity_labels=labels, extraction_identity=identity)
                if should_stop() or lost.is_set():
                    self._queue.release(lease, failed=False)
                    return True
                result = self._registration.commit(prepared)
                saved += result.saved + result.replayed
                rejected += result.rejected
            with self._queue.guard(snapshot) as connection:
                self._queue.finish(connection, snapshot, saved_count=saved, rejected_count=rejected)
        except StaleThreadSnapshot:
            self._queue.release(lease, failed=False)
        except Exception:
            self._queue.release(lease, failed=True)
            raise
        finally:
            finished.set()
            renewal.join()
        return True
