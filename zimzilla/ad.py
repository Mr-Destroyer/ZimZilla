"""`/ad` — an Active Directory campaign, and the interlock that keeps it safe.

The command is a **doctrine**, not an engine. `/bug-hunt` already is the
campaign loop — recon, a read-only planner, one wave of concurrent agents,
harvest, stop — and that loop is not web-specific; only the prompts, the
fallback vector matrix and the campaign directory were. `hunt.CampaignKind`
lifted those into data, so this module is what `/ad` actually adds:

  * the four prompt templates, rewritten for a domain instead of a website;
  * `AD_VECTORS`, the fixed fallback matrix — the *unauthenticated* surface,
    because that is the only thing a wave can run blind;
  * `probe_tools()`, which finds out what is installed before a brief is
    written that needs it;
  * and `make_interlock()`, the part that matters.

Why the interlock exists
------------------------
A bug in `/bug-hunt` fails to find a bug. A bug here locks out a client's
workforce at 09:00 on a Monday. So the ceiling on this command is arithmetic
rather than a promise from the model: one shared token bucket for the whole
wave, one hard attempt counter, and a circuit breaker that refuses every
credential-bearing command once the count reaches the declared lockout
threshold minus one. Ten parallel agents share one budget, because ten agents
with ten budgets spray ten times what the operator authorised.

The counter is deliberately **conservative**: a command it cannot parse as
*definitely* non-credential is counted. It cannot see inside a shell script, so
the prompts forbid writing a spray loop to disk as well — the interlock is the
floor, not the whole control.

What the operator must supply and this module cannot check: authorisation. The
gate is typing the domain back (`ack_phrase`), and the notice is on every run.

No Textual, no colours — the same split as `zimzilla/osint.py` and
`zimzilla/phish.py`, so the whole interlock is testable without a campaign, a
model, or a network.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

from . import hunt

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

# One DNS label: letters, digits, inner hyphens. Not a full RFC 1035
# implementation — the job is to catch an empty argument, a sentence typed into
# the wrong field, or a URL pasted where a domain belongs.
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def validate_target(raw: str) -> str:
    """Normalise and check an AD target, or raise ``ValueError``.

    A domain, not a URL and not a sentence. The distinction matters beyond
    tidiness: the scope deny-list is written in domains, the campaign directory
    is named after this string, and an operator who passes
    ``https://dc01.corp.local/owa`` would get a campaign for a name no rule in
    `allow.yaml` can match.
    """
    t = (raw or "").strip().rstrip("/")
    if not t:
        raise ValueError("no target — give the domain, e.g. /ad corp.local")
    if "://" in t:
        raise ValueError(
            f"{raw!r} is a URL. /ad takes the DOMAIN, not a host or a URL — "
            f"try {t.split('://', 1)[1].split('/', 1)[0].split(':', 1)[0]!r}")
    if "/" in t or "\\" in t or any(c.isspace() for c in t):
        raise ValueError(
            f"{raw!r} is not a domain name. Give the AD domain on its own, "
            "e.g. corp.local")
    t = t.lower().rstrip(".")
    if _IPV4_RE.match(t):
        # An address is a domain controller, not a domain. Allowed — an operator
        # pointing at a lab box knows what they mean — but the deny-list is
        # written in domains, so the scope guard will not be able to see it.
        return t
    if not _DOMAIN_RE.match(t) or len(t) > 253:
        raise ValueError(
            f"{raw!r} does not look like an AD domain. Expected a dotted name "
            "such as corp.local or ad.internal")
    return t


def ack_phrase(domain: str) -> str:
    """The exact string the operator must type back to arm the command.

    Typing the domain is the gate. It is not a security control and is not
    pretending to be one — it is a speed bump that makes "I am about to run
    credential attacks against this specific domain" a thing the operator had
    to *say*, rather than a thing that happened because they typed `/ad` and hit
    enter. A wrong ack is refused, so the domain cannot be reached by a typo in
    the target field either.
    """
    return validate_target(domain)


def refusal(domain: str) -> str:
    """The line to run, verbatim, when the gate was not passed."""
    return f"/ad {domain} --ack {domain}"


# ---------------------------------------------------------------------------
# Tooling probe
# ---------------------------------------------------------------------------

#: The toolchain this doctrine knows how to use, and what each one is for.
#: ``(name, install hint, vector)`` — one place, so ``probe_tools`` and
#: ``inspect`` can never disagree about what counts as an AD tool.
#:
#: The hints name Debian/Kali packages because that is what this box is; on
#: another distribution the hint is still the right search term.
_TOOL_CATALOGUE: tuple[tuple[str, str, str], ...] = (
    # --- impacket, the workhorse set. Present on a stock Kali.
    ("GetNPUsers.py", "impacket-scripts", "asrep-roast"),
    ("GetUserSPNs.py", "impacket-scripts", "kerberoast"),
    ("GetADUsers.py", "impacket-scripts", "user-enum"),
    ("secretsdump.py", "impacket-scripts", "dcsync-rights"),
    ("lookupsid.py", "impacket-scripts", "user-enum"),
    ("samrdump.py", "impacket-scripts", "user-enum"),
    ("rpcdump.py", "impacket-scripts", "domain-enum"),
    ("reg.py", "impacket-scripts", "laps-read"),
    ("psexec.py", "impacket-scripts", "host-sprawl"),
    ("wmiexec.py", "impacket-scripts", "host-sprawl"),
    ("smbexec.py", "impacket-scripts", "host-sprawl"),
    ("atexec.py", "impacket-scripts", "scheduled-tasks"),
    ("dcomexec.py", "impacket-scripts", "host-sprawl"),
    ("mssqlclient.py", "impacket-scripts", "host-sprawl"),
    ("ntlmrelayx.py", "impacket-scripts", "relay"),
    ("addcomputer.py", "impacket-scripts", "acl-paths"),
    # --- SMB and RPC, from samba-common-bin / smbclient.
    ("smbclient", "smbclient", "gpp-cpassword"),
    ("rpcclient", "smbclient", "null-session"),
    ("smbmap", "smbmap", "gpp-cpassword"),
    ("enum4linux-ng", "enum4linux-ng", "null-session"),
    # --- the modern enumeration and ADCS tooling.
    ("nxc", "netexec", "smb-signing"),
    ("certipy", "certipy-ad", "adcs-enroll"),
    ("bloodhound-python", "bloodhound.py", "acl-paths"),
    ("rusthound", "rusthound-ce", "acl-paths"),
    ("windapsearch", "windapsearch", "ldap-anon"),
    ("ldapsearch", "ldap-utils", "ldap-anon"),
    ("gpp-decrypt", "gpp-decrypt", "gpp-cpassword"),
    ("kerbrute", "kerbrute", "user-enum"),
    # --- coercion, relay and offline cracking.
    ("mitm6", "mitm6", "coercion"),
    ("responder", "responder", "coercion"),
    ("hashcat", "hashcat", "kerberoast"),
    ("john", "john", "kerberoast"),
    # --- transport. Always worth knowing whether these are there, because a
    #     domain that does not resolve is a domain nothing else can reach.
    ("nmap", "nmap", "smb-signing"),
    ("dig", "dnsutils", "domain-enum"),
    ("nslookup", "dnsutils", "domain-enum"),
    ("openssl", "openssl", "adcs-enroll"),
)

#: Vector -> the tools it needs. Derived, never hand-written, so the two cannot
#: drift. A vector whose tools are all absent is out of scope for this run, and
#: the recon prompt says so by name rather than letting a wave spend ten agents
#: rediscovering it.
VECTOR_TOOLS: dict[str, tuple[str, ...]] = {}
for _n, _h, _v in _TOOL_CATALOGUE:
    VECTOR_TOOLS[_v] = VECTOR_TOOLS.get(_v, ()) + (_n,)
del _n, _h, _v

#: Tools whose entire purpose is guessing a credential. They are the highest
#: lockout risk on the box, so they are named as tools the interlock inspects —
#: a `hydra` that went unrecognised would be the one command that matters
#: slipping through. Kept out of `_TOOL_CATALOGUE` because they are not part of
#: the AD toolchain the probe reports on: nothing about `hydra` tells the
#: operator whether this domain is weak.
_SPRAY_TOOLS: frozenset[str] = frozenset(
    ("hydra", "medusa", "patator", "ncrack", "crowbar", "spray", "sprayhound"))

#: The SMB swiss-army knives, by every name they ship under. `crackmapexec` is
#: the old name, `nxc`/`netexec` the current ones, and they are the single most
#: likely tool for an agent to reach for when it decides to spray — so all three
#: have to be recognised, not just the one the probe happens to find.
_C2_ALIASES: tuple[str, ...] = ("crackmapexec", "cme", "nxc", "netexec")

#: Every name the interlock treats as AD tooling. A command naming none of these
#: is not inspected at all — `ls`, `cat` and a wordlist grep are not credential
#: attempts and must not burn the budget.
AD_TOOL_NAMES: frozenset[str] = (
    frozenset(n for n, _h, _v in _TOOL_CATALOGUE)
    | _SPRAY_TOOLS
    | frozenset(_C2_ALIASES)
)

#: Tools that never submit a credential, whatever their flags. A port scanner, a
#: resolver, an offline cracker, a TLS client and a cpassword decoder do not
#: authenticate to anything — so `nmap --script smb2-security-mode` is a read and
#: must not spend the budget. Checked before the conservative default, which
#: would otherwise count every one of them.
#:
#: The exception is a spray run through one of them — `nmap --script smb-brute`
#: guesses passwords as surely as hydra does — so the spray marker is tested
#: first and wins.
#:
#: `enum4linux-ng` and `bloodhound-python` are here for the same reason as the
#: scanner: their whole job is to *collect* what the domain will hand an
#: anonymous caller. They have no password flag to carry one, so a command that
#: names them is enumeration, and counting it would spend the campaign's budget
#: on the one family of work that cannot lock an account.
_NO_AUTH_TOOLS: frozenset[str] = frozenset(
    ("nmap", "dig", "nslookup", "host", "openssl", "hashcat", "john",
     "gpp-decrypt", "enum4linux-ng", "enum4linux", "bloodhound-python"))


@dataclass(frozen=True)
class ToolProbe:
    """One tool, and whether this host actually has it."""

    name: str
    path: str | None
    hint: str
    vector: str

    @property
    def present(self) -> bool:
        return self.path is not None


def probe_tools() -> list[ToolProbe]:
    """Find out what is installed, once, before any brief is written.

    A brief that sends an agent after a binary that is not on the box wastes the
    agent entirely, and ten agents each discovering that for themselves wastes
    the wave. So the probe runs here, its result is written into the recon
    prompt, and every vector whose tooling is missing is named as out of scope.

    ``shutil.which`` rather than an import, because half of these are scripts
    with no Python package to import and the question is whether the *command*
    resolves.
    """
    return [
        ToolProbe(name=n, path=shutil.which(n), hint=h, vector=v)
        for n, h, v in _TOOL_CATALOGUE
    ]


def render_tools(probes: list[ToolProbe]) -> str:
    """The probe result as the recon prompt wants it: present, then missing."""
    have = [p for p in probes if p.present]
    missing = [p for p in probes if not p.present]
    lines = [
        "PRESENT on this host — use these:",
        "  " + (", ".join(p.name for p in have) or "(none of them)"),
        "",
        "ABSENT — a vector that needs one of these is OUT OF SCOPE for this run.",
        "Do not write a brief around a tool in this list, and do not spend a turn",
        "installing it. Say the vector was skipped for missing tooling instead:",
    ]
    for p in missing:
        lines.append(f"  {p.name:<20} would need: {p.hint}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Verdict:
    """What the interlock made of one command.

    Pure data, and frozen, so ``inspect`` is a function of its argument alone
    and every rule in it can be asserted by writing a command down.
    """

    #: The command submits something that can be *wrong*, which is the only
    #: thing that locks an account. Conservative: see ``inspect``.
    credential: bool = False
    #: The command changes the directory rather than reading it. Refused
    #: outright unless the operator armed the run.
    destructive: bool = False
    #: The AD tool the command names, or "". Empty means nothing here is AD
    #: tooling and the interlock leaves the call alone entirely.
    tool: str = ""
    reason: str = ""

    @property
    def relevant(self) -> bool:
        """Whether the interlock has any business with this command."""
        return bool(self.tool)


def unavailable_vectors(probes: list[ToolProbe]) -> list[str]:
    """Vectors whose every tool is missing — skipped, and named as skipped.

    "Every" rather than "any": most vectors have a fallback. A vector with one
    tool present is runnable with a worse method, and degrading to it is the
    point of the probe. Only a vector with nothing left to run is out.
    """
    present = {p.name for p in probes if p.present}
    return [
        vector for vector, tools in sorted(VECTOR_TOOLS.items())
        if tools and not (present & set(tools))
    ]


# ---------------------------------------------------------------------------
# The interlock
# ---------------------------------------------------------------------------

class InterlockRefusal(RuntimeError):
    """A tool call the interlock refused.

    A plain ``RuntimeError`` on purpose. ``agent.py`` already wraps every tool
    call in ``except Exception`` and turns the exception into an ``is_error``
    ToolResult carrying the message, so raising this is all it takes for the
    agent to *read why* it was refused and adapt — and for the run to continue
    rather than die. No new plumbing in the agent loop.
    """


class Throttle:
    """A shared token bucket. One instance per campaign, not per agent.

    Ten parallel agents sharing one bucket is the whole point: a per-agent
    throttle is ten times the rate the operator agreed to. The bucket refills at
    ``rate`` tokens per second up to ``burst``, and a caller that arrives to an
    empty bucket waits — inside the lock, so the wait is served in arrival order
    and a burst of ten agents becomes a queue rather than a stampede.

    ``rate <= 0`` disables throttling entirely and every acquire returns at
    once. That is the honest meaning of "no throttle", and it is what the tests
    use so they are not paying real seconds for arithmetic they can check
    directly.
    """

    def __init__(self, rate: float = 1.0, burst: int = 4):
        self.rate = max(0.0, float(rate))
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._stamp = time.monotonic()
        self._lock = asyncio.Lock()
        #: How many calls actually had to wait, and for how long in total. Read
        #: by the tests, and by the closing report when it wants to say how hard
        #: the throttle bit.
        self.waited = 0
        self.slept = 0.0

    @property
    def tokens(self) -> float:
        """Tokens available right now, refilled to this instant."""
        if self.rate <= 0:
            return float(self.burst)
        return min(self.burst,
                   self._tokens + (time.monotonic() - self._stamp) * self.rate)

    async def acquire(self) -> float:
        """Take one token, waiting if the bucket is empty. Returns the wait."""
        if self.rate <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            delay = (1.0 - self._tokens) / self.rate
            self.waited += 1
            self.slept += delay
            await asyncio.sleep(delay)
            self._tokens = 0.0
            self._stamp = time.monotonic()
            return delay


class AttemptBudget:
    """The circuit breaker. Counts credential attempts, then refuses.

    The ceiling is ``lockout - 1``, and that is the entire design. An operator
    who says the domain locks accounts after five bad passwords is saying they
    can afford four. So four are allowed and the fifth is refused — before it
    runs, not after — and once tripped *every* subsequent credential-bearing
    command in the campaign is refused, because a wave that has already walked
    into the threshold has no business trying a sixth account.

    Nothing here counts reads. A null session, an anonymous bind, a DNS query
    and an SMB signature check are not credential attempts and must not consume
    the budget, or the breaker would trip on reconnaissance.
    """

    def __init__(self, lockout: int = 5):
        self.lockout = max(1, int(lockout))
        #: Attempts permitted before the refusal. Never below one: a lockout of
        #: 1 means the first wrong password locks the account, so zero attempts
        #: are affordable — but a budget that permits nothing makes the command
        #: useless, and refusing to run at all is the operator's call, not this
        #: class's. It is reported loudly in the banner instead.
        self.limit = max(1, self.lockout - 1)
        self.spent = 0
        self.tripped = False
        #: The commands that spent the budget, for the report. Bounded, because
        #: a long campaign should not grow this without limit.
        self.history: list[str] = []

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self, command: str = "") -> None:
        """Consume one attempt, or raise ``InterlockRefusal``.

        Raises *before* incrementing past the limit, so ``spent`` never exceeds
        ``limit`` and the reported count is always the number of attempts that
        actually ran.
        """
        if self.tripped:
            raise InterlockRefusal(
                f"BLOCKED — the attempt budget is spent. {self.limit} of "
                f"{self.limit} permitted credential attempts have run against a "
                f"domain that locks at {self.lockout}, so the campaign is "
                f"refused any further credential-bearing command. Continue with "
                f"unauthenticated vectors, or start a new campaign with a higher "
                f"--lockout once you have confirmed the real policy with the "
                f"client.")
        if self.spent + 1 > self.limit:
            self.tripped = True
            raise InterlockRefusal(
                f"BLOCKED — this would be credential attempt "
                f"{self.spent + 1}, and the domain locks accounts at "
                f"{self.lockout}. The budget is {self.limit} (lockout - 1) and "
                f"it is now spent; every further credential-bearing command in "
                f"this campaign is refused. This is the interlock working, not "
                f"an error — report what the attempts so far established and "
                f"stop guessing.")
        self.spent += 1
        if len(self.history) < 50:
            self.history.append(command.strip()[:200])


# --- command inspection ----------------------------------------------------
#
# A pure function, so the whole interlock is testable without a campaign, a
# model, or a network. Everything below is a regex over the command string.

#: Flags that carry a secret. If one of these is present the command is trying
#: to authenticate with something that can be *wrong*, which is the only thing
#: that locks an account.
#:
#: `-usersfile` is deliberately NOT here. It names a list of *usernames* — it is
#: what AS-REP roasting and enumeration take — and counting it would spend the
#: whole budget on the one technique that cannot lock anything, which is exactly
#: backwards. A file of passwords is `-w`/`--wordlist`, and that one is here.
_SECRET_FLAGS: tuple[str, ...] = (
    "-p", "--password", "--passwords", "-pw", "--pw",
    "-hashes", "--hashes",
    "-w", "--wordlist", "--passwords-file",
)

#: Flags that positively mean *no* secret is being offered. Checked after the
#: secret flags, so `-N` alongside a `-p` is still counted — a command that
#: carries a password is a credential attempt whatever else it says.
#:
#: `-x` is ldapsearch's simple-bind *without* a DN, and `-Y`/`-Q` are GSSAPI and
#: SASL with no credential offered. They belong here rather than in
#: `_NO_AUTH_TOOLS` because ldapsearch is not a tool that *cannot* authenticate:
#: with `-D` and `-w` it very much can, and that form must still be counted.
_NO_SECRET_FLAGS: tuple[str, ...] = (
    "-N", "--no-pass", "-no-pass", "--anonymous", "-x",
)

#: The impacket target syntaxes that carry a password inline, plus samba's
#: `user%pass`. Three shapes, because impacket accepts all three:
#:
#:   domain/user:pass@host     the full form
#:   domain/user:pass          with the host given separately as -dc-ip
#:   user%pass                 samba's, as `-U j.doe%Passw0rd`
#:
#: The domain half is required to be *dotted* in the second shape. That is what
#: separates `corp.local/j.doe:Passw0rd` from a file:line reference like
#: `src/app.py:12` — an AD domain always has a dot, a source directory usually
#: does not — and it keeps a grep result from being read as a credential.
_SECRET_INLINE_RE = re.compile(
    r"[a-zA-Z0-9_.\-]+:[^@\s/\\]+@|"                          # j.doe:Pass@dc01
    r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+/[a-zA-Z0-9_.\-]+:[^@\s/\\]+|"  # corp.local/j.doe:Pass
    r"[a-zA-Z0-9_.\-]+%[^@\s/\\]+"                            # -U j.doe%Pass
)

#: The netexec/crackmapexec password flags, which the shared table above cannot
#: carry because `-p` means *port* to nmap. Checked only when the command names
#: one of the C2 aliases, so `nmap -p445` is a port scan and `nxc smb … -p X` is
#: a credential. `-H` is the same story: a hash to nxc, something else to nmap.
_C2_SECRET_FLAGS: tuple[str, ...] = (
    "-p", "--password", "--passwords", "-H", "--hash", "--hashes",
    "-u", "--username", "--users", "-w", "--wordlist",
)

#: A long-form password flag that carries its value inline: `--password=Pass1`.
_INLINE_SECRET_FLAG_RE = re.compile(
    r"(?:^|\s)(?:--password|--passwords|--pw|--hashes|--hash|--wordlist)=")

#: netexec/crackmapexec spray markers. `--continue-on-success` means the run
#: keeps going after a hit, which is the shape of a spray rather than a single
#: login attempt; `--no-bruteforce` is the opposite and is an explicit opt-out.
_SPRAY_MARKER_RE = re.compile(
    r"--continue-on-success|--no-bruteforce(?!=)|(?:^|\s)-M\s+spray|"
    r"(?:^|\s)--spray\b")

#: A `brute` token that is not itself a tool name. `nmap --script smb-brute` and
#: `--brute` are sprays hiding in an argument list, where a flag table would
#: never see them — but `kerbrute` is a tool whose *name* contains the word, and
#: `kerbrute userenum` submits nothing. Matched as a word and then filtered
#: against `AD_TOOL_NAMES`, which is what tells the two apart.
_BRUTE_WORD_RE = re.compile(r"[a-zA-Z0-9_.-]*brute[a-zA-Z0-9_.-]*")

#: Invocations that are known to submit no password, whatever else is on the
#: line. The AS-REP roast is the flagship unauthenticated technique — Kerberos
#: pre-authentication is disabled on the target account, so no password is ever
#: checked and no lockout is possible — and kerbrute's enumeration subcommand
#: asks the KDC which accounts exist without ever offering a credential.
#: Counting these would spend the campaign's whole budget on the work that
#: cannot lock anything, which is precisely backwards. Checked *after* the
#: secret flags, so `GetNPUsers.py -p …` is still a credential attempt.
_NO_SUBMIT_RE = re.compile(
    r"\bGetNPUsers\.py\b|"
    r"\bkerbrute\s+userenum\b|"
    r"\benumdom(?:users|groups|aliases)\b|"
    r"\bquerydispinfo\b|\bqueryuser\b")

#: A recursive, forced delete. Destructive on its own, but only interesting to
#: this interlock when it is aimed at a domain controller — hence the pairing
#: with `_DC_PATH_RE` below.
_RM_RF_RE = re.compile(r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+")

#: Paths that belong to a domain controller's own state. `/var/lib/samba`,
#: `C:\Windows\NTDS`, `/etc/krb5.keytab` — deleting any of these is not a
#: penetration test, it is the incident.
_DC_PATH_RE = re.compile(
    r"/var/lib/samba|/var/lib/sss|/etc/sssd|/etc/krb5|/var/kerberos|"
    r"/etc/samba|/var/lib/ldap|/etc/ldap|"
    r"ntds\.dit|\\Windows\\NTDS|\\Windows\\SYSVOL|sysvol",
    re.IGNORECASE)

#: An empty user — `-U ''` — is a null session, not a credential.
_NULL_USER_RE = re.compile(r"""-U\s+(?:''|"")""")

#: Things that change the domain rather than read it. Refused unless the
#: operator armed the run, because these are the commands that turn a
#: penetration test into an incident.
_DESTRUCTIVE: tuple[tuple[re.Pattern, str], ...] = (
    # Both spellings: impacket scripts take single-dash long options
    # (`-delete`), PowerShell cmdlets and nxc take the double-dash GNU form.
    # `--delete`, and impacket's `-action delete` — which spells the same thing
    # as a *value* rather than a flag, so a rule that only looked for the flag
    # would miss the form `secretsdump.py` actually documents. Both the
    # space-separated and `=`-joined spellings, since argparse takes either.
    (re.compile(r"(?:^|\s)--?delete\b|(?:^|\s)--?action[=\s]+delete\b|"
                r"\bRemove-AD(?:User|Group|Object)\b"),
     "deletes a directory object"),
    (re.compile(r"(?:^|\s)--?action\s+reset\b"),
     "resets an account password"),
    (re.compile(r"\bset-password\b|\bSet-ADAccountPassword\b|\bdsmod\s+user\b|"
                r"\bnet\s+user\b[^|;]*/add\b|\bnet\s+user\b[^|;]*/active:no\b"),
     "sets a password or creates or disables an account"),
    # `net user <name> <password>` — no flag at all, so nothing above catches
    # it. The third token is the password, and it is a password because it does
    # not start with `/`: `net user j.doe /domain` is a read, `net user j.doe
    # NewPass /domain` is a reset, and the difference is that slash.
    (re.compile(r"\bnet\s+user\s+\S+\s+(?!/)\S"), "sets an account password"),
    (re.compile(r"\bnet\s+accounts\b[^|;]*/(?:minpwlen|lockoutthreshold|"
                r"maxpwage|forcelogoff|uniquepw)\b"),
     "changes the domain password or lockout policy"),
    (re.compile(r"\bAdd-DomainObjectAcl\b|\bSet-DomainObject\b|"
                r"\bAdd-ADGroupMember\b|\bAdd-DomainGroupMember\b|\bdsacls\b"),
     "modifies ACLs or group membership"),
    # An ADCS *write*, matched on certipy's subcommand — the token immediately
    # after the tool name — rather than on any flag that follows it.
    #
    # `certipy find` only reads, and a blanket rule on the word `template`
    # refused the recon that establishes whether ESC1 exists at all. Matching
    # arguments instead is worse in the other direction: `-ca CORP-CA` appears
    # in `certipy req` and `certipy find` alike, so a rule keyed on it refuses a
    # certificate *request* while letting the same request through when the CA
    # is auto-discovered. The subcommand is what decides.
    #
    # `template` and `ca` change ADCS configuration; `account` sets passwords
    # and `shadow` adds key credentials, both of which are writes to the
    # directory. `req` is deliberately absent: enrolling a certificate does not
    # modify directory state, which is the line this rule draws.
    (re.compile(r"\bcertipy(?:\.py)?\b[^|;]*?\s"
                r"(?:template|ca|account|shadow)\b"),
     "modifies an ADCS template, CA or account"),
    (re.compile(r"\bntlmrelayx\.py\b[^|;]*(?:--escalate-user|--delegate-access|"
                r"--add-computer|--sid|(?:^|\s)-c\s)"),
     "writes to the domain through a relay"),
    # A machine account is a *new object* in the directory, so this is a write
    # however it is spelled — with a password, or with a hash.
    (re.compile(r"\baddcomputer\.py\b[^|;]*"
                r"(?:-computer-pass|-computer-password|-hashes)"),
     "joins a computer to the domain"),
    (re.compile(r"\bgpo(?:backup|restore)\b|\bdcpromo\b|\bntdsutil\b[^|;]*"
                r"\b(?:delete|repair|authoritative)\b"),
     "modifies or restores directory state"),
)

#: Any word that could be a command name or a tool. Split on the characters that
#: separate one command from the next, so `a && b` and `a | b` are both seen.
_TOOL_WORD_RE = re.compile(r"[a-zA-Z0-9_./\-]+")


def _words(command: str) -> list[str]:
    """Every token that could be a tool name, basename-normalised."""
    return [w.rsplit("/", 1)[-1] for w in _TOOL_WORD_RE.findall(command)]


def _named_tool(command: str) -> str:
    """The first AD tool the command names, or "".

    Matched on whole tokens so `--wordlist` cannot be read as a tool called
    `wordlist`, and on the basename so `/usr/bin/GetUserSPNs.py` and
    `GetUserSPNs.py` are the same tool.
    """
    for base in _words(command):
        if base in AD_TOOL_NAMES:
            return base
        # `impacket-GetUserSPNs` and `impacket-secretsdump` are the same scripts
        # under the distro's console-script names.
        if base.startswith("impacket-"):
            stem = base[len("impacket-"):]
            for candidate in (f"{stem}.py", stem):
                if candidate in AD_TOOL_NAMES:
                    return candidate
    return ""


def _has_flag(command: str, flag: str) -> bool:
    """True if *flag* appears as a whole argument — `-p`, not `-passive`."""
    return re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|=|$)", command) is not None


def inspect(command: str) -> Verdict:
    """Read one shell command. Pure — no state, no I/O, no network.

    This is the whole interlock's decision surface, which is why it is a
    function of the string alone: every rule below can be tested by writing the
    command down, and the tests need no campaign, no model and no domain.

    The classification is deliberately asymmetric. Destructive verbs are
    detected by a positive match on a specific pattern, because refusing a
    read-only command costs the operator a turn. Credential attempts are the
    other way round: **counted unless the command is definitely non-credential**,
    because under-counting is how a domain gets locked out and over-counting
    only costs budget the operator can raise.
    """
    cmd = (command or "").strip()
    if not cmd:
        return Verdict()

    tool = _named_tool(cmd)
    words = set(_words(cmd))
    lowered = cmd.lower()

    # --- does it submit a secret? This is computed first because it decides how
    #     a destructive command is classified, not just whether it is counted.
    has_secret = (
        any(_has_flag(cmd, f) for f in _SECRET_FLAGS)
        or _SECRET_INLINE_RE.search(cmd) is not None
        or _INLINE_SECRET_FLAG_RE.search(cmd) is not None
        or (bool(words & set(_C2_ALIASES))
            and any(_has_flag(cmd, f) for f in _C2_SECRET_FLAGS))
    )

    # --- destructive verbs, before the tool test, because the worst of them —
    #     `net user j.doe NewPass`, `net accounts /lockoutthreshold:0` — name no
    #     AD tool at all and would otherwise fall through as "not our business".
    #     A command that writes to the directory is the interlock's business
    #     however it spells itself.
    #
    #     `credential` is taken from the secret check rather than assumed: a
    #     destructive command that also carries a password is refused outright
    #     by the interlock and must not *also* have spent the attempt budget on
    #     the way in, or the operator loses an attempt to a command that never
    #     ran.
    for pattern, why in _DESTRUCTIVE:
        if pattern.search(cmd):
            return Verdict(credential=has_secret, destructive=True,
                           tool=tool or "net", reason=why)

    if not tool:
        # A recursive delete of a domain controller's own state names no AD tool
        # — `rm -rf /var/lib/samba` is just `rm` — but it is still the end of
        # the client's Monday, so it is caught here rather than falling through
        # as "not our business". Anything else is `ls`, `cat`, `grep`: nothing
        # to say, and nothing that may spend the budget.
        if _RM_RF_RE.search(cmd) and _DC_PATH_RE.search(cmd):
            return Verdict(credential=False, destructive=True, tool="rm",
                           reason="rm: recursively deletes a directory that "
                                  "looks like a domain controller's")
        return Verdict()

    # Guessing is checked next and wins over everything below, including the
    # no-auth tools: `nmap --script smb-brute` is a port scanner by name and a
    # password spray in fact.
    brute_words = {
        w for w in _BRUTE_WORD_RE.findall(cmd) if w.rsplit("/", 1)[-1] not in AD_TOOL_NAMES
    }
    if words & _SPRAY_TOOLS or _SPRAY_MARKER_RE.search(cmd) or brute_words \
            or "spray" in lowered or "brute-force" in lowered \
            or "bruteforce" in lowered:
        return Verdict(credential=True, tool=tool,
                       reason=f"{tool}: password guessing or spraying")

    if _NULL_USER_RE.search(cmd):
        return Verdict(credential=False, tool=tool,
                       reason=f"{tool}: null session, no credential offered")

    if any(_has_flag(cmd, f) for f in _NO_SECRET_FLAGS):
        return Verdict(credential=False, tool=tool,
                       reason=f"{tool}: explicitly no password")

    # A recursive delete aimed at a domain controller's own state. Destructive,
    # and not a credential attempt, so it is refused outright without spending
    # budget. `rm -rf /tmp/scratch` is housekeeping and the interlock leaves it
    # alone; `rm -rf /var/lib/samba` is the end of the client's Monday.
    if _RM_RF_RE.search(cmd) and _DC_PATH_RE.search(cmd):
        return Verdict(credential=False, destructive=True, tool=tool,
                       reason=f"{tool}: recursively deletes a directory that "
                              f"looks like a domain controller's")

    # Tools that cannot authenticate to anything, whatever flags they carry. A
    # port scanner, a resolver, an offline cracker and a TLS client offer no
    # credential to be wrong, so they cannot lock an account.
    #
    # Checked *after* the spray rule above, because `nmap --script smb-brute` is
    # a port scanner by name and a password spray in fact — and *before* the
    # secret-flag test below, because `-p` means **port** to nmap and
    # **password** to netexec. A scanner refused for naming a port is a campaign
    # that cannot map its own target.
    if tool in _NO_AUTH_TOOLS:
        return Verdict(credential=False, tool=tool,
                       reason=f"{tool}: does not authenticate — no credential "
                              f"to lock an account with")

    if has_secret:
        return Verdict(credential=True, tool=tool,
                       reason=f"{tool}: submits a credential")

    # Invocations that are known to submit nothing. The AS-REP roast is the
    # flagship unauthenticated technique: pre-authentication is disabled on the
    # target account, so no password is ever checked and no lockout is possible.
    # Counting it would spend the campaign's whole budget on the one technique
    # that cannot lock anything. Checked after the secret flags, so a
    # `GetNPUsers.py -p …` is still counted.
    if _NO_SUBMIT_RE.search(cmd):
        return Verdict(credential=False, tool=tool,
                       reason=f"{tool}: submits no credential — nothing to "
                              f"lock an account with")

    # AD tooling with no recognisable secret and no `-N`. Could be a null
    # session, could be an interactive prompt that will fail, could be a spray
    # hidden behind a flag this table does not know. Counted, and said so.
    return Verdict(credential=True, tool=tool,
                   reason=f"{tool}: cannot tell whether this authenticates — "
                          f"counted conservatively")


def make_interlock(
    throttle: Throttle,
    budget: AttemptBudget,
    *,
    armed: bool = False,
    on_refuse: Callable[[Verdict, str], None] | None = None,
) -> Callable[[str, dict], object]:
    """Build the tool hook `/ad` installs on every agent in the campaign.

    ``bash`` only. Everything else the agents do — reading a file, writing a
    report — is not a credential attempt and must not queue behind a throttle.

    ``armed`` is the operator's explicit opt-in to the destructive verbs, and it
    is off unless they asked. Nothing in this module turns it on by itself.

    ``on_refuse`` is a reporting seam, not a control: the UI uses it to put the
    refusal in the transcript, so the operator sees the breaker trip as it
    happens rather than inferring it from a wave that went quiet.
    """

    def _note(verdict: Verdict, command: str) -> None:
        if on_refuse is not None:
            try:
                on_refuse(verdict, command)
            except Exception:  # noqa: BLE001
                pass

    @asynccontextmanager
    async def _guard(name: str, args: dict):
        if name != "bash":
            yield
            return
        command = str((args or {}).get("command") or (args or {}).get("cmd") or "")
        verdict = inspect(command)
        if not verdict.relevant:
            yield
            return

        if verdict.destructive and not armed:
            _note(verdict, command)
            raise InterlockRefusal(
                f"BLOCKED — refusing a command that {verdict.reason}. /ad does "
                f"not modify the directory: a penetration test that changes "
                f"ACLs, resets a password or writes through a relay is an "
                f"incident, not a finding. Re-run with --arm if the client has "
                f"authorised write actions, or report what you found without "
                f"using it.")

        if verdict.credential:
            await throttle.acquire()
            try:
                budget.spend(command)
            except InterlockRefusal:
                _note(verdict, command)
                raise
        yield

    return _guard


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

#: ``(name, brief, owns, tools)``. The three-tuple the engine's fallback roster
#: wants is derived below, so the tool requirement lives beside the brief
#: instead of in a parallel table that can drift.
#:
#: **These are all unauthenticated on purpose.** A fallback matrix is what runs
#: when the planner's reply cannot be read, which means it has to be safe to run
#: blind — with no foothold, no credential and no assumption about what an
#: earlier wave obtained. Every vector here is reachable from outside the
#: domain, and the ones that are not (kerberoast, ADCS) say so in the brief and
#: tell the agent to report the *reachability* rather than force the exploit.
_VECTOR_TABLE: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "asrep-roast",
        "AS-REP roasting. Ask the KDC for a TGT for every account that does not "
        "require Kerberos pre-authentication, and crack the response offline. "
        "Enumerate the accounts first — GetNPUsers.py takes a -usersfile, and "
        "the domain's own naming convention (first.last, flast) generates a "
        "usable list without any credential. This needs NO authentication and "
        "cannot lock an account: pre-auth is disabled, so no password is being "
        "checked. Report every account that returned a hash, and whether the "
        "hash cracked and to what.",
        "asrep-roast", ("GetNPUsers.py", "hashcat"),
    ),
    (
        "kerberoast",
        "Kerberoasting. Enumerate accounts with a servicePrincipalName and "
        "request a service ticket for each, then crack it offline. Requesting a "
        "TGS normally needs a credential — so establish FIRST whether an "
        "anonymous or null LDAP/SAMR bind can read the SPN list, and if it "
        "cannot, report that as the finding and stop: an unauthenticated "
        "kerberoast is only possible where the bind is open, and the open bind "
        "is itself the bug worth reporting. Never guess a password to get the "
        "bind.",
        "kerberoast", ("GetUserSPNs.py", "hashcat"),
    ),
    (
        "null-session",
        "Null and guest sessions. Test whether the domain controllers accept an "
        "SMB or SAMR session with no credential at all — `rpcclient -U '' -N` "
        "and `smbclient -L //<dc> -N`. If one is accepted, enumerate what it "
        "exposes: users, groups, password policy, shares, sessions. The "
        "password policy is the single most valuable thing here, because every "
        "later decision about how hard this engagement may push depends on the "
        "real lockout threshold rather than the one we were told. Report the "
        "policy verbatim, including the lockout threshold and duration.",
        "null-session", ("rpcclient", "smbclient", "enum4linux-ng"),
    ),
    (
        "ldap-anon",
        "Anonymous LDAP. Query the directory with no bind — `ldapsearch -x -H "
        "ldap://<dc> -b 'dc=corp,dc=local'` and windapsearch if present. An "
        "anonymous bind that returns objects is a finding on its own. Enumerate "
        "the naming context, the domain functional level, and any object the "
        "bind can actually read: users, groups, computers, and especially "
        "attributes that should never be world-readable. Do not send a bind "
        "with credentials to widen this — if the anonymous read stops short, "
        "that is the result.",
        "ldap-anon", ("ldapsearch", "windapsearch"),
    ),
    (
        "smb-signing",
        "SMB signing and protocol posture. Determine, for every host you can "
        "reach, whether SMB signing is required, enabled-but-not-required, or "
        "off. Unsigned SMB is what makes NTLM relay possible, so this is the "
        "question that decides whether the relay vectors are worth a wave. Use "
        "`nmap --script smb2-security-mode,smb2-capabilities -p445 <targets>` "
        "and nxc if it is installed. Report per host, with the exact script "
        "output — 'signing not required' on a member server is a high finding "
        "and needs the evidence to say so.",
        "smb-signing", ("nmap", "nxc"),
    ),
    (
        "adcs-enroll",
        "ADCS reachability. Find the certificate authorities: the CA is "
        "published in the directory as an enrollment service object and often "
        "answers on its own hostname at /certsrv/. Establish whether the web "
        "enrollment endpoint is reachable unauthenticated, and whether the CA's "
        "certificate is retrievable — `openssl s_client` against the CA host, "
        "and a plain fetch of http://<ca>/certsrv/. Do NOT modify a template or "
        "a CA. This vector is reconnaissance for the ADCS exploitation wave, "
        "and its output is the list of CAs and templates to attack later, not "
        "an attack.",
        "adcs-enroll", ("certipy", "openssl"),
    ),
    (
        "domain-enum",
        "Domain mapping by DNS. No credential is involved and this is the "
        "cheapest high-value work in the wave. Resolve the domain's own records: "
        "the SRV records that name every domain controller (_ldap._tcp.dc._msdcs, "
        "_kerberos._tcp, _gc._tcp), the site topology, the A records, and any "
        "zone transfer the nameserver will hand over (AXFR — a success is a "
        "critical finding). Build the host list the rest of the campaign will "
        "use. Report the DCs by name and address, the functional level if you "
        "can read it, and whether the nameserver leaked the zone.",
        "domain-enum", ("dig", "nslookup", "rpcdump.py"),
    ),
    (
        "user-enum",
        "Username enumeration. Establish which accounts exist without "
        "authenticating to any of them. Kerberos is the safe oracle: an AS-REQ "
        "for a nonexistent principal returns a different error from one for a "
        "real account with pre-authentication required, and kerbrute uses "
        "exactly that — it enumerates without ever submitting a password. If "
        "kerbrute is absent, the null-session RPC path (enumdomusers, "
        "lookupsid) does the same job. Build a real user list from the domain's "
        "naming convention and the names you find. Do NOT follow this with a "
        "password spray in this brief: enumeration and guessing are separate, "
        "and the guessing budget is the operator's to spend.",
        "user-enum", ("kerbrute", "lookupsid.py", "samrdump.py", "GetADUsers.py"),
    ),
    (
        "gpp-cpassword",
        "SYSVOL and the cpassword problem. If any read access to SYSVOL is "
        "possible, hunt Group Policy Preferences XML for `cpassword` — the "
        "AES key Microsoft published, which makes every stored GPP password "
        "decryptable by anyone who can read the file. Also look for scripts, "
        "batch files and unattend.xml carrying credentials, and for shares that "
        "expose them. `smbclient //<dc>/SYSVOL -N -c 'recurse; ls'` and a grep "
        "for cpassword is the whole method. Report the exact file path and the "
        "decrypted password if gpp-decrypt is available, or the cpassword blob "
        "and its location if it is not.",
        "gpp-cpassword", ("smbclient", "gpp-decrypt", "smbmap"),
    ),
    (
        "laps-read",
        "LAPS and readable secrets. Determine whether the local-administrator "
        "password is managed by LAPS, and whether the attribute that holds it "
        "(`ms-Mcs-AdmPwd` on legacy LAPS, `msLAPS-Password` on Windows LAPS) is "
        "readable by anything less than a domain admin. A null or anonymous "
        "bind that can read it is a critical finding. Also check for readable "
        "description and info fields carrying passwords — a helpdesk-set "
        "password in a user's description is common and is a real finding. "
        "Report the attribute, the object it was read from, and the exact "
        "query that read it.",
        "laps-read", ("rpcclient", "ldapsearch", "reg.py"),
    ),
)

#: The fallback roster, in the shape ``hunt.fallback_roster`` consumes.
AD_VECTORS: tuple[tuple[str, str, str], ...] = tuple(
    (n, b, o) for n, b, o, _t in _VECTOR_TABLE
)

#: The authenticated vectors — the menu for wave 2 and beyond, once a wave has
#: obtained a foothold. Not in ``AD_VECTORS``: a fallback matrix has to be safe
#: to run with no credential at all, and every one of these needs one. The
#: planner prompt names them so the second wave has somewhere to go.
AD_LATER_VECTORS: tuple[tuple[str, str], ...] = (
    ("acl-paths", "BloodHound-style ACL analysis: find the shortest path from "
                  "what we hold to Domain Admin — GenericAll, WriteDacl, "
                  "WriteOwner, ForceChangePassword, AddMember — and the object "
                  "each one hangs off."),
    ("delegation", "Unconstrained, constrained and resource-based constrained "
                   "delegation. An unconstrained-delegation host that we can "
                   "write to is a domain compromise; RBCD on a machine account "
                   "we control is the modern one."),
    ("adcs-esc", "ADCS exploitation: ESC1 through ESC16 against the templates "
                 "the recon wave listed. Certificate templates are the most "
                 "common full-domain escalation in a modern estate."),
    ("dcsync-rights", "Who holds Replicating Directory Changes and "
                      "Replicating Directory Changes All. Either one is a "
                      "DCSync, which is every hash in the domain."),
    ("trust-abuse", "Domain and forest trusts: the trust key, SID history, "
                    "and whether the trust is transitive or filtered. A trust "
                    "is a doorway and the trust key is often the lock."),
    ("coercion", "Authentication coercion — PetitPotam, PrinterBug, "
                 "DFSCoerce — against a host that will relay to the CA or the "
                 "DC. This one needs the relay in place first."),
    ("relay", "NTLM relay. Only worth a wave where the recon found SMB signing "
              "off, or an ADCS web enrollment endpoint that accepts NTLM. "
              "Relay to LDAP for RBCD, to the CA for a certificate, or to SMB "
              "for a shell — the choice follows from what is unsigned."),
    ("host-sprawl", "Lateral movement across the member servers: local admin "
                    "reuse, cached credentials, service accounts, and which "
                    "hosts the credential we hold actually opens."),
    ("scheduled-tasks", "Scheduled tasks and services on hosts we can reach — "
                        "a task running as a privileged account, or a service "
                        "binary path we can write to."),
    ("unquoted-paths", "Unquoted service paths and weak directory permissions "
                       "on the member servers — the classic local privilege "
                       "escalation that turns a member-server foothold into a "
                       "domain one."),
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

#: `{tools}` is substituted by ``build_recon_prompt`` *before* the engine calls
#: ``.format(target=…, contract=…)``. Left in place it raises KeyError, which is
#: the right failure: a recon prompt with no tool list is a prompt that sends
#: agents after binaries that are not installed.
_RECON_TEMPLATE = """\
You are opening a security engagement against a declared, authorised Active
Directory domain.

