from pathlib import Path

import pytest

from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    use_character_repo_root,
    write_character_card,
)


class TestLoadTtsConfig:
    def test_should_read_typed_tts_config_from_digital_souls_extension(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_character_card(tmp_path, "test", character_card_document())
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import VoicevoxTtsConfig
        from app.characters.loader import load_tts_config

        config = load_tts_config("test")

        assert config == VoicevoxTtsConfig(speaker_id=14)

    def test_should_not_read_legacy_top_level_tts_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = character_card_data(extensions={})
        data["tts_config"] = {"engine": "voicevox", "speaker_id": 99}
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import TtsConfigMissingError
        from app.characters.loader import load_tts_config

        with pytest.raises(TtsConfigMissingError):
            load_tts_config("test")

    @pytest.mark.parametrize(
        "extensions",
        [
            {},
            {"digital_souls": {}},
        ],
    )
    def test_should_raise_typed_missing_error_when_tts_config_is_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        extensions: dict[str, object],
    ) -> None:
        data = character_card_data(extensions=extensions)
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import TtsConfigMissingError
        from app.characters.loader import load_tts_config

        with pytest.raises(TtsConfigMissingError):
            load_tts_config("test")

    @pytest.mark.parametrize("digital_souls", [None, []])
    def test_should_raise_validation_error_for_invalid_digital_souls_extension(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        digital_souls: object,
    ) -> None:
        data = character_card_data(
            extensions={"digital_souls": digital_souls}
        )
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import TtsConfigValidationError
        from app.characters.loader import load_tts_config

        with pytest.raises(TtsConfigValidationError):
            load_tts_config("test")

    @pytest.mark.parametrize(
        "tts_config",
        [
            None,
            [],
            {"engine": "other", "speaker_id": 14},
            {"engine": "voicevox"},
            {"engine": "voicevox", "speaker_id": True},
            {"engine": "voicevox", "speaker_id": "14"},
        ],
    )
    def test_should_raise_validation_error_for_invalid_tts_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tts_config: object,
    ) -> None:
        data = character_card_data(
            extensions={"digital_souls": {"tts_config": tts_config}}
        )
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import TtsConfigValidationError
        from app.characters.loader import load_tts_config

        with pytest.raises(TtsConfigValidationError):
            load_tts_config("test")
