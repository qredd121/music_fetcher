from __future__ import annotations

from music_fetcher.config import (
    default_library_db,
    excluded_ignore_globs,
    load_config,
)


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("PREFER_FORMAT", "flac,mp3 320")
    monkeypatch.setenv("SEARCH_TIMEOUT", "12")
    monkeypatch.setenv("SLSKD_API_KEY", "secret")
    config = load_config(config_path=tmp_path / "missing.toml")
    assert config.paths.music == (tmp_path / "music")
    assert config.paths.downloads == (tmp_path / "dl")
    assert config.download.format_preference == ["FLAC", "MP3 320"]
    assert config.search.timeout == 12.0
    assert config.slskd.api_key == "secret"


def test_toml_and_defaults(tmp_path, monkeypatch):
    for key in ("MUSIC_DIR", "DOWNLOADS_DIR", "PREFER_FORMAT", "SEARCH_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text(
        'log_level = "debug"\n'
        "[download]\n"
        "retries = 5\n"
    )
    config = load_config(config_path=toml)
    assert config.log_level == "DEBUG"
    assert config.download.retries == 5
    assert config.download.format_preference == ["FLAC", "MP3 320", "MP3 V0"]


def test_exclude_defaults_empty(tmp_path):
    config = load_config(config_path=tmp_path / "missing.toml")
    assert config.paths.exclude == []
    assert excluded_ignore_globs(config) == []


def test_exclude_parses_and_builds_globs(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[paths]\nexclude = ["I/Incelcore", "M/Some Artist/"]\n')
    config = load_config(config_path=toml)
    assert config.paths.exclude == ["I/Incelcore", "M/Some Artist/"]
    assert excluded_ignore_globs(config) == ["Incelcore", "Some Artist"]


def test_default_library_db_honors_beetsdir(monkeypatch, tmp_path):
    monkeypatch.setenv("BEETSDIR", str(tmp_path / "beets"))
    assert default_library_db() == tmp_path / "beets" / "library.db"
    monkeypatch.delenv("BEETSDIR", raising=False)
    assert default_library_db().name == "library.db"
