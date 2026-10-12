"""`/ad` regression suite: the gate, the interlock, the probe, the doctrine.

Run:  python tests/test_ad.py   (from an activated venv)

Everything here is offline and model-free. The interlock is the reason that is
possible: ``ad.inspect`` is a pure function of a command string, and
``Throttle``/``AttemptBudget`` are plain objects, so the entire safety surface —
what is refused, what spends budget, when the breaker trips — is asserted by
writing commands down. No campaign, no network, no domain.

Three invariants this file is built around:

* **A read never spends budget.** A null session, an anonymous bind, a DNS
  query and an SMB signature check cannot lock an account. If any of them
  counted, a campaign would trip its own breaker during reconnaissance.
* **A destructive command is refused *before* the budget.** It never runs, so it
  must not consume an attempt the operator paid for with ``--lockout``.
* **The breaker trips at ``lockout - 1``.** Not at ``lockout``. An operator who
  says the domain locks at five is saying four are affordable.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import ad as ad_mod  # noqa: E402
from zimzilla import hunt as hunt_mod  # noqa: E402
from zimzilla import scope as scope_mod  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _probes(*present: str) -> list[ad_mod.ToolProbe]:
    """A synthetic probe result, so the tests do not depend on this box."""
    return [
        ad_mod.ToolProbe(name=n, path=(f"/usr/bin/{n}" if n in present else None),
                         hint=f"install {n}", vector=v)
        for n, _h, v in ad_mod._TOOL_CATALOGUE
    ]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_validate_target() -> None:
    """The domain, normalised — and everything that is not one, refused."""

    # --- accepted, and normalised
    check("gate: a plain domain is accepted",
          ad_mod.validate_target("corp.local") == "corp.local")
    check("gate: case is normalised",
          ad_mod.validate_target("CORP.LOCAL") == "corp.local")
    check("gate: a trailing dot is stripped",
          ad_mod.validate_target("corp.local.") == "corp.local")
    check("gate: surrounding space is stripped",
          ad_mod.validate_target("  corp.local  ") == "corp.local")
    check("gate: a three-label domain is accepted",
          ad_mod.validate_target("ad.corp.internal") == "ad.corp.internal")
    check("gate: an inner hyphen is allowed",
          ad_mod.validate_target("my-corp.local") == "my-corp.local")
    check("gate: an IPv4 DC address is accepted (a lab box)",
          ad_mod.validate_target("10.10.10.5") == "10.10.10.5")

    # --- refused, each with a message rather than a bare False
    for raw, why in [
        ("", "empty"),
        ("corp", "no dot — a bare label is not a domain"),
        ("https://dc01.corp.local/owa", "a URL"),
        ("http://corp.local", "a URL"),
        ("corp.local/owa", "a path"),
        ("dc01.corp.local\\share", "a UNC path"),
        ("corp.local dc01", "two tokens"),
        ("scan the whole domain please", "a sentence"),
        ("-corp.local", "a leading hyphen"),
        ("corp..local", "an empty label"),
        ("corp.local:389", "a port"),
        ("*.corp.local", "a wildcard"),
    ]:
        try:
            got = ad_mod.validate_target(raw)
            check(f"gate: {why} is refused", False, f"accepted as {got!r}")
        except ValueError:
            check(f"gate: {why} is refused", True)

    # The URL case is the one worth a helpful message: an operator pasting a URL
    # has the domain in their hand and should not have to retype it.
    try:
        ad_mod.validate_target("https://dc01.corp.local/owa")
    except ValueError as e:
        check("gate: a URL is told the domain it contains",
              "'dc01.corp.local'" in str(e), str(e))
    try:
        ad_mod.validate_target("")
    except ValueError as e:
        check("gate: an empty target is told what to type",
              "/ad corp.local" in str(e), str(e))

    # --- the ack phrase and the refusal line
    check("gate: the ack phrase is the normalised domain",
          ad_mod.ack_phrase("CORP.Local.") == "corp.local")
    check("gate: the refusal names the exact line to run",
          ad_mod.refusal("corp.local") == "/ad corp.local --ack corp.local")


# ---------------------------------------------------------------------------
# inspect() — the decision surface, as a pure function
# ---------------------------------------------------------------------------

#: ``(command, credential, destructive)``. Every row is a claim about what the
#: interlock does with one shell command, and the whole safety argument rests on
#: these being right — which is why they are written out rather than derived.
_TABLE: tuple[tuple[str, bool, bool], ...] = (
    # --- reads. None of these may spend budget.
    ("smbclient -L //dc01.corp.local -N", False, False),
    ("rpcclient -U '' -N dc01.corp.local", False, False),
    ("rpcclient -U '' -N dc01.corp.local -c enumdomusers", False, False),
    ("nmap -sV -p 88,389,445 dc01.corp.local", False, False),
    ("nmap --script smb2-security-mode -p 445 dc01.corp.local", False, False),
    ("nmap --script smb-security-mode dc01.corp.local", False, False),
    ("dig @10.10.10.5 corp.local AXFR", False, False),
    ("dig @10.10.10.5 _ldap._tcp.dc._msdcs.corp.local SRV", False, False),
    ("nslookup -type=SRV _kerberos._tcp.corp.local", False, False),
    ("openssl s_client -connect dc01.corp.local:636", False, False),
    ("ldapsearch -H ldap://dc01.corp.local -x -b 'dc=corp,dc=local'", False, False),
    ("GetNPUsers.py corp.local/ -usersfile users.txt -no-pass -dc-ip 10.10.10.5",
     False, False),
    ("kerbrute userenum -d corp.local --dc 10.10.10.5 users.txt", False, False),
    ("enum4linux-ng -A dc01.corp.local", False, False),
    ("bloodhound-python -d corp.local -ns 10.10.10.5 -c All", False, False),
    ("ls -la /tmp", False, False),
    ("cat /etc/hosts", False, False),
    ("grep -r corp.local /tmp/notes", False, False),
    ("rm -rf /tmp/scratch", False, False),

    # --- offline cracking. No credential is offered to anything.
    ("hashcat -m 18200 asrep.txt rockyou.txt", False, False),
    ("john --format=krb5asrep asrep.txt", False, False),

    # --- credential attempts. Each spends one.
    ("GetUserSPNs.py corp.local/j.doe:Passw0rd -dc-ip 10.10.10.5 -request",
     True, False),
    ("GetNPUsers.py corp.local/j.doe:Passw0rd -dc-ip 10.10.10.5", True, False),
    ("secretsdump.py corp.local/j.doe:Passw0rd@dc01.corp.local", True, False),
    ("secretsdump.py -hashes :aad3b435b51404ee j.doe@dc01.corp.local",
     True, False),
    ("certipy find -u j.doe@corp.local -p Passw0rd -dc-ip 10.10.10.5",
     True, False),
    ("certipy find -u j.doe@corp.local -password Passw0rd", True, False),
    ("nxc smb dc01.corp.local -u j.doe -p Passw0rd", True, False),
    ("crackmapexec smb dc01.corp.local -u j.doe -p Passw0rd", True, False),
    ("cme smb dc01.corp.local -u j.doe -H :aad3b435b51404ee", True, False),
    # Destructive as well as credentialed: a machine account is a new directory
    # object, so this one writes to the domain even though the write is benign
    # in isolation.
    ("addcomputer.py -computer-name EVIL$ -computer-pass Passw0rd "
     "corp.local/j.doe:Passw0rd", True, True),
    ("smbclient //dc01.corp.local/share -U j.doe%Passw0rd", True, False),
    ("ldapsearch -H ldap://dc01.corp.local -D 'j.doe@corp.local' "
     "-w Passw0rd -b 'dc=corp,dc=local'", True, False),
    ("psexec.py corp.local/j.doe:Passw0rd@dc01.corp.local", True, False),
    ("wmiexec.py -hashes :aad3b435b51404ee corp.local/j.doe@dc01.corp.local",
     True, False),
    ("windapsearch -d corp.local -u j.doe -p Passw0rd --da", True, False),
    ("smbmap -H dc01.corp.local -u j.doe -p Passw0rd", True, False),

    # --- spraying and guessing, whatever it is spelled with
    ("hydra -L users.txt -p 'Summer2026!' smb://dc01.corp.local", True, False),
    ("medusa -h dc01.corp.local -U users.txt -p Passw0rd -M smbnt", True, False),
    ("patator smb_login host=dc01.corp.local user=FILE0 password=Passw0rd",
     True, False),
    ("ncrack -U users.txt -P passwords.txt smb://dc01.corp.local", True, False),
    ("crowbar -b smb -s dc01.corp.local -u j.doe -C passwords.txt", True, False),
    ("sprayhound -U users.txt -p Passw0rd -d corp.local", True, False),
    ("nmap --script smb-brute -p 445 dc01.corp.local", True, False),
    ("kerbrute passwordspray -d corp.local users.txt 'Summer2026!'", True, False),
    ("nxc smb dc01.corp.local -u users.txt -p Passw0rd --continue-on-success",
     True, False),

    # --- destructive. Refused outright, and none of them spends budget.
    ("rm -rf /var/lib/samba", False, True),
    ("rm -rf /etc/sssd", False, True),
    ("rm -rf /var/lib/sss", False, True),
    ("secretsdump.py corp.local/j.doe:Passw0rd@dc01.corp.local --delete",
     True, True),
    ("secretsdump.py corp.local/j.doe:Passw0rd@dc01.corp.local --action delete",
     True, True),
    ("net user j.doe NewPass123 /domain", False, True),
    ("net accounts /lockoutthreshold:0", False, True),
    ("certipy template -u j.doe@corp.local -p Passw0rd -template WebServer "
     "-save-old", True, True),
    ("certipy ca -u j.doe@corp.local -p Passw0rd -ca CORP-CA "
     "-enable-template WebServer", True, True),
    # The certipy line is drawn at the *subcommand*, so these three are the
    # negative cases that keep it there. `find` names a template and a CA and
    # reads; `req` enrols and does not modify directory state, whether or not
    # the CA is named explicitly; `account`/`shadow` set credentials and are
    # writes. A rule keyed on `-ca` would refuse `req` here and let the same
    # request through when the CA is auto-discovered.
    ("certipy find -u j.doe@corp.local -p Passw0rd -template WebServer "
     "-ca CORP-CA", True, False),
    ("certipy req -u j.doe@corp.local -p Passw0rd -ca CORP-CA "
     "-template User", True, False),
    ("certipy account -u j.doe@corp.local -p Passw0rd -user svc -upn svc@corp.local",
     True, True),
    ("ntlmrelayx.py -t ldap://dc01.corp.local --escalate-user j.doe",
     False, True),
    ("dsmod user 'CN=j.doe,CN=Users,DC=corp,DC=local' -pwd NewPass123",
     False, True),

    # --- not AD tooling at all. The interlock has no business with it.
    ("python3 -m http.server 8000", False, False),
    ("pip install certipy-ad", False, False),
    ("echo hello", False, False),
    ("", False, False),
)


def test_inspect() -> None:
    """Every row of the table, against the pure decision function."""
    bad: list[str] = []
    for cmd, cred, dest in _TABLE:
        v = ad_mod.inspect(cmd)
        if v.credential != cred or v.destructive != dest:
            bad.append(
                f"{cmd[:60]!r}: got credential={v.credential} "
                f"destructive={v.destructive} ({v.reason}), "
                f"wanted {cred}/{dest}")
    check(f"inspect: all {len(_TABLE)} commands classified correctly",
          not bad, "\n    " + "\n    ".join(bad) if bad else "")


def test_inspect_details() -> None:
    """The properties the table cannot express."""

    # `-usersfile` names *usernames*, not passwords. Counting it would spend the
    # campaign's whole budget on the one technique that cannot lock anything —
    # the most serious way this table could have been wrong.
    v = ad_mod.inspect("GetNPUsers.py corp.local/ -usersfile users.txt "
                       "-no-pass -dc-ip 10.10.10.5")
    check("inspect: -usersfile is not a secret", not v.credential, v.reason)
    check("inspect: but the same tool with -p IS counted",
          ad_mod.inspect("GetNPUsers.py corp.local/j.doe:Passw0rd").credential)

    # `-p` means *port* to nmap and *password* to netexec. That is exactly why
    # the C2 flags are gated on a C2 alias being named.
    check("inspect: nmap's -p is a port, not a password",
          not ad_mod.inspect("nmap -p 445 dc01.corp.local").credential)
    check("inspect: netexec's -p is a password",
          ad_mod.inspect("nxc smb dc01.corp.local -u j.doe -p Passw0rd").credential)

    # A read is not a reset. The negative lookahead is what separates them.
    check("inspect: `net user j.doe /domain` is a read",
          not ad_mod.inspect("net user j.doe /domain").destructive)
    check("inspect: `net user j.doe NewPass /domain` is a reset",
          ad_mod.inspect("net user j.doe NewPass /domain").destructive)

    # `rm -rf /tmp/scratch` is housekeeping; the same command against a domain
    # controller's state is the end of the client's Monday.
    check("inspect: rm -rf on a scratch dir is left alone",
          not ad_mod.inspect("rm -rf /tmp/scratch").destructive)
    check("inspect: rm -rf on a DC's state is refused",
          ad_mod.inspect("rm -rf /var/lib/samba").destructive)

    # A tool whose *name* contains "brute" is not a spray.
    check("inspect: `kerbrute userenum` is not a spray",
          not ad_mod.inspect("kerbrute userenum -d corp.local users.txt").credential)
    check("inspect: `kerbrute passwordspray` is",
          ad_mod.inspect("kerbrute passwordspray -d corp.local u.txt 'x'").credential)

    # `relevant` is what keeps the interlock out of everything that is not AD
    # work — a false positive here would throttle the agent's ordinary shell.
    for cmd in ("ls -la", "python3 -m http.server", "echo hi",
                "pip install certipy-ad"):
        check(f"inspect: {cmd!r} is not the interlock's business",
              not ad_mod.inspect(cmd).relevant)

    # A source path is not a credential: `domain/user:pass` requires a *dotted*
    # domain, which is what keeps `src/app.py:12` from matching.
    check("inspect: a source path with a colon is not a credential",
          not ad_mod.inspect("grep -n corp.local src/app.py:12").credential)

    # Every refusal carries a reason, because the agent is handed it verbatim.
    for cmd in ("hydra -L u -p P smb://dc01.corp.local",
                "rm -rf /var/lib/samba",
                "nxc smb dc01.corp.local -u j.doe -p Passw0rd"):
        v = ad_mod.inspect(cmd)
        check(f"inspect: {cmd[:30]!r} explains itself", bool(v.reason), v.reason)


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

async def test_throttle() -> None:
    """The token bucket's arithmetic, and that it is shared."""

    # rate <= 0 is genuinely free — the honest meaning of "no throttle".
    free = ad_mod.Throttle(rate=0, burst=1)
    start = time.monotonic()
    for _ in range(50):
        await free.acquire()
    elapsed = time.monotonic() - start
    check("throttle: rate 0 never waits", elapsed < 0.05, f"{elapsed:.4f}s")
    check("throttle: rate 0 reports a full bucket",
          free.tokens == float(free.burst))

    # A burst is free, and the overflow pays.
    t = ad_mod.Throttle(rate=10.0, burst=2)
    start = time.monotonic()
    for _ in range(5):
        await t.acquire()
    elapsed = time.monotonic() - start
    # 2 free, 3 at 10/s = 0.3s
    check("throttle: burst is free and the overflow is paced",
          0.25 < elapsed < 0.45, f"{elapsed:.4f}s")
    check("throttle: the wait is recorded", t.waited == 3, str(t.waited))
    check("throttle: the slept total matches", abs(t.slept - 0.3) < 0.05,
          f"{t.slept:.4f}")

    # One bucket, many agents: the rate is the operator's, not per-agent.
    shared = ad_mod.Throttle(rate=20.0, burst=1)
    start = time.monotonic()
    await asyncio.gather(*(shared.acquire() for _ in range(5)))
    elapsed = time.monotonic() - start
    check("throttle: concurrent callers share one bucket",
          0.15 < elapsed < 0.30, f"{elapsed:.4f}s")
    check("throttle: and queue rather than stampede", shared.waited == 4,
          str(shared.waited))


