"""全スレッドの同一キャラクター知識から、設定された入力予算内の照合候補を選ぶ。"""

from collections.abc import Callable
import re
import unicodedata

from app.memory.semantic.contracts import SemanticRecord


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _grams(text: str) -> set[str]:
    return {text[index:index + size] for size in (2, 3) for index in range(max(0, len(text) - size + 1))}


def select_catalog(
    catalog: tuple[SemanticRecord, ...], *, conversation: str,
    fits: Callable[[tuple[SemanticRecord, ...]], bool],
) -> tuple[SemanticRecord, ...]:
    """収まる場合は全件を保持する。超過時だけ属性・値と会話の一致を優先する。"""
    if fits(catalog):
        return catalog
    if not fits(()):
        raise ValueError("semantic input and schema exceed the configured token budget")
    query = _normalize(conversation)
    query_grams = _grams(query)

    def relevance(record: SemanticRecord) -> tuple[float, float]:
        proposition = record.proposition
        if proposition is None:
            return -1, record.updated_at.timestamp()
        predicate, value = _normalize(proposition.predicate), _normalize(proposition.value)
        # 主語「ユーザー」など全件共通の文字列では順位を上げない。
        predicate_grams = _grams(predicate)
        value_grams = _grams(value)
        score = (
            (20 if len(predicate) >= 2 and predicate in query else 0)
            + (4 if len(value) >= 2 and value in query else 0)
            + 8 * len(predicate_grams & query_grams) / max(1, len(predicate_grams))
            + len(value_grams & query_grams) / max(1, len(value_grams))
        )
        return score, record.updated_at.timestamp()

    ranked = tuple(sorted(catalog, key=relevance, reverse=True))
    # 個々のレコードとschemaを含む、本番clientのtoken見積りを使う。
    low, high = 0, len(ranked)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(ranked[:middle]):
            low = middle
        else:
            high = middle - 1
    if catalog and low == 0:
        raise ValueError("semantic input leaves no budget for a catalog entry")
    return ranked[:low]
