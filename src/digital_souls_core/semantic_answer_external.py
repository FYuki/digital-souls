"""Opt-in external answer evaluation; production adapters and policy remain unchanged."""

import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .character import Profile
from .local_embedding import LocalEmbeddingProfile

ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
# Credentials cannot overwrite process controls or the evaluation/database configuration.
RESERVED = re.compile(
    r"^(?:PATH|HOME|TMPDIR|LANG|.*PROXY|DOTENV.*|NODE.*|PYTHON.*|DSC_.*|"
    r"PROMPTFOO_.*|LITELLM_.*|LD_.*|DYLD_.*)$"
)


class ExternalAnswerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: StrictBool = False
    chat: Profile
    embedding: LocalEmbeddingProfile
    answer_provider: Literal["litellm", "codex_subscription"] = "litellm"
    codex_executable: str | None = None
    codex_home: str | None = None
    api_key_env: str | None = Field(default=None, pattern=ENV_NAME)
    api_base_env: str | None = Field(default=None, pattern=ENV_NAME)

    def require_enabled(self) -> None:
        if (
            not self.enabled
            or not self.embedding.enabled
            or not self.chat.external_send_allowed
            or self.chat.transport != "sdk"
            or not re.fullmatch(r"openai/[A-Za-z0-9_.-]{1,128}", self.chat.model)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.embedding.model)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.embedding.model_digest)
            or any(RESERVED.fullmatch(n) for n in (self.api_key_env, self.api_base_env) if n)
        ):
            raise ValueError("Enabled external OpenAI SDK answer profile required")
        if self.answer_provider == "codex_subscription":
            if (
                self.api_key_env is not None
                or self.api_base_env is not None
                or not self.codex_executable
                or not Path(self.codex_executable).is_absolute()
                or not self.codex_home
                or not Path(self.codex_home).is_absolute()
            ):
                raise ValueError("Absolute Codex paths without API credentials required")
        elif (
            not self.api_key_env
            or self.api_key_env == self.api_base_env
            or self.codex_executable is not None
            or self.codex_home is not None
        ):
            raise ValueError("External API credentials required")


def load_external_profile(path: str | None) -> ExternalAnswerProfile:
    try:
        if path is None or not Path(path).is_absolute():
            raise ValueError
        resolved = Path(path).resolve(strict=True)
        # No repository-managed profile or symlink to one can enable external sending.
        if resolved.is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError
        tracked = subprocess.run(
            ["git", "-C", str(resolved.parent), "ls-files", "--error-unmatch", "--", resolved.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if tracked.returncode == 0:
            raise ValueError
        profile = ExternalAnswerProfile.model_validate_json(resolved.read_text(encoding="utf-8"))
        profile.require_enabled()
        return profile
    except Exception:
        raise ValueError("Invalid external answer profile") from None


def credential_environment(
    profile: ExternalAnswerProfile, inherited: Mapping[str, str]
) -> dict[str, str]:
    profile.require_enabled()
    if profile.answer_provider == "codex_subscription":
        return {}
    assert profile.api_key_env is not None
    mapped = {"OPENAI_API_KEY": profile.api_key_env}
    if profile.api_base_env:
        mapped["OPENAI_BASE_URL"] = profile.api_base_env
    if any(not inherited.get(name, "").strip() for name in mapped.values()):
        raise ValueError("External credentials unavailable")
    return {target: inherited[source] for target, source in mapped.items()}


@contextmanager
def external_credentials(profile: ExternalAnswerProfile) -> Iterator[None]:
    profile.require_enabled()
    if profile.answer_provider == "codex_subscription":
        yield
        return
    assert profile.api_key_env is not None
    values = credential_environment(profile, os.environ)
    # Clear even optional SDK names so ambient alternate credentials cannot be used.
    names = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", profile.api_key_env}
    if profile.api_base_env:
        names.add(profile.api_base_env)
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in previous.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