# ---------------------------------------------------------------------------
# AttemptBudget — the circuit breaker
# ---------------------------------------------------------------------------

def test_budget() -> None:
    """The breaker trips at lockout - 1, and refuses everything after."""

    b = ad_mod.AttemptBudget(lockout=5)
    check("budget: the limit is lockout - 1", b.limit == 4, str(b.limit))
    check("budget: nothing is spent to begin with", b.spent == 0)
    check("budget: and it has not tripped", not b.tripped)
    check("budget: the full allowance is available", b.remaining == 4)

    for i in range(1, 5):
        b.spend(f"attempt {i}")
        check(f"budget: attempt {i} of 4 is allowed", b.spent == i, str(b.spent))
    check("budget: the allowance is now spent", b.remaining == 0)
    check("budget: but it has still not tripped", not b.tripped)

    try:
        b.spend("attempt 5")
        check("budget: attempt 5 is refused", False, "it was allowed")
    except ad_mod.InterlockRefusal as e:
        check("budget: attempt 5 is refused", True)
        check("budget: the refusal says it would be the fifth",
              "attempt 5" in str(e), str(e)[:80])
        check("budget: and names the lockout", "locks accounts at 5" in str(e),
              str(e)[:120])
    check("budget: the refusal tripped the breaker", b.tripped)
    # Refused *before* incrementing, so `spent` is always the number of
    # attempts that actually ran — never an overcount.
    check("budget: a refused attempt does not increment the count",
          b.spent == 4, str(b.spent))

    # Once tripped, everything after is refused too — a wave that walked into
    # the threshold has no business trying a sixth account.
    for i in range(6, 9):
        try:
            b.spend(f"attempt {i}")
            check(f"budget: attempt {i} refused after the trip", False)
        except ad_mod.InterlockRefusal:
            check(f"budget: attempt {i} refused after the trip", True)

    # The history is bounded, so a long campaign cannot grow it without limit.
    h = ad_mod.AttemptBudget(lockout=1000)
    for i in range(200):
        h.spend(f"cmd {i}")
    check("budget: the history is bounded", len(h.history) <= 50,
          str(len(h.history)))

    # A lockout of 1 is reported, not enforced as a zero budget: refusing to run
    # at all is the operator's call, not this class's.
    one = ad_mod.AttemptBudget(lockout=1)
    check("budget: lockout 1 still permits one attempt", one.limit == 1)
    check("budget: a nonsense lockout is floored at 1",
          ad_mod.AttemptBudget(lockout=0).limit == 1)


