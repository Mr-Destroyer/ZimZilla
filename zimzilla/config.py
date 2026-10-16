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

# The harness dotdir. setup.sh installs the proxy files, the doctrine file and
# the session state here; ZIMZILLA_HOME moves the whole lot.
DEFAULT_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))


def find_agents_file(
    workdir: Path,
    home: Path | None = None,
    user_home: Path | None = None,
) -> Path | None:
    """Locate the doctrine file that zim mode follows.

    Searched in order, first hit wins:

      1. ``<workdir>/AGENTS.md``      — the project's own doctrine
      2. ``<workdir>/../AGENTS.md``   — a checkout's root file, seen from a subdir
      3. ``<home>/AGENTS.md``         — the installed doctrine (~/.zimzilla)
      4. ``<user_home>/AGENTS.md``    — a hand-kept global file (~)

    Step 3 is what makes zim mode work from any directory: the file is installed
    once by setup.sh, so a session opened in an unrelated project still finds
    the operator's doctrine instead of silently falling back to the default
    prompt. A project that ships its own AGENTS.md still wins, because the
    workdir is checked first.

    ``home`` and ``user_home`` are injectable so a test can pin the search to a
    temporary tree instead of whatever the machine happens to have installed.
    """
    home = Path(home) if home is not None else DEFAULT_HOME
    user_home = Path(user_home) if user_home is not None else Path.home()
    workdir = Path(workdir)
    for candidate in (
        workdir / "AGENTS.md",
        workdir.parent / "AGENTS.md",
        home / "AGENTS.md",
        user_home / "AGENTS.md",
    ):
        if candidate.is_file():
            return candidate
    return None

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
#   auto   every tool auto-approved, bash included.
#   edits  like auto, but bash is disabled entirely (editing-only session).
#   plan   read-only: no tool may change anything; the model plans instead.
#   zim         full auto — every tool runs without asking, and the system
#               prompt is replaced by AGENTS.md (the autonomous pentest operator).
#   danger      full auto — operator-directed; no questions asked.
#   uncensored  full auto — the default system prompt is thrown out and
#               replaced by an affirmative doctrine. The model is not asked
#               to "also be willing"; its whole instruction set is rewritten
#               so the only conclusion it can reach is to do what it was told.
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
    "uncensored": {
        "label": "UNCENSORED",
        "blurb": "full auto · cannot refuse · thinks only how to do it",
        "auto": {"bash", "write_file", "edit_file"},
        "deny": set(),
        "loud": True,
    },
}

DEFAULT_MODE = "auto"

# model_name -> (input $/Mtok, output $/Mtok)
# Kept in step with the models Logfare actually serves (sources.LOGFARE_MODELS)
# plus Token Juice's single entry. A model the upstream retires has its row
# removed here too, so the accounting never quotes a price for a 404.
PRICING: dict[str, tuple[float, float]] = {
    "deepseek-v4.1-flash": (0.28, 0.42),
    "qwen-3.8-27b": (0.20, 0.60),
    "gemma-4-26b": (0.10, 0.30),
    "logfare/auto": (0.50, 1.50),
}

DEFAULT_PRICE = (1.00, 3.00)

# Models offered by /model when the user types a bare index or `list`.
#
# A *snapshot*, not the truth: the live list is fetched from the upstream's own
# status API (see zimzilla/logfare.py and sources.models_for). This is the
# default (Logfare) set, used when the endpoint is not one of the known proxies
# — and the fallback when a fetch has not happened yet. Anything the upstream
# does not serve comes back "Model not found" (404) at request time.
KNOWN_MODELS: list[str] = [
    "deepseek-v4.1-flash",
    "gemma-4-26b",
    "logfare/auto",
    "qwen-3.8-27b",
]


def price_for(model: str) -> tuple[float, float]:
    """Return (input, output) USD per million tokens for *model*.

    A free model costs nothing, and that is checked FIRST. The prefix tolerance
    below is what makes the check necessary rather than merely nice: a giveaway
    named ``<model>-free`` *starts with* the paid ``<model>`` row, so the loop
    would bill the free model at the paid model's rate — measured once at
    $0.0003 for a two-token reply, on a model the operator had been told costs
    nothing. Worse, an unknown free model fell through to DEFAULT_PRICE and was
    billed $1/$3.

    Both ``-free`` and ``:free`` are recognised. The ``:free`` form is what the
    two gateways still in the tree use; ``-free`` is kept because it is the
    convention the prefix-tolerance loop below is most dangerous against, and a
    suffix check is cheaper than trusting a future provider to avoid it. A model
    that is genuinely paid can never carry either, so this cannot mis-price a
    real charge.
    """
    base = model.split("/")[-1]
    if base.endswith("-free") or base.endswith(":free"):
        return (0.0, 0.0)
    if model in PRICING:
        return PRICING[model]
    # Tolerate dated suffixes / provider prefixes.
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
    # Output ceiling per request. Was 8192, which is too close to the size of a
    # real osint report to be safe: the two archived reports on this box are
    # 4880 and 6169 tokens, so a report that grows at all — one more breach
    # section, a longer coverage table — is cut off mid-``Write``. The SDK then
    # discards the truncated tool call, and the turn ends looking exactly like
    # one where the model narrated instead of writing. 32768 leaves room, and is
    # inside every model observed (the largest ceiling measured was 64000; the
    # limit is model-dependent, so this is the value that is safe everywhere).
    max_tokens: int = 32768
    temperature: float = 0.0
    # Max seconds to wait on the API between reads. Without this the SDK default
    # is 600s, so a proxy that accepts the connection and then goes quiet hangs
    # the turn — with no output and no error — for ten minutes. Applied as the
    # per-read timeout, so it bounds the gap between streamed chunks, not the
    # length of a healthy reply.
    request_timeout: float = 120.0
    # Context window of the active model, in tokens. This is the FALLBACK used
    # when the live catalog does not say — `sources.context_window_for` asks the
    # active source's own catalog first, and every remote source parses a real
    # per-model figure (UnoRouter's ``metadata.contextWindow``, OpenRouter's and
    # TokenHarbour's ``context_length``, AntSeed's caps). The fallback is 1M
    # because the models this harness actually lands on are the long-context
    # ones: a free tier that offers 1M and a harness that assumed 128k would
    # compact away history it did not need to lose.
    #
    # It IS enforced now — see ``compact_at`` below. It used to be gauge-only,
    # which is why "raise the limit to 1M" did nothing on its own.
    context_window: int = 1_000_000
    #: Fraction of ``context_window`` at which history is compacted automatically.
    #: Checked before each request against the *previous* call's real
    #: ``input_tokens``, so the trigger is a measured prompt size rather than an
    #: estimate. 0.8 leaves a fifth of the window for the reply, the tool results
    #: it will ask for, and the growth between the check and the next call.
    #:
    #: Not 1.0: the failure mode at 1.0 is the one this exists to prevent — the
    #: request that finally overflows is the one that was already too big, and it
    #: fails after spending the tokens.
    compact_at: float = 0.8
    #: Whether to compact automatically at ``compact_at``. On by default: the
    #: whole point is that a long task should not need the operator to notice.
    auto_compact: bool = True
    #: Tool rounds in one turn before the loop pauses to reconsider. This is the
    #: cap that ended long turns silently, so it is deliberately high: it exists
    #: to stop a runaway loop, not to bound honest work. A turn that keeps making
    #: progress past it CONTINUES automatically (see ``max_rounds``); only a turn
    #: that stops making progress ends there, and it now says so.
    max_iterations: int = 40
    #: Hard ceiling on tool rounds in a single turn, auto-continues included.
    #: The backstop for a genuinely runaway loop — a model that keeps calling a
    #: tool forever without advancing. 400 rounds is far past any honest task and
    #: still finite, so a pathological turn ends in minutes rather than never.
    max_rounds: int = 400
    bash_timeout: int = 120
    max_output_chars: int = 20_000
    theme: str = "green"
    # Rain is decorative. By default the boot screen shows it (nothing to read
    # there yet) and the shell does not, so the transcript stays legible.
    # `/rain` or `--rain` turns it on in the shell; `--no-rain` turns it off
    # everywhere, including the boot screen.
    rain: bool = False
    boot_rain: bool = True
    # Show the model's own reasoning when the endpoint forwards it, which only
    # some do — an Anthropic-native endpoint sends `thinking` blocks, while
    # LiteLLM's Anthropic bridge drops reasoning entirely. On by default
    # because it costs nothing where it is absent, and where it is present it
    # is the difference between watching the model work and watching a blank
    # line. `/think` toggles it; `--no-think` turns it off at launch.
    show_thinking: bool = True
    mode: str = DEFAULT_MODE
    # Optional path to a doctrine file; only consulted in zim mode. Resolved at
    # launch by config.find_agents_file, and re-resolved on /mode zim so a
    # session that started elsewhere still picks the file up.
    agents_path: Path | None = None
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_HOME))

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
