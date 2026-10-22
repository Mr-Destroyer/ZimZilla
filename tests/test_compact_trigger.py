"""The 200k auto-compact trigger, overflow recovery, and the animation.

Run:  ./.venv/bin/python tests/test_compact_trigger.py

Pins the fix for a reported failure:

    "once I reach the context limit of a model it shows bad request"

Two things were wrong behind that. The threshold was a *percentage of the
resolved window*, so a model whose window resolved too large — the catalog
over-promising, or nothing published and the 1M fallback applying — never
reached 80% before the gateway answered 400. And the overflow detector only
matched a handful of wordings, so a bare ``400 bad request`` on an over-long
prompt fell through to the generic handler instead of being compacted.

What is pinned here:

1. ``compact_threshold`` is the EARLIER of the window fraction and
   ``compact_tokens`` (200k by default).
2. A 1M-window session therefore compacts at 200k, not at 800k.
3. A small window still compacts by fraction — the absolute cap never delays it.
4. ``compact_tokens=0`` restores percentage-only behaviour.
5. A limit stated by the endpoint in its own rejection is read back and wins
   over the catalog for the rest of the session.
6. A bare 400/413 on a prompt already at the threshold is treated as an
   overflow; the same 400 on a small prompt is not.
7. The animation renders real numbers, shows the outcome, and actually moves.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui.rails import ContextGauge  # noqa: E402
from zimzilla.ui.renderers import COMPACT_SPINNER, compaction_panel  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _bad_request(text: str, status: int = 400) -> anthropic.APIStatusError:
    resp = type("R", (), {"status_code": status, "request": None, "headers": {}})()
    cls = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    return cls(text, response=resp, body=None)


# ---------------------------------------------------------------------------
# 1-4 — the threshold itself
# ---------------------------------------------------------------------------

def test_threshold_is_the_earlier_of_fraction_and_cap(tmp: Path) -> None:
    cfg = _cfg(tmp)  # defaults: compact_at 0.8, compact_tokens 200_000
    cfg.context_window = 1_000_000
    agent = Agent(cfg)
    check("threshold: a 1M window compacts at the 200k cap",
          agent.compact_threshold(1_000_000) == 200_000,
          str(agent.compact_threshold(1_000_000)))

    # The reported bug, stated as a property: at 200k the session must compact
    # on a 1M window. Under the old percentage-only rule the trigger was 800k,
    # so the request at 200k went out and came back as a bad request.
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    agent._last_input_tokens = 200_000
    check("threshold: a 200k prompt trips it on a 1M window",
          agent._should_compact(1_000_000) is True)
    agent._last_input_tokens = 199_000
    check("threshold: 199k does not", agent._should_compact(1_000_000) is False)

    cfg.context_window = 100_000
    small = Agent(cfg)
    check("threshold: a small window still compacts by fraction",
          small.compact_threshold(100_000) == 80_000,
          str(small.compact_threshold(100_000)))

    cfg.context_window = 1_000_000
    cfg.compact_tokens = 0
    pct = Agent(cfg)
    check("threshold: compact_tokens=0 is percentage-only",
          pct.compact_threshold(1_000_000) == 800_000,
          str(pct.compact_threshold(1_000_000)))

    cfg.compact_tokens = 200_000
    cfg.compact_at = 0.5
    half = Agent(cfg)
    check("threshold: the fraction can win over the cap",
          half.compact_threshold(100_000) == 50_000,
          str(half.compact_threshold(100_000)))


# ---------------------------------------------------------------------------
# 5 — the endpoint's own figure
# ---------------------------------------------------------------------------

def test_stated_window_limit_is_read_back(tmp: Path) -> None:
    cases = {
        "prompt is too long: 210000 tokens > 200000 maximum": 200_000,
        "This model's maximum context length is 8192 tokens.": 8192,
        "input length and `max_tokens` exceed context limit: 210000 > 200000": 200_000,
        "Invalid model name passed in model=claude-opus-5": 0,
        "bad request": 0,
    }
    for message, want in cases.items():
        got = Agent._stated_window_limit(message)
        check(f"stated: {message[:38]!r} -> {want}", got == want, str(got))

    cfg = _cfg(tmp)
    cfg.context_window = 1_000_000
    agent = Agent(cfg)
    check("stated: a fresh agent trusts the configured window",
          agent.context_window() == 1_000_000, str(agent.context_window()))
    agent._window_hint = 200_000
    check("stated: the endpoint's figure caps it",
          agent.context_window() == 200_000, str(agent.context_window()))
    check("stated: and the threshold follows the corrected window",
          agent.compact_threshold() == 160_000,
          str(agent.compact_threshold()))


# ---------------------------------------------------------------------------
# 6 — the bare 400
# ---------------------------------------------------------------------------

def test_bare_bad_request_on_a_full_prompt_is_an_overflow(tmp: Path) -> None:
    cfg = _cfg(tmp)
    cfg.context_window = 1_000_000
    agent = Agent(cfg)
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    bare = _bad_request("bad request")
    agent._last_input_tokens = 0
    check("bare 400: unmeasured prompt is not assumed to be an overflow",
          agent._looks_like_overflow(bare, 1_000_000) is False)

    agent._last_input_tokens = 199_000
    check("bare 400: a prompt at the threshold IS an overflow",
          agent._looks_like_overflow(bare, 1_000_000) is True)

    agent._last_input_tokens = 12_000
    check("bare 400: a small prompt is not (the model name stays the suspect)",
          agent._looks_like_overflow(bare, 1_000_000) is False)

    check("bare 400: 413 is an overflow whatever it says",
          agent._looks_like_overflow(_bad_request("nope", 413), 1_000_000) is True)
    check("bare 400: a non-HTTP error never is",
          agent._looks_like_overflow(ValueError("bad request"), 1_000_000) is False)


def test_overflow_wordings() -> None:
    wanted = (
        "prompt is too long: 210000 tokens > 200000 maximum",
        "This model's maximum context length is 8192 tokens",
        "too many tokens in request",
        "input length and `max_tokens` exceed context limit: 210000 > 200000",
        "Please reduce the length of the messages",
        "Request too large for this endpoint",
    )
    for msg in wanted:
        check(f"wording: {msg[:40]!r}",
              Agent._is_context_overflow(_bad_request(msg)))
    check("wording: a bad model name still does not match",
          not Agent._is_context_overflow(
              _bad_request("Invalid model name passed in model=nope")))


# ---------------------------------------------------------------------------
# 7 — the animation
# ---------------------------------------------------------------------------

def test_panel_renders_real_numbers_and_moves() -> None:
    p = get_palette("green", "#101315")
    base = {"used": 182_400, "window": 1_000_000, "threshold": 200_000,
            "trigger": "threshold", "elapsed": 2.5}

    frame0 = compaction_panel({**base, "frame": 0}, p, width=72).plain
    frame7 = compaction_panel({**base, "frame": 7}, p, width=72).plain
    check("panel: names the operation", "CONTEXT COMPACTION" in frame0)
    check("panel: shows the trigger size", "200.0k" in frame0)
    check("panel: shows the measured prompt", "182.4k" in frame0)
    check("panel: draws all four cascade stages",
          all(stage in frame0 for stage in ("read", "fold", "summarise", "rebuild")),
          frame0.replace("\n", "|")[:120])
    check("panel: the spinner is drawn", any(s in frame0 for s in COMPACT_SPINNER))
    check("panel: it animates (two frames differ)", frame0 != frame7)
    check("panel: the cascade is not a progress bar that finishes",
          "%" not in frame0.split("rebuild")[0], frame0.replace("\n", "|")[:160])

    done_ok = compaction_panel({**base, "frame": 9, "done": True, "ok": True,
                                "note": "The session built X."}, p, width=72).plain
    check("panel: success says so", "history compacted" in done_ok)
    check("panel: success reports what was freed", "freed" in done_ok)
    check("panel: the summary note is shown", "The session built X." in done_ok)
    check("panel: the spinner stops when done",
          not any(s in done_ok for s in COMPACT_SPINNER))

    done_bad = compaction_panel({**base, "frame": 3, "done": True, "ok": False,
                                 "note": "the summariser returned nothing"},
                                p, width=72).plain
    check("panel: failure says so", "compaction failed" in done_bad)
    check("panel: failure keeps the reason", "returned nothing" in done_bad)

    overflow = compaction_panel({**base, "trigger": "overflow", "frame": 1},
                                p, width=72).plain
    check("panel: an overflow names itself", "overflow" in overflow)


def test_gauge_draws_the_threshold_and_folds() -> None:
    p = get_palette("green", "#101315")
    g = ContextGauge(p, total=1_000_000, width=12)
    g.set(182_400, 1_000_000, 200_000)
    still = g.gauge_text().plain
    check("gauge: shows usage against the window", "182.4k / 1.0M" in still, still)
    check("gauge: names the compact threshold", "compacts at 200.0k" in still, still)
    check("gauge: marks the threshold on the bar", "┊" in still, still)
    check("gauge: is not folding when idle", "folding" not in still)

    g.set_compacting(True)
    a = g.gauge_text().plain
    g.tick()
    g.tick()
    b = g.gauge_text().plain
    check("gauge: folding while compacting", "folding" in a)
    check("gauge: the sweep moves", a != b)
    check("gauge: the sweep cursor is drawn", "▸" in a or "▸" in b)

    g.set_compacting(False)
    check("gauge: stops folding afterwards", "folding" not in g.gauge_text().plain)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zz-compact-"))
    test_threshold_is_the_earlier_of_fraction_and_cap(tmp)
    test_stated_window_limit_is_read_back(tmp)
    test_bare_bad_request_on_a_full_prompt_is_an_overflow(tmp)
    test_overflow_wordings()
    test_panel_renders_real_numbers_and_moves()
    test_gauge_draws_the_threshold_and_folds()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