# ---------------------------------------------------------------------------
# make_interlock — the ToolHook
# ---------------------------------------------------------------------------

async def _call(hook, cmd: str) -> str:
    try:
        async with hook("bash", {"command": cmd}):
            return "ran"
    except ad_mod.InterlockRefusal as e:
        return f"refused: {e}"


async def test_interlock() -> None:
    """The hook, over the real async-CM path the agent loop uses."""

    budget = ad_mod.AttemptBudget(lockout=3)
    refusals: list[tuple] = []
    hook = ad_mod.make_interlock(
        ad_mod.Throttle(rate=0), budget,
        on_refuse=lambda v, c: refusals.append((v.reason, c)))

    # Reads run and spend nothing.
    for cmd in ("nmap -sV dc01.corp.local",
                "smbclient -L //dc01.corp.local -N",
                "GetNPUsers.py corp.local/ -usersfile u.txt -no-pass"):
        check(f"interlock: {cmd[:32]!r} runs",
              await _call(hook, cmd) == "ran")
    check("interlock: reads spent no budget", budget.spent == 0,
          str(budget.spent))
    check("interlock: and reported no refusal", refusals == [])

    # Credentials run until the limit, then stop.
    spray = "hydra -L users.txt -p 'Summer2026!' smb://dc01.corp.local"
    check("interlock: attempt 1 runs", await _call(hook, spray) == "ran")
    check("interlock: attempt 2 runs", await _call(hook, spray) == "ran")
    third = await _call(hook, spray)
    check("interlock: attempt 3 is refused", third.startswith("refused"),
          third[:60])
    check("interlock: the budget is spent", budget.spent == 2, str(budget.spent))
    check("interlock: the breaker tripped", budget.tripped)
    check("interlock: the refusal was reported", len(refusals) == 1,
          str(len(refusals)))
    check("interlock: the report carries the command",
          bool(refusals) and "hydra" in refusals[0][1])

    # A read still runs after the trip — the campaign degrades rather than dies.
    check("interlock: reads still run after the trip",
          await _call(hook, "nmap -sV dc01.corp.local") == "ran")
    check("interlock: and a credential command still does not",
          (await _call(hook, "nxc smb dc01.corp.local -u a -p b"))
          .startswith("refused"))

    # Destructive, refused before the budget — and it must not consume one.
    b2 = ad_mod.AttemptBudget(lockout=3)
    seen: list[tuple] = []
    hook2 = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b2,
                                  on_refuse=lambda v, c: seen.append((v, c)))
    out = await _call(hook2, "rm -rf /var/lib/samba")
    check("interlock: a destructive command is refused",
          out.startswith("refused"), out[:70])
    check("interlock: the refusal says why",
          "domain controller" in out, out[:120])
    check("interlock: and it spent no budget", b2.spent == 0, str(b2.spent))
    check("interlock: and did not trip the breaker", not b2.tripped)
    check("interlock: the destructive refusal was reported",
          len(seen) == 1 and seen[0][0].destructive)

    # --arm permits the write but not the spray.
    b3 = ad_mod.AttemptBudget(lockout=5)
    armed = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b3, armed=True)
    check("interlock: --arm permits a destructive command",
          await _call(armed, "rm -rf /var/lib/samba") == "ran")
    check("interlock: --arm still does not permit a spray to overspend",
          await _call(armed, "hydra -L u -p P smb://dc01.corp.local") == "ran")

    # Non-bash tools are never the interlock's business.
    b4 = ad_mod.AttemptBudget(lockout=1)
    hook4 = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b4)
    for tool in ("read_file", "write_file", "list_dir", "report_finding"):
        async with hook4(tool, {"path": "x"}):
            pass
    check("interlock: only bash is inspected", b4.spent == 0)

    # A reporting seam that raises must not take the campaign down with it.
    b5 = ad_mod.AttemptBudget(lockout=1)

    def boom(v, c):
        raise RuntimeError("the sink is broken")

    hook5 = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b5, on_refuse=boom)
    try:
        await _call(hook5, "hydra -L u -p P smb://dc01.corp.local")
        await _call(hook5, "hydra -L u -p P smb://dc01.corp.local")
        check("interlock: a broken reporting seam does not break the refusal",
              True)
    except RuntimeError:
        check("interlock: a broken reporting seam does not break the refusal",
              False, "the sink's exception escaped")

    # The args key: `cmd` as well as `command`, because the tool schema has
    # changed spelling before and a hook that missed it would be a silent hole.
    b6 = ad_mod.AttemptBudget(lockout=1)
    hook6 = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b6)
    async with hook6("bash", {"cmd": "hydra -L u -p P smb://dc01.corp.local"}):
        pass
    check("interlock: the `cmd` spelling is inspected too", b6.spent == 1,
          str(b6.spent))

    # An empty or malformed args dict must not crash the hook.
    b7 = ad_mod.AttemptBudget(lockout=1)
    hook7 = ad_mod.make_interlock(ad_mod.Throttle(rate=0), b7)
    for args in ({}, None, {"command": ""}, {"command": None}):
        async with hook7("bash", args):
            pass
    check("interlock: an empty command is a no-op", b7.spent == 0)


