from collections.abc import Mapping, Sequence


def played_text_prefix(
    generated_text: str,
    chunks: Sequence[Mapping[str, object]],
    *,
    last_played_sequence: int,
) -> str:
    if last_played_sequence < 0:
        raise ValueError("last_played_sequence must not be negative")

    chunks_by_sequence: dict[int, Mapping[str, object]] = {}
    for chunk in chunks:
        sequence = chunk.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ValueError("chunk sequence must be a positive integer")
        if sequence in chunks_by_sequence:
            raise ValueError("chunk sequence must be unique")
        chunks_by_sequence[sequence] = chunk

    played_end = 0
    for sequence in range(1, last_played_sequence + 1):
        played_chunk = chunks_by_sequence.get(sequence)
        if played_chunk is None:
            break
        text_range = played_chunk.get("text_range")
        if not isinstance(text_range, Mapping):
            raise ValueError("chunk text_range must be an object")
        start = text_range.get("start")
        end = text_range.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != played_end
            or end < start
        ):
            raise ValueError("chunk text ranges must form a contiguous prefix")
        played_end = end

    code_points = list(generated_text)
    if played_end > len(code_points):
        raise ValueError("chunk text range exceeds generated text")
    return "".join(code_points[:played_end])