DOMAIN: {target}

This is the RECON phase. Map the domain before anyone attacks it. Do not attempt
exploitation here — that is the next phase, and a recon run that stops early to
try a roast has failed at its actual job.

TOOLING ON THIS HOST
--------------------
{tools}

Work with the tools you have. Establish, as far as you can, WITHOUT
AUTHENTICATING TO ANY ACCOUNT:

- The domain controllers: their names and addresses, from DNS SRV records
  (`_ldap._tcp.dc._msdcs.<domain>`, `_kerberos._tcp.<domain>`, `_gc._tcp`).
  A domain that will not resolve is a real result — report it and stop.
- The domain itself: functional level, naming context, NetBIOS name, the site
  topology if DNS gives it up, and whether a zone transfer is permitted.
- The unauthenticated surface: does the DC accept a null SMB or SAMR session?
  Does anonymous LDAP return objects? Is SMB signing required on each host?
  These three answers decide which vectors the next wave can even attempt.
- The password policy. If any null session reaches it, this is the single most
  valuable fact in the engagement — the real lockout threshold, not the one we
  were told. Report it verbatim.
- Users and groups, to the extent they are readable without a credential, and
  the naming convention the domain uses (first.last, flast, firstl).
- ADCS: which certificate authorities are published, where they answer, and
  whether web enrollment is reachable.
