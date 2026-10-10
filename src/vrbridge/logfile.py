"""The bridge's log file: where it goes, what bounds it, and the handler that writes it.

`vrbridge` opens one per run (`cli.main`). Constructing a `VRBridge` attaches nothing, so
a library embedder who wants a file calls `attach_log_file` themselves. The file is the
console's stream written down -- same format, same level -- and docs/design.md §The log
file holds the rulings on what it deliberately is not.
"""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import app_base_dir
from .utils import LOG_FORMAT, setup_logging

#: A file rolls over once at this size, so one run holds at most twice this, and what it
#: keeps is the newest lines -- the ones an incident is read from. At INFO a session is a
#: few kilobytes; the cap is the backstop for `--log-level DEBUG` and `--log-callbacks`.
MAX_BYTES = 5 * 1024 * 1024

#: Per-run files in the default directory older than this are deleted at the next start.
KEEP_DAYS = 14


def default_log_path() -> Path:
    """A new file for this run, in `logs/` beside the settings file.

    The pid keeps two bridges started in the same second apart, as it does for
    `paramlog.default_path`. The OSC target cannot name the file: under discovery it is
    not known until a client is found, which is after the first lines are written.
    """
    name = time.strftime(f"vrbridge_%Y%m%d_%H%M%S_{os.getpid()}.log")
    return app_base_dir() / "logs" / name


def prune_logs(directory: Path, keep_days: float = KEEP_DAYS) -> int:
    """Delete the per-run files in `directory` last written over `keep_days` ago.

    Only names `default_log_path` and its rollover produce, so nothing else kept in the
    directory is touched. Returns how many were removed.
    """
    if not directory.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for pattern in ("vrbridge_*.log", "vrbridge_*.log.1"):
        for path in directory.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                # Held open by a bridge still running, or removed by one pruning beside us.
                continue
    return removed


class _LogFileHandler(RotatingFileHandler):
    def doRollover(self):
        # Windows refuses the rename while another process holds the file -- an editor or a
        # script reading it mid-run -- and the base class then drops every record until
        # that reader lets go. Writing on past the cap loses nothing, and the rollover is
        # tried again at the next record.
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def attach_log_file(path: Path) -> logging.Handler:
    """Append the `vrbridge` logger's output to `path`, and return the handler.

    Raises OSError when the directory cannot be made or the file cannot be opened. Each
    record is flushed as it is written, so a killed process loses none.
    """
    logger = logging.getLogger("vrbridge")
    if not logger.handlers:
        # setup_logging adds the console handler only to a bare logger, and ours would
        # make it look configured: attached first, the file would cost the console.
        setup_logging()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _LogFileHandler(path, maxBytes=MAX_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return handler
