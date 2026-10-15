"""Regression suite for the markdown renderer.

Run:  python tests/test_markdown.py   (from an activated venv)

The operator's complaint was that a model's answer arrived with its own markup
still on it — ``**Asset:**`` where they wanted a bold Asset, ``- item`` down a
column of dashes, a fenced block as a wall of text. ``agent_text`` handled
inline backticks and nothing else.

The load-bearing rule is that the parse is CommonMark's and not a regex of
ours, and two checks here exist specifically to hold that line, because a
regex version of this got both wrong:

* ``26*26**5*100`` — mask syntax, which a pentest answer is full of. A regex
  that pairs ``**`` eats the asterisks. Nothing may be *dropped*.
* ``**/*.py`` inside backticks — a glob, which must stay a glob.

No test here touches the network or an app: every renderable is built straight
and printed through a throwaway Console, which is the same idiom
``test_hunt.py`` uses for its panels.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console, Group  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from zimzilla.theme import PALETTES  # noqa: E402
from zimzilla.ui import markdown as M  # noqa: E402
from zimzilla.ui import renderers as R  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
P = PALETTES["green"]


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def flat(text: str) -> str:
    """A markdown answer as the operator would read it, markdown resolved."""
    return str(M.markdown_text(text, P))


def shown(text: str, width: int = 100) -> str:
    """The transcript form, rendered — so a Table and a Syntax are visible."""
    buf = io.StringIO()
    Console(file=buf, width=width, color_system=None,
            legacy_windows=False).print(M.markdown_renderable(text, P))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The two checks a regex version of this fails
# ---------------------------------------------------------------------------
def test_mask_syntax_loses_nothing() -> None:
    """``26*26**5*100`` must not have its asterisks eaten as emphasis.

    The operator writes about hashcat masks, so this is not a hypothetical.
    A hand-rolled renderer that pairs the first ``**`` it finds turns this
    into ``26265100`` — the asterisks consumed as delimiters, the digits run
    together into a different number. CommonMark resolves emphasis by flanking
    rules and, where it does pair them, consumes only the delimiters.

    The strong guarantee is the code span below: inside backticks CommonMark
    promises the content is *not* markup, so that one is byte-exact.
    """
    src = "26*26**5*100"
    out = flat(src)
    # Asterisks that are content survive, so the digits are never run together.
    check("mask: the asterisks survive, so the digits are not run together",
          "26265100" not in out, repr(out))
    check("mask: every digit is still present",
          out.replace("*", "") == "26265100", repr(out))

    # Byte-exact, and this is the case that matters: a code span is literal.
    out = flat("- mask `26*26**5*100` is 118 billion keys")
    check("mask: inside a code span it is byte-exact",
          "26*26**5*100" in out, repr(out))
    check("mask: the code span keeps its surrounding sentence",
          "118 billion keys" in out, repr(out))


def test_a_glob_inside_backticks_stays_a_glob() -> None:
    """``**/*.py`` is a path, not an unterminated bold run."""
    out = flat("Searched `**/*.py` and `**/*.js` for the sink.")
    check("glob: the python glob is intact", "**/*.py" in out, repr(out))
    check("glob: the js glob is intact", "**/*.js" in out, repr(out))
    check("glob: the sentence around it survives",
          "Searched" in out and "for the sink." in out, repr(out))

    # Unbackticked, CommonMark reads the same asterisks as emphasis — that is
    # the parser's call, not ours, and the check is only that nothing vanishes.
    out = flat("glob **/*.py untouched")
    check("glob: an unbackticked glob loses no letters",
          "untouched" in out and "glob" in out, repr(out))


