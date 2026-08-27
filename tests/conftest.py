from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr("music_fetcher.config.load_dotenv", lambda *a, **k: None)
    for key in (
        "MUSIC_DIR",
        "DOWNLOADS_DIR",
        "STATE_DIR",
        "PREFER_FORMAT",
        "SEARCH_TIMEOUT",
        "DOWNLOAD_TIMEOUT",
        "SLSKD_BASE_URL",
        "MUSICBRAINZ_CONTACT",
        "LOG_LEVEL",
        "SLSKD_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
