from __future__ import annotations

import json
from pathlib import Path

from mediafile import MediaFile
from typer.testing import CliRunner

from music_fetcher import beetsbridge, cli
from music_fetcher.beetsbridge import ImportOutcome
from music_fetcher.jobs.db import JobStore
from music_fetcher.tags import META_FILE, PLAN_FILE, audio_files

from .test_tags import make_wav

runner = CliRunner()


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))


def _fake_beets(monkeypatch) -> list[Path]:
    calls: list[Path] = []

    def fake_import(config, source, *, singletons=False):
        calls.append(source)
        for path in audio_files(source):
            path.unlink()
        return ImportOutcome("imported", "ok")

    monkeypatch.setattr(beetsbridge, "import_path", fake_import)
    return calls


def _job(tmp_path: Path, folder: Path, meta: dict, kind: str = "album"):
    store = JobStore(tmp_path / "state" / "jobs.db")
    job = store.create(kind=kind, query="q", expected_files=1, payload={"meta": meta})
    store.update_state(job.id, "downloaded", set_fields={"folder": str(folder)})
    return store, job


def test_import_writes_meta_and_imports(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = _fake_beets(monkeypatch)
    folder = tmp_path / "dl" / "album"
    track = make_wav(folder / "01 - a.wav")
    tags_seen = {}

    def fake_import(config, source, *, singletons=False):
        media = MediaFile(str(track))
        tags_seen.update(albumartist=media.albumartist, year=media.year, title=media.title)
        calls.append(source)
        track.unlink()
        return ImportOutcome("imported", "ok")

    monkeypatch.setattr(beetsbridge, "import_path", fake_import)
    store, job = _job(tmp_path, folder, {"album": {"albumartist": "A", "album": "B", "year": 2001}})

    result = runner.invoke(cli.app, ["import", "--yes"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["results"][0]["state"] == "imported"
    assert tags_seen == {"albumartist": "A", "year": 2001, "title": "a"}
    assert store.get(job.id).state == "imported"


def test_import_quarantines_missing_tags(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = _fake_beets(monkeypatch)
    folder = tmp_path / "dl" / "Some Album"
    make_wav(folder / "01 - a.wav", artist="A")
    store, job = _job(tmp_path, folder, {})

    result = runner.invoke(cli.app, ["import", "--yes"])
    assert result.exit_code == 0, result.output
    entry = json.loads(result.stdout)["results"][0]
    assert entry["state"] == "quarantined"
    assert entry["missing"] == {"01 - a.wav": ["year"]}
    quarantined = tmp_path / "state" / "quarantine" / "Some Album"
    assert (quarantined / PLAN_FILE).exists()
    assert not folder.exists()
    assert calls == []
    saved = store.get(job.id)
    assert saved.state == "quarantined"
    assert saved.folder == str(quarantined)


def test_tags_set_on_orphan_quarantine_then_imports(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = _fake_beets(monkeypatch)
    folder = tmp_path / "state" / "quarantine" / "CD2 - Внимание"
    make_wav(folder / "01 - a.wav", artist="Кино")
    make_wav(folder / "02 - b.wav", artist="Кино")

    shown = runner.invoke(cli.app, ["tags", "show", "CD2 - Внимание"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["missing"]["01 - a.wav"] == ["year"]

    dry = runner.invoke(cli.app, ["tags", "set", "CD2 - Внимание", "--year", "1990", "--dry-run"])
    report = json.loads(dry.stdout)
    assert report["ok"] is True
    assert report["files"][0]["changes"]["year"] == [None, 1990]
    assert not (folder / META_FILE).exists()
    assert calls == []

    applied = runner.invoke(cli.app, ["tags", "set", "CD2 - Внимание", "--year", "1990", "--yes"])
    assert applied.exit_code == 0, applied.output
    report = json.loads(applied.stdout)
    assert report["import"]["state"] == "imported"
    assert calls == [folder]
    assert not folder.exists()


def test_tags_set_requires_yes_when_not_tty(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _fake_beets(monkeypatch)
    folder = tmp_path / "state" / "quarantine" / "x"
    make_wav(folder / "01 - a.wav")
    result = runner.invoke(cli.app, ["tags", "set", "x", "--year", "1990"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "confirmation_required"


def test_tags_set_per_file_by_index(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _fake_beets(monkeypatch)
    folder = tmp_path / "state" / "quarantine" / "x"
    make_wav(folder / "a.wav")
    make_wav(folder / "b.wav")
    result = runner.invoke(
        cli.app, ["tags", "set", "x", "--file", "2", "--title", "Bee", "--track", "2", "--dry-run"]
    )
    report = json.loads(result.stdout)
    assert report["meta"]["files"] == {"b.wav": {"title": "Bee", "track": 2}}
    assert report["files"][1]["proposed"]["title"] == "Bee"


def test_unknown_target_errors(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["tags", "show", "nope"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "target_not_found"
