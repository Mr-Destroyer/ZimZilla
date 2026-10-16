"""Regression suite for the boot screen's spinning logo.

Run:  ./.venv/bin/python tests/test_boot.py

The operator asked:

    "when the harness loads , the zimzilla logo should spin , so it will look
     cool"

Cosmetic on its face, but two of the properties below are load-bearing and
were both *broken* in the first cut of this feature, which is why they are
pinned rather than left to the eye:

1. **It must settle onto the static mark.** The mark the splash uses is
   ``EMBLEM`` — a rounded block, ``▄███▄`` across the top. Scaling a flat cap
   by ``|cos theta|`` gives ``▄▄▄▄▄`` at full width, so the animation used to
   end with a visible snap as the logo changed shape. Facing frames therefore
   return ``EMBLEM`` itself.

2. **The spin must be symmetric.** Deriving geometry from ``|cos theta|``
   directly put the mirror-image frames on opposite sides of a rounding
   boundary: ``cos(pi/3)`` and ``cos(2pi/3)`` are both ``|0.5|``, but one
   rendered three columns wide and its own mirror four, with a different
   shading glyph. Frames are now keyed off the frame index, and the second
   half-turn is the first read backwards.

The clock is never read inside the animation. ``spin_frame_index(elapsed)``
takes the time as a parameter — the ``spinner_at(now)`` idiom from ``web.py``.
That is deliberate: the one flaky check in this suite is flaky precisely
because the other animation reads ``time.monotonic()`` internally with no
injectable ``now``, and this feature is not going to join it. The purity check
at the end is that promise, asserted.

No network, and no Textual harness: the wiring checks drive ``BootScreen``
directly with a stubbed ``query_one``, so nothing here depends on a terminal.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui.banner import (  # noqa: E402
    EMBLEM,
    EMBLEM_WIDTH,
    SPIN_FRAMES,
    SPIN_SECONDS,
    SPIN_STEP,
    emblem_frame,
    spin_frame,
    spin_frame_index,
)
from zimzilla.ui.boot import TICKS_PER_FRAME, BootScreen  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _width(frame: list[str]) -> int:
    """Visible width of a frame — its widest row, stripped."""
    return max(len(r.strip()) for r in frame)


# ---------------------------------------------------------------------------
# 1 — it lands exactly on the static mark
# ---------------------------------------------------------------------------

def test_lands_on_static_mark() -> None:
    """The property that makes the animation settle without a visible jump."""
    check("emblem_frame(0.0) is EMBLEM exactly",
          emblem_frame(0.0) == list(EMBLEM), "")
    check("emblem_frame(pi) is EMBLEM exactly",
          emblem_frame(math.pi) == list(EMBLEM), "")
    check("spin_frame(0) is EMBLEM exactly",
          spin_frame(0) == list(EMBLEM), "")
    check("the far side of the turn is EMBLEM too (a symmetric mark)",
          spin_frame(SPIN_FRAMES // 2) == list(EMBLEM), "")

    # The specific snap that was fixed: a flat cap at full width.
    full = spin_frame(0)
    check("the full-width frame keeps its rounded cap, not a flat one",
          "▄███▄" in full[0] and "▀███▀" in full[-1],
          f"top={full[0]!r}")


# ---------------------------------------------------------------------------
# 2 — every frame is the same shape, so the layout cannot jump
# ---------------------------------------------------------------------------

def test_every_frame_is_four_by_seven() -> None:
    """A frame of a different height would shove the wordmark up and down."""
    bad = []
    for i in range(SPIN_FRAMES):
        fr = spin_frame(i)
        if len(fr) != len(EMBLEM) or any(len(r) != EMBLEM_WIDTH for r in fr):
            bad.append((i, [len(r) for r in fr]))
    check("all frames are 4 rows x EMBLEM_WIDTH cols", not bad, str(bad))

    check("EMBLEM_WIDTH still matches EMBLEM",
          EMBLEM_WIDTH == max(len(r) for r in EMBLEM), str(EMBLEM_WIDTH))


# ---------------------------------------------------------------------------
# 3 — the spin is symmetric (the regression this feature actually had)
# ---------------------------------------------------------------------------

def test_spin_is_symmetric() -> None:
    """Frame i and its mirror must render identically.

    This is the check that caught the float-boundary artifact: keying the
    geometry off ``|cos theta|`` made f2 three columns wide and f10 four.
    """
    asym = []
    for i in range(SPIN_FRAMES):
        j = (SPIN_FRAMES - i) % SPIN_FRAMES
        if spin_frame(i) != spin_frame(j):
            asym.append((i, j, spin_frame(i)[1], spin_frame(j)[1]))
    check("mirror-image frames are identical", not asym, str(asym[:2]))

    # ...and the same must hold through the public angle-based entry point,
    # which is what the boot screen and any future caller use.
    asym_angle = []
    for i in range(SPIN_FRAMES):
        a = i * 2 * math.pi / SPIN_FRAMES
        b = (SPIN_FRAMES - i) * 2 * math.pi / SPIN_FRAMES
        if emblem_frame(a) != emblem_frame(b):
            asym_angle.append(i)
    check("mirror-image angles agree too", not asym_angle, str(asym_angle))


# ---------------------------------------------------------------------------
# 4 — it actually spins
# ---------------------------------------------------------------------------

def test_it_actually_spins() -> None:
    """Guards the other failure mode: a no-op that renders EMBLEM every tick."""
    widths = [_width(spin_frame(i)) for i in range(SPIN_FRAMES)]
    check("the width varies across the turn", len(set(widths)) > 1, str(widths))
    check("there is an edge-on frame narrower than the mark",
          min(widths) < EMBLEM_WIDTH, f"min={min(widths)}")
    check("there is a full-width frame", max(widths) == EMBLEM_WIDTH,
          f"max={max(widths)}")
    check("frame 0 is the full-width one, so it starts legible",
          widths[0] == EMBLEM_WIDTH, str(widths[0]))

    # Distinct frames, not just distinct widths — the shading has to move too.
    check("frames are not all the same renderable",
          len({tuple(spin_frame(i)) for i in range(SPIN_FRAMES)}) > 2,
          "")


def test_shading_is_monotone() -> None:
    """The fill darkens as the mark turns away, and brightens coming back.

    An earlier table (``█ █ ▓ ▒ ▓ ▓ █``) plateaued and then jumped, so the last
    step read as a flash rather than a turn. What is asserted here is the shape
    of the sequence: it is a mirror of itself, and the darker glyphs belong to
    the narrower frames.
    """
    from zimzilla.ui.banner import SPIN_SHADES

    order = "█▓▒░"  # lit -> dark
    folds = [order.index(c) for c in SPIN_SHADES]

    check("shading is mirrored about edge-on",
          folds == folds[::-1], str(folds))

    # Walking from facing-the-viewer to edge-on, brightness only ever drops.
    inward = folds[:len(folds) // 2 + 1]
    check("shading never brightens on the way in",
          all(a <= b for a, b in zip(inward, inward[1:])), str(inward))
    check("the darkest glyphs sit on the narrowest frames",
          order.index(spin_frame(len(folds) // 2)[1].strip()[0]) == max(folds),
          f"{spin_frame(len(folds) // 2)[1].strip()[0]} at edge-on")

    full = [i for i in range(SPIN_FRAMES) if _width(spin_frame(i)) == EMBLEM_WIDTH]
    check("the lit glyph is reserved for the widest frames",
          all(spin_frame(i)[1].strip()[0] == "█" for i in full), str(full))


# ---------------------------------------------------------------------------
# 5 — the frame index is total and cyclic
# ---------------------------------------------------------------------------

def test_frame_index_is_total_and_cyclic() -> None:
    """It is fed a monotonic tick count, so it must never run off the end."""
    check("elapsed 0 is frame 0", spin_frame_index(0.0) == 0, "")
    check("exactly one revolution wraps to frame 0",
          spin_frame_index(SPIN_SECONDS) == 0, str(spin_frame_index(SPIN_SECONDS)))
    check("a negative elapsed still lands in range",
          0 <= spin_frame_index(-SPIN_STEP) < SPIN_FRAMES,
          str(spin_frame_index(-SPIN_STEP)))

    big = [spin_frame_index(t) for t in (999.9, 1e6, SPIN_STEP * 10_000)]
    check("a large elapsed still lands in range",
          all(0 <= i < SPIN_FRAMES for i in big), str(big))

    frac = [spin_frame_index(SPIN_STEP * k + SPIN_STEP / 3) for k in range(SPIN_FRAMES)]
    check("a fractional elapsed steps one frame at a time",
          frac == list(range(SPIN_FRAMES)), str(frac))

    # Every frame is reachable by wall-clock time, and each exactly once per turn.
    seen = [spin_frame_index(SPIN_STEP * k) for k in range(SPIN_FRAMES)]
    check("one revolution visits every frame once",
          sorted(seen) == list(range(SPIN_FRAMES)), str(seen))


def test_angle_rounding_hits_each_frame_once() -> None:
    """Stepping by 2*pi/SPIN_FRAMES must not skip or double a frame.

    ``emblem_frame`` rounds rather than floors, so this is the property that
    depends on it.
    """
    got = [emblem_frame(i * 2 * math.pi / SPIN_FRAMES) for i in range(SPIN_FRAMES)]
    want = [spin_frame(i) for i in range(SPIN_FRAMES)]
    check("each frame angle maps to its own frame", got == want, "")


# ---------------------------------------------------------------------------
# 6 — the boot screen wiring
# ---------------------------------------------------------------------------

class _Capture:
    """Stands in for a Static: records what it was last told to display."""

    def __init__(self) -> None:
        self.last = None

    def update(self, renderable) -> None:
        self.last = renderable


def _screen(checks=3) -> tuple[BootScreen, dict]:
    """A BootScreen with its widget lookups stubbed out, so it needs no app."""
    bs = BootScreen(get_palette("green"),
                    [(f"CHECK {i}", "ok", True) for i in range(checks)],
                    lambda *a, **k: None)
    caps: dict[str, _Capture] = {}
    bs.query_one = lambda sel, *a, **k: caps.setdefault(sel, _Capture())
    _stub_timer(bs)
    return bs, caps


def _stub_timer(bs: BootScreen) -> None:
    """Replace ``set_timer`` with a recorder.

    Reaching the fade phase calls ``set_timer`` to schedule the handoff, and
    Textual's real one needs a running event loop. The app always has one; a
    unit test driving ``_tick`` directly does not. Recording the call instead
    keeps the phase machine under test without dragging in an app.
    """
    bs.timers: list[tuple] = []
    bs.set_timer = lambda delay, cb, *a, **k: bs.timers.append((delay, cb))


def _emblem_plain(caps: dict) -> str:
    return caps["#boot-emblem"].last.plain


def test_wiring_before_first_tick() -> None:
    """Mounted but not yet ticked: the mark is there, upright."""
    bs, caps = _screen()
    bs._render_emblem()
    check("the emblem widget renders before the first tick",
          caps["#boot-emblem"].last is not None, "")
    check("it starts as the static mark",
          _emblem_plain(caps) == "\n".join(EMBLEM), repr(_emblem_plain(caps)))


def test_wiring_spins_then_settles() -> None:
    """The whole point: it turns during the reveal and stops when it is done."""
    bs, caps = _screen()
    bs._render_emblem()

    # Walk the banner phase, collecting what the widget was shown.
    seen: list[str] = []
    for _ in range(400):
        if bs._phase != "banner":
            break
        bs._tick()
        seen.append(_emblem_plain(caps))
    check("the banner phase ends within a sane number of ticks",
          bs._phase != "banner", f"phase={bs._phase}")

    check("the emblem changed while the wordmark revealed",
          len(set(seen)) > 1, f"{len(set(seen))} distinct")

    # The spin must actually complete before it settles. The first cut of this
    # feature held each frame for a single tick, so all twelve went by in
    # 0.54s — well before the wordmark finished revealing — and the logo sat
    # static for most of the animation. The banner phase is ~32 ticks, so the
    # spin needs at least SPIN_FRAMES * TICKS_PER_FRAME of them to finish.
    #
    # Note the count of *distinct* renderables is lower than SPIN_FRAMES: the
    # mark is symmetric, so a frame going in and its twin coming out look the
    # same. Five distinct pictures is correct; what must hold is that the whole
    # revolution was walked, which is the frame-slot count below.
    check("the revolution is held for its full duration",
          len(seen) >= SPIN_FRAMES * TICKS_PER_FRAME,
          f"{len(seen)} ticks of {SPIN_FRAMES * TICKS_PER_FRAME} needed")
    check("it is not already static partway through the reveal",
          len(set(seen)) > 1 and seen[0] != seen[len(seen) // 2],
          f"{len(set(seen))} distinct pictures")

    # The frame count and the geometry must both hold across the whole spin.
    shapes = {tuple(len(r) for r in s.split("\n")) for s in seen}
    check("every spun frame is 4 rows of EMBLEM_WIDTH",
          shapes == {(EMBLEM_WIDTH,) * len(EMBLEM)}, str(shapes))

    check("it settles on the static mark once revealed",
          _emblem_plain(caps) == "\n".join(EMBLEM), repr(_emblem_plain(caps)))

    # And it must stay settled while the system check runs.
    for _ in range(4):
        bs._tick()
    check("it stays still through the check phase",
          _emblem_plain(caps) == "\n".join(EMBLEM), repr(_emblem_plain(caps)))


def test_wiring_styles_the_frame_as_a_unit() -> None:
    """Every row of a spun frame shares one style — lit frames bright, edge-on dim.

    Styling row by row was the first cut: it lit the half-block caps and dimmed
    the fill *on the same frame*, which reads as a mistake rather than as a
    turn. The style now comes from the frame's own fill glyph, so it tracks the
    shade table instead of inventing a second brightness curve.

    Note which frame is which: ``SPIN_FRAMES // 2`` is the mark facing *away*,
    which is full width and lit. The narrowest frame is a quarter turn in.
    """
    bs, caps = _screen()
    bs._render_emblem()

    def styles():
        return {str(s.style) for s in caps["#boot-emblem"].last.spans}

    check("the upright frame is styled bold accent",
          styles() == {"bold #00ffaa"}, str(styles()))

    edge_at = SPIN_FRAMES // 4
    while bs._spin_index() != edge_at and bs._phase == "banner":
        bs._tick()
    check("the edge-on frame is reached during the banner phase",
          bs._spin_index() == edge_at, f"phase={bs._phase}")
    check("it is the narrowest frame",
          _width([r for r in caps["#boot-emblem"].last.plain.split("\n")])
          < EMBLEM_WIDTH,
          caps["#boot-emblem"].last.plain.replace("\n", " / "))

    check("every row of it shares one style, not a mix",
          len(styles()) == 1, str(styles()))
    check("and that style is dim, not the accent",
          "bold" not in str(styles()), str(styles()))

    # The far side of the turn comes back full width and lit.
    while bs._spin_index() != SPIN_FRAMES // 2 and bs._phase == "banner":
        bs._tick()
    check("the far side is full width and lit again",
          _width([r for r in caps["#boot-emblem"].last.plain.split("\n")]) == EMBLEM_WIDTH
          and styles() == {"bold #00ffaa"},
          str(styles()))


def test_wiring_reaches_done() -> None:
    """Regression guard: adding the spin must not break the boot sequence."""
    done: list[tuple] = []
    bs = BootScreen(get_palette("green"), [("A", "1", True)],
                    lambda *a, **k: done.append(a))
    caps: dict[str, _Capture] = {}
    bs.query_one = lambda sel, *a, **k: caps.setdefault(sel, _Capture())
    _stub_timer(bs)

    phases = []
    for _ in range(600):
        phases.append(bs._phase)
        if bs._phase == "done":
            break
        bs._tick()

    check("the phase machine still reaches done", bs._phase == "done",
          f"ended in {bs._phase}")
    check("it passes through banner -> checks -> fade -> done",
          [p for p in ("banner", "checks", "fade", "done") if p in phases]
          == ["banner", "checks", "fade", "done"], str(sorted(set(phases))))

    check("the handoff timer is scheduled with the documented dwell",
          len(bs.timers) == 1 and bs.timers[0][0] == 2.2
          and bs.timers[0][1] == bs._finish,
          str([(d, getattr(c, "__name__", c)) for d, c in bs.timers]))

    bs._finish()
    check("on_done still fires", len(done) == 1, str(len(done)))
    bs._finish()
    check("on_done fires only once", len(done) == 1, str(len(done)))


def test_ticks_per_frame_keeps_the_turn_in_step() -> None:
    """The spin's duration must track SPIN_SECONDS, not drift from it."""
    seconds = TICKS_PER_FRAME * SPIN_FRAMES * 0.045
    check("a revolution lasts about SPIN_SECONDS",
          abs(seconds - SPIN_SECONDS) < 0.05,
          f"{seconds:.2f}s vs {SPIN_SECONDS}s")
    check("each frame is held at least one tick", TICKS_PER_FRAME >= 1,
          str(TICKS_PER_FRAME))


