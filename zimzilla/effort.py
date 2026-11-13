"""`/effort` — raise or lower how hard the current model thinks.

One level, two wire shapes. There is no single ``effort`` field across the
protocols this harness speaks, so the level is translated per protocol at
request time:

* **Chat Completions** (``openai_compat``) takes a top-level
  ``reasoning_effort`` — ``"low"``/``"medium"``/``"high"``. That is the OpenAI
  spelling and the one LiteLLM, vLLM and the OpenAI-shaped gateways map, which
  is what every direct route here is.
* **Messages** (the ``anthropic`` SDK, used against Anthropic-native endpoints
  and their clones) has no effort field: the equivalent is an enabled
  ``thinking`` block with a ``budget_tokens`` ceiling, which must be at least
  ``MIN_BUDGET`` and strictly below the request's own ``max_tokens``. Hence
  :func:`body` takes the call's ceiling and clamps — a budget the server will
  reject is worse than a smaller one it accepts.

  **That shape is not universal.** Sonnet 5, Opus 4.7+ and Opus 5/5.5 removed
  ``budget_tokens`` outright; on those the block is ``{"type": "adaptive"}``
  and the level rides in ``output_config.effort``. Sending the older shape is
  a 400 that names the replacement:

      "thinking.type.enabled" is not supported for this model. Use
      "thinking.type.adaptive" and "output_config.effort" ...

  So :func:`body` picks the shape from the model id (:func:`needs_adaptive`),
  and the agent can force it after a refusal (:func:`wants_adaptive`) — the
  first request has no error to read, and by the time one arrives the turn has
  already failed once.

**Not every model has either knob**, and that is a fact about the endpoint
rather than a mistake the operator can fix by rewording anything. So the level
is sent optimistically and the agent's existing drop-and-retry path handles the
refusal: a 400 naming the parameter drops exactly that field, reports
``param_dropped`` in the transcript, and re-sends. ``agent._unsupported_sampling_param``
lists these names for that reason. The same path covers the second-order
interaction — an Anthropic endpoint rejects ``temperature`` once thinking is
enabled, and ``temperature`` is matched before ``thinking``, so the sampling
knob goes first and the retry carries the budget.

The choice is remembered the way the theme is (``$ZIMZILLA_HOME/effort``,
mirroring ``current-theme``), because a preference that silently resets on
every launch reads as a bug the first time it is relied on. ``ZIMZILLA_EFFORT``
or ``--effort`` overrides the remembered value for one session.

No Textual, no colours, no model: the whole module is testable without a
terminal or an API key, the same split as ``osint.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Where the remembered level lives. Same convention as ``theme.py``'s
#: ``current-theme``: one small file, no config to edit, and a read-only home
#: degrades to "no preference" rather than an error.
ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: Anthropic's own floor for a thinking budget. Below this the request is
#: rejected outright, so there is nothing to send and nothing to guess.
MIN_BUDGET = 1_024

#: Headroom kept under the request ceiling. ``budget_tokens`` must be strictly
#: less than ``max_tokens`` or the API refuses the call, and the answer needs
#: room of its own beyond the thinking.
CEILING_HEADROOM = 1_024


@dataclass(frozen=True)
class Level:
    """One effort level, in both dialects."""

    name: str
    blurb: str
    #: The ``reasoning_effort`` value on Chat Completions.
    wire: str
    #: The ``thinking.budget_tokens`` value on Messages.
    budget: int


#: The registry. The menu, the status line and the request body are all built
#: from this table, so adding a level moves nothing else.
LEVELS: dict[str, Level] = {
    "low": Level(
        name="low",
        blurb="a quick look — cheapest, least thinking",
        wire="low",
        budget=2_048,
    ),
    "medium": Level(
        name="medium",
        blurb="the balance — more thinking where it helps",
        wire="medium",
        budget=8_192,
    ),
    "high": Level(
        name="high",
        blurb="think it through — slowest, and the most tokens",
        wire="high",
        budget=16_384,
    ),
}

#: Display order, and the order the menu offers. Cheapest first, so "raise it"
#: is always a step down the list.
LEVEL_ORDER: tuple[str, ...] = ("low", "medium", "high")

#: Words that mean "stop sending anything and let the model decide". Spelled
#: several ways because the honest answer to "how do I undo this" should not
#: depend on guessing one exact token.
OFF_WORDS = frozenset({"off", "none", "default", "auto", "reset", "unset"})


# ---------------------------------------------------------------------------
# which thinking shape a model takes
# ---------------------------------------------------------------------------

#: Model families that removed ``thinking.budget_tokens``. On these the block
#: is ``{"type": "adaptive"}`` and the level is carried by
#: ``output_config.effort`` instead. Matched on the id because there is no
#: capability flag on the wire to ask for, and the alternative — sending the
#: old shape and reading the 400 — costs a failed request on every new session.
#:
#: Measured against Coarena's whole catalog (18 models, 2026-11): **every**
#: ``claude-*`` entry there needs the adaptive shape — ``sonnet-5-5``,
#: ``opus-5-5``, ``haiku-5-5`` and all three ``fable-5*`` — while every
#: non-Claude entry (``gemini-4-*``, ``grok-4-7``, ``kimi-k3``, ``glm-5-3``,
#: ``qwen3-8``, ``deepseek-v4``, ``hy4``, ``mimo``, ``minimax``) accepts the
#: budget. So the rule is a version test on the Claude line rather than a list
#: of names: a family list missed ``fable-5`` and ``haiku-5-5`` on the first
#: pass, and would miss the next release too.
#:
#: The threshold is Claude 5 and up, plus Opus 4.7 — the generation that
#: dropped ``budget_tokens``. ``claude-haiku-4-5`` and older keep the budget.
ADAPTIVE_MIN_MAJOR = 5
#: Opus 4.7 is the one 4.x that already dropped the budget.
ADAPTIVE_OPUS_MIN_MINOR = 7


def _claude_version(flat: str) -> tuple[int, int] | None:
    """``(major, minor)`` for a flattened Claude id, or ``None``.

    *flat* is already lowercased with ``.``/``_`` turned into ``-``, so
    ``claude-sonnet-5-5`` and ``claude-sonnet-5.5`` both read as ``(5, 5)``.
    The first two integers after the family word are the version; a date suffix
    (``-20260101``) is longer than two digits and is ignored, which is what
    keeps ``claude-sonnet-5-5-20260101`` on the adaptive shape.
    """
    if "claude" not in flat:
        return None
    parts = flat.split("-")
    nums: list[int] = []
    for part in parts:
        if part.isdigit() and len(part) <= 2:
            nums.append(int(part))
            if len(nums) == 2:
                break
        elif nums:
            # A non-numeric token after the version starts (a date, a suffix)
            # ends the version rather than being read as part of it.
            break
    if not nums:
        return None
    return (nums[0], nums[1] if len(nums) > 1 else 0)


def needs_adaptive(model: str | None) -> bool:
    """Whether *model* takes ``thinking.type=adaptive`` rather than a budget.

    A guess, and only ever a guess: an unrecognised id keeps the budget shape,
    which is what every model before this generation accepted. A wrong guess
    costs one 400 and the agent's drop-and-retry, which is the same price the
    old unconditional shape paid on every adaptive model.
    """
    text = (model or "").strip().lower()
    if not text:
        return False
    # `claude-sonnet-5-5` and `claude-sonnet-5.5` both normalise to the same
    # run of digits, so the separators are flattened before matching.
    flat = text.replace(".", "-").replace("_", "-")
    version = _claude_version(flat)
    if version is None:
        return False
    major, minor = version
    if major >= ADAPTIVE_MIN_MAJOR:
        return True
    # Opus 4.7 dropped the budget while the rest of the 4.x line kept it.
    return "opus" in flat and major == 4 and minor >= ADAPTIVE_OPUS_MIN_MINOR


def wants_adaptive(model: str | None, forced: bool = False) -> bool:
    """Whether to send the adaptive shape: the model says so, or a 400 did.

    *forced* is the agent's ``_adaptive_thinking`` flag, set the first time an
    endpoint refuses the budget shape. It outranks the id test because the
    endpoint is the authority on what it accepts — a model this table has
    never heard of can still be adaptive, and the refusal is the evidence.
    """
    return forced or needs_adaptive(model)



# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate(value: str | None) -> tuple[bool, str]:
    """Whether *value* names a level this harness can send.

    Returns ``(ok, message)``: the message is the error to show when ``ok`` is
    false, and a short description of the level when it is true.
    """
    text = (value or "").strip().lower()
    if not text:
        return False, "usage: /effort " + " | ".join(LEVEL_ORDER) + " | off"
    if text in OFF_WORDS:
        return True, "off — nothing sent, the model's own default"
    if text in LEVELS:
        return True, LEVELS[text].blurb
    return False, (
        f"unknown effort: {text} — try "
        + " | ".join(LEVEL_ORDER) + " | off"
    )


def normalise(value: str | None) -> str | None:
    """The canonical level for *value*, or ``None`` for off/unknown.

    Used on the way in from a file, an environment variable or a command line,
    so a junk value cannot reach the request body.
    """
    text = (value or "").strip().lower()
    return text if text in LEVELS else None


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------

def body(level: str | None, protocol: str, ceiling: int,
         model: str | None = None, adaptive: bool = False) -> dict:
    """The request-body fragment that carries *level* on *protocol*.

    Empty when the level is off, unknown, or when the ceiling leaves no room
    for a legal thinking budget — an empty dict is the honest "send nothing",
    and it keeps the default path byte-identical to what it was before this
    command existed.

    *model* and *adaptive* choose the Messages shape: the budget block for
    everything that still takes one, ``{"type": "adaptive"}`` plus
    ``output_config.effort`` for the families that removed it. See
    :func:`needs_adaptive`.
    """
    spec = LEVELS.get(normalise(level) or "")
    if spec is None:
        return {}
    if protocol == "anthropic":
        if wants_adaptive(model, adaptive):
            # No budget to clamp: the model decides how much to spend, and the
            # level is the only control. `display: summarized` is what makes
            # the reasoning visible in the transcript — the default is
            # "omitted", which would silently turn /think into a no-op on
            # exactly the models this branch exists for.
            return {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": spec.wire},
            }
        budget = min(spec.budget, int(ceiling) - CEILING_HEADROOM)
        if budget < MIN_BUDGET:
            return {}
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    # Chat Completions, and the default for anything unrecognised: the field is
    # top-level and every OpenAI-shaped server either honours it or says so.
    return {"reasoning_effort": spec.wire}


def describe(level: str | None, protocol: str, ceiling: int,
             model: str | None = None, adaptive: bool = False) -> str:
    """What will actually go on the wire, in the operator's own terms.

    Accepts ``None`` (and any off-word) because the status panel asks what the
    *current* setting sends, and "off" is the default state of a fresh session.
    Said out loud rather than implied, because the two protocols do visibly
    different things with the same level and only one of them can be seen in
    the transcript (thinking is streamed back; an effort field is not).
    """
    spec = LEVELS.get(normalise(level) or "")
    if spec is None:
        return "nothing sent — the model's own default"
    if protocol == "anthropic":
        if wants_adaptive(model, adaptive):
            return (f"adaptive thinking, output_config.effort={spec.wire} "
                    f"on Messages")
        fragment = body(level, protocol, ceiling, model, adaptive)
        thinking = fragment.get("thinking")
        if not thinking:
            return (f"no room for a thinking budget under the {ceiling}-token "
                    f"ceiling — nothing sent")
        return (f"thinking budget {thinking['budget_tokens']} of {ceiling} "
                f"tokens on Messages")
    return f"reasoning_effort={spec.wire} on Chat Completions"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def pref_file() -> Path:
    """The remembered level, beside ``current-theme`` and ``current-source``."""
    return ZIMZILLA_HOME / "effort"


def read_persisted() -> str | None:
    """The remembered level, or ``None``.

    Never raises: a missing, unreadable or junk file means "no preference",
    which is exactly the behaviour before ``/effort`` existed.
    """
    try:
        text = pref_file().read_text(encoding="utf-8")
    except OSError:
        return None
    return normalise(text)


def persist(level: str | None) -> None:
    """Record *level*; ``None`` (or any off-word) forgets it.

    Best-effort like ``theme.set_current_theme``: a read-only home must not
    break the TUI over a preference. Forgetting removes the file rather than
    writing "off" or an empty line, so absence stays the single meaning of
    "no preference" — and a half-written file can never read as a level.
    """
    try:
        name = normalise(level)
        if name is None:
            pref_file().unlink(missing_ok=True)
            return
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        pref_file().write_text(name + "\n", encoding="utf-8")
    except OSError:
        pass