# ---------------------------------------------------------------------------
# The tooling probe
# ---------------------------------------------------------------------------

def test_probe() -> None:
    """The probe, and the "detect and degrade" decision built on it."""

    probes = ad_mod.probe_tools()
    check("probe: every catalogue entry is probed",
          len(probes) == len(ad_mod._TOOL_CATALOGUE), str(len(probes)))
    check("probe: each probe names a vector", all(p.vector for p in probes))
    check("probe: each probe carries an install hint", all(p.hint for p in probes))
    check("probe: presence is a fact about the path",
          all(p.present == (p.path is not None) for p in probes))

    # The synthetic probes are what the tests reason with, so that they do not
    # depend on what happens to be installed on the machine running them.
    p = _probes("nmap", "rpcclient")
    # Compared as a set: `_probes` preserves the catalogue's order, not the
    # order the caller named them in, and that ordering is an implementation
    # detail the test has no business pinning.
    check("probe: a synthetic probe is present only when named",
          {q.name for q in p if q.present} == {"nmap", "rpcclient"})

    text = ad_mod.render_tools(p)
    check("probe: the render lists what is present",
          "nmap" in text and "PRESENT" in text, text[:120])
    check("probe: the render lists what is absent",
          "certipy" in text and "ABSENT" in text)
    check("probe: the render says an absent tool's vector is out of scope",
          "OUT OF SCOPE" in text)
    check("probe: the render carries the install hint",
          "install certipy" in text, text[-200:])
    check("probe: the render is empty-safe",
          "none of them" in ad_mod.render_tools(_probes()).split("ABSENT")[0])

    # `unavailable_vectors` is "every tool missing", not "any": most vectors
    # have a fallback, and degrading to it is the point of the probe.
    none = ad_mod.unavailable_vectors(_probes())
    check("probe: with nothing installed, every vector is unavailable",
          set(none) == set(ad_mod.VECTOR_TOOLS), str(sorted(none)))
    every = ad_mod.unavailable_vectors(_probes(*ad_mod.AD_TOOL_NAMES))
    check("probe: with everything installed, none is", every == [], str(every))

    # A vector with one tool present is runnable with a worse method.
    # `kerberoast` is the one with a fallback chain — SPN enumeration and the
    # cracking can each come from a different tool — so hashcat alone keeps it
    # alive. `asrep-roast` needs `GetNPUsers.py` specifically and is still
    # skipped; asserting it here would be asserting the absence of a fallback.
    partial = ad_mod.unavailable_vectors(_probes("hashcat"))
    check("probe: a vector with one tool present is not skipped",
          "kerberoast" not in partial, str(sorted(partial)))
    check("probe: a vector with nothing present is",
          "coercion" in partial, str(sorted(partial)))

    # The catalogue itself: names unique, and every vector in VECTOR_TOOLS.
    names = [n for n, _h, _v in ad_mod._TOOL_CATALOGUE]
    check("probe: no tool is listed twice", len(names) == len(set(names)))
    check("probe: every catalogue vector is a known vector",
          all(v in ad_mod.VECTOR_TOOLS for _n, _h, v in ad_mod._TOOL_CATALOGUE),
          str([v for _n, _h, v in ad_mod._TOOL_CATALOGUE
               if v not in ad_mod.VECTOR_TOOLS]))
    check("probe: the derived tables agree with the catalogue",
          all(t in set(names) | ad_mod._SPRAY_TOOLS | set(ad_mod._C2_ALIASES)
              for t in ad_mod.AD_TOOL_NAMES))


