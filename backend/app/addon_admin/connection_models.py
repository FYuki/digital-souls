"""管理UIの型付き入力。任意Manifest・secret参照を入力として受け取らない。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.external_mcp.models import Connection, Json


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


def clean_text(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid_text")
    return value


class HTTPSettings(StrictInput):
    transport: Literal["streamable_http"]
    endpoint: str = Field(min_length=1, max_length=4096)
    auth: Literal["none", "bearer"] = "none"

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return clean_text(value)


class StdioSettings(StrictInput):
    transport: Literal["stdio"]
    command: str = Field(min_length=1, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("command")
    @classmethod
    def valid_command(cls, value: str) -> str:
        clean_text(value)
        # shellへ渡す文字列ではなく、単一の実行ファイル名/絶対パスだけを扱う。
        if any(char in value for char in "|;&<>`$") or (
            " " in value and not value.startswith("/")
        ):
            raise ValueError("invalid_command")
        return value

    @field_validator("args")
    @classmethod
    def valid_args(cls, values: list[str]) -> list[str]:
        if any(len(value) > 8192 for value in values):
            raise ValueError("invalid_args")
        for value in values:
            clean_text(value)
        return values


class ConnectionInput(StrictInput):
    display_name: str = Field(min_length=1, max_length=128)
    settings: Annotated[HTTPSettings | StdioSettings, Field(discriminator="transport")]

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid_name")
        return clean_text(value)

    def connection(
        self, connection_id: str, secret_ref: str, previous: Connection | None = None
    ) -> Connection:
        manifest: Json = (
            previous.manifest
            if previous
            else {
                "manifest_version": "1.0",
                "capabilities": {
                    "source": "mcp_discovery",
                    "tools": [],
                    "resources": [],
                    "prompts": [],
                },
                "core_policy": {
                    "enabled": False,
                    "sharing": {"mode": "shared"},
                    "resource_binding_required": False,
                    "restrictions": [],
                    "execution_budget": {
                        "max_calls_per_loop": 6,
                        "max_consecutive_same_tool": 3,
                        "max_identical_call": 2,
                        "normal_max_auto_cycles": 3,
                    },
                },
            }
        )
        old = manifest.get("connection", {})
        c: Json = {
            "id": connection_id,
            "ownership": "external",
            "protocol": "mcp",
            "transport": self.settings.transport,
            "trust": old.get("trust", {"annotations": False, "addon_metadata": False}),
            "grant": old.get(
                "grant",
                {"scope": "validated_snapshot", "activation": "next_execution_loop"},
            ),
            "auth": {"type": "none"},
        }
        if isinstance(self.settings, HTTPSettings):
            c["endpoint"] = self.settings.endpoint
            if self.settings.auth == "bearer":
                c["auth"] = {"type": "bearer", "secret_ref": secret_ref}
        else:
            c["stdio"] = {"command": self.settings.command, "args": self.settings.args}
        manifest["connection"] = c
        return Connection.from_manifest(manifest)

    @classmethod
    def from_connection(
        cls, connection: Connection, display_name: str
    ) -> ConnectionInput:
        # 既存ManifestはConnectionで検証済み。新規フォームの長さ・文字種制限を
        # 移行時に遡及適用せず、SDKへ渡していた既存のargvをそのまま保存する。
        c = connection.manifest["connection"]
        settings = (
            HTTPSettings.model_construct(
                transport="streamable_http",
                endpoint=c["endpoint"],
                auth=c["auth"]["type"],
            )
            if c["transport"] == "streamable_http"
            else StdioSettings.model_construct(
                transport="stdio",
                args=c["stdio"].get("args", []),
                command=c["stdio"]["command"],
            )
        )
        return cls(display_name=display_name, settings=settings)


class CredentialInput(StrictInput):
    token: SecretStr = Field(min_length=1, max_length=16384)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or any(ord(char) < 33 or ord(char) > 126 for char in raw):
            raise ValueError("invalid_credential")
        return value
