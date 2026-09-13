from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from irodori_service.contracts import (
    VOICE_ID_PATTERN,
    ServiceError,
    SpeechRequest,
    validate_wav,
)


def register_voice(source: Path, metadata: Path, directory: Path) -> str:
    """既存IDの音声は上書きせず、hashを確認して同一資産だけ再利用する。"""
    record = json.loads(metadata.read_text(encoding="utf-8"))
    voice_id = record["voice_id"]
    if not isinstance(voice_id, str) or not VOICE_ID_PATTERN.fullmatch(voice_id):
        raise ValueError("invalid voice ID")
    payload = SpeechRequest.model_validate(record["reference_synthesis_request"])
    if payload.voice != voice_id:
        raise ValueError("reference synthesis voice ID mismatch")
    audio = source.read_bytes()
    validate_wav(audio)
    digest = hashlib.sha256(audio).hexdigest()
    if record["audio"]["sha256"] != digest:
        raise ValueError("reference WAV hash mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    normalized = json.dumps(
        {"voice_id": voice_id, "sha256": digest, "request": payload.model_dump()},
        ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8") + b"\n"
    # linkによる排他的な公開で、途中のファイルや競合した別資産を露出しない。
    _install_immutable(directory / f"{voice_id}.wav", audio)
    _install_immutable(directory / f"{voice_id}.json", normalized)
    return voice_id


def _install_immutable(target: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    try:
        temporary.chmod(0o444)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != content:
                raise ValueError("registered voice cannot be overwritten")
    finally:
        temporary.unlink()


class RegisteredVoices:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def resolve(self, voice_id: str) -> Path:
        if not VOICE_ID_PATTERN.fullmatch(voice_id):
            raise ServiceError("tts_voice_invalid", 422)
        target = self.directory / f"{voice_id}.wav"
        metadata = self.directory / f"{voice_id}.json"
        try:
            if target.is_symlink() or metadata.is_symlink():
                raise ServiceError("tts_voice_changed", 409)
            record = json.loads(metadata.read_text(encoding="utf-8"))
            if record["voice_id"] != voice_id:
                raise ServiceError("tts_voice_changed", 409)
            audio = target.read_bytes()
            if hashlib.sha256(audio).hexdigest() != record["sha256"]:
                raise ServiceError("tts_voice_changed", 409)
            validate_wav(audio)
        except FileNotFoundError as error:
            raise ServiceError("tts_voice_not_found", 404) from error
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ServiceError("tts_voice_invalid", 422) from error
        return target

    def warmup_request(self, voice_id: str) -> SpeechRequest:
        self.resolve(voice_id)
        record = json.loads((self.directory / f"{voice_id}.json").read_text())
        payload = SpeechRequest.model_validate(record["request"])
        # 本文は会話データを使わない固定fixture。声・推論条件は登録済みのものを使う。
        return payload.model_copy(update={"input": "こんにちは。音声の準備ができました。"})

    def list_ids(self) -> list[str]:
        voices = []
        for metadata in sorted(self.directory.glob("*.json")):
            self.resolve(metadata.stem)
            voices.append(metadata.stem)
        return voices


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(register_voice(args.audio, args.metadata, args.directory))


if __name__ == "__main__":
    main()
