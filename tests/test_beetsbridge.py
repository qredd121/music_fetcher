from __future__ import annotations

from pathlib import Path

import yaml

from music_fetcher import beetsbridge
from music_fetcher.beetsbridge import adopt_library, generate_config, import_path
from music_fetcher.config import (
    AppConfig,
    BeetsConfig,
    DownloadConfig,
    PathsConfig,
    SearchConfig,
    SlskdConfig,
)


def _config(tmp_path: Path, exclude: list[str] | None = None) -> AppConfig:
    return AppConfig(
        slskd=SlskdConfig(),
        paths=PathsConfig(
            downloads=tmp_path / "dl",
            music=tmp_path / "Music",
            state=tmp_path / "state",
            exclude=exclude or [],
        ),
        search=SearchConfig(),
        download=DownloadConfig(),
        beets=BeetsConfig(),
    )


def test_generate_config_pins_directory_and_templates(tmp_path):
    config = _config(tmp_path)
    path = generate_config(config)
    doc = yaml.safe_load(path.read_text())

    assert doc["directory"] == str(tmp_path / "Music")
    assert "inline" in doc["plugins"]
    assert "musicbrainz" not in doc["plugins"]
    assert "languages" not in doc["import"]
    assert "artist_letter" in doc["item_fields"]
    assert "artist_letter" in doc["album_fields"]
    assert doc["paths"]["default"].startswith("$artist_letter/")
    assert doc["paths"]["singleton"].startswith("$artist_letter/")
    assert doc["paths"]["comp"].startswith("$artist_letter/")
    # No naive %left bucket that dumps empty-artist items into the root.
    assert "%left" not in doc["paths"]["default"]


def test_generate_config_writes_ignore_globs(tmp_path):
    config = _config(tmp_path, exclude=["I/Incelcore"])
    doc = yaml.safe_load(generate_config(config).read_text())
    assert "Incelcore" in doc["ignore"]
    # Built-in defaults are preserved.
    assert ".*" in doc["ignore"]


def test_adopt_library_is_in_place(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    def fake_run_beet(cfg, args, *, extra_config=None, plugins=None, capture=True):
        captured["args"] = args
        captured["extra_config"] = extra_config
        captured["plugins"] = plugins

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(beetsbridge, "run_beet", fake_run_beet)
    adopt_library(config)

    args = captured["args"]
    # Real CLI flags force in-place behavior.
    assert "-C" in args and "-M" in args and "-W" in args
    # The broken `--set import.*` approach must not be used.
    assert "--set" not in args
    assert not any(str(a).startswith("import.") for a in args)
    # Plugins disabled via -p '' (global flag), not a second -c overlay that
    # would shadow the base config's `ignore` exclusions.
    assert captured["plugins"] == ""
    assert captured["extra_config"] is None


def test_adopt_rebuild_backs_up_and_deletes_db(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db = tmp_path / "beets" / "library.db"
    db.parent.mkdir(parents=True)
    db.write_text("olddata")

    monkeypatch.setattr(beetsbridge, "default_library_db", lambda: db)
    monkeypatch.setattr(
        beetsbridge, "run_beet", lambda *a, **k: type("R", (), {"returncode": 0})()
    )

    adopt_library(config, rebuild=True)

    backup = db.with_suffix(db.suffix + ".bak")
    assert backup.exists()
    assert backup.read_text() == "olddata"
    assert not db.exists()


def test_import_path_is_as_is(tmp_path, monkeypatch):
    config = _config(tmp_path)
    source = tmp_path / "album"
    source.mkdir()
    (source / "01.flac").write_bytes(b"x")
    captured = {}

    def fake_run_beet(cfg, args, *, extra_config=None, plugins=None, capture=True):
        captured["args"] = args
        captured["extra_config"] = extra_config
        (source / "01.flac").unlink()
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(beetsbridge, "run_beet", fake_run_beet)
    outcome = import_path(config, source)

    assert outcome.state == "imported"
    assert captured["args"][:3] == ["import", "-A", "--quiet"]
    assert "--search-id" not in captured["args"]
    assert captured["extra_config"] is None


def test_import_path_reports_leftovers(tmp_path, monkeypatch):
    config = _config(tmp_path)
    source = tmp_path / "single"
    source.mkdir()
    (source / "01.flac").write_bytes(b"x")
    captured = {}

    def fake_run_beet(cfg, args, *, extra_config=None, plugins=None, capture=True):
        captured["args"] = args
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(beetsbridge, "run_beet", fake_run_beet)
    outcome = import_path(config, source, singletons=True)

    assert "--singletons" in captured["args"]
    assert outcome.state == "skipped"
    assert source.exists()


def test_artist_letter_transliterates():
    from beetsplug.inline import _compile_func

    letter = _compile_func(beetsbridge._ARTIST_LETTER_FIELD, "albumartist, artist")
    assert letter("Янка Дягилева", "") == "I"
    assert letter("Оргазм Нострадамуса", "") == "O"
    assert letter("aphex twin", "") == "A"
    assert letter("", "") == "#"
    assert letter("2Pac", "") == "#"