# ---------------------------------------------------------------------------
# The doctrine
# ---------------------------------------------------------------------------

def test_doctrine() -> None:
    """`ad_kind` as a `CampaignKind`, and why it is a function."""

    kind = ad_mod.ad_kind(probes=_probes("nmap", "rpcclient"))
    check("doctrine: the name is `ad`", kind.name == "ad")
    check("doctrine: the command is `/ad`", kind.command == "ad")
    check("doctrine: the directory prefix is `ad-`", kind.dir_prefix == "ad-")
    check("doctrine: it carries AD's own vectors",
          kind.vectors == ad_mod.AD_VECTORS and bool(kind.vectors))
    check("doctrine: it carries AD's own contract",
          kind.contract == ad_mod.FINDING_CONTRACT)
    check("doctrine: and AD's own prompts",
          kind.recon_prompt != hunt_mod.RECON_PROMPT
          and kind.planner_prompt != hunt_mod.WAVE_PLANNER_PROMPT
          and kind.summary_prompt != hunt_mod.SUMMARY_PROMPT)
    check("doctrine: it installs an interlock", kind.interlock is not None)
    check("doctrine: the default kind installs none",
          hunt_mod.HUNT_KIND.interlock is None)

    # The default `CampaignKind` must be byte-identical to what shipped, or
    # every `/bug-hunt` changes behaviour by being routed through the seam.
    check("doctrine: HUNT_KIND's prompts are the module constants",
          hunt_mod.HUNT_KIND.recon_prompt is hunt_mod.RECON_PROMPT
          and hunt_mod.HUNT_KIND.planner_prompt is hunt_mod.WAVE_PLANNER_PROMPT
          and hunt_mod.HUNT_KIND.contract is hunt_mod.FINDING_CONTRACT
          and hunt_mod.HUNT_KIND.summary_prompt is hunt_mod.SUMMARY_PROMPT)
    check("doctrine: HUNT_KIND is named and prefixed as it shipped",
          hunt_mod.HUNT_KIND.name == "hunt"
          and hunt_mod.HUNT_KIND.command == "bug-hunt"
          and hunt_mod.HUNT_KIND.dir_prefix == ""
          and hunt_mod.HUNT_KIND.vectors == ())

    # A fresh instance per campaign, because the interlock holds state.
    a = ad_mod.ad_kind(probes=[])
    b = ad_mod.ad_kind(probes=[])
    check("doctrine: each call builds its own interlock",
          a.interlock is not b.interlock)
    check("doctrine: and its own throttle, so one run cannot spend another's",
          a.recon_prompt == b.recon_prompt)

    # The probe result reaches the recon prompt — that is "detect and degrade".
    with_tools = ad_mod.build_recon_prompt(_probes("nmap"))
    check("doctrine: the recon prompt carries the probe result",
          "PRESENT on this host" in with_tools and "nmap" in with_tools)
    check("doctrine: and names an absent tool's vector as out of scope",
          "OUT OF SCOPE" in with_tools and "certipy" in with_tools)
    check("doctrine: no unsubstituted placeholder is left in it",
          "{tools}" not in with_tools and "{target}" in with_tools
          and "{contract}" in with_tools)

    # Every prompt must survive the engine's exact `.format()` kwargs, or the
    # campaign dies on the first wave with a KeyError.
    for label, text, kwargs in [
        ("recon", kind.recon_prompt,
         {"target": "corp.local", "contract": kind.contract}),
        ("planner", kind.planner_prompt,
         {"target": "corp.local", "wave_size": 10, "recon": "r",
          "recon_note": "n", "findings": "f", "waves": "w",
          "contract": kind.contract}),
        ("summary", kind.summary_prompt,
         {"target": "corp.local", "waves": "w", "recon": "r", "findings": "f"}),
    ]:
        try:
            text.format(**kwargs)
            check(f"doctrine: the {label} prompt formats cleanly", True)
        except (KeyError, IndexError) as e:
            check(f"doctrine: the {label} prompt formats cleanly", False, str(e))

    # The vector tables.
    check("doctrine: AD_VECTORS are 3-tuples the roster can use",
          all(len(v) == 3 for v in ad_mod.AD_VECTORS))
    check("doctrine: every vector names who owns it",
          all(v[0] and v[1] for v in ad_mod.AD_VECTORS))
    check("doctrine: the later-wave vectors are named but not rostered",
          all(len(v) == 2 for v in ad_mod.AD_LATER_VECTORS)
          and not (set(n for n, _ in ad_mod.AD_LATER_VECTORS)
                   & set(v[0] for v in ad_mod.AD_VECTORS)))
    check("doctrine: the fallback roster is a real matrix",
          len(ad_mod.AD_VECTORS) >= 8, str(len(ad_mod.AD_VECTORS)))

    # The prompts carry the safety rules, because the model reads them.
    check("doctrine: the planner mentions the lockout threshold",
          "lockout" in kind.planner_prompt.lower())
    check("doctrine: the planner names the lockout signal as a stop",
          "1909" in kind.planner_prompt
          or "LOCKED_OUT" in kind.planner_prompt.upper())
    # The arithmetic, not just the word. A planner told "be careful" plans
    # nothing differently; a planner told it has four attempts plans around it.
    check("doctrine: the planner states the operator's lockout in numbers",
          "locks an account after 5" in kind.planner_prompt)
    check("doctrine: and the budget that follows from it",
          "permitted 4 credential attempts" in kind.planner_prompt)
    check("doctrine: a custom lockout reaches the planner's arithmetic",
          "locks an account after 3" in ad_mod.build_planner_prompt(3)
          and "permitted 2 credential attempts" in ad_mod.build_planner_prompt(3))
    check("doctrine: a lockout of one still permits one attempt",
          "permitted 1 credential attempts" in ad_mod.build_planner_prompt(1))
    check("doctrine: no sentinel survives into the built prompt",
          "__LOCKOUT__" not in kind.planner_prompt
          and "__BUDGET__" not in kind.planner_prompt)
    # The bare constant is a template, so it must NOT be what ships.
    check("doctrine: the shipped prompt is the built one, not the template",
          kind.planner_prompt != ad_mod.PLANNER_PROMPT)
    check("doctrine: the contract asks for proven vs indicated",
          "proven" in kind.contract.lower()
          and "indicat" in kind.contract.lower())
    check("doctrine: the summary requires the skipped work to be named",
          "not done" in kind.summary_prompt.lower())

    # The default instance exists for reading prompts and the prefix.
    check("doctrine: AD_KIND is the default instance",
          ad_mod.AD_KIND.name == "ad" and ad_mod.AD_KIND.dir_prefix == "ad-")


