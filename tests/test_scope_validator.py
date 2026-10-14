"""/scope-validator: parsing, scope-file writing, the recon deny guard, ranking, tree.

Runs without a model or the network: the model reply is a fixture, and recon is
checked through the deny-list gate before any socket would open.

Run:  python tests/test_scope_validator.py   (from an activated venv)
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import scope_validator as sv  # noqa: E402
from zimzilla.scope import HostSet, Scope  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


OVERVIEW_REPLY = """Here is the scope:
```json
{"programme": "Acme Bounty",
 "in_scope": [
   {"asset": "*.acme.com", "type": "wildcard", "note": "web"},
   {"asset": "api.acme.io", "type": "domain", "note": "api"},
   {"asset": "203.0.113.10", "type": "ip", "note": "vpn"},
   {"asset": "198.51.100.0/24", "type": "cidr", "note": "lab"},
   {"asset": "Acme iOS app", "type": "other", "note": "mobile"}
 ],
 "out_of_scope": [
   {"asset": "status.acme.com", "type": "domain", "note": "third party"},
   {"asset": "*.staging.acme.com", "type": "wildcard", "note": "excluded"}
 ],
 "rules": ["no DoS", "no automated scanning above 10 rps"]}
```"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zz-scopeval-"))

    # --- extraction -------------------------------------------------------
    ex = sv.parse_extraction(OVERVIEW_REPLY)
    check("extraction reads fenced JSON", ex is not None)
    check("programme name read", ex and ex.programme == "Acme Bounty")
    check("in-scope count", ex and len(ex.in_scope) == 5, str(ex and len(ex.in_scope)))
    check("out-of-scope count", ex and len(ex.out_of_scope) == 2)
    check("rules read", ex and len(ex.rules) == 2)
    check("unreadable reply -> None", sv.parse_extraction("no json here") is None)
    dup = sv.parse_extraction('{"in_scope":[{"asset":"a.com"},{"asset":"A.com"}]}')
    check("duplicate asset (case-insensitive) collapsed", dup and len(dup.in_scope) == 1)

    # --- classification ---------------------------------------------------
    cases = {
        "*.acme.com": "wildcard", "api.acme.io": "domain", "203.0.113.10": "ip",
        "198.51.100.0/24": "cidr", "Acme iOS app": "other", "999.1.1.1": "other",
        "https://x.io/path": "domain", "2001:db8::1": "ip", "fe80::/10": "cidr",
    }
    for asset, want in cases.items():
        got = sv._classify(asset)
        check(f"classify {asset!r} -> {want}", got == want, got)

    # --- scope files ------------------------------------------------------
    allow, deny, skipped = sv.build_scope(ex.in_scope, ex.out_of_scope)
    check("wildcard becomes its base domain", "acme.com" in allow.get("domains", []))
    check("ip in ips", allow.get("ips") == ["203.0.113.10"], str(allow))
    check("cidr in cidrs", allow.get("cidrs") == ["198.51.100.0/24"], str(allow))
    check("unplaceable asset reported, not dropped", "Acme iOS app" in skipped, str(skipped))
    check("out-of-scope domains kept", "status.acme.com" in deny.get("domains", []), str(deny))

    a_path, d_path = sv.write_scope_files(tmp, allow, deny, programme="Acme Bounty")
    check("allow.yaml written", a_path.exists() and a_path.name == "allow.yaml")
    check("out-of-scope.yaml written", d_path.exists() and d_path.name == "out-of-scope.yaml")

    problems = sv.verify_scope_files(a_path, d_path)
    check("written files load back through Scope.load", problems == [], str(problems))

    s = Scope.load(a_path, d_path)
    check("guard: allowed host is in allow list", s.allow.contains("api.acme.io")[0])
    check("guard: deny beats allow for out-of-scope host",
          s.deny.contains("status.acme.com")[0])
    check("guard: wildcard subdomain matched", s.allow.contains("www.acme.com")[0])

    # overwrite keeps the old file aside rather than destroying it
    a_path2, _ = sv.write_scope_files(tmp, {"domains": ["other.example"]}, {})
    baks = list(tmp.glob("allow.yaml.bak-*"))
    check("existing allow.yaml moved aside, not destroyed", len(baks) == 1, str(baks))
    check("new allow.yaml has new content",
          "other.example" in a_path2.read_text())

    # --- recon deny guard -------------------------------------------------
    deny_scope = Scope(deny_path=d_path, deny=HostSet.from_mapping({"domains": ["status.acme.com"]}))
    called: list[str] = []
    real = sv.recon_host
    sv.recon_host = lambda h, timeout=0: (called.append(h), sv.HostRecon(host=h))[1]
    try:
        sv.recon_all(["api.acme.io", "status.acme.com", "www.status.acme.com"], deny_scope)
    finally:
        sv.recon_host = real
    check("recon never touches an out-of-scope host", "status.acme.com" not in called, str(called))
    check("recon still runs the in-scope host", "api.acme.io" in called, str(called))
    check("recon skips subdomains of an out-of-scope host",
          "www.status.acme.com" not in called, str(called))

    # --- ranking ----------------------------------------------------------
    assets = ["*.acme.com", "api.acme.io", "203.0.113.10"]
    reply = ('{"ranking": [{"asset": "api.acme.io", "rank": 1, "value": "high", "why": "api"},'
             ' {"asset": "*.acme.com", "rank": 2, "value": "medium", "why": "web"}],'
             ' "summary": "start with the api"}')
    ranked, summary = sv.parse_ranking(reply, assets)
    check("ranking order kept", [r.asset for r in ranked][:2] == ["api.acme.io", "*.acme.com"])
    check("unranked asset appended, not dropped",
          any(r.asset == "203.0.113.10" and r.value == "unranked" for r in ranked))
    check("ranks renumbered densely", [r.rank for r in ranked] == [1, 2, 3])
    check("summary read", summary == "start with the api")
    hallucinated = sv.parse_ranking('{"ranking":[{"asset":"evil.com","rank":1,"value":"high"}]}', assets)
    check("model cannot add an asset that was not in scope",
          hallucinated and all(r.asset in assets for r in hallucinated[0]))

    # --- tree -------------------------------------------------------------
    recon = {"api.acme.io": sv.HostRecon(host="api.acme.io", resolves=True,
                                         ip="10.1.1.1", status=200, server="nginx",
                                         title="Acme API")}
    tree = sv.render_plain_tree("Acme Bounty", ranked, recon, ex.out_of_scope)
    check("tree names the programme", tree.startswith("Acme Bounty"))
    check("tree lists in-scope first", tree.index("IN SCOPE") < tree.index("OUT OF SCOPE"))
    check("tree shows rank and value", "#1 [high] api.acme.io" in tree, tree)
    check("tree shows recon evidence", "HTTP 200" in tree and "Acme API" in tree)
    check("tree lists out-of-scope", "status.acme.com" in tree)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all scope-validator checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