# ---------------------------------------------------------------------------
# 7 — purity: no clock, no state, no flakiness
# ---------------------------------------------------------------------------

def test_the_animation_reads_no_clock() -> None:
    """The promise that keeps this out of the suite's flaky corner."""
    check("spin_frame is a pure function of its index",
          all(spin_frame(i) == spin_frame(i) for i in range(SPIN_FRAMES)), "")
    check("emblem_frame is a pure function of its angle",
          all(emblem_frame(a) == emblem_frame(a)
              for a in (0.0, 0.3, math.pi / 2, 2.4, math.pi)), "")
    check("spin_frame_index is a pure function of its elapsed",
          all(spin_frame_index(t) == spin_frame_index(t)
              for t in (0.0, 0.08, 0.37, 5.0)), "")

    # It must not have started consulting the wall clock behind our back.
    # Checked by AST rather than substring: the word "time" appears in prose
    # comments throughout this module, so a substring match would only be
    # measuring the docstrings.
    import ast
    import zimzilla.ui.banner as B

    tree = ast.parse(Path(B.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    check("banner.py imports no clock module",
          not ({"time", "datetime", "perf_counter"} & imported),
          str(sorted(imported)))

    spin_fns = {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name in ("spin_frame", "emblem_frame", "spin_frame_index")}
    check("all three spin functions are defined and clock-free",
          spin_fns == {"spin_frame", "emblem_frame", "spin_frame_index"},
          str(sorted(spin_fns)))


# ---------------------------------------------------------------------------

def main() -> int:
    test_lands_on_static_mark()
    test_every_frame_is_four_by_seven()
    test_spin_is_symmetric()
    test_it_actually_spins()
    test_shading_is_monotone()
    test_frame_index_is_total_and_cyclic()
    test_angle_rounding_hits_each_frame_once()
    test_wiring_before_first_tick()
    test_wiring_spins_then_settles()
    test_wiring_styles_the_frame_as_a_unit()
    test_wiring_reaches_done()
    test_ticks_per_frame_keeps_the_turn_in_step()
    test_the_animation_reads_no_clock()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