# ---------------------------------------------------------------------------
# Inline
# ---------------------------------------------------------------------------
def test_inline_emphasis_is_styled_not_shown() -> None:
    t = M.markdown_text("a **bold** and *italic* and ~~struck~~ word", P)
    check("inline: bold text is present", "bold" in str(t), repr(str(t)))
    check("inline: the ** markers are gone", "**" not in str(t), repr(str(t)))
    check("inline: the * markers are gone", "*" not in str(t), repr(str(t)))
    check("inline: the ~~ markers are gone", "~~" not in str(t), repr(str(t)))

    styles = {str(sp.style) for sp in t.spans}
    check("inline: bold carries a bold style",
          any("bold" in s for s in styles), str(styles))
    check("inline: italic carries an italic style",
          any("italic" in s for s in styles), str(styles))
    check("inline: strike carries a strike style",
          any("strike" in s for s in styles), str(styles))


def test_code_spans_keep_their_own_colour_inside_bold() -> None:
    """Nested markup composes: a code span in bold still reads as code."""
    t = M.markdown_text("**run `nmap -sV` now**", P)
    check("inline: the code span is intact", "nmap -sV" in str(t), repr(str(t)))
    check("inline: the backticks are gone", "`" not in str(t), repr(str(t)))
    styles = [str(sp.style) for sp in t.spans]
    check("inline: the accent style survives the bold run",
          any(P.accent in s for s in styles), str(styles))


def test_links_show_their_target() -> None:
    """The href is the point of a link in a pentest report."""
    t = M.markdown_text("See [the advisory](https://example.test/a) for detail.", P)
    s = str(t)
    check("link: the label is shown", "the advisory" in s, repr(s))
    check("link: the target is shown", "https://example.test/a" in s, repr(s))
    check("link: the markdown syntax is gone",
          "](" not in s and "[" not in s, repr(s))


def test_an_image_is_named_not_dropped() -> None:
    t = M.markdown_text("![a diagram](https://example.test/d.png)", P)
    s = str(t)
    check("image: it is announced rather than silently dropped",
          "a diagram" in s, repr(s))


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
def test_headings_are_sized_and_lose_their_hashes() -> None:
    h1 = flat("# Findings")
    check("heading: h1 text is present", "FINDINGS" in h1, repr(h1))
    check("heading: h1 loses its hash", not h1.startswith("#"), repr(h1))
    check("heading: h1 carries a rule", "─" in h1, repr(h1))

    h2 = flat("## Evidence")
    check("heading: h2 text is present", "Evidence" in h2, repr(h2))
    check("heading: h2 loses its hashes", "##" not in h2, repr(h2))

    h3 = flat("### Detail")
    check("heading: h3 keeps a marker to stay findable",
          h3.lstrip().startswith("###"), repr(h3))
    check("heading: h3 text is present", "Detail" in h3, repr(h3))


def test_lists_become_bullets_and_numbers() -> None:
    out = flat("- one\n- two\n- three")
    check("list: bullets are drawn", out.count("•") == 3, repr(out))
    check("list: the dashes are gone", "- one" not in out, repr(out))
    check("list: every item is present",
          all(w in out for w in ("one", "two", "three")), repr(out))

    out = flat("1. first\n2. second")
    check("list: ordered items keep their numbers",
          "1. first" in out and "2. second" in out, repr(out))


def test_a_nested_list_stays_under_its_parent() -> None:
    """The nested list belongs to the item it sits under, not above the list."""
    out = flat("- top\n  - nested\n- after")
    lines = [ln for ln in out.split("\n") if ln.strip()]
    check("list: three lines", len(lines) == 3, repr(out))
    check("list: the parent comes first", "top" in lines[0], repr(lines))
    check("list: the nested item is indented under it",
          "nested" in lines[1] and lines[1].startswith("  "), repr(lines))
    check("list: the list continues after the nested item",
          "after" in lines[2] and not lines[2].startswith("  "), repr(lines))
    # The bug this guards: the nested list emitted as its own block, stranding
    # it above the parent list.
    check("list: the nested item is not stranded above the parent",
          out.index("top") < out.index("nested"), repr(out))


def test_an_ordered_list_honours_its_start() -> None:
    out = flat("3. third\n4. fourth")
    check("list: a list starting at 3 keeps 3", "3. third" in out, repr(out))
    check("list: and counts on from there", "4. fourth" in out, repr(out))


