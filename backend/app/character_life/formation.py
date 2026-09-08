"""#100のACTIVE projectionからLife Stateを形成し、正本の変更時は採用しない。"""

from collections.abc import Callable
from uuid import UUID

from app.external_mcp.models import digest, validate_arguments

from .cognition import FORMATION_SCHEMA, FormationPort, PrivacyPort
from .models import Kind, LifeError, LifeState, Result
from .ports import ReflectionSource
from .store import Store


class LifeFormation:
    def __init__(
        self,
        store: Store,
        source: ReflectionSource,
        cognition: FormationPort,
        privacy: PrivacyPort,
    ) -> None:
        self.store, self.source, self.cognition, self.privacy = (
            store,
            source,
            cognition,
            privacy,
        )

    async def run(
        self, character: str, *, check_current: Callable[[], None] = lambda: None
    ) -> Result:
        check_current()
        reflections = await self.source.active(character)
        check_current()
        if reflections is None:
            return Result.DEFERRED
        if len(reflections) > 16 or any(
            r.character_id != character or not r.active for r in reflections
        ):
            raise LifeError(Result.REJECTED, "reflection_boundary_invalid")
        ids = {r.id for r in reflections}
        if len(ids) != len(reflections):
            raise LifeError(Result.REJECTED, "reflection_duplicate")
        revisions = self.source.current_revisions(character)
        if revisions is None:
            return Result.DEFERRED
        if any(revisions.get(r.id) != r.revision for r in reflections):
            return Result.SUPERSEDED
        invalidated = self.store.reconcile_reflections(character, revisions)
        if not reflections:
            return Result.APPLIED if invalidated else Result.NO_CHANGE
        fingerprint = digest(sorted((str(r.id), r.revision) for r in reflections))
        if self.store.formation_exists(character, fingerprint):
            return Result.NO_CHANGE
        proposal = await self.cognition.form(
            [r.model_dump(mode="json") for r in reflections]
        )
        check_current()
        validate_arguments(FORMATION_SCHEMA, proposal)
        states = []
        for candidate in proposal["states"]:
            sources = tuple(UUID(value) for value in candidate["source_ids"])
            if not set(sources).issubset(ids):
                raise LifeError(Result.REJECTED, "reflection_evidence_invalid")
            if not await self.privacy.allowed(candidate["content"]):
                raise LifeError(Result.REJECTED, "state_privacy_blocked")
            check_current()
            states.append(
                LifeState(
                    character_id=character,
                    kind=Kind(candidate["kind"]),
                    content=candidate["content"],
                    source="reflection",
                    source_ids=sources,
                    reflection_revisions={i: revisions[i] for i in sources},
                )
            )
        current = await self.source.active(character)
        check_current()
        if current is None or any(
            r.character_id != character or not r.active for r in current
        ):
            return Result.SUPERSEDED
        if digest(sorted((str(r.id), r.revision) for r in current)) != fingerprint:
            return Result.SUPERSEDED
        latest = self.source.current_revisions(character)
        if latest is None or any(latest.get(r.id) != r.revision for r in reflections):
            return Result.SUPERSEDED
        return self.store.apply_formation(character, fingerprint, tuple(states))
