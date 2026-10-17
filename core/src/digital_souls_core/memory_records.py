"""Canonical memory values and invariants, independent of persistence and inference.

These values validate structure, not source eligibility or truth. A storage port
must recheck current source versions, permissions and referenced records.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from itertools import combinations

from .history import Binding, SourceReference
from .memory_contracts import SourceVersion


class RecordKind(Enum):
    EPISODE = "episode"
    FACT = "fact"
    SEMANTIC = "semantic"
    EPISODE_FACT_LINK = "episode_fact_link"


class RecordState(Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Speaker(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    DEVELOPER = "developer"
    TOOL = "tool"


class EpisodeContext(Enum):
    ACTUAL = "actual"
    HYPOTHETICAL = "hypothetical"
    FICTION = "fiction"


class FormationType(Enum):
    DIRECT_EXTRACTION = "direct_extraction"
    EXPERIENCE_DERIVED = "experience_derived"


class TimePrecision(Enum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"


def _nonempty(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty text required")


def _integer(value: int, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("integer outside permitted range")


def _aware(value: datetime | None, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")


def _binding(value: Binding) -> None:
    if not isinstance(value, Binding):
        raise ValueError("Binding required")


def _source_reference(value: SourceReference) -> None:
    if not isinstance(value, SourceReference):
        raise ValueError("SourceReference required")
    _nonempty(value.conversation_id)
    _integer(value.turn_revision, 1)
    _integer(value.message_index, 0)


@dataclass(frozen=True)
class Citation:
    """Half-open character offsets [start, end) in one versioned history message.

    The quote is addressed, not copied. Bounds against the actual message and
    correspondence of speaker/Binding to history are the storage port's duty.
    """

    binding: Binding
    source: SourceVersion
    speaker: Speaker
    start: int
    end: int

    def __post_init__(self) -> None:
        _binding(self.binding)
        if not isinstance(self.source, SourceVersion) or not isinstance(self.speaker, Speaker):
            raise ValueError("versioned source and explicit speaker required")
        _source_reference(self.source.reference)
        _integer(self.source.epoch, 0)
        _integer(self.start, 0)
        _integer(self.end, 0)
        if self.start >= self.end:
            raise ValueError("citation range must be nonempty")


def _citations(
    values: tuple[Citation, ...], *, binding: Binding | None = None, required: bool = False
) -> None:
    if type(values) is not tuple or (required and not values):
        raise ValueError("immutable citations required")
    for value in values:
        if not isinstance(value, Citation):
            raise ValueError("Citation required")
        if binding is not None and value.binding != binding:
            raise ValueError("citation Binding mismatch")


@dataclass(frozen=True, kw_only=True)
class PartialDateTime:
    """Calendar components known at exactly the stated precision, without filling gaps."""

    precision: TimePrecision
    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    second: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.precision, TimePrecision):
            raise ValueError("TimePrecision required")
        depth = list(TimePrecision).index(self.precision)
        components = (self.year, self.month, self.day, self.hour, self.minute, self.second)
        bounds = ((1, 9999), (1, 12), (1, 31), (0, 23), (0, 59), (0, 59))
        for index, (value, (minimum, maximum)) in enumerate(zip(components, bounds, strict=True)):
            if index > depth:
                if value is not None:
                    raise ValueError("component exceeds stated precision")
            elif type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("missing or invalid calendar component")
        if self.month is not None and self.day is not None:
            date(self.year, self.month, self.day)

    def _components(self) -> tuple[int, ...]:
        return tuple(
            value
            for value in (self.year, self.month, self.day, self.hour, self.minute, self.second)
            if value is not None
        )


@dataclass(frozen=True, kw_only=True)
class TemporalValue:
    """Unknown (no endpoints), one partial point, or an inclusive ordered range.

    A month point is not a duration. Range endpoints must have the same precision:
    ordering different precisions would require inventing unknown components.
    timezone records interpretation context; this type does not interpret dates.
    """

    start: PartialDateTime | None = None
    end: PartialDateTime | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        if self.timezone is not None:
            _nonempty(self.timezone)
        for value in (self.start, self.end):
            if value is not None and not isinstance(value, PartialDateTime):
                raise ValueError("PartialDateTime required")
        if self.end is not None:
            if self.start is None:
                raise ValueError("range start required")
            if self.start.precision is not self.end.precision:
                raise ValueError("range endpoints must have comparable precision")
            if self.start._components() > self.end._components():
                raise ValueError("range start must not exceed end")


@dataclass(frozen=True)
class ExplicitReason:
    """Caller-supplied explicitly stated reason, never an inferred motivation."""

    text: str = field(repr=False)
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        _nonempty(self.text)
        _citations(self.citations, required=True)


@dataclass(frozen=True, kw_only=True)
class FiveW:
    predicate: str = field(repr=False)
    object: str | None = field(default=None, repr=False)
    who: str | None = field(default=None, repr=False)
    when: TemporalValue | None = None
    where: str | None = field(default=None, repr=False)
    why: ExplicitReason | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _nonempty(self.predicate)
        for value in (self.object, self.who, self.where):
            if value is not None:
                _nonempty(value)
        if self.when is not None and not isinstance(self.when, TemporalValue):
            raise ValueError("TemporalValue required")
        if self.why is not None and not isinstance(self.why, ExplicitReason):
            raise ValueError("ExplicitReason required")


def _five_w(value: FiveW, binding: Binding) -> None:
    if not isinstance(value, FiveW):
        raise ValueError("FiveW required")
    if value.why is not None:
        _citations(value.why.citations, binding=binding, required=True)


def _time(value: TemporalValue) -> None:
    if not isinstance(value, TemporalValue):
        raise ValueError("TemporalValue required, including for unknown dates")


@dataclass(frozen=True, kw_only=True)
class RecordRef:
    kind: RecordKind
    record_id: str
    version: int
    binding: Binding

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecordKind):
            raise ValueError("RecordKind required")
        _nonempty(self.record_id)
        _integer(self.version, 1)
        _binding(self.binding)


@dataclass(frozen=True, kw_only=True)
class RecordHead:
    """Content-free current identity/version/state, also usable after body erasure."""

    reference: RecordRef
    state: RecordState

    def __post_init__(self) -> None:
        if not isinstance(self.reference, RecordRef) or not isinstance(self.state, RecordState):
            raise ValueError("typed record reference and state required")


def _reference(value: RecordRef, kind: RecordKind, binding: Binding | None = None) -> None:
    if not isinstance(value, RecordRef) or value.kind is not kind:
        raise ValueError("reference kind mismatch")
    if binding is not None and value.binding != binding:
        raise ValueError("reference Binding mismatch")


@dataclass(frozen=True, kw_only=True)
class _RecordFields:
    """Private field reuse; not a catch-all memory record."""

    version: int
    binding: Binding
    normalized_text: str = field(repr=False)
    created_at: datetime
    last_user_mentioned_at: datetime | None
    state: RecordState

    def __post_init__(self) -> None:
        _integer(self.version, 1)
        _binding(self.binding)
        _nonempty(self.normalized_text)
        _aware(self.created_at, required=True)
        _aware(self.last_user_mentioned_at)
        if not isinstance(self.state, RecordState):
            raise ValueError("RecordState required")


@dataclass(frozen=True, kw_only=True)
class Episode(_RecordFields):
    episode_id: str
    five_w: FiveW = field(repr=False)
    experience_time: TemporalValue
    experienced_at: datetime | None
    context: EpisodeContext
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonempty(self.episode_id)
        _five_w(self.five_w, self.binding)
        _time(self.experience_time)
        _aware(self.experienced_at)
        if not isinstance(self.context, EpisodeContext):
            raise ValueError("EpisodeContext required")
        _citations(self.citations, binding=self.binding, required=True)


@dataclass(frozen=True, kw_only=True)
class Fact(_RecordFields):
    """One content version of a caller-assigned stable Fact identity.

    Every version owns its FiveW, target time, normalized text and citations.
    Persistence keeps prior versions; no identity is derived from content.
    """

    fact_id: str
    five_w: FiveW = field(repr=False)
    target_time: TemporalValue
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonempty(self.fact_id)
        _five_w(self.five_w, self.binding)
        _time(self.target_time)
        _citations(self.citations, binding=self.binding, required=True)


@dataclass(frozen=True, kw_only=True)
class EpisodeFactLink:
    """Content-free, versioned association between one Episode and one Fact."""

    link_id: str
    version: int
    binding: Binding
    created_at: datetime
    state: RecordState
    episode: RecordRef
    fact: RecordRef

    def __post_init__(self) -> None:
        _nonempty(self.link_id)
        _integer(self.version, 1)
        _binding(self.binding)
        _aware(self.created_at, required=True)
        if not isinstance(self.state, RecordState):
            raise ValueError("RecordState required")
        _reference(self.episode, RecordKind.EPISODE, self.binding)
        _reference(self.fact, RecordKind.FACT, self.binding)


@dataclass(frozen=True, kw_only=True)
class Proposition:
    subject: str = field(repr=False)
    attribute: str = field(repr=False)
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.subject, self.attribute, self.value):
            _nonempty(value)


@dataclass(frozen=True, kw_only=True)
class EpisodeEvidence:
    """Episode reference plus all its citation source addresses for independence.

    Epoch, quote range and content version never make the same source independent.
    The port must verify this set against the referenced Episode's citations.
    """

    reference: RecordRef
    sources: frozenset[SourceReference]

    def __post_init__(self) -> None:
        _reference(self.reference, RecordKind.EPISODE)
        if type(self.sources) is not frozenset or not self.sources:
            raise ValueError("nonempty immutable Episode source set required")
        for source in self.sources:
            _source_reference(source)


def _independent_pair(values: tuple[EpisodeEvidence, ...]) -> bool:
    by_id: dict[str, set[SourceReference]] = {}
    for value in values:
        by_id.setdefault(value.reference.record_id, set()).update(value.sources)
    return any(first.isdisjoint(second) for first, second in combinations(by_id.values(), 2))


@dataclass(frozen=True, kw_only=True)
class Semantic(_RecordFields):
    semantic_id: str
    formation_type: FormationType
    proposition: Proposition = field(repr=False)
    applicability: TemporalValue
    citations: tuple[Citation, ...]
    episode_evidence: tuple[EpisodeEvidence, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonempty(self.semantic_id)
        if not isinstance(self.formation_type, FormationType):
            raise ValueError("FormationType required")
        if not isinstance(self.proposition, Proposition):
            raise ValueError("Proposition required")
        _time(self.applicability)
        _citations(self.citations, binding=self.binding)
        if type(self.episode_evidence) is not tuple:
            raise ValueError("immutable Episode evidence required")
        for value in self.episode_evidence:
            if not isinstance(value, EpisodeEvidence):
                raise ValueError("EpisodeEvidence required")
            _reference(value.reference, RecordKind.EPISODE, self.binding)
        if self.formation_type is FormationType.DIRECT_EXTRACTION:
            if not any(value.speaker is Speaker.USER for value in self.citations):
                raise ValueError("direct extraction requires user citation")
        elif not _independent_pair(self.episode_evidence):
            raise ValueError("experience derivation requires two independent Episodes")


def dependency_invalidated(
    reference: RecordRef, current: RecordHead | Episode | Fact | Semantic | EpisodeFactLink | None
) -> bool:
    """Fail closed unless current identity, Binding, version and active state match.

    Missing records, future references and mismatched identities are invalid too.
    No storage access, mutation or Reflection-specific behavior is performed.
    """
    if current is None:
        return True
    if isinstance(current, RecordHead):
        return reference != current.reference or current.state is not RecordState.ACTIVE
    if isinstance(current, Episode):
        kind, record_id = RecordKind.EPISODE, current.episode_id
    elif isinstance(current, Fact):
        kind, record_id = RecordKind.FACT, current.fact_id
    elif isinstance(current, Semantic):
        kind, record_id = RecordKind.SEMANTIC, current.semantic_id
    else:
        kind, record_id = RecordKind.EPISODE_FACT_LINK, current.link_id
    return (
        reference.kind is not kind
        or reference.record_id != record_id
        or reference.binding != current.binding
        or reference.version != current.version
        or current.state is not RecordState.ACTIVE
    )
