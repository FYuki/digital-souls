import json
from pathlib import Path

import pytest


def character_card_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "テスト人格",
        "description": "キャラクター概要",
        "personality": "性格と話し方",
        "scenario": "ユーザーとの関係",
        "first_mes": "初回だけの挨拶",
        "mes_example": "<START>\n会話例",
        "creator_notes": "",
        "system_prompt": "必ず守る指示",
        "post_history_instructions": "最後に守る指示",
        "alternate_greetings": [],
        "group_only_greetings": [],
        "tags": [],
        "creator": "test-suite",
        "character_version": "1.0",
        "extensions": {
            "digital_souls": {
                "tts_config": {
                    "engine": "voicevox",
                    "speaker_id": 14,
                    "speaker_name": "テスト話者",
                    "style_name": "ノーマル",
                }
            }
        },
    }
    data.update(overrides)
    return data


def character_card_document(
    *,
    spec: object = "chara_card_v3",
    spec_version: object = "3.0",
    data: object | None = None,
    **extra_fields: object,
) -> dict[str, object]:
    document: dict[str, object] = {
        "spec": spec,
        "spec_version": spec_version,
        "data": character_card_data() if data is None else data,
    }
    document.update(extra_fields)
    return document


def write_character_card(
    repo_root: Path,
    character: str,
    document: object,
) -> Path:
    character_dir = repo_root / "characters" / character
    character_dir.mkdir(parents=True)
    card_path = character_dir / f"{character}.card.json"
    card_path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    return card_path


def use_character_repo_root(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
) -> None:
    import app.characters.loader as loader

    monkeypatch.setattr(loader, "_get_repo_root", lambda: repo_root)
