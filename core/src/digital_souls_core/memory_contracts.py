"""Storage-independent source version shared by canonical records."""

from dataclasses import dataclass

from .history import SourceReference


@dataclass(frozen=True)
class SourceVersion:
    reference: SourceReference
    epoch: int
