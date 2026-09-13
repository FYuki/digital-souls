"""採用案の評価入口。通常アプリと同じ抽出器・schemaを使用する。"""
import hashlib
from pathlib import Path
from app.memory.formation.compact_extractor import *  # noqa: F403


def design_fingerprint():
    from app.memory.formation import compact_extractor, ground_schema
    digest = hashlib.sha256()
    for module in (compact_extractor, ground_schema):
        path = Path(module.__file__)
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