def test_ad_case_dir(tmp_path: Path) -> None:
    """An AD campaign's directory cannot collide with a web hunt's."""

    cfg = _cfg(tmp_path, state_dir=tmp_path / "state")
    ad_dir = hunt_mod.case_dir(cfg, "corp.local", ad_mod.AD_KIND)
    web_dir = hunt_mod.case_dir(cfg, "corp.local", hunt_mod.HUNT_KIND)
    check("dir: the AD campaign is prefixed", ad_dir.name == "ad-corp.local",
          ad_dir.name)
    check("dir: the web hunt is not", web_dir.name == "corp.local", web_dir.name)
    check("dir: they do not collide", ad_dir != web_dir)
    check("dir: both live under the one hunts root",
          ad_dir.parent == web_dir.parent and ad_dir.parent.name == "hunts",
          str(ad_dir.parent))
    check("dir: both are created", ad_dir.is_dir() and web_dir.is_dir())


# ---------------------------------------------------------------------------
# The scope guard — this is what made `/ad` safe to ship at all
# ---------------------------------------------------------------------------

#: Commands whose target is an AD domain or a domain controller. Every one of
#: these was ALLOWED before `scope.py` learned the AD toolchain, and `/ad`
#: cannot be safe while the guard is blind to the syntax its own toolchain uses.
_SCOPE_TABLE: tuple[tuple[str, str], ...] = (
    ("smbclient -L //dc01.corp.local -N", "dc01.corp.local"),
    ("GetUserSPNs.py corp.local/j.doe:Pass -dc-ip 10.10.10.5 -request",
     "corp.local"),
    ("secretsdump.py corp.local/j.doe:Pass@dc01.corp.local", "dc01.corp.local"),
    ("certipy find -u j.doe@corp.local -dc-ip 10.10.10.5", "corp.local"),
    ("rpcclient -U '' -N dc01.corp.local", "dc01.corp.local"),
    ("bloodhound-python -d corp.local -ns 10.10.10.5", "corp.local"),
    ("ldapsearch -H ldap://dc01.corp.local -x -b 'dc=corp,dc=local'",
     "dc01.corp.local"),
    ("nxc smb dc01.corp.local -u j.doe -p Pass", "dc01.corp.local"),
)


