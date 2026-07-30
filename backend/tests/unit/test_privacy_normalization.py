from __future__ import annotations


def test_should_build_nfkc_view_without_changing_original_text() -> None:
    from app.privacy.normalization import build_normalized_view

    original = "番号：１２３４"
    view = build_normalized_view(original, casefold=False)

    assert original == "番号：１２３４"
    assert view.text == "番号:1234"
    assert view.original_span(3, 7) == (3, 7)


def test_should_casefold_only_when_requested() -> None:
    from app.privacy.normalization import build_normalized_view

    preserved = build_normalized_view("Bearer ABC", casefold=False)
    folded = build_normalized_view("Bearer ABC", casefold=True)

    assert preserved.text == "Bearer ABC"
    assert folded.text == "bearer abc"


def test_should_restore_one_to_many_nfkc_match_to_single_source_character() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("前㍿後", casefold=False)
    start = view.text.index("株式会社")

    assert view.original_span(start, start + len("株式会社")) == (1, 2)


def test_should_restore_many_to_one_nfkc_match_to_complete_source_sequence() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("前e\u0301後", casefold=False)
    start = view.text.index("é")

    assert view.original_span(start, start + 1) == (1, 3)


def test_should_restore_hangul_jamo_nfkc_match_to_complete_source_sequence() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("前\u1100\u1161後", casefold=False)
    start = view.text.index("가")

    assert view.original_span(start, start + 1) == (1, 3)


def test_should_restore_casefold_expansion_to_single_source_character() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("aßz", casefold=True)

    assert view.text == "assz"
    assert view.original_span(1, 3) == (1, 2)


def test_should_remove_format_characters_and_restore_match_across_them() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("token=ab\u200bcd", casefold=False)
    start = view.text.index("abcd")

    assert view.text == "token=abcd"
    assert view.original_span(start, start + 4) == (6, 11)


def test_should_collapse_whitespace_and_map_to_entire_source_run() -> None:
    from app.privacy.normalization import build_normalized_view

    view = build_normalized_view("a \t\n b", casefold=False)

    assert view.text == "a b"
    assert view.original_span(1, 2) == (1, 5)


def test_should_remove_only_explicit_compact_separators() -> None:
    from app.privacy.normalization import build_compact_view, build_normalized_view

    normalized = build_normalized_view("+81 (90)-0000 0000", casefold=False)
    compact = build_compact_view(normalized, separators=(" ", "(", ")", "-"))

    assert compact.text == "+819000000000"
    assert compact.original_span(0, len(compact.text)) == (0, 18)


def test_should_preserve_unapproved_punctuation_in_compact_view() -> None:
    from app.privacy.normalization import build_compact_view, build_normalized_view

    normalized = build_normalized_view("12/34-56", casefold=False)
    compact = build_compact_view(normalized, separators=("-",))

    assert compact.text == "12/3456"
    assert compact.original_span(0, len(compact.text)) == (0, 8)


def test_should_not_expose_source_map_in_repr() -> None:
    from app.privacy.normalization import build_normalized_view

    source = "token=synthetic-sensitive-value"
    view = build_normalized_view(source, casefold=True)

    rendered = repr(view)
    assert source not in rendered
    assert "synthetic-sensitive-value" not in rendered
    assert "source_map" not in rendered