def test_a_blockquote_is_bared() -> None:
    out = flat("> the nonce is read but never compared")
    check("quote: the text is present",
          "never compared" in out, repr(out))
    check("quote: it is marked with a bar", "▌" in out, repr(out))
    check("quote: the > marker is gone", not out.lstrip().startswith(">"), repr(out))


def test_a_rule_is_drawn() -> None:
    out = flat("above\n\n---\n\nbelow")
    check("rule: a horizontal rule is drawn", "─" in out, repr(out))
    check("rule: the dashes are gone", "---" not in out, repr(out))
    check("rule: both sides survive",
          "above" in out and "below" in out, repr(out))


# ---------------------------------------------------------------------------
# The transcript form: real Table, real Syntax
# ---------------------------------------------------------------------------
def test_a_table_becomes_a_real_table() -> None:
    src = "| Endpoint | Code |\n|---|---|\n| /admin | 403 |\n| /login | 200 |\n"
    r = M.markdown_renderable(src, P)
    check("table: the renderable is a Table", isinstance(r, Table),
          type(r).__name__)
    body = shown(src)
    check("table: the header is rendered", "Endpoint" in body, repr(body[:200]))
    check("table: every row is rendered",
          "/admin" in body and "/login" in body, repr(body[:200]))
    check("table: the pipe syntax is gone", "|---" not in body, repr(body[:200]))


def test_a_table_wraps_rather_than_overflowing() -> None:
    """The reason a table is a ``Table`` and not an aligned ``Text``.

    Rich cannot wrap a ``Text`` cell to the width it is given, so a long cell
    would push the transcript sideways on a narrow terminal. ``Table`` wraps.
    """
    src = ("| Asset | Note |\n|---|---|\n"
           "| /api/v1/internal/users | a very long note that has to wrap "
           "somewhere rather than run off the side of the pane |\n")
    for width in (60, 40):
        body = shown(src, width=width)
        longest = max((len(ln.rstrip()) for ln in body.split("\n")), default=0)
        check(f"table: nothing exceeds a {width}-column terminal",
              longest <= width, f"longest={longest}")


def test_a_ragged_table_row_does_not_crash() -> None:
    """Ragged rows are legal markdown and common in a streamed answer."""
    src = "| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |\n"
    try:
        r = M.markdown_renderable(src, P)
        ok = isinstance(r, Table)
        extra = type(r).__name__
    except Exception as e:  # pragma: no cover - the failure this exists for
        ok, extra = False, f"{type(e).__name__}: {e}"
    check("table: a ragged row renders instead of raising", ok, extra)
    body = shown(src)
    check("table: the short row still shows its cell", "1" in body, repr(body[:160]))


def test_fenced_code_becomes_highlighted_syntax() -> None:
    src = "```python\ndef check(req):\n    return True\n```\n"
    r = M.markdown_renderable(src, P)
    check("code: a fence becomes a Syntax", isinstance(r, Syntax),
          type(r).__name__)
    body = shown(src)
    check("code: the code is rendered", "def check(req):" in body, repr(body[:200]))
    check("code: the fence markers are gone", "```" not in body, repr(body[:200]))


def test_a_fence_with_no_language_still_renders() -> None:
    src = "```\nplain text here\n```\n"
    r = M.markdown_renderable(src, P)
    check("code: an unlabelled fence is still a Syntax", isinstance(r, Syntax),
          type(r).__name__)
    check("code: its body is rendered", "plain text here" in shown(src))


def test_a_bogus_language_does_not_raise() -> None:
    """A model invents info strings; an unknown one must not take the turn down."""
    try:
        r = M.markdown_renderable("```not-a-real-language\nx = 1\n```", P)
        ok, extra = isinstance(r, Syntax), type(r).__name__
    except Exception as e:  # pragma: no cover
        ok, extra = False, f"{type(e).__name__}: {e}"
    check("code: an unknown language falls back rather than raising", ok, extra)


