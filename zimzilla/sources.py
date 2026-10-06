"""Upstream "sources" — the two LiteLLM proxies ZimZilla can talk through.

ZimZilla speaks the Anthropic Messages protocol to whatever ``ANTHROPIC_BASE_URL``
points at. Two local LiteLLM proxies front two different upstreams:

    logfare      :4001  →  logfare.ai        (the default, shipped with the repo)
    tokenjuice   :4000  →  api.tokenjuice.ai (deepseek-claude's endpoint)

They go down independently, so the point of this module is to make switching
between them a one-liner — ``/zim-tokenjuice`` / ``/zim-logfare`` — and to have
the choice survive a restart (``~/.zimzilla/current-source``).

Nothing here echoes a key. Credentials live in each source's profile file, mode
600, exactly as before.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
DEFAULT_HOME = Path.home()
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Source:
    """One upstream: where to reach it, how to start it, what to call the model."""

    key: str            # "logfare" | "tokenjuice"
    label: str          # human name, for the transcript
    port: int
    profile: Path       # shell profile to source (holds the key; mode 600)
    service: Path       # start-litellm.sh-compatible manager
    config: Path        # the litellm yaml
    model: str          # default model name for this upstream
    root: Path          # checkout the service manager is started from
    models: tuple[str, ...] = ()   # everything this upstream actually serves

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"


# What each upstream's catalog really advertises as chat-capable. Anything not
# listed here returns "Model not found" (404) at request time, so /model offers
# exactly these — no more. Verified against each upstream's /v1/models and a
# per-model chat/completions probe.
#
# Logfare's catalog also lists image/TTS/STT models (flux-*, sdxl-lightning,
# whisper-large-v3-turbo, aura-2-en, nova-3, phoenix-1.0, lucid-origin); they
# reject chat completions, so they are omitted here.
#
# Token Juice is deliberately a single entry: its whole catalog is one model,
# exposed upstream as `deepseek-ai/DeepSeek-V4.1-Flash` and aliased to
# `deepseek-v4.1-flash` by its litellm config.
LOGFARE_MODELS: tuple[str, ...] = (
    "claude-opus-4.6",
    "claude-sonnet-4.6",
    "deepseek-v3.2",
    "gemma-4-26b",
    "gemma-4-31b",
    "glm-5",
    "gpt-oss-120b",
    "grok-4.6",
    "kimi-k2-thinking",
    "kimi-k2.5",
    "logfare/auto",
    "qwen-3.8-27b",
    "space-bunny-alpha",
)

TOKENJUICE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
)

MODELS_BY_KEY: dict[str, tuple[str, ...]] = {
    "logfare": LOGFARE_MODELS,
    "tokenjuice": TOKENJUICE_MODELS,
}


def models_for(base_url: str) -> tuple[str, ...] | None:
    """The model list for whichever source this endpoint points at.

    None means "not a known source" — the caller should fall back to the
    default registry rather than showing an empty list.
    """
    key = active_key(base_url)
    if key is None:
        return None
    return MODELS_BY_KEY.get(key) or None


def _first(paths: list[Path]) -> Path | None:
    for p in paths:
        if p and p.is_file():
            return p
    return None


def _service_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "logfare" / "start-litellm.sh",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "start-litellm.sh",
        DEFAULT_HOME / "start-litellm.sh",
    ]


def _config_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "logfare" / "litellm-config.yaml",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "litellm-config.yaml",
        DEFAULT_HOME / "litellm-config.yaml",
    ]


def _profile_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "source",
            DEFAULT_ROOT / "packaging" / "logfare" / "source",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "source",
        DEFAULT_HOME / "claude-source" / "deepseek-claude",
    ]


def _build(key: str, label: str, port: int, model: str) -> Source | None:
    profile = _first(_profile_candidates(key))
    service = _first(_service_candidates(key))
    config = _first(_config_candidates(key))
    if profile is None or service is None or config is None:
        return None
    return Source(
        key=key, label=label, port=port, profile=profile,
        service=service, config=config, model=model, root=DEFAULT_ROOT,
        models=MODELS_BY_KEY.get(key, ()),
    )


def discover() -> dict[str, Source]:
    """Every source whose profile, service and config are actually present."""
    found: dict[str, Source] = {}
    logfare = _build("logfare", "Logfare", 4001, "grok-4.6")
    if logfare:
        found["logfare"] = logfare
    tokenjuice = _build("tokenjuice", "Token Juice", 4000, "deepseek-v4.1-flash")
    if tokenjuice:
        found["tokenjuice"] = tokenjuice
    return found


def source_for_port(port: int) -> str | None:
    """Which source key owns a given proxy port, if any."""
    for key, src in discover().items():
        if src.port == port:
            return key
    return None


# ---------------------------------------------------------------------------
# current selection (persisted so a restart keeps the working source)
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return ZIMZILLA_HOME / "current-source"


def current_key() -> str | None:
    """The source the user last selected, if it is still a valid one."""
    try:
        key = _state_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key if key in discover() else None


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    try:
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(key + "\n", encoding="utf-8")
    except OSError:
        pass


def active_key(base_url: str) -> str | None:
    """Best guess at which source the running session is pointed at."""
    m = re.search(r":(\d{2,5})(?:/|$)", base_url or "")
    if not m:
        return None
    return source_for_port(int(m.group(1)))


# ---------------------------------------------------------------------------
# profile / proxy
# ---------------------------------------------------------------------------

def load_profile(src: Source) -> dict[str, str]:
    """Read the profile's exports without a shell, so nothing is echoed.

    Parses the ``export VAR="value"`` lines the profiles are written in. A
    profile that uses anything more exotic than that is read by the launcher
    anyway; this is only for the in-TUI switch.
    """
    out: dict[str, str] = {}
    try:
        text = src.profile.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export "):]
        if "=" not in body:
            continue
        name, _, value = body.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def proxy_healthy(port: int, timeout: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_proxy(src: Source, timeout: float = 180.0) -> tuple[bool, str]:
    """Start the source's proxy if it is not already answering. Blocking."""
    if proxy_healthy(src.port):
        return True, "already running"
    if not src.service.is_file():
        return False, f"no service manager at {src.service}"

    env = dict(os.environ)
    env.setdefault("ZIMZILLA_ROOT", str(src.root))
    env["LITELLM_ENV_FILE"] = str(src.profile)
    env["LITELLM_CONFIG"] = str(src.config)
    env["LITELLM_PORT"] = str(src.port)
    # Do not wrap this in flock. The launcher already serialises its own
    # cold-start, and wrapping here is how a leaked lock (LiteLLM inheriting
    # the launcher's lock fd) made /zim-tokenjuice hang until timeout while
    # a manual start of the same script finished in seconds. Two sources
    # live on different ports, so they can come up independently.
    argv = [str(src.service), "start"]

    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return False, f"could not run {src.service}: {e}"

    if proxy_healthy(src.port):
        return True, "started"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit {proc.returncode}"
    return False, tail
