"""Session persistence: save / load / list conversation state."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

_SLUG = re.compile(r"[^a-zA-Z0-9._-]+")


def sessions_dir(cfg) -> Path:
    d = Path(cfg.state_dir) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str, limit: int = 40) -> str:
    text = _SLUG.sub("-", text.strip()).strip("-")
    return (text[:limit] or "session")


def save(cfg, agent, name: str | None = None) -> Path:
    """Persist the current conversation. Returns the file written."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = _slug(name) if name else stamp
    filename = f"{stamp}-{label}.json" if name else f"{stamp}.json"
    path = sessions_dir(cfg) / filename

    payload = {
        "version": 1,
        "saved_at": time.time(),
        "model": cfg.model,
        "base_url": cfg.base_url,
        "workdir": str(cfg.workdir),
        "unsafe": cfg.unsafe,
        "allow_path": str(cfg.allow_path) if cfg.allow_path else None,
        "deny_path": str(cfg.deny_path) if cfg.deny_path else None,
        "scope_armed": agent.scope.armed,
        "scope_loaded": agent.scope.loaded,
        "tokens": {
            "input": agent.session_input_tokens,
            "output": agent.session_output_tokens,
        },
        "cost": agent.session_cost,
        "turn_count": agent.turn_count,
        "messages": agent.messages,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def load(cfg, agent, path: str) -> tuple[bool, str]:
    """Restore a conversation into *agent*. Returns (ok, message)."""
    p = Path(path).expanduser()
    if not p.exists():
        # Allow loading by bare name from the sessions dir.
        candidate = sessions_dir(cfg) / path
        if candidate.exists():
            p = candidate
        else:
            matches = sorted(sessions_dir(cfg).glob(f"*{path}*"))
            if not matches:
                return False, f"no session matching '{path}'"
            p = matches[-1]

    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        return False, f"cannot parse session: {e}"

    agent.messages = data.get("messages", [])
    agent.turn_count = int(data.get("turn_count", 0))
    toks = data.get("tokens", {})
    agent.session_input_tokens = int(toks.get("input", 0))
    agent.session_output_tokens = int(toks.get("output", 0))
    agent.session_cost = float(data.get("cost", 0.0))
    if data.get("model"):
        cfg.model = data["model"]
    return True, f"loaded {len(agent.messages)} messages from {p.name}"


def list_sessions(cfg) -> list[dict]:
    out = []
    for p in sorted(sessions_dir(cfg).glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
            out.append(
                {
                    "file": p.name,
                    "saved_at": data.get("saved_at", 0),
                    "model": data.get("model", "?"),
                    "turns": data.get("turn_count", 0),
                    "messages": len(data.get("messages", [])),
                }
            )
        except Exception:
            continue
    return out
