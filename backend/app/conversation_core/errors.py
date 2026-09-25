"""Conversation Coreで共有する内部例外。"""

from __future__ import annotations


class DeliveryError(RuntimeError):
    """delivery portがCore eventの送信に失敗した。"""
