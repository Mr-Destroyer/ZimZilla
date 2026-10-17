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
    models: tuple[str, ...] = ()   # declared fallback list; the live one is fetched

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"


# The *fallback* list for each upstream — what to show when the live catalog
# has not been fetched (no network, or /model opened before the startup warm-up
# finished). The real list comes from the upstream's own status API; see
# models_for().
#
# This is a snapshot, so it rots: it was fourteen models, ten of which Logfare
# retired upstream, and /model kept offering them long after every request for
# them 404'd. Treat it as a starting point that /logfare-models corrects, not as
# the truth. Anything not in the live catalog returns "Model not found" (404) at
# request time.
#
# Logfare's catalog also lists image/TTS/STT models (flux-*, sdxl-lightning,
# whisper-large-v3-turbo, aura-2-en, nova-3, phoenix-1.0, lucid-origin); they
# reject chat completions, so they are omitted here.
#
# Token Juice is deliberately a single entry: its whole catalog is one model,
# exposed upstream as a snapshot-suffixed `deepseek-ai/DeepSeek-V4.1-Flash-<n>`
# id and aliased to `deepseek-v4.1-flash` by its litellm config. It publishes no
# status API, so this list is all there is for it. The alias is stable; the
# upstream id is not — packaging/tokenjuice/litellm-config.yaml is the only
# place that name belongs.
LOGFARE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "gemma-4-26b",
    "logfare/auto",
    "qwen-3.8-27b",
)

TOKENJUICE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
)

MODELS_BY_KEY: dict[str, tuple[str, ...]] = {
    "logfare": LOGFARE_MODELS,
    "tokenjuice": TOKENJUICE_MODELS,
}


def catalog_for(base_url: str):
    """The active source's live catalog, or ``None`` if it has none.

    Only Logfare publishes one. Returned as the source module's own ``Model``
    objects so the caller can read health without knowing where it came from.
    Never fetches: this is the cache, and a cold cache is ``None`` — /model runs
    on the UI thread and must not block on the network.
    """
    if active_key(base_url) != "logfare":
        return None
    from . import logfare as lf_mod
    return lf_mod.cached_models()


def models_for(base_url: str) -> tuple[str, ...] | None:
    """The model list for whichever source this endpoint points at.

    None means "not a known source" — the caller should fall back to the
    default registry rather than showing an empty list.

    Logfare's, TokenHarbour's and OpenRouter's lists are fetched, not declared,
    so they come from a catalog cache rather than MODELS_BY_KEY. A cold cache
    yields None, and the caller falls back to the declared list — which is why
    the per-source *-models commands exist to warm it.
    """
    key = active_key(base_url)
    if key is None:
        return None
    if key == "logfare":
        from . import logfare as lf_mod
        cached = lf_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        cached = th_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "openrouter":
        from . import openrouter as or_mod
        cached = or_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "unorouter":
        from . import unorouter as uno_mod
        cached = uno_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "antseed":
        from . import antseed as as_mod
        cached = as_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        cached = ah_mod.cached_ids()
        return tuple(cached) if cached else None
    return MODELS_BY_KEY.get(key) or None


def catalog_models(key: str) -> list | None:
    """The cached catalog of *key*, as that source module's own ``Model`` list.

    ``None`` for a source with no catalog (Logfare's is health-only and carries
    no window) or a cold cache. Kept as a small dispatch rather than a dict of
    importers so the modules stay lazily imported — ``sources`` is imported by
    their callers, and a module-level import would make them import each other.
    """
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        return th_mod.cached_models()
    if key == "openrouter":
        from . import openrouter as or_mod
        return or_mod.cached_models()
    if key == "unorouter":
        from . import unorouter as uno_mod
        return uno_mod.cached_models()
    if key == "antseed":
        from . import antseed as as_mod
        return as_mod.cached_models()
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        return ah_mod.cached_models()
    return None


def context_window_for(base_url: str, model: str) -> int | None:
    """The active model's real context window, from its source's live catalog.

    ``None`` when nothing knows — an unknown source, a cold cache, or a catalog
    that simply does not publish a window (Logfare's status API has no such
    field). The caller falls back to ``Config.context_window``, so a ``None``
    here is not a failure: it means "use the default".

    Read from the cache and never fetched, for the same reason ``catalog_for``
    never fetches: this is called on the turn path, and a blocking network read
    there would stall the very loop it is measuring.

    The field is spelled ``context_length`` in every module that has one, but it
    is read by name off whatever the module's own ``Model`` exposes rather than
    by importing four dataclasses to unify them — that keeps each source's
    parsing where it already lives.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        window = getattr(entry, "context_length", None)
        if isinstance(window, int) and window > 0:
            return window
        return None
    return None


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
    logfare = _build("logfare", "Logfare", 4001, "deepseek-v4.1-flash")
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


#: Sources that are always selectable, whether or not `discover()` finds them
#: on this machine. TokenHarbour, OpenRouter, UnoRouter and AIHubMix are remote:
#: they have no local profile, service or config to discover, so gating them on
#: discover() would silently discard the selection and drop a session back onto
#: Logfare at the next launch. AntSeed is local but equally undiscoverable by
#: `discover()`, which only knows the two LiteLLM proxies — its buyer is started
#: and stopped by the operator, so requiring it to be up at selection time would
#: lose the choice every time the proxy was down.
BUILTIN_KEYS = frozenset({"tokenharbour", "openrouter", "unorouter", "antseed", "aihubmix"})


def current_key() -> str | None:
    """The source the user last selected, if it is still a valid one.

    A discovered source is valid while its files are present. A built-in one is
    valid always — there is nothing on disk that could go missing.
    """
    try:
        key = _state_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if key in BUILTIN_KEYS:
        return key
    return key if key in discover() else None


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    try:
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(key + "\n", encoding="utf-8")
    except OSError:
        pass


def active_key(base_url: str) -> str | None:
    """Best guess at which source the running session is pointed at.

    The local proxies are identified by their port. The hosted gateways are
    remote, so they are identified by host instead — and checked first, because
    a hosted URL has no port to match on and would otherwise fall through to
    ``None``. Each gateway exposes an ``is_<name>`` predicate, so the host
    string lives in exactly one module rather than being spelled again here.
    """
    # Imported locally: sources is imported by these modules' callers, and a
    # module-level import would make them import each other.
    from . import aihubmix as ah_mod
    from . import antseed as as_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    if th_mod.is_tokenharbour(base_url):
        return "tokenharbour"
    if or_mod.is_openrouter(base_url):
        return "openrouter"
    if uno_mod.is_unorouter(base_url):
        return "unorouter"
    if ah_mod.is_aihubmix(base_url):
        return "aihubmix"
    # Checked before the port match, and by port rather than by name: the buyer
    # hands out a plain http://localhost:8377 with no "antseed" in it, and the
    # regex below would otherwise find port 8377, miss it in source_for_port()
    # (which only knows the two LiteLLM proxies) and return None.
    if as_mod.is_antseed(base_url):
        return "antseed"
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
