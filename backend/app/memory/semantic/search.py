"""属性名と本人の根拠発言から、ベクトル検索を補う候補を選ぶ。"""

import re
import unicodedata


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _trigrams(value: str) -> set[str]:
    return {value[i:i + 3] for i in range(max(0, len(value) - 2))}


def lexical_relevance(query: str, *, predicate: str, value: str, quotes: tuple[str, ...]) -> float:
    query = _normalize(query)
    predicate, value = _normalize(predicate), _normalize(value)
    grams = _trigrams(query)
    # 「私の」等の共通表現だけでは候補にしない。意味のない一文字一致も使わない。
    exact = 20.0 if len(predicate) >= 2 and predicate in query else 0.0
    value_exact = 4.0 if len(value) >= 2 and value in query else 0.0
    property_overlap = len(grams & _trigrams(predicate + value))
    quote_overlap = max((len(grams & _trigrams(_normalize(text))) for text in quotes), default=0)
    if not exact and not value_exact and max(property_overlap, quote_overlap) < 2:
        return 0.0
    return exact + value_exact + property_overlap + 0.5 * quote_overlap