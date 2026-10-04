"""Runtime configuration, model registry and pricing."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------
# The harness talks the Anthropic Messages protocol to whatever endpoint
# ANTHROPIC_BASE_URL points at. By default that is the local LiteLLM proxy
# (:4001) fronting Logfare, whose default model is deepseek-v4.1-flash.
#
# Prices are USD per million tokens and are *estimates* for CLI accounting —
# they are not billing-accurate. Unknown models fall back to DEFAULT_PRICE.

DEFAULT_BASE_URL = "http://localhost:4001"
DEFAULT_MODEL = "deepseek-v4.1-flash"

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
#   auto   every tool auto-approved, bash included.
#   edits  like auto, but bash is disabled entirely (editing-only session).
#   plan   read-only: no tool may change anything; the model plans instead.
#   zim    full auto — every tool runs without asking, and the system prompt is
#          replaced by AGENTS.md (the autonomous pentest operator).
# Each mode is described by two sets and a label:
#
#   auto   tools that run WITHOUT asking (everything else that needs permission
#          is gated: the operator sees a preview and answers y/n/a)
#   deny   tools that do not exist in this mode at all — withheld from the
#          model's tool list, and refused if called anyway
#   loud   the "armed" state: shown in red in the header and the mode list
#
# As shipped, every mode lists each permission-needing tool (bash, write_file,
# edit_file — see tools.GATED_TOOLS) in either `auto` or `deny`, so the
# permission gate never actually fires. A mode that omits a tool from both sets
# would leave it gated: previewed, then answered y/n/a.
MODES: dict[str, dict] = {
    "auto": {
        "label": "AUTO",
        "blurb": "full auto · bash included",
        "auto": {"bash", "write_file", "edit_file"},
        "deny": set(),
    },
    "edits": {
        "label": "EDITS",
        "blurb": "edits auto-approved · bash disabled",
        "auto": {"write_file", "edit_file"},
        "deny": {"bash"},
    },
    "plan": {
        "label": "PLAN",
        "blurb": "read-only · plan, do not change",
        "auto": set(),
        "deny": {"bash", "write_file", "edit_file"},
    },
    "zim": {
        "label": "ZIM",
        "blurb": "full auto · follows AGENTS.md",
        "auto": {"bash", "write_file", "edit_file"},
        "deny": set(),
        "loud": True,
    },
    "danger": {
        "label": "DANGER",
        "blurb": "full auto · obeys the operator, no questions",
        "auto": {"bash", "write_file", "edit_file"},
        "deny": set(),
        "loud": True,
    },
}

DEFAULT_MODE = "auto"

# model_name -> (input $/Mtok, output $/Mtok)
PRICING: dict[str, tuple[float, float]] = {
    "deepseek-v4.1-flash": (0.28, 0.42),
    "deepseek-v3.2": (0.28, 0.42),
    "step-3.7-flash": (0.25, 1.00),
    "claude-opus-5": (15.00, 75.00),
    "claude-fable-5-1": (15.00, 75.00),
    "claude-opus-4.6": (15.00, 75.00),
    "claude-sonnet-5-5": (3.00, 15.00),
    "gpt-6-astra": (5.00, 20.00),
    "gpt-6-sol": (5.00, 20.00),
    "gpt-5.6-sol": (3.00, 12.00),
    "gemini-3.8-flash": (0.30, 1.20),
    "kimi-k3": (1.00, 3.00),
    "kimi-k2.7-code": (1.00, 3.00),
    "kimi-k2.6": (0.60, 2.50),
    "kimi-k2.5": (0.60, 2.50),
    "glm-5.3": (0.60, 2.20),
    "glm-5.3-flash": (0.20, 0.80),
    "glm-5": (0.60, 2.20),
    "grok-4.6": (3.00, 15.00),
    "qwen-3.8-27b": (0.20, 0.60),
    "gemma-4-26b": (0.10, 0.30),
    "ling-3.0-flash-vl": (0.10, 0.30),
    "mimo-v2.6-pro": (0.40, 1.60),
    "logfare/auto": (0.50, 1.50),
    "space-bunny-alpha": (0.50, 1.50),
    "moondream3.1": (0.20, 0.60),
}

DEFAULT_PRICE = (1.00, 3.00)

# Models offered by /model when the user types a bare index or `list`.
#
# These are the models the *upstream* actually serves, not a wish list: the
# Logfare catalog advertises exactly these as chat-capable, and anything else
# comes back "Model not found" (404) at request time. Per-source lists live in
# sources.py — this is the default (Logfare) set, used when the endpoint is not
# one of the known proxies.
KNOWN_MODELS: list[str] = [
    "deepseek-v4.1-flash",
    "gemma-4-26b",
    "ling-3.0-flash-vl",
    "logfare/auto",
    "qwen-3.8-27b",
    "space-bunny-alpha",
]


def price_for(model: str) -> tuple[float, float]:
    """Return (input, output) USD per million tokens for *model*."""
    if model in PRICING:
        return PRICING[model]
    # Tolerate dated suffixes / provider prefixes.
    base = model.split("/")[-1]
    for name, price in PRICING.items():
        if base == name or base.startswith(name) or name.startswith(base):
            return price
    return DEFAULT_PRICE


@dataclass
class Config:
    """Everything the harness needs to run a session."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    auth_token: str | None = None
    model: str = DEFAULT_MODEL
    workdir: Path = field(default_factory=Path.cwd)
    unsafe: bool = False
    # Target scope. `allow_path` is the operator's declared, authorised targets
    # and its PRESENCE arms the session (tools run unattended); `deny_path` is
    # the never-touch list. See zimzilla/scope.py.
    allow_path: Path | None = None
    deny_path: Path | None = None
    #: A leftover scope.yaml, detected but deliberately NOT read — the old file
    #: was an allow-list that also blocked, so reading it as a deny-list would
    #: block the very hosts it declared. Kept only so the UI can say so.
    legacy_scope_path: Path | None = None
    max_tokens: int = 8192
    temperature: float = 0.0
    # Max seconds to wait on the API between reads. Without this the SDK default
    # is 600s, so a proxy that accepts the connection and then goes quiet hangs
    # the turn — with no output and no error — for ten minutes. Applied as the
    # per-read timeout, so it bounds the gap between streamed chunks, not the
    # length of a healthy reply.
    request_timeout: float = 120.0
    # Context window of the active model, in tokens. Used only to draw the
    # telemetry gauge — it is never sent to the endpoint and never enforced:
    # the harness does not truncate history on its own (that is /compact's
    # job). A wrong value therefore costs nothing but a misleading gauge.
    context_window: int = 128_000
    max_iterations: int = 40
    bash_timeout: int = 120
    max_output_chars: int = 20_000
    theme: str = "green"
    # Rain is decorative. By default the boot screen shows it (nothing to read
    # there yet) and the shell does not, so the transcript stays legible.
    # `/rain` or `--rain` turns it on in the shell; `--no-rain` turns it off
    # everywhere, including the boot screen.
    rain: bool = False
    boot_rain: bool = True
    mode: str = DEFAULT_MODE
    # Optional path to a zh-config; only consulted in zim mode.
    agents_path: Path | None = None
    state_dir: Path = field(default_factory=lambda: Path.home() / ".zimzilla")

    @property
    def api_key_present(self) -> bool:
        return bool(self.auth_token or self.api_key)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build a config from the environment, with explicit overrides winning."""
        cfg = cls(
            base_url=os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN") or None,
            model=os.environ.get("ZIMZILLA_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or DEFAULT_MODEL,
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        cfg.workdir = Path(cfg.workdir).expanduser().resolve()
        return cfg

    def redacted_endpoint(self) -> str:
        """Endpoint suitable for display; never includes credentials."""
        return self.base_url
