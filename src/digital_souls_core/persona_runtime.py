"""Registered evaluation entries use the production inference coordinators."""

from typing import Any, Protocol

from .application import Inference
from .contracts import CompletionInput
from .conversations import Conversations
from .errors import CoreError
from .history import TurnInput
from .persona_evaluation import Case, CaseSet, Judge, Observation, aggregate


class Entry(Protocol):
    async def observe(self, case: Case, run: int) -> Observation: ...


class ChatEntry:
    def __init__(self, inference: Inference, character_id: str) -> None:
        self.inference = inference
        self.character_id = character_id

    async def observe(self, case: Case, run: int) -> Observation:
        prepared = await self.inference.prepare(
            self.character_id,
            CompletionInput.model_validate({"messages": [m.model_dump() for m in case.messages]}),
            alias=False,
        )
        self.inference.check(prepared)
        result = await self.inference.provider.complete(
            prepared.character.config.profile, prepared.payload
        )
        if len(result["choices"]) != 1 or result["choices"][0]["finish_reason"] != "stop":
            raise ValueError("incomplete response")
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("nontext response")
        return Observation(
            entry="chat",
            run=run,
            case_id=case.id,
            text=text,
            card_sha256=prepared.headers["X-Character-Card-SHA256"],
        )


class HistorySession(Protocol):
    """Fresh scoped history per case/run, with any prior conversation seeded."""

    def start(self, case: Case) -> tuple[str, int]: ...
    def finish(self, conversation_id: str) -> None: ...


class HistoryEntry:
    def __init__(self, service: Conversations, character_id: str, session: HistorySession) -> None:
        self.service = service
        self.character_id = character_id
        self.session = session

    async def observe(self, case: Case, run: int) -> Observation:
        cid, revision = self.session.start(case)
        try:
            # Do not put rejected system input in a fake stored history: it could
            # never be saved through the production API. Exercise its rejection.
            caller = [m for m in case.messages if m.role == "system"]
            receipt = await self.service.complete(
                self.character_id,
                cid,
                TurnInput.model_validate(
                    {
                        "request_id": f"persona-{run}",
                        "expected_revision": revision,
                        "messages": [m.model_dump() for m in (*caller, case.messages[-1])],
                    }
                ),
            )
            return Observation(
                entry="history",
                run=run,
                case_id=case.id,
                text=receipt.message.content,
                card_sha256=receipt.card_sha256,
            )
        except CoreError as error:
            if error.code == "invalid_turn" and any(m.role == "system" for m in case.messages):
                return Observation(
                    entry="history",
                    run=run,
                    case_id=case.id,
                    text=None,
                    card_sha256=None,
                    status="NOT RUN",
                    reason="caller_system_unsupported",
                )
            raise
        finally:
            self.session.finish(cid)


async def evaluate(
    cases: CaseSet,
    entries: dict[str, Entry],
    *,
    judge: Judge,
    commit: str,
    commit_dirty: bool = False,
    runs: int = 3,
    mismatch_policy: str = "warn",
    include_responses: bool = False,
) -> dict[str, Any]:
    if type(runs) is not int or runs < 1 or not {"chat", "history"} <= entries.keys():
        raise ValueError("empty evaluation")
    if mismatch_policy not in ("warn", "gate"):
        raise ValueError("invalid mismatch policy")
    cases = CaseSet.model_validate(cases.model_dump())
    fixed_version = cases.version
    observations = []
    for run in range(1, runs + 1):
        for name, entry in entries.items():
            for case in cases.cases:
                try:
                    observation = await entry.observe(case, run)
                except Exception:
                    observation = Observation(
                        entry=name,
                        run=run,
                        case_id=case.id,
                        text=None,
                        card_sha256=None,
                        status="ERROR",
                        reason="evaluation_failed",
                    )
                if (observation.entry, observation.run, observation.case_id) != (
                    name,
                    run,
                    case.id,
                ):
                    raise ValueError("entry returned a different observation address")
                observations.append(observation)
    if cases.version != fixed_version:
        raise ValueError("case set changed during evaluation")
    report = aggregate(
        cases,
        observations,
        entries=tuple(entries),
        runs=runs,
        commit=commit,
        commit_dirty=commit_dirty,
        mismatch_policy="gate" if mismatch_policy == "gate" else "warn",
        include_responses=include_responses,
    )
    if "activity" in entries:
        del report["not_run"]["activity"]
    report["entries"] = list(entries)
    identity = dict(judge.identity)
    if set(identity) != {"id", "model", "prompt", "settings"} or any(
        not v for v in identity.values()
    ):
        raise ValueError("fixed judge identity required")
    qualitative = {}
    for name in entries:
        responses = [
            x.text
            for x in observations
            if x.entry == name and x.status == "RUN" and x.text is not None
        ]
        tendencies = await judge.estimate(responses)
        if set(tendencies) != {"CP", "NP", "A", "FC", "AC"}:
            raise ValueError("invalid qualitative judge report")
        qualitative[name] = {"tendencies": tendencies, "response_count": len(responses)}
    if judge.identity != identity:
        raise ValueError("judge identity changed during evaluation")
    report["egogram"] = {
        "judge": identity,
        "entries": qualitative,
        "real_judge": "NOT RUN",
        "used_for_verdict": False,
    }
    return report
