from __future__ import annotations

import logging

from rich.logging import RichHandler

from .config import load_config

_configured = False


def configure_logging(override: str | None = None) -> None:
    global _configured
    if _configured:
        return
    level_name = override or load_config().log_level
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    for noisy in ("httpx", "httpcore", "urllib3", "musicbrainzngs"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