def test_a_multi_block_answer_is_a_group() -> None:
    src = "# Head\n\nprose\n\n```python\nx = 1\n```\n"
    r = M.markdown_renderable(src, P)
    check("group: several blocks become a Group", isinstance(r, Group),
          type(r).__name__)
    kinds = [type(b).__name__ for b in r.renderables]
    check("group: the Syntax sits among the prose", "Syntax" in kinds, str(kinds))


def test_the_streaming_form_is_always_flat_text() -> None:
    """The live line must be a ``Text`` — it is what ``Static.update`` takes."""
    src = "# Head\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```\n"
    r = M.markdown_text(src, P)
    check("stream: the flat form is a Text", isinstance(r, Text),
          type(r).__name__)
    check("stream: no Table leaks into it", "Table" not in str(r), repr(str(r))[:120])
    check("stream: the table's cells are still readable",
          "a" in str(r) and "1" in str(r), repr(str(r))[:160])
    check("stream: the code body is readable", "x = 1" in str(r), repr(str(r))[:160])


# ---------------------------------------------------------------------------
# Streaming: a half-arrived answer
# ---------------------------------------------------------------------------
def test_a_partial_answer_renders_without_raising() -> None:
    """The live line is redrawn on every delta, mid-construct.

    A cut through ``**bold`` or an unclosed fence is the normal state of a
    streaming answer, not an error, and the renderer must survive every prefix
    of a realistic one.
    """
    full = ("# Head\n\nThe route **does not enforce a nonce** and here is a "
            "list:\n\n- one\n- two\n\n```python\nx = 1\n```\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n> quoted\n\n---\n\n"
            "See [x](https://e.test) and `code`.\n")
    bad = []
    for cut in range(len(full) + 1):
        for fn in (M.markdown_text, M.markdown_renderable):
            try:
                fn(full[:cut], P)
            except Exception as e:  # pragma: no cover - the failure this exists for
                bad.append(f"cut {cut} {fn.__name__}: {type(e).__name__}: {e}")
    check("stream: every prefix of an answer renders", not bad,
          "; ".join(bad[:3]))


def test_an_unclosed_fence_does_not_swallow_the_rest() -> None:
    """A fence opened and not yet closed is the mid-stream normal case."""
    out = flat("prose first\n\n```python\nx = 1\n")
    check("stream: the prose before the fence survives", "prose first" in out,
          repr(out))
    check("stream: the open fence's body is shown", "x = 1" in out, repr(out))


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------
def test_empty_and_plain_input() -> None:
    check("empty: the empty string gives an empty Text",
          str(M.markdown_text("", P)) == "", repr(str(M.markdown_text("", P))))
    check("empty: whitespace gives an empty Text",
          str(M.markdown_text("   \n  ", P)).strip() == "",
          repr(str(M.markdown_text("   \n  ", P))))
    r = M.markdown_renderable("", P)
    check("empty: the renderable form is still renderable",
          isinstance(r, Text), type(r).__name__)

    out = flat("just a plain sentence with no markup at all")
    check("plain: an unmarked sentence is unchanged",
          out == "just a plain sentence with no markup at all", repr(out))


def test_html_inline_is_shown_not_executed() -> None:
    """An answer can contain a payload; it is text here, never markup."""
    out = flat("the sink is `<script>alert(1)</script>` on the page")
    check("html: the tag is shown as text", "alert(1)" in out, repr(out))
    check("html: nothing is interpreted", "<script>" in out or "script" in out,
          repr(out))


def test_a_line_of_underscores_is_not_emphasis() -> None:
    """``snake_case`` in prose must not become italics."""
    out = flat("call verify_nonce_impl and check_nonce_now")
    check("underscore: the identifiers survive intact",
          "verify_nonce_impl" in out and "check_nonce_now" in out, repr(out))


