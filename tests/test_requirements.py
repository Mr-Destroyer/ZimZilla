"""Guards the dependency split between the harness and the LiteLLM proxy.

Run:  python tests/test_requirements.py

requirements.txt describes the HARNESS environment. The proxy is installed
separately, into its own venv (see setup.sh), because the two cannot share one:
litellm[proxy] requires rich<14.0, while textual 8.x requires rich>=14.2. While
litellm[proxy] was listed in requirements.txt, `pip install -r requirements.txt`
did not merely pick a poor version — it failed to resolve at all
(ResolutionImpossible) and installed *nothing*. That is indistinguishable from
a broken installer, and it is the failure this file exists to prevent.

The assertions are deliberately narrow. If litellm ever relaxes its rich pin,
this test and the proxy venv in setup.sh are the two things to revisit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))


def requirements() -> list[str]:
    """The requirement lines, comments and blanks stripped."""
    text = (ROOT / "requirements.txt").read_text()
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def dist_name(line: str) -> str:
    """`litellm[proxy]>=1.100.1` -> `litellm[proxy]`."""
    return re.split(r"[<>=!;\s]", line, maxsplit=1)[0].lower()


def main() -> int:
    reqs = requirements()
    names = [dist_name(r) for r in reqs]

    check("requirements: parses to a non-empty list", bool(reqs), str(reqs))

    # The editable install is what puts the `zimzilla` console script in the
    # venv's bin; dropping it silently leaves a venv that cannot run the app.
    check("requirements: installs zimzilla itself (-e .)", "-e ." in reqs, str(reqs))

    # The unsatisfiable pair. Neither of these belongs to the harness.
    check("requirements: no litellm (unsatisfiable with textual 8.x's rich>=14.2)",
          not any(n.startswith("litellm") for n in names), str(names))
    check("requirements: no uvloop (proxy-only; it arrives with litellm)",
          "uvloop" not in names, str(names))

    # The floor that litellm[proxy] would otherwise drag down to 13.x.
    rich = [r for r in reqs if dist_name(r) == "rich"]
    check("requirements: keeps textual's rich>=14.2 floor",
          bool(rich) and ">=14.2" in rich[0], str(rich))

    # Cross-check: the interpreter check setup.sh runs before it wires up a
    # proxy must test the HARNESS's deps, not the proxy's — `import litellm`
    # there would fail on every correctly-installed machine.
    setup = (ROOT / "setup.sh").read_text()
    dep_check = [ln for ln in setup.splitlines() if "import anthropic" in ln]
    check("setup.sh: the harness dependency check exists",
          bool(dep_check), "no 'import anthropic' line found")
    check("setup.sh: that check does not import litellm",
          bool(dep_check) and all("litellm" not in ln for ln in dep_check),
          str(dep_check))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, ok, extra in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if not ok and extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