- Hypotheses: which of the four families — credentials, ADCS, ACL and
  delegation, coercion and relay — this domain is actually weak in, and what
  each one would need to proceed.

RULES FOR THIS PHASE
- Do NOT submit a password to anything. Not one. Recon is unauthenticated, and
  a guessed password here spends the campaign's entire attempt budget before a
  brief has been written.
- Do NOT modify the directory: no ACL changes, no password resets, no template
  edits, no writing through a relay.
- Prefer the offline path everywhere it exists. Roasting and cracking happens
  on this host, not against the DC.
- Treat a lockout error (`STATUS_ACCOUNT_LOCKED_OUT`, Kerberos `err: 1909`) as
  a stop signal: report it immediately and change nothing further.

Be concrete. A hypothesis that names a DC and an attribute is worth ten that
name a category. If the domain does not answer at all, say so plainly and report
what you tried — that is a real result, not a failure.

Finish with a short written brief: what the domain is, what you could reach
without a credential, what you could not, the password policy if you found it,
and your ranked hypotheses for where the weaknesses are.

{contract}
"""

PLANNER_PROMPT = """\
You are directing a security engagement against a declared, authorised Active
Directory domain. Your job is to decide what the NEXT wave of {wave_size} agents
should each do.

DOMAIN: {target}

