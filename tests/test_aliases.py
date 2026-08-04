import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.aliases as aliases


def test_load_artist_groups_returns_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(aliases, "ALIASES_FILE", tmp_path / "missing.json")
    alias_map, display = aliases.load_artist_groups("PLxxx")
    assert alias_map == {}
    assert display == {}


def test_load_artist_groups_returns_empty_for_unknown_playlist(monkeypatch, tmp_path):
    path = tmp_path / "artist_groups.json"
    path.write_text(json.dumps({"PLother": {"Foo": ["Bar"]}}), encoding="utf-8")
    monkeypatch.setattr(aliases, "ALIASES_FILE", path)

    alias_map, display = aliases.load_artist_groups("PLxxx")
    assert alias_map == {}
    assert display == {}


def test_load_artist_groups_builds_normalized_alias_map(monkeypatch, tmp_path):
    path = tmp_path / "artist_groups.json"
    path.write_text(
        json.dumps({"PLxxx": {"Kaguya": ["ryo (supercell)", "Kaguya(cv. Yuko Natsuyoshi)"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(aliases, "ALIASES_FILE", path)

    alias_map, display = aliases.load_artist_groups("PLxxx")
    assert alias_map == {
        "kaguya": "kaguya",
        "ryo (supercell)": "kaguya",
        "kaguya(cv. yuko natsuyoshi)": "kaguya",
    }
    assert display == {"kaguya": "Kaguya"}