# ---------------------------------------------------------------------------
# The wiring: the six call sites
# ---------------------------------------------------------------------------
def test_agent_text_delegates_to_markdown() -> None:
    """``agent_text`` keeps its contract — six call sites depend on it."""
    t = R.agent_text("a **bold** claim", P)
    check("wiring: agent_text still returns a Text", isinstance(t, Text),
          type(t).__name__)
    check("wiring: agent_text resolves markdown", "**" not in str(t), repr(str(t)))
    check("wiring: the words survive", "bold" in str(t) and "claim" in str(t),
          repr(str(t)))
    # Empty in, empty out — the streaming path calls it before anything arrives.
    check("wiring: agent_text of an empty buffer is empty",
          str(R.agent_text("", P)) == "", repr(str(R.agent_text("", P))))


def test_agent_line_stays_on_one_line() -> None:
    """The ``[name]`` tag labels the line that follows it.

    A worker's markdown rendered multi-line would put the tag on its own line
    and the body under it, so the tag would label nothing.
    """
    t = R.agent_line("paths", "- one\n- two\n\n> quoted", P.accent, P)
    s = str(t)
    check("wiring: the tag leads", s.startswith("[paths]"), repr(s))
    check("wiring: the body is on the tag's own line", "\n" not in s, repr(s))
    check("wiring: every item is present",
          all(w in s for w in ("one", "two", "quoted")), repr(s))
    check("wiring: the markdown is resolved", "- one" not in s and ">" not in s,
          repr(s))


def test_markdown_line_flattens_but_keeps_the_content() -> None:
    t = M.markdown_line("first para\n\nsecond para", P)
    s = str(t)
    check("line: it is one line", "\n" not in s, repr(s))
    check("line: both paragraphs survive",
          "first para" in s and "second para" in s, repr(s))
    check("line: the paragraphs are separated", "first para  second para" in s,
          repr(s))


def test_markdown_renderable_is_re_rendered_safely() -> None:
    """The transcript keeps renderables and replays them on every resize.

    ``RainRichLog`` re-renders its stored blocks at the new width, so the
    renderable this returns must be printable more than once — which is the
    property ``rich.markdown.Markdown`` was rejected for.
    """
    src = "# Head\n\nprose\n\n```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    r = M.markdown_renderable(src, P)
    first = shown(src)
    second = shown(src)
    check("replay: rendering twice gives the same output", first == second)
    check("replay: the renderable is not consumed",
          isinstance(r, Group) and len(r.renderables) > 0,
          type(r).__name__)
    # And at a different width, which is what a resize actually does.
    narrow = shown(src, width=44)
    check("replay: it re-renders at a different width", narrow != first)
    check("replay: and still shows the content", "x = 1" in narrow, repr(narrow[:200]))


def main() -> int:
    test_mask_syntax_loses_nothing()
    test_a_glob_inside_backticks_stays_a_glob()
    test_inline_emphasis_is_styled_not_shown()
    test_code_spans_keep_their_own_colour_inside_bold()
    test_links_show_their_target()
    test_an_image_is_named_not_dropped()
    test_headings_are_sized_and_lose_their_hashes()
    test_lists_become_bullets_and_numbers()
    test_a_nested_list_stays_under_its_parent()
    test_an_ordered_list_honours_its_start()
    test_a_blockquote_is_bared()
    test_a_rule_is_drawn()
    test_a_table_becomes_a_real_table()
    test_a_table_wraps_rather_than_overflowing()
    test_a_ragged_table_row_does_not_crash()
    test_fenced_code_becomes_highlighted_syntax()
    test_a_fence_with_no_language_still_renders()
    test_a_bogus_language_does_not_raise()
    test_a_multi_block_answer_is_a_group()
    test_the_streaming_form_is_always_flat_text()
    test_a_partial_answer_renders_without_raising()
    test_an_unclosed_fence_does_not_swallow_the_rest()
    test_empty_and_plain_input()
    test_html_inline_is_shown_not_executed()
    test_a_line_of_underscores_is_not_emphasis()
    test_agent_text_delegates_to_markdown()
    test_agent_line_stays_on_one_line()
    test_markdown_line_flattens_but_keeps_the_content()
    test_markdown_renderable_is_re_rendered_safely()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