WHAT IS KNOWN
-------------
{recon}
{recon_note}
WHAT HAS ALREADY BEEN FOUND
---------------------------
{findings}

WAVE HISTORY
------------
{waves}

You have READ-ONLY tools. Use them to check a hypothesis before you brief an
agent on it — enough to know the host answers and the brief is not a guess — but
do not attempt the work itself. That is what the agents are for.

THE ONE RULE THAT OVERRIDES THE OTHERS
- Never write a brief that guesses passwords. No spraying, no brute force, no
  wordlist against a live account, no "try these ten common passwords". This
  domain locks an account after __LOCKOUT__ bad passwords, so the whole campaign
  is permitted __BUDGET__ credential attempts in total — and a hard circuit
  breaker refuses every credential-bearing command once they are spent. A brief
  built around guessing spends them and is then refused mid-wave. Offline
  cracking of a hash we already hold is always the better path and is always
  allowed.
- Never write a brief that modifies the domain. No ACL changes, no password
  resets, no ADCS template edits, no `--delete`, no relay that writes. The
  interlock refuses these outright, so a brief built on one wastes an agent.
- If any worker sees `STATUS_ACCOUNT_LOCKED_OUT`, `err: 1909`, or a lockout
  notice in an output, that is a stop signal for the entire wave: brief nobody
  to retry that account, and say so in the finding.

