"""Assistant markdown, rendered as the thing it describes.

A model answers in markdown whether or not anything asked it to: `#` for a
heading, `**` for emphasis, a dash and a space for a bullet, a fenced block for
code, a pipe grid for a table. ``agent_text`` handled exactly one of those —
inline backticks — so everything else reached the transcript as its own source
text. A finding read ``**Asset:**`` where the operator wanted **Asset**, and a
list read ``- item`` down a column of dashes.

This module renders the markdown instead of showing it. The parse is
``markdown-it-py``'s, not a regex of ours, and that is the load-bearing choice:

* A regex that pairs ``**`` sees the asterisks in ``26*26**5*100`` — mask
  syntax, and the sort of thing a pentest answer is full of — and eats them.
  CommonMark resolves emphasis by flanking rules and leaves them alone.
* The same applies to ``**/*.py``: a glob inside backticks must stay a glob,
  and only a parser that knows about code spans can promise that.

``markdown-it-py`` is not a new *install*: rich and textual both require it, so
it is already in every environment that can run ZimZilla. It is nonetheless
listed in ``requirements.txt`` now, because ZimZilla imports it directly and a
transitive dependency it does not declare is one an upstream release can take
away without anything here noticing until import time.

Why not ``rich.markdown.Markdown`` itself: it is a ``__rich_console__``
renderable that only exists while it is being printed. The transcript keeps its
renderables and re-renders them on every resize (``RainRichLog._blocks``), and
the live streaming line needs the text form of a half-arrived answer to hand to
``Static.update``. So this module returns ``rich.text.Text`` — which is what
``agent_text`` always returned, and what ``write_block``, ``set_stream`` and
``agent_line`` already accept.

Two entry points, because the streaming path cannot afford the other one:

``markdown_text(text, palette)``
    A flat ``Text``. Called once per ``text_delta``, so it is the cheap path:
    parse and build, no layout. At 1.4 KB that is ~2 ms, against ~0.08 ms for
    the regex it replaces — the price of reading markdown, and the reason this
    is only wired to the assistant's own prose and not to every line a hunt
    worker emits.

``markdown_renderable(text, palette)``
    The same parse, but fenced code comes back as a ``Syntax`` (highlighted
    with the same theme tool output uses, so code in an answer and code in a
    tool panel read alike) and tables as a real ``rich.table.Table``. A
    ``Group`` when the answer has more than one block. Called once per turn,
    when the answer is finished and handed to the transcript.

A table is a ``Table`` rather than an aligned ``Text`` for one reason: Rich
cannot wrap a ``Text`` cell to the terminal width, so a table with a long cell
would push the transcript sideways on a narrow terminal. ``Table`` wraps.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..theme import Palette

#: ``commonmark`` for the spec, plus the two extensions a model actually uses.
#: ``table`` is a GitHub extension rather than CommonMark proper, and answers
#: are full of them; ``strikethrough`` costs nothing and a model that writes
#: ``~~retired~~`` means it.
_PARSER = MarkdownIt("commonmark").enable("table").enable("strikethrough")

#: A fence's info string, first word only: ```python title="x" is python.
_INFO_RE = re.compile(r"^\s*([\w+.#-]*)")

#: Rules under a heading. A fixed run rather than the heading's own length:
#: a heading is short and the rule is a separator, not an underline, so it
#: should read the same under every heading on the page.
_RULE_WIDTH = 44


# ---------------------------------------------------------------------------
# Inline: emphasis, code, links
# ---------------------------------------------------------------------------
def _inline(children, palette: Palette, *, base: str = "") -> Text:
    """The token stream of one inline run, as styled text.

    Style is carried on a stack rather than recomputed per token, so nested
    markup composes — ``**bold with `code` inside**`` keeps the code span's own
    colour and inherits nothing that would fight it.
    """
    t = Text()
    stack: list[str] = []
    #: Hrefs of the links currently open, innermost last. The href lives on
    #: ``link_open`` and the closing token carries nothing, so it has to be
    #: held from the open until the close to be emitted after the label.
    hrefs: list[str] = []

    def style() -> str:
        parts = [*stack]
        if base:
            parts.append(base)
        return " ".join(parts) if parts else palette.primary

    for tok in children or []:
        kind = tok.type
        if kind == "text":
            t.append(tok.content, style=style())
        elif kind == "code_inline":
            # Its own style, and deliberately not the inherited one: a code
            # span inside bold should still read as a code span.
            t.append(tok.content, style=f"bold {palette.accent}")
        elif kind in ("strong_open", "em_open", "s_open"):
            stack.append({"strong_open": "bold", "em_open": "italic",
                          "s_open": "strike"}[kind])
        elif kind in ("strong_close", "em_close", "s_close"):
            if stack:
                stack.pop()
        elif kind == "link_open":
            stack.append(f"underline {palette.accent}")
            hrefs.append(dict(tok.attrs or {}).get("href", ""))
        elif kind == "link_close":
            if stack:
                stack.pop()
            # The href is the reason a link is worth having in a pentest
            # report, and it is invisible once the label is all that shows.
            # Read from the *open* — ``link_close`` carries no attrs at all,
            # which is how the first draft of this dropped every target.
            href = hrefs.pop() if hrefs else ""
            if href:
                t.append(f" <{href}>", style=palette.dim)
        elif kind == "softbreak":
            t.append(" ", style=style())
        elif kind == "hardbreak":
            t.append("\n", style=style())
        elif kind == "image":
            # No image protocol here; name it so the operator knows one was
            # there rather than silently dropping it.
            src = dict(tok.attrs or {}).get("src", "")
            alt = "".join(c.content for c in (tok.children or []))
            t.append(f"[image: {alt or src}]", style=palette.dim)
        elif kind == "html_inline":
            t.append(tok.content, style=palette.dim)
    return t


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
def _heading(level: int, body: Text, palette: Palette) -> Text:
    """A heading, sized by level the only way a terminal can size anything."""
    t = Text()
    if level == 1:
        t.append(str(body).upper(), style=f"bold {palette.accent}")
        t.append("\n")
        t.append("─" * _RULE_WIDTH, style=palette.dim)
    elif level == 2:
        t.append(str(body), style=f"bold {palette.accent}")
        t.append("\n")
        t.append("─" * _RULE_WIDTH, style=palette.dim)
    else:
        # h3 and deeper are structure inside a section, not a new section —
        # a marker keeps them findable without competing with the h1/h2 rule.
        t.append("#" * level + " ", style=palette.dim)
        t.append(str(body), style=f"bold {palette.primary}")
    return t


def _quote(body: Text, palette: Palette) -> Text:
    """A blockquote: a bar down the left, the same bar the transcript uses."""
    t = Text()
    for n, line in enumerate(body.split("\n")):
        if n:
            t.append("\n")
        t.append("▌ ", style=palette.dim)
        t.append_text(line)
    return t


def _mono_theme(palette: Palette):
    """The syntax theme ``renderers.hacker_syntax`` uses, for fenced code.

    Duplicated deliberately rather than imported: ``renderers`` imports this
    module, so importing back would be a cycle. The two must stay in step —
    a fenced block in an answer and a tool's output should read the same.
    """
    from pygments.style import Style as PygStyle
    from pygments.token import (
        Comment, Error, Generic, Keyword, Name, Number, Operator, String, Token,
    )

    p, dim, accent, red, amber = (
        palette.primary, palette.dim, palette.accent, palette.red, palette.amber,
    )

    class Mono(PygStyle):
        background_color = palette.bg
        styles = {
            Token: p,
            Comment: dim,
            Keyword: f"bold {accent}",
            Operator: p,
            Name: p,
            Name.Builtin: accent,
            Name.Function: f"bold {p}",
            Name.Class: f"bold {accent}",
            String: amber,
            Number: accent,
            Generic: p,
            Generic.Deleted: red,
            Generic.Inserted: accent,
            Error: red,
        }

    return Mono


def _code_block(code: str, info: str, palette: Palette, *, rich: bool):
    """A fenced block: highlighted as code, or flattened to plain text."""
    lang = (_INFO_RE.match(info or "").group(1) or "text")
    code = code.rstrip("\n")
    if rich:
        # Syntax, not a Panel: the answer is prose, and boxing every snippet
        # would bury it. Same highlight theme as a tool panel either way.
        return Syntax(code, lang, theme=_mono_theme(palette), line_numbers=False,
                      word_wrap=True, background_color=palette.bg)
    t = Text()
    for n, line in enumerate(code.split("\n")):
        if n:
            t.append("\n")
        t.append(line, style=palette.primary)
    return t


def _table(header: list[Text], rows: list[list[Text]], palette: Palette,
           *, rich: bool):
    """A pipe table. A real ``Table`` when it is going to be printed.

    ``Table`` is the only one of the two that wraps a cell to the width it is
    given; an aligned ``Text`` would overflow the pane on a narrow terminal.
    """
    if not rich:
        t = Text()
        for n, cell in enumerate(header):
            if n:
                t.append("  ", style=palette.dim)
            t.append(str(cell), style=f"bold {palette.accent}")
        for row in rows:
            t.append("\n")
            for n, cell in enumerate(row):
                if n:
                    t.append("  ", style=palette.dim)
                t.append(str(cell), style=palette.primary)
        return t

    tbl = Table(
        box=None,
        show_header=bool(header),
        header_style=f"bold {palette.accent}",
        border_style=palette.dim,
        pad_edge=False,
        padding=(0, 1),
        show_edge=False,
    )
    for cell in header:
        tbl.add_column(cell)
    width = len(header) or 1
    for row in rows:
        # Ragged rows are legal markdown and common in a streamed answer; pad
        # and trim so ``add_row`` is never handed the wrong arity.
        cells = (row + [Text("")] * width)[:width]
        tbl.add_row(*cells)
    return tbl


def _parse(text: str, palette: Palette, *, rich: bool) -> list:
    """The token stream, as a list of renderables — one per block."""
    blocks: list = []
    #: Open lists. Each frame collects ``(marker, body)`` so the list is
    #: assembled once, at its close, when its depth is finally known.
    lists: list[dict] = []
    #: >0 while inside a list item, so the item's own paragraph is captured
    #: into the list rather than also emitted as a standalone block.
    item_depth = 0
    quote_depth = 0

    def quoted(body: Text) -> Text:
        return _quote(body, palette) if quote_depth else body

    tokens = _PARSER.parse(text)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        kind = tok.type

        if kind == "heading_open":
            level = int(tok.tag[1:])
            body = _inline(tokens[i + 1].children, palette)
            blocks.append(_heading(level, body, palette))
            i += 3
            continue

        if kind == "hr":
            blocks.append(Text("─" * _RULE_WIDTH, style=palette.dim))
            i += 1
            continue

        if kind in ("fence", "code_block"):
            blocks.append(_code_block(tok.content, tok.info, palette, rich=rich))
            i += 1
            continue

        if kind == "blockquote_open":
            quote_depth += 1
            i += 1
            continue

        if kind == "blockquote_close":
            quote_depth -= 1
            i += 1
            continue

        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append({
                "ordered": kind.startswith("ordered"),
                "n": int(dict(tok.attrs or {}).get("start", 1) or 1),
                "items": [],
            })
            i += 1
            continue

        if kind in ("bullet_list_close", "ordered_list_close"):
            frame = lists.pop()
            depth = len(lists)
            body = Text()
            for n, (marker, item) in enumerate(frame["items"]):
                if n:
                    body.append("\n")
                body.append("  " * depth, style=palette.dim)
                body.append(marker, style=palette.accent)
                body.append_text(item)
            if lists:
                # A nested list belongs to the item it sits under. Emitting it
                # as its own block would strand it above the parent list, which
                # is what the first draft of this did.
                if lists[-1]["items"]:
                    parent = lists[-1]["items"][-1][1]
                    for line in body.split("\n"):
                        parent.append("\n")
                        parent.append_text(line)
            else:
                blocks.append(quoted(body))
            i += 1
            continue

        if kind == "list_item_open":
            item_depth += 1
            i += 1
            continue

        if kind == "list_item_close":
            item_depth -= 1
            i += 1
            continue

        if kind == "table_open":
            header: list[Text] = []
            rows: list[list[Text]] = []
            row: list[Text] = []
            in_head = False
            j = i + 1
            while j < len(tokens) and tokens[j].type != "table_close":
                inner = tokens[j].type
                if inner == "thead_open":
                    in_head = True
                elif inner == "thead_close":
                    in_head = False
                elif inner == "inline":
                    cell = _inline(tokens[j].children, palette)
                    if in_head:
                        header.append(cell)
                    else:
                        row.append(cell)
                elif inner == "tr_close" and not in_head:
                    rows.append(row)
                    row = []
                j += 1
            if row:
                rows.append(row)
            blocks.append(_table(header, rows, palette, rich=rich))
            i = j + 1
            continue

        if kind == "inline":
            if item_depth:
                # Inside a list item: capture it as the item's text. The list
                # frame is what emits it, at list close.
                if lists:
                    frame = lists[-1]
                    marker = "• " if not frame["ordered"] else f"{frame['n']}. "
                    if frame["ordered"]:
                        frame["n"] += 1
                    frame["items"].append(
                        (marker, _inline(tok.children, palette)))
                i += 1
                continue
            blocks.append(quoted(_inline(tok.children, palette)))
            i += 1
            continue

        i += 1

    return blocks


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def markdown_text(text: str, palette: Palette) -> Text:
    """Assistant prose as a flat ``Text``, markdown resolved.

    The streaming path: called on every delta, so it stops at the ``Text`` and
    never builds a ``Table`` or a ``Syntax``.
    """
    blocks = _parse(text, palette, rich=False)
    if not blocks:
        return Text()
    if len(blocks) == 1:
        return blocks[0]
    out = Text()
    for n, block in enumerate(blocks):
        if n:
            out.append("\n\n")
        out.append_text(block)
    return out


def markdown_renderable(text: str, palette: Palette) -> RenderableType:
    """Assistant prose for the transcript: tables and code included.

    Returns a ``Text`` for the common case — most answers are one block — and a
    ``Group`` when the answer has more than one, so a ``Table`` or a ``Syntax``
    can sit between the prose rather than being flattened into it.
    """
    blocks = _parse(text, palette, rich=True)
    if not blocks:
        return Text()
    if len(blocks) == 1:
        return blocks[0]
    out: list[RenderableType] = []
    for n, block in enumerate(blocks):
        if n:
            out.append(Text(""))
        out.append(block)
    return Group(*out)


def markdown_line(text: str, palette: Palette) -> Text:
    """Markdown flattened onto *one* line, for a prefixed worker line.

    ``agent_line`` puts a ``[name]`` tag in front of a worker's prose, and a
    tag only means anything if the text it labels starts where the tag ends.
    A multi-line renderable there would put the tag on its own line and the
    body under it. Blank lines collapse and line breaks become separators, so
    a worker's markdown reads as the one-line brief it is.
    """
    t = markdown_text(text, palette)
    out = Text()
    written = 0
    for line in t.split("\n"):
        if not str(line).strip():
            # Blank lines collapse rather than becoming a gap. A worker's
            # markdown is a brief, not a document, and an empty line here
            # would be a break the tag above it does not label.
            continue
        if written:
            out.append("  ", style=palette.dim)
        out.append_text(line)
        written += 1
    return out
