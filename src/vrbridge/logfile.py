"""The bridge's log file: where it goes, what bounds it, and the handler that writes it.

`vrbridge` opens one per run (`cli.main`). Constructing a `VRBridge` attaches nothing, so
a library embedder who wants a file calls `attach_log_file` themselves. The file is the
console's stream written down -- same format, same level -- and docs/design.md §The log
file holds the rulings on what it deliberately is not.
"""

from __future__ import annotations

import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import app_base_dir
from .utils import LOG_FORMAT, setup_logging

#: A file rolls over once at this size, so one run holds at most twice this, and what it
#: keeps is the newest lines -- the ones an incident is read from. At INFO a session is a
#: few kilobytes; the cap is the backstop for `--log-level DEBUG` and `--log-callbacks`.
#: It does not hold while another process has the file open: see _LogFileHandler.
MAX_BYTES = 5 * 1024 * 1024

#: Per-run files in the default directory older than this are deleted at the next start.
KEEP_DAYS = 14

#: The names default_log_path and its rollover produce, whole. Pruning matches nothing
#: looser: a hand-kept `vrbridge_notes.log` in the same directory is not ours to delete.
_RUN_FILE = re.compile(r"vrbridge_\d{8}_\d{6}_\d+\.log(\.1)?")


def default_log_path() -> Path:
    """A new file for this run, in `logs/` under the bridge's base directory -- the
    checkout root in a source install. That is `app_base_dir()`, not wherever
    `$VRBRIDGE_CONFIG` points.

    The pid keeps two bridges started in the same second apart, as it does for
    `paramlog.default_path`. The OSC target cannot name the file: under discovery it is
    not known until a client is found, which is after the first lines are written.
    """
    name = time.strftime(f"vrbridge_%Y%m%d_%H%M%S_{os.getpid()}.log")
    return app_base_dir() / "logs" / name


def prune_logs(directory: Path, keep_days: float = KEEP_DAYS) -> int:
    """Delete the per-run files in `directory` last written over `keep_days` ago.

    Only whole names a run produces, and never through a link, so nothing else kept in the
    directory is touched. Returns how many were removed.
    """
    if not directory.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in directory.iterdir():
        if not _RUN_FILE.fullmatch(path.name):
            continue
        try:
            if path.is_symlink() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            # Held open by a bridge still running, or removed by one pruning beside us.
            continue
    return removed


class _LogFileHandler(RotatingFileHandler):
    """One backup, and a rollover that Windows may refuse without costing anything.

    Windows refuses to rename a file another process holds open -- an editor or a script
    reading the log mid-run. The stock rollover deletes the old backup *before* that rename
    and raises out of it, so a refusal loses the backup and then every record until the
    reader lets go. Here the move is one `os.replace`, which overwrites the backup only if
    it succeeds, and the stream is reopened either way. The cost of a refusal is the cap:
    while the file is held, it grows past `maxBytes`, and the rollover is tried again at
    each record.
    """

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            os.replace(self.baseFilename, self.baseFilename + ".1")
        except OSError:
            pass
        self.stream = self._open()


def attach_log_file(path: Path) -> logging.Handler:
    """Append the `vrbridge` logger's output to `path`, and return the handler.

    Attaching a path already attached returns the handler it has, so a second call cannot
    double every line. Raises OSError when the directory cannot be made or the file cannot
    be opened. Each record is flushed as it is written, so a killed process loses none.
    """
    logger = logging.getLogger("vrbridge")
    target = os.path.normcase(os.path.abspath(path))
    for h in logger.handlers:
        if isinstance(h, _LogFileHandler) and os.path.normcase(h.baseFilename) == target:
            return h
    if not logger.handlers:
        # setup_logging adds the console handler only to a bare logger, and ours would
        # make it look configured: attached first, the file would cost the console.
        setup_logging()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _LogFileHandler(path, maxBytes=MAX_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return handler