Rules:
- Return AT MOST {wave_size} workers. Fewer is better than padding.
- Every brief must be INDEPENDENT: no agent may depend on another's output,
  because they all run at the same time and cannot talk to each other.
- Wave 1 is the unauthenticated surface only. If the findings show no foothold
  yet, every brief must be reachable with no credential at all.
- Once a foothold exists, the highest-value work is the path it opens: the ACL
  edge it unlocks, the host it reaches, the template it can enroll against.
  These are the families worth covering, roughly in the order they pay:
  credential material, ADCS, ACL and delegation abuse, then coercion and relay.
- Do not repeat work already done. Read the findings and the wave history and
  aim at what is still open. If an earlier finding was marked low-confidence or
  a worker failed, a brief that re-tests it properly is worth a slot.
- Each worker gets a short lowercase `name` (e.g. "asrep-roast") and a `brief`
  that is a complete, self-contained instruction. The worker sees the brief and
  nothing else — it must say what to test, against which host, with which tool,
  and how to prove it.
- `owns` is the host, account or vector that brief is responsible for. Two
  workers must not claim the same one; the wave is deduplicated on this.

Reply with ONE JSON object and nothing else:

{{"summary": "one line on how you are reading the domain now",
  "workers": [{{"name": "short-name",
               "brief": "the full instruction for this agent",
               "owns": ["the host, account or vector it owns"]}}]}}

