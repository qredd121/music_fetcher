from __future__ import annotations

import json

from typer.testing import CliRunner

from music_fetcher import cli
from music_fetcher.jobs.db import JobStore
from music_fetcher.slskd.client import SearchFile, SearchResult

runner = CliRunner()


def _result(username, files, slot=True, speed=1000):
    return SearchResult(
        username=username,
        has_free_slot=slot,
        upload_speed=speed,
        queue_length=0,
        files=[SearchFile(filename=f, size=s, extension=e) for f, s, e in files],
    )


def _fake_search_results():
    return [
        _result("good", [(f"You and I - Curtain Falls\\{i:02d}.flac", 100, "FLAC") for i in range(12)]),
        _result("meh", [("Some Other Album\\01.mp3", 100, "MP3")]),
    ]


def _patch_slskd(monkeypatch):
    def fake_search(self, text, *, timeout, poll_interval=1.5):
        return _fake_search_results()

    monkeypatch.setattr(cli.SlskdClient, "__init__", lambda self, cfg: None)
    monkeypatch.setattr(cli.SlskdClient, "search", fake_search)


def _use_tmp_state(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("SLSKD_API_KEY", "x")


def test_search_emits_json(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)

    result = runner.invoke(cli.app, ["search", "you and i curtain falls"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["query"] == "you and i curtain falls"
    assert data["count"] == len(data["results"])
    assert data["results"], "expected at least one candidate"
    top = data["results"][0]
    assert {"id", "username", "folder", "formats", "files"}.issubset(top)
    # best-first: the complete FLAC folder should rank ahead of the lone mp3
    assert top["username"] == "good"


ALBUM_ARGS = ["--artist", "You and I", "--album", "Curtain Falls"]


def test_dl_album_requires_artist_and_album(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)
    result = runner.invoke(cli.app, ["dl", "album", "you and i", "--list"])
    assert result.exit_code != 0


def test_dl_album_list_outputs_candidates(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)

    result = runner.invoke(cli.app, ["dl", "album", *ALBUM_ARGS, "--year", "1999", "--list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["artist"] == "You and I"
    assert data["year"] == 1999
    assert data["query"] == "You and I Curtain Falls"
    assert "release" not in data
    assert data["count"] == len(data["results"])
    assert data["results"][0]["username"] == "good"


def test_dl_album_result_id_enqueues_with_meta(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)
    monkeypatch.setattr(cli, "_wait_for_job", lambda *a, **k: None)

    listing = runner.invoke(cli.app, ["dl", "album", *ALBUM_ARGS, "--list"])
    chosen_id = json.loads(listing.output)["results"][0]["id"]

    result = runner.invoke(
        cli.app,
        ["dl", "album", *ALBUM_ARGS, "--year", "1999", "--result-id", chosen_id, "--no-wait"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["username"] == "good"
    assert data["file_count"] == 12
    job = JobStore(tmp_path / "state" / "jobs.db").get(data["job_id"])
    assert job.payload["meta"] == {
        "album": {"albumartist": "You and I", "album": "Curtain Falls", "year": 1999}
    }


def test_dl_album_bad_result_id_errors(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)

    result = runner.invoke(
        cli.app, ["dl", "album", *ALBUM_ARGS, "--result-id", "deadbeef", "--no-wait"]
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["error"] == "result_id_not_found"
    assert data["id"] == "deadbeef"


def test_dl_track_stores_meta(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    _patch_slskd(monkeypatch)
    listing = runner.invoke(cli.app, ["dl", "track", "--artist", "A", "--title", "T", "--list"])
    assert json.loads(listing.output)["query"] == "A T"
    chosen_id = json.loads(listing.output)["results"][0]["id"]
    result = runner.invoke(
        cli.app,
        ["dl", "track", "--artist", "A", "--title", "T", "--result-id", chosen_id, "--no-wait"],
    )
    assert result.exit_code == 0, result.output
    job = JobStore(tmp_path / "state" / "jobs.db").get(json.loads(result.output)["job_id"])
    assert job.payload["meta"] == {"album": {"artist": "A", "title": "T"}}
