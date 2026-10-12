"""Explicitly injected localhost administrative drain route."""

from dataclasses import asdict
from typing import Annotated

from fastapi import FastAPI
from pydantic import Field

from .contracts import StrictModel
from .formation import FormationDrain, job_limit


class DrainInput(StrictModel):
    max_jobs: Annotated[int, Field(ge=1)] | None = None


def register_formation(app: FastAPI, formation: FormationDrain) -> None:
    @app.post("/v1/memory/formation/drain")
    async def drain(body: DrainInput) -> dict[str, str | int]:
        job_limit(formation.config, body.max_jobs)
        result = await formation.drain(max_jobs=body.max_jobs)
        return {"status": result.status, **asdict(result)}
