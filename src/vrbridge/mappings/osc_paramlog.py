"""Timestamped CSV logger for whitelisted OSC parameters.

A general-purpose recorder for anyone measuring an avatar over its own OSC
surface: name the parameters, get a CSV of every change with a wall-clock
timestamp. Built for measurement runs (each row is one *change*, so
inter-row spacing on a per-frame-emitted parameter is the sender's frame
clock), and shaped by two standing rulings:

- The whitelist is REQUIRED, not opt-out. Full OSC traffic is far too noisy
  to log raw, and an enumerate-everything default would be the parameter
  discovery docs/design.md descopes. Patterns admit named shapes of traffic;
  nothing here enumerates a peer.
- The logged stream is the change-filtered stream. A repeated identical value
  is one row (OSCManager suppresses it), which also folds the doubled inbound
  delivery (docs/design.md §Inbound delivery semantics) into one row. A
  parameter that re-sends its current value is therefore invisible here --
  fine for measurement, where only transitions carry information.

CSV schema: `time,address,value,type` -- time.time() to microseconds (the
cross-log join key; both loggers of a two-client run share one machine
clock), the concrete address, the value, and the OSC-side Python type name.
The stamp is taken per datagram thread before the write lock, so under load
rows can land in the file out of timestamp order: the TIME COLUMN is the
ordering key, and analysis sorts on it rather than trusting row order.
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping

_GLOB_CHARS = ("*", "?", "[")

#: Where a bare parameter name lives on the wire.
PARAMS_PREFIX = "/avatar/parameters/"


def to_address(spec: str) -> str:
    """A spec starting '/' is a full OSC address; anything else names an avatar
    parameter and gets the /avatar/parameters/ prefix."""
    return spec if spec.startswith("/") else PARAMS_PREFIX + spec


class ParamLogMapping(Mapping):
    """Log every change of the whitelisted parameters to a CSV file.

    `params` is required and non-empty: bare names/globs are avatar parameters
    (`SyncProbe/*`), a leading `/` names any full address (`/avatar/change`).
    The file is created on register() with a header row and flushed per row --
    a crash loses nothing, and measurement runs end by Ctrl-C, not close().
    """
    name = "osc_paramlog"

    def __init__(self, bridge: VRBridge, params: list[str], path: str | Path):
        super().__init__(bridge)
        if not params:
            raise ValueError(
                "osc_paramlog requires a non-empty whitelist: pass the parameter "
                "names or globs to record (full traffic is too noisy to log raw).")
        self.addresses = [to_address(p) for p in params]
        self.path = Path(path)
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None
        self.rows = 0

    def _attach(self) -> None:
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["time", "address", "value", "type"])
        self._fh.flush()
        cb = self._gate(self._on_value)
        for addr in self.addresses:
            if any(ch in addr for ch in _GLOB_CHARS):
                self.bridge.on_osc_pattern(addr, cb)
            else:
                self.bridge.on_osc(addr, cb)
        self.bridge.log.info("osc_paramlog recording %d whitelist entries to %s",
                             len(self.addresses), self.path)

    def _on_value(self, ctx, address: str, value) -> None:
        row = [f"{time.time():.6f}", address, value, type(value).__name__]
        with self._lock:
            if self._writer is None:
                return
            self._writer.writerow(row)
            self._fh.flush()
            self.rows += 1

    def close(self) -> None:
        """Flush and close the file. Idempotent; rows arriving after close are dropped."""
        with self._lock:
            if self._fh is not None:
                self._fh.close()
            self._fh = None
            self._writer = None