def _scope_with_denied(denied: list[str]) -> scope_mod.Scope:
    """A `Scope` whose deny-list holds *denied*, built in memory.

    Constructed rather than loaded from a file: the guard's behaviour is what
    is under test, not its YAML reader, and a tempfile per case would only add
    a way for the test to fail for a reason that is not the one being asserted.

    The key is `domains`, not `deny`. `HostSet.from_mapping` reads only
    `domain`/`domains`/`in_scope` for hostnames, `ip`/`ips`/`hosts` for
    addresses and `cidr`/`cidrs`/`networks` for ranges — it is polarity-neutral,
    because the same reader fills both the allow-list and the deny-list and only
    the caller knows which is which. An unrecognised key is silently dropped, so
    `{"deny": [...]}` builds an *empty* set that permits everything and every
    assertion against it passes for the wrong reason.
    """
    scope = scope_mod.Scope()
    scope.deny = scope_mod.HostSet.from_mapping({"domains": denied})
    scope.deny_path = Path("/dev/null")  # `check_command` gates on this
    assert not scope.deny.empty, "the test's deny-list did not load"
    return scope


def test_scope_guard() -> None:
    """Every AD-shaped target is *seen* by the guard, so the deny-list bites.

    This is the defect that had to be fixed before `/ad` could ship: the guard
    could not see `smbclient -L //dc01.corp.local`, impacket's
    `domain/user:pass@host`, or a UNC path, so a deny-listed domain was
    reachable from a plain `/bug-hunt` — never mind `/ad`.
    """
    scope = _scope_with_denied(["corp.local"])
    for cmd, host in _SCOPE_TABLE:
        targets = scope.extract_targets(cmd)
        check(f"scope: {cmd[:40]!r} yields {host}", host in targets,
              str(sorted(targets)))
        violations = scope.check_command(cmd)
        check(f"scope: {cmd[:40]!r} is BLOCKED", bool(violations),
              "allowed" if not violations else violations[0].reason)


def test_scope_still_ignores_filenames() -> None:
    """The filename heuristic still runs — it just stops eating AD forms."""
    scope = _scope_with_denied(["corp.local"])
    for token in ("./report.json", "src/app.py", "notes.txt"):
        targets = scope.extract_targets(f"cat {token}")
        check(f"scope: {token} is not a host", token not in targets,
              str(sorted(targets)))
        check(f"scope: `cat {token}` is not blocked",
              not scope.check_command(f"cat {token}"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    test_validate_target()
    test_inspect()
    test_inspect_details()
    await test_throttle()
    test_budget()
    await test_interlock()
    test_probe()
    test_doctrine()
    test_scope_guard()
    test_scope_still_ignores_filenames()
    with tempfile.TemporaryDirectory() as d:
        test_ad_case_dir(Path(d))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
