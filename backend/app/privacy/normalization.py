from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class NormalizedView:
    text: str
    _spans: tuple[tuple[int, int], ...]

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if start < 0 or start >= end or end > len(self._spans):
            raise ValueError("normalized span is invalid")
        selected = self._spans[start:end]
        return min(span[0] for span in selected), max(span[1] for span in selected)

    def __repr__(self) -> str:
        return f"NormalizedView(length={len(self.text)})"


@dataclass(frozen=True, repr=False)
class RecognitionViews:
    normalized: NormalizedView
    casefold: NormalizedView
    compact_financial: NormalizedView
    compact_phone: NormalizedView

    def __repr__(self) -> str:
        return f"RecognitionViews(length={len(self.normalized.text)})"


def _nfkd_characters(text: str) -> list[tuple[str, tuple[int, int]]]:
    return [
        (decomposed, (source_index, source_index + 1))
        for source_index, source_character in enumerate(text)
        for decomposed in unicodedata.normalize("NFKD", source_character)
    ]


def _canonical_order(
    characters: list[tuple[str, tuple[int, int]]],
) -> tuple[tuple[str, tuple[int, int]], ...]:
    ordered: list[tuple[str, tuple[int, int]]] = []
    sequence: list[tuple[str, tuple[int, int]]] = []
    for mapped_character in characters:
        if unicodedata.combining(mapped_character[0]) == 0 and sequence:
            ordered.extend(_ordered_sequence(sequence))
            sequence = []
        sequence.append(mapped_character)
    ordered.extend(_ordered_sequence(sequence))
    return tuple(ordered)


def _ordered_sequence(
    sequence: list[tuple[str, tuple[int, int]]],
) -> tuple[tuple[str, tuple[int, int]], ...]:
    if not sequence:
        return ()
    if unicodedata.combining(sequence[0][0]) == 0:
        starter = sequence[:1]
        combining = sequence[1:]
    else:
        starter = []
        combining = sequence
    if not combining:
        return tuple(starter)
    by_combining_class: list[list[tuple[str, tuple[int, int]]]] = [
        [] for _ in range(256)
    ]
    for mapped_character in combining:
        by_combining_class[unicodedata.combining(mapped_character[0])].append(
            mapped_character
        )
    return tuple(
        starter
        + [
            mapped_character
            for combining_class in range(1, 256)
            for mapped_character in by_combining_class[combining_class]
        ]
    )


def _nfkc_characters(text: str) -> tuple[tuple[str, tuple[int, int]], ...]:
    decomposed = _canonical_order(_nfkd_characters(text))
    normalized = unicodedata.normalize(
        "NFC",
        "".join(character for character, _span in decomposed),
    )
    mapped: list[tuple[str, tuple[int, int]]] = []
    offset = 0
    for character in normalized:
        decomposition_length = len(unicodedata.normalize("NFD", character))
        sources = decomposed[offset : offset + decomposition_length]
        if not sources:
            raise ValueError("normalization source mapping is invalid")
        mapped.append(
            (
                character,
                (
                    min(span[0] for _source, span in sources),
                    max(span[1] for _source, span in sources),
                ),
            )
        )
        offset += decomposition_length
    if offset != len(decomposed):
        raise ValueError("normalization source mapping is invalid")
    return tuple(mapped)


def _casefold_characters(
    characters: tuple[tuple[str, tuple[int, int]], ...],
) -> tuple[tuple[str, tuple[int, int]], ...]:
    return tuple(
        (folded, span)
        for character, span in characters
        for folded in character.casefold()
    )


def build_normalized_view(text: str, *, casefold: bool) -> NormalizedView:
    normalized = _normalized_view(_nfkc_characters(text))
    if not casefold:
        return normalized
    return _normalized_view(
        _casefold_characters(
            tuple(zip(normalized.text, normalized._spans, strict=True))
        )
    )


def _normalized_view(
    normalized_characters: tuple[tuple[str, tuple[int, int]], ...],
) -> NormalizedView:
    characters: list[str] = []
    spans: list[tuple[int, int]] = []
    pending_space_span: tuple[int, int] | None = None

    for character, span in normalized_characters:
        if unicodedata.category(character) == "Cf":
            continue
        if character.isspace():
            if pending_space_span is None:
                pending_space_span = span
            else:
                pending_space_span = (pending_space_span[0], span[1])
            continue
        if pending_space_span is not None:
            characters.append(" ")
            spans.append(pending_space_span)
            pending_space_span = None
        characters.append(character)
        spans.append(span)

    if pending_space_span is not None:
        characters.append(" ")
        spans.append(pending_space_span)
    return NormalizedView("".join(characters), tuple(spans))


def build_recognition_views(text: str) -> RecognitionViews:
    normalized = build_normalized_view(text, casefold=False)
    casefold = _normalized_view(
        _casefold_characters(
            tuple(zip(normalized.text, normalized._spans, strict=True))
        )
    )
    return RecognitionViews(
        normalized=normalized,
        casefold=casefold,
        compact_financial=build_compact_view(
            casefold,
            separators=(" ", "-"),
        ),
        compact_phone=build_compact_view(
            casefold,
            separators=(" ", "(", ")", "-"),
        ),
    )


def build_compact_view(
    normalized: NormalizedView,
    *,
    separators: tuple[str, ...],
) -> NormalizedView:
    separator_set = frozenset(separators)
    retained = tuple(
        (character, span)
        for character, span in zip(normalized.text, normalized._spans, strict=True)
        if character not in separator_set
    )
    return NormalizedView(
        "".join(character for character, _span in retained),
        tuple(span for _character, span in retained),
    )
