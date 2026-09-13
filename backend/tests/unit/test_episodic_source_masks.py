"""重複範囲・境界・roleを保つ削除maskの検証。"""

from datetime import UTC, datetime
from uuid import uuid4

from app.memory.episodic.contracts import SourceSpan
from app.memory.episodic.source_masks import visible_ranges


def test_union_of_masks_preserves_original_offsets_and_other_source_identity():
    source = SourceSpan(source_id=uuid4(), revision=2, role="user", start=0, end=20,
                        stated_at=datetime.now(UTC))
    masks = tuple(source.model_copy(update={"start": start, "end": end})
                  for start, end in [(3, 7), (5, 10), (12, 14), (14, 16), (4, 6)])
    assert visible_ranges(source, masks) == ((0, 3), (10, 12), (16, 20))
    assert visible_ranges(source.model_copy(update={"role": "assistant"}), masks) == ()
    assert visible_ranges(source.model_copy(update={"revision": 3}), masks) == ((0, 20),)
    assert visible_ranges(source.model_copy(update={"source_id": uuid4()}), masks) == ((0, 20),)
    assert visible_ranges(source, (source,)) == ()
