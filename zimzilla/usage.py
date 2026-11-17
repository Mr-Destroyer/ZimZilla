"""Persistent usage accounting for `/usage`.

One record per completed turn: model, source, input/output tokens, cost and the
local day. The ledger stays in ``~/.zimzilla/usage.json`` (mode 600), so the
statistics survive restarts and can answer "which model do I actually use?"
instead of only "what has this session spent?".

The file is small and append-shaped: aggregate instead of keeping a turn log.
That keeps `/usage` fast after months of use and avoids turning the state
directory into a second transcript.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


def _blank() -> dict:
    return {
        "version": 1,
        "totals": {"turns": 0, "input": 0, "output": 0, "cost": 0.0},
        "models": {},
        "sources": {},
        "days": {},
        "first": 0.0,
        "last": 0.0,
    }


def _bucket(container: dict, key: str) -> dict:
    bucket = container.get(key)
    if not isinstance(bucket, dict):
        bucket = {"turns": 0, "input": 0, "output": 0, "cost": 0.0, "last": 0.0}
        container[key] = bucket
    for name in ("turns", "input", "output"):
        bucket[name] = int(bucket.get(name) or 0)
    bucket["cost"] = float(bucket.get("cost") or 0.0)
    bucket["last"] = float(bucket.get("last") or 0.0)
    return bucket


def format_tokens(value: int) -> str:
    value = int(value or 0)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


@dataclass
class UsageStats:
    path: Path = field(default_factory=lambda: Path.home() / ".zimzilla" / "usage.json")
    data: dict = field(default_factory=_blank)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.load()

    # ---- persistence ------------------------------------------------------
    def load(self) -> None:
        self.data = _blank()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        totals = payload.get("totals")
        if isinstance(totals, dict):
            self.data["totals"].update({
                "turns": int(totals.get("turns") or 0),
                "input": int(totals.get("input") or 0),
                "output": int(totals.get("output") or 0),
                "cost": float(totals.get("cost") or 0.0),
            })
        for name in ("models", "sources", "days"):
            value = payload.get(name)
            if isinstance(value, dict):
                self.data[name] = value
        self.data["first"] = float(payload.get("first") or 0.0)
        self.data["last"] = float(payload.get("last") or 0.0)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---- recording --------------------------------------------------------
    def record(self, model: str, source: str, input_tokens: int,
               output_tokens: int, cost: float, *, when: float | None = None) -> None:
        model = (model or "unknown").strip() or "unknown"
        source = (source or "unknown").strip() or "unknown"
        input_tokens = max(0, int(input_tokens or 0))
        output_tokens = max(0, int(output_tokens or 0))
        cost = max(0.0, float(cost or 0.0))
        ts = float(when if when is not None else time.time())
        day = date.fromtimestamp(ts).isoformat()

        totals = self.data["totals"]
        totals["turns"] += 1
        totals["input"] += input_tokens
        totals["output"] += output_tokens
        totals["cost"] += cost
        self.data["first"] = self.data["first"] or ts
        self.data["last"] = ts

        for container, key in ((self.data["models"], model),
                               (self.data["sources"], source),
                               (self.data["days"], day)):
            bucket = _bucket(container, key)
            bucket["turns"] += 1
            bucket["input"] += input_tokens
            bucket["output"] += output_tokens
            bucket["cost"] += cost
            bucket["last"] = ts
        self.save()

    def reset(self) -> None:
        self.data = _blank()
        self.save()

    # ---- views ------------------------------------------------------------
    def ranked(self, kind: str, limit: int = 8) -> list[tuple[str, dict]]:
        """Models/sources ordered by turns, then tokens."""
        container = self.data.get(kind) or {}
        rows = [(str(key), _bucket(container, str(key))) for key in container]
        rows.sort(key=lambda row: (row[1].get("turns", 0),
                                   row[1].get("input", 0) + row[1].get("output", 0)),
                  reverse=True)
        return rows[:limit]

    def top(self, kind: str) -> tuple[str, dict] | None:
        rows = self.ranked(kind, limit=1)
        return rows[0] if rows else None

    def recent_days(self, days: int = 14) -> list[tuple[str, int]]:
        today = date.today()
        out: list[tuple[str, int]] = []
        for offset in range(days - 1, -1, -1):
            key = (today - timedelta(days=offset)).isoformat()
            bucket = (self.data.get("days") or {}).get(key) or {}
            out.append((key, int(bucket.get("turns") or 0)))
        return out

    def last_used(self) -> datetime | None:
        ts = float(self.data.get("last") or 0)
        return datetime.fromtimestamp(ts) if ts else None
