"""本文を保持せず、管理操作で利用停止した出典の範囲を除外する。"""

from app.memory.episodic.contracts import SourceSpan


def visible_ranges(source: SourceSpan, masks: tuple[SourceSpan, ...]) -> tuple[tuple[int, int], ...]:
    """元発言の文字位置を保ち、重複・包含したmaskも一度だけ除く。"""
    # ユーザー入力を受けた同一turnの返答は、その内容を言い換えている可能性がある。
    # 返答の一部だけを安全と推測せず、削除元からの再取得経路として除外する。
    if source.role == "assistant" and any(
        mask.role == "user" and mask.source_id == source.source_id and mask.revision == source.revision
        for mask in masks
    ):
        return ()
    spans = sorted(
        (max(source.start, mask.start), min(source.end, mask.end))
        for mask in masks
        if source.source_id == mask.source_id and source.revision == mask.revision
        and source.role == mask.role and source.start < mask.end and mask.start < source.end
    )
    cursor = source.start
    result = []
    for start, end in spans:
        if cursor < start:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < source.end:
        result.append((cursor, source.end))
    return tuple(result)


def overlaps_mask(source: SourceSpan, masks: tuple[SourceSpan, ...]) -> bool:
    return visible_ranges(source, masks) != ((source.start, source.end),)
