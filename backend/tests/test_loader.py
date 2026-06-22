import pytest


class TestLoadPersonality:
    def test_returns_string_for_existing_character(self):
        from app.characters.loader import load_personality

        result = load_personality("miori")

        assert isinstance(result, str)

    def test_returns_nonempty_content_for_miori(self):
        from app.characters.loader import load_personality

        result = load_personality("miori")

        assert len(result) > 0

    def test_returned_content_includes_character_name(self):
        from app.characters.loader import load_personality

        result = load_personality("miori")

        assert "光織" in result

    def test_raises_file_not_found_for_unknown_character(self):
        from app.characters.loader import load_personality

        with pytest.raises(FileNotFoundError):
            load_personality("nonexistent_character_xyz_99999")

    def test_raises_exactly_file_not_found_not_subclass(self):
        from app.characters.loader import load_personality

        with pytest.raises(FileNotFoundError) as exc_info:
            load_personality("nonexistent_character_xyz_99999")

        assert exc_info.type is FileNotFoundError

    def test_returns_plain_str_not_bytes(self):
        from app.characters.loader import load_personality

        result = load_personality("miori")

        assert type(result) is str

    def test_raises_file_not_found_when_personality_is_directory(self, tmp_path, monkeypatch):
        char_dir = tmp_path / "characters" / "dirchar"
        char_dir.mkdir(parents=True)
        (char_dir / "personality.md").mkdir()

        import app.characters.loader as loader_module

        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        from app.characters.loader import load_personality

        with pytest.raises(FileNotFoundError):
            load_personality("dirchar")

    def test_content_with_tmp_path(self, tmp_path, monkeypatch):
        char_dir = tmp_path / "characters" / "testchar"
        char_dir.mkdir(parents=True)
        expected = "# TestChar\nThis is the system prompt."
        (char_dir / "personality.md").write_text(expected, encoding="utf-8")

        import app.characters.loader as loader_module

        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        from app.characters.loader import load_personality

        result = load_personality("testchar")

        assert result == expected

    def test_rejects_path_traversal_outside_characters_directory(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        inside_dir = repo_root / "characters" / "safe"
        inside_dir.mkdir(parents=True)
        (inside_dir / "personality.md").write_text("safe prompt", encoding="utf-8")

        outside_dir = repo_root / "secrets"
        outside_dir.mkdir()
        (outside_dir / "personality.md").write_text("outside prompt", encoding="utf-8")

        import app.characters.loader as loader_module

        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: repo_root)

        from app.characters.loader import load_personality

        with pytest.raises(FileNotFoundError):
            load_personality("../secrets")
