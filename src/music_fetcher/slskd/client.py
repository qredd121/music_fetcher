from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import SlskdConfig


class SlskdError(RuntimeError):
    pass


def _pick(raw: dict, *names: str, default=None):
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


@dataclass
class SearchFile:
    filename: str
    size: int
    extension: str = ""
    bitrate: int | None = None

    @classmethod
    def from_api(cls, raw: dict) -> SearchFile:
        filename = str(_pick(raw, "filename", "Filename", default=""))
        size = int(_pick(raw, "size", "Size", default=0))
        extension = str(_pick(raw, "extension", "Extension", default="") or "")
        if not extension and filename:
            extension = filename.rsplit(".", 1)[-1].lower()
        extension = extension.strip(".").upper()
        bitrate_raw = _pick(raw, "bitRate", "bitrate")
        bitrate = int(bitrate_raw) if bitrate_raw else None
        return cls(filename=filename, size=size, extension=extension, bitrate=bitrate)


@dataclass
class SearchResult:
    username: str
    has_free_slot: bool
    upload_speed: int
    queue_length: int
    files: list[SearchFile] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict) -> SearchResult:
        return cls(
            username=str(_pick(raw, "username", "userName", default="")),
            has_free_slot=bool(_pick(raw, "hasFreeUploadSlot", default=False)),
            upload_speed=int(_pick(raw, "uploadSpeed", default=0)),
            queue_length=int(_pick(raw, "queueLength", default=0)),
            files=[SearchFile.from_api(item) for item in raw.get("files", [])],
        )

    @property
    def folder(self) -> str:
        if not self.files:
            return ""
        first = self.files[0].filename.replace("\\", "/")
        parent = str(Path(first).parent)
        return parent.rsplit("/", 1)[-1]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def extensions(self) -> set[str]:
        return {f.extension for f in self.files if f.extension}


class SlskdClient:
    def __init__(self, config: SlskdConfig) -> None:
        if not config.api_key:
            raise SlskdError(
                "SLSKD_API_KEY is not set; put it in .env or export it before running"
            )
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers={"X-API-Key": config.api_key},
            timeout=config.timeout,
        )

    def _url(self, path: str) -> str:
        return f"{self._config.api_prefix}{path}"

    def ping(self) -> bool:
        for path in (self._url("/ping"), self._url("/application")):
            try:
                response = self._client.get(path)
            except httpx.HTTPError:
                return False
            if response.status_code == 200:
                return True
        return False

    def search(
        self,
        text: str,
        *,
        timeout: float,
        poll_interval: float = 1.5,
    ) -> list[SearchResult]:
        search_id = uuid.uuid4()
        payload = {"id": str(search_id), "searchText": text}
        response = self._client.post(self._url("/searches"), json=payload)
        if response.status_code == 409:
            self._purge_searches()
            response = self._client.post(self._url("/searches"), json=payload)
        if response.status_code >= 400:
            raise SlskdError(f"search request failed: HTTP {response.status_code}")

        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                status = self._client.get(self._url(f"/searches/{search_id}"))
                if status.status_code >= 400:
                    raise SlskdError(f"search poll failed: HTTP {status.status_code}")
                state = str(status.json().get("state", ""))
                if "Complete" in state or "Cancelled" in state:
                    break
                time.sleep(poll_interval)

            responses = self._client.get(self._url(f"/searches/{search_id}/responses"))
            if responses.status_code >= 400:
                raise SlskdError(f"search results failed: HTTP {responses.status_code}")
            return [SearchResult.from_api(item) for item in responses.json()]
        finally:
            self._client.delete(self._url(f"/searches/{search_id}"))

    def _purge_searches(self) -> None:
        listing = self._client.get(self._url("/searches"))
        if listing.status_code >= 400:
            return
        for item in listing.json():
            search_id = item.get("id")
            if search_id:
                self._client.delete(self._url(f"/searches/{search_id}"))

    def enqueue(self, username: str, files: Sequence[SearchFile]) -> None:
        payload = [{"filename": f.filename, "size": f.size} for f in files]
        response = self._client.post(
            self._url(f"/transfers/downloads/{username}"), json=payload
        )
        if response.status_code >= 400:
            raise SlskdError(
                f"enqueue failed: HTTP {response.status_code} {response.text.strip()[:120]}"
            )

    def transfers(self) -> list[dict]:
        response = self._client.get(self._url("/transfers/downloads"))
        if response.status_code >= 400:
            raise SlskdError(f"transfers fetch failed: HTTP {response.status_code}")
        return response.json()

    def user_presence(self, username: str) -> str | None:
        response = self._client.get(self._url(f"/users/{username}/status"))
        if response.status_code >= 400:
            return None
        return response.json().get("presence")

    def cancel_transfer(self, username: str, filename: str) -> None:
        response = self._client.delete(
            self._url(f"/transfers/downloads/{username}"),
            params={"filename": filename},
        )
        if response.status_code >= 400:
            raise SlskdError(f"cancel failed: HTTP {response.status_code}")