{contract}
"""

FINDING_CONTRACT = """\
REPORTING
---------
Report each confirmed weakness TWICE, and the first time is the one that
matters:

1. THE MOMENT YOU CONFIRM IT — call the ``report_finding`` tool. Do this as soon
   as you have the evidence in hand, in the same turn you got it, before you go
   on to the next thing. This is a live engagement: the operator is watching a
   tracker that fills in as you work. A finding you sit on until the end of your
   turn is a finding the operator spends that whole time not knowing about.

   Report a weakness you have *indicated* as well as one you have *proven*, but
   never blur the two — see the evidence rule below.

2. AGAIN IN YOUR FINAL MESSAGE — end it with ONE JSON object on its own lines,
   wrapped in a ```json fence:

```json
{"findings": [{"title": "short name for the weakness",
               "severity": "critical|high|medium|low|info",
               "asset": "the host, account or object affected",
               "summary": "what the weakness is, in two or three sentences",
               "evidence": "the exact command and the exact output that shows it",
               "remediation": "how to fix it"}]}
```

Rules for both:
- One entry per distinct weakness, and the same weakness is the same weakness in
  both places. The two are reconciled by title and asset, so repeating yourself
  costs nothing; reporting the same weakness under two names inflates the count.
- If you found nothing, call no tool and omit the block entirely (or send
  `{"findings": []}`) — do not invent a finding to fill either one.
- `severity` must be one of the five words above. Judge it honestly against what
  the weakness actually grants: a readable attribute with no path from it is
  `low`; a path from an account we hold to Domain Admin is `critical`.
- `evidence` must be something you actually observed — **the exact command and
  the exact output**, not a summary of them and not what you expect them to
  return. Say plainly whether the technique was PROVEN (you did it and have the
  result) or only INDICATED (the configuration allows it and you did not carry
  it through). A finding that does not say which is a finding nobody can act on,
  and will be discarded. ``report_finding`` refuses a report with no evidence,
  which is deliberate.
- Never submit a password to produce a finding. If a weakness can only be shown
  by guessing a credential, report the configuration that makes the guess
  worthwhile and say the exploitation was not carried out.
- Put nothing after the final JSON block. Anything you want to say, say before it.
"""

SUMMARY_PROMPT = """\
An Active Directory engagement against a domain has been running as a sequence
of waves of parallel agents. Write the closing report.

DOMAIN: {target}
WAVES RUN: {waves}

RECON
-----
{recon}

EVERY FINDING, IN THE ORDER THEY WERE CONFIRMED
-----------------------------------------------
{findings}

Write the report the operator will actually read — the one that goes to the
client. It must cover, in this order:

1. **The domain's exposure, in plain language.** What an unauthenticated
   attacker on the network can see and do, before any credential is involved.
   This is the section a non-specialist reads, and it is the one that decides
   whether the engagement is taken seriously.
2. **What was tested** — the ground covered, wave by wave, naming the hosts and
   the techniques. Not a list of agent names.
3. **What was found** — each confirmed weakness, worst first, with its severity,
   the evidence that proves it, and — explicitly — whether the attack path was
   **proven** or only **indicated**. A proven path to Domain Admin and an
   indicated one are different findings and must not read the same.
4. **What is worth checking next** — the specific, still-open leads: the ACL
   edge, the template, the host. Concrete enough that someone could pick it up.
5. **What was NOT done, and why.** The attempts the interlock refused, the
   vectors skipped for missing tooling, and any credential guessing that was
   deliberately avoided. This section is not optional. A client reading a
   domain report needs to know exactly where the engagement stopped, because
   everything past that line is unknown rather than clean.
6. **What is not worth the time** — the avenues checked and found clean.

Be honest about coverage. If a whole family was never tested, say so rather than
letting silence read as "clean". Do not pad, and do not overstate: an indicated
path presented as a proven one is the single worst failure this report can have.
"""


def build_recon_prompt(probes: list[ToolProbe]) -> str:
    """The recon prompt with this host's tooling baked in.

    Substituted here rather than at format time because the engine's contract is
    ``.format(target=…, contract=…)`` — it knows nothing about tools, and giving
    it a prompt it cannot format would break the seam for every other doctrine.
    """
    return _RECON_TEMPLATE.replace("{tools}", render_tools(probes))


# ---------------------------------------------------------------------------
# The doctrine
# ---------------------------------------------------------------------------

#: The default lockout threshold when the operator does not state one. Five is
#: the most conservative value in common use — the Windows legacy default, and
#: the number a client is most likely to be running if nobody has looked. It is
#: deliberately not the modern default of ten: being wrong on the low side costs
#: a re-run, being wrong on the high side costs a locked-out workforce.
DEFAULT_LOCKOUT = 5

#: Commands per second the whole wave may sustain, and how many may go at once.
#: A domain controller answers fast, and the danger is not volume against the DC
#: — it is the account. One per second sustained is slow enough that a mistake is
#: visible in the transcript before it becomes a lockout.
DEFAULT_RATE = 1.0
DEFAULT_BURST = 4


def build_planner_prompt(lockout: int = DEFAULT_LOCKOUT) -> str:
    """``PLANNER_PROMPT`` with the operator's lockout arithmetic baked in.

    The planner is told the budget in numbers, not adjectives: "be careful" is
    a wish, "you have four attempts against a domain that locks at five" is a
    constraint a model can actually plan against. The numbers come from
    ``--lockout``, so they have to be substituted here rather than written into
    the constant.

    Not a ``str.format`` field like ``{target}``: the engine calls
    ``.format(target=…, wave_size=…, …)`` with a fixed kwarg set, and a prompt
    carrying ``{lockout}`` would raise ``KeyError`` on the first wave of every
    campaign. Sentinels are doubled-underscore names instead, and the test suite
    asserts none of them survives.
    """
    budget = max(1, int(lockout) - 1)
    return (PLANNER_PROMPT
            .replace("__LOCKOUT__", str(int(lockout)))
            .replace("__BUDGET__", str(budget)))


def ad_kind(
    *,
    lockout: int = DEFAULT_LOCKOUT,
    rate: float = DEFAULT_RATE,
    burst: int = DEFAULT_BURST,
    armed: bool = False,
    probes: list[ToolProbe] | None = None,
    on_refuse: Callable[[Verdict, str], None] | None = None,
) -> hunt.CampaignKind:
    """Build the `/ad` doctrine, with its own throttle and attempt budget.

    A *function*, not a constant, because the interlock holds per-campaign
    state: a throttle and an attempt counter belong to one run. A module-level
    kind handed to two campaigns in one session would carry the first campaign's
    spent budget into the second and refuse it early — a conservative failure,
    but a wrong one, and the kind of bug that is invisible until it matters.

    ``probes`` is injectable so the tests do not depend on what happens to be
    installed on the machine running them.
    """
    if probes is None:
        probes = probe_tools()
    throttle = Throttle(rate=rate, burst=burst)
    budget = AttemptBudget(lockout=lockout)
    return hunt.CampaignKind(
        name="ad",
        command="ad",
        dir_prefix="ad-",
        recon_prompt=build_recon_prompt(probes),
        planner_prompt=build_planner_prompt(lockout),
        contract=FINDING_CONTRACT,
        summary_prompt=SUMMARY_PROMPT,
        vectors=AD_VECTORS,
        interlock=make_interlock(throttle, budget, armed=armed,
                                 on_refuse=on_refuse),
    )


#: The doctrine's default instance. Fine for reading the prompts, the vectors
#: and the directory prefix — which is what the tests and the UI's help text
#: want. **A real campaign calls ``ad_kind()``**, so it gets its own throttle
#: and its own budget; see the note on that function for why that matters.
AD_KIND = ad_kind()
