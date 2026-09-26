"""Versioned, thread-safe context store.

Contract (testing brief §2.1):
  * idempotent on (scope, context_id, version) — the same version re-posted is a no-op
  * a higher version replaces the previous one atomically
  * a lower version is rejected as stale
Older versions are kept so that a partial payload (e.g. a category push that only
carries a new digest) can still fall back on fields it did not re-send.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .textutil import fix_mojibake

SCOPES = ("category", "merchant", "customer", "trigger")


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[tuple[str, str], dict] = {}
        self._history: dict[tuple[str, str], list[dict]] = {}
        self.started = time.time()

    # ----------------------------------------------------------------- writes
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[str, int]:
        """Returns (status, current_version). status in {'stored', 'duplicate', 'stale'}."""
        payload = fix_mojibake(payload)
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None:
                if version < cur["version"]:
                    return "stale", cur["version"]
                if version == cur["version"]:
                    return ("duplicate" if cur["payload"] == payload else "stale"), cur["version"]
            self._data[key] = {"version": version, "payload": payload, "stored_at": time.time()}
            self._history.setdefault(key, []).append(self._data[key])
            return "stored", version

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._history.clear()

    # ----------------------------------------------------------------- reads
    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        with self._lock:
            cur = self._data.get((scope, context_id))
            if cur is None:
                return None
            payload = cur["payload"]
            history = self._history.get((scope, context_id), [])
            if len(history) > 1:
                merged = {}
                for h in history:  # oldest -> newest; newest wins per top-level key
                    merged.update(h["payload"])
                merged.update(payload)
                return merged
            return payload

    def version(self, scope: str, context_id: str) -> Optional[int]:
        with self._lock:
            cur = self._data.get((scope, context_id))
            return cur["version"] if cur else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        with self._lock:
            for scope, _ in self._data:
                out[scope] = out.get(scope, 0) + 1
        return out

    def all(self, scope: str) -> list[dict]:
        with self._lock:
            return [self.get(s, cid) for (s, cid) in list(self._data) if s == scope]

    def customers_of(self, merchant_id: str) -> list[dict]:
        return [c for c in self.all("customer") if c and c.get("merchant_id") == merchant_id]
