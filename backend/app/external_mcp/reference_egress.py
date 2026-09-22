"""Core登録済みreadの参照IDだけを許可する決定的な送信形状検査。"""
import re

from .models import Json

_REFERENCE = re.compile(r"^[A-Za-z0-9_.:~-]{1,256}$")


def reference_arguments_allowed(arguments: Json) -> bool:
    # 本文、URL、ネストした任意payloadはこの経路で送らない。
    # 呼出側guardは値の出所・登録時対象との一致を、共有Policyはsecretを再検証する。
    return len(arguments) <= 64 and all(
        isinstance(key, str) and _REFERENCE.fullmatch(key) is not None and (
            isinstance(value, str) and _REFERENCE.fullmatch(value) is not None
            or type(value) is int and 0 <= value <= 2**53 - 1
            or type(value) is bool
        )
        for key, value in arguments.items()
    )
