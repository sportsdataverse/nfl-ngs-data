"""Pipeline logging -- timestamped lines on stdout (the processor tees stdout).

Handler resolves ``sys.stdout`` at emit time so pytest capsys keeps working.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_FORMAT = "%(asctime)s [%(levelname)s] ngs_data_build: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _StdoutHandler(logging.StreamHandler):
    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


def get_logger() -> logging.Logger:
    logger = logging.getLogger("ngs_data_build")
    if not logger.handlers:
        handler = _StdoutHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def human_size(n_bytes: int) -> str:
    size = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"
