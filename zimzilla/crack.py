"""`/crack` — offline password cracking, and the honest arithmetic it runs on.

Why this command exists
-----------------------
`/ad` dead-ends. Its interlock forbids guessing passwords *online* — correctly,
because the domain locks accounts — and its planner prompt tells agents that
"offline cracking of a hash we already hold is always the better path". There
was no such path. An agent that Kerberoasted a hash had nowhere to take it, and
the hash died in a transcript. This is the destination `/ad` already pointed at,
and it stands on its own for a Responder capture, a NetNTLMv2 from a relay, or
any hash file an operator has.

Why it is not a campaign
------------------------
`/bug-hunt` and `/ad` are wave engines: N agents, all concurrent, independent
briefs. Cracking is the opposite shape — one sequential job that saturates every
core it can get and appends to one potfile. Ten concurrent agents would fight
over the same four cores and corrupt each other's work. So this is a **command
with a playbook**, like `/osint` and `/phish`: it opens a case directory, hands
the agent a briefing as one turn, and the ordinary loop does the rest.

Why the arithmetic is the product
---------------------------------
Measured on this class of host by running hashcat for real, there is no GPU — one
device, the CPU, through PoCL:

    NTLM           (1000)   118.8 MH/s
    NetNTLMv2      (5600)     1.91 MH/s
    Kerberos TGS   (13100)    1.16 MH/s
    Kerberos ASREP (18200)    1.21 MH/s

Kerberos is ~100x slower than NTLM: it is salted and iterated, so each guess
costs real work. rockyou.txt is ~12 seconds per Kerberos hash; rockyou plus
best64 is around ten minutes; the 30,000-rule set is *days*. A tool that just
said "cracking..." would have operators starting runs they cancel. So the
estimate is computed from these rates, printed before anything runs, and the
ladder is bounded by a wall-clock budget the operator sets. `plan()` drops a
stage that does not fit and *names it dropped* rather than quietly overrunning.

The figures are from real runs rather than `hashcat -b`, and the difference is
not academic: the benchmark reports the kernel's best case, and a real run is
1.5x slower on NTLM and **11x slower on NetNTLMv2**. Planning from benchmark
numbers would call a stage affordable when it is not.

No Textual, no colours, no model — the same split as `zimzilla/osint.py`, so the
whole thing is testable without a terminal, a model, or a network.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Hash modes
# ---------------------------------------------------------------------------

#: The hashcat modes this command understands, and what each one is. ``john`` is
#: the equivalent john format, kept so the playbook can offer the fallback when
#: hashcat is absent, and so a report can name the format in a way a reader who
#: uses john recognises.
#:
#: Deliberately a curated list rather than hashcat's full 400+ modes. These are
#: the ones an engagement actually produces: the three Kerberos/Windows families
#: from `/ad` and `/net`, the unix crypt family from a shadow file, the bare
#: digests from a database dump, and WPA from a handshake.
HASH_MODES: dict[int, tuple[str, str]] = {
    0:     ("MD5", "raw-md5"),
    100:   ("SHA1", "raw-sha1"),
    500:   ("md5crypt ($1$)", "md5crypt"),
    1000:  ("NTLM", "nt"),
    1400:  ("SHA256", "raw-sha256"),
    1700:  ("SHA512", "raw-sha512"),
    1800:  ("sha512crypt ($6$)", "sha512crypt"),
    3200:  ("bcrypt ($2a$)", "bcrypt"),
    5500:  ("NetNTLMv1", "netntlm"),
    5600:  ("NetNTLMv2", "netntlmv2"),
    13100: ("Kerberos 5 TGS-REP (Kerberoast)", "krb5tgs"),
    18200: ("Kerberos 5 AS-REP (AS-REP roast)", "krb5asrep"),
    22000: ("WPA-PBKDF2-PMKID+EAPOL", "wpapsk"),
}

#: Signature regexes, checked in order, for when `hashcat --identify` is not
#: available or does not recognise a file. The `$...$` prefixes are hashcat's own
#: on-disk format and are unambiguous; the bare-digest rules are checked last and
#: keyed on the *length* of a hex line, which is the only thing that separates a
#: 32-character NTLM from an MD5.
_SIGNATURES: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"^\$krb5tgs\$"), 13100),
    (re.compile(r"^\$krb5asrep\$"), 18200),
    (re.compile(r"^[^:]+::[^:]+:[0-9a-fA-F]{16}:[0-9a-fA-F]{32}:"), 5600),  # NetNTLMv2
    (re.compile(r"^[^:]+::[^:]+:[0-9a-fA-F]{48}:[0-9a-fA-F]{48}:"), 5500),   # NetNTLMv1
    (re.compile(r"^\$1\$"), 500),
    (re.compile(r"^\$6\$"), 1800),
    (re.compile(r"^\$2[abxy]\$"), 3200),
    (re.compile(r"^WPA\*"), 22000),
)

#: Hex-only line -> mode, by digest length. Ordered longest first so a 64-char
#: line is not mistaken for a prefix of a shorter rule.
_HEX_LENGTHS: tuple[tuple[int, int], ...] = (
    (128, 1700),  # sha512
    (64, 1400),   # sha256
    (40, 100),    # sha1
    (32, 1000),   # NTLM (also MD5's length — NTLM is the far more likely source)
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class Detected:
    """One hash format found in a file, and how much of it is there."""

    mode: int
    name: str
    count: int
    sample: str

    @property
    def john(self) -> str:
        return HASH_MODES.get(self.mode, ("", ""))[1]


def _line_mode(line: str) -> int | None:
    """The hashcat mode a single line looks like, or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for pattern, mode in _SIGNATURES:
        if pattern.search(line):
            return mode
    # A bare hex digest, or the digest inside a dump line. `user:hash` and
    # `user:rid:lm:nt:::` are both common, and a pwdump line *ends* in colons —
    # so the tail after the last one is empty and checking only the tail finds
    # nothing. Every colon-separated field is tried instead, which catches the
    # bare digest, the `user:hash` pair and the pwdump line alike.
    for field in line.split(":"):
        if _HEX_RE.match(field):
            for length, mode in _HEX_LENGTHS:
                if len(field) == length:
                    return mode
    return None


def identify_text(text: str) -> list[Detected]:
    """Every hash format present in *text*, with counts, most-prevalent first.

    Pure, so the whole identification surface is testable by writing a file's
    contents down — no hashcat, no disk. `identify` wraps this with the
    `--identify` call for files whose format is not one of the signatures.
    """
    counts: dict[int, int] = {}
    samples: dict[int, str] = {}
    for line in text.splitlines():
        mode = _line_mode(line)
        if mode is None:
            continue
        counts[mode] = counts.get(mode, 0) + 1
        # A short, non-secret excerpt: enough to recognise the format in a
        # banner, never the whole hash. The `$...$` prefix is the tell.
        samples.setdefault(mode, line.strip()[:60])
    return [
        Detected(mode=m, name=HASH_MODES.get(m, ("unknown", ""))[0],
                 count=c, sample=samples[m])
        for m, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def identify(path: Path) -> list[Detected]:
    """Identify every hash format in a file, or in every file under a directory.

    Signature matching first, because it is free and it is right for every format
    an engagement actually produces. `hashcat --identify` is consulted only when
    the signatures found nothing, since it is the slower path and its output is a
    table to parse — but it is the honest fallback for a format this module has
    never seen, and refusing to identify a hash because it is unfamiliar would be
    the wrong way round.
    """
    path = Path(path)
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() \
        else [path]

    found: dict[int, Detected] = {}
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for d in identify_text(text):
            prior = found.get(d.mode)
            if prior is None:
                found[d.mode] = d
            else:
                found[d.mode] = Detected(d.mode, d.name,
                                         prior.count + d.count, prior.sample)

    if found:
        return sorted(found.values(), key=lambda d: (-d.count, d.mode))

    # Nothing matched a signature. Ask hashcat, which knows formats this module
    # does not — and whose `--identify` output names the mode directly.
    for f in files:
        mode = _identify_via_hashcat(f)
        if mode is not None:
            found[mode] = Detected(
                mode=mode, name=HASH_MODES.get(mode, (f"mode {mode}", ""))[0],
                count=1, sample="(identified by hashcat)")
            break
    return sorted(found.values(), key=lambda d: (-d.count, d.mode))


def _identify_via_hashcat(path: Path) -> int | None:
    """`hashcat --identify <file>`, reduced to the first mode it names."""
    exe = shutil.which("hashcat")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "--identify", str(path), "--quiet"],
            capture_output=True, text=True, timeout=60, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # The table rows look like `  13100 | Kerberos 5, etype 23, TGS-REP | ...`.
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"\s*(\d+)\s*\|", line)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# The ladder, and the arithmetic that bounds it
# ---------------------------------------------------------------------------

#: Measured throughput per mode, in candidates per second, from a **real run** on
#: this host — a 4-core CPU with no GPU, through PoCL.
#:
#: Measured from actual `hashcat -m N -a 3 <hash> <mask> --runtime` runs, not from
#: `hashcat -b`. That distinction matters more than it looks: the benchmark
#: reports the kernel's best case, and a real run against a specific salt with
#: the default tuning is consistently slower — by 1.5x on NTLM and by **11x on
#: NetNTLMv2**. Planning from benchmark figures would tell an operator a stage is
#: affordable when it is not, which is the one way this command actively
#: misleads. So the table is real-run figures, and it is deliberately pessimistic.
#:
#: A mode not listed falls back to `FALLBACK_RATE`, because over-estimating the
#: time is safe and under-estimating is what makes an operator start a run they
#: cannot finish.
RATES: dict[int, float] = {
    0:     102e6,
    100:   83e6,
    500:   2.5e5,
    1000:  119e6,
    1400:  43e6,
    1700:  13e6,
    1800:  1.06e3,
    3200:  1.0e3,
    5500:  115e6,
    5600:  1.9e6,
    13100: 1.16e6,
    18200: 1.21e6,
    22000: 2e5,
}

#: The rate used for a mode with no measured figure. Set to the slowest rate this
#: host has actually measured (bcrypt, ~1000 H/s), so an unknown mode is planned
#: conservatively rather than optimistically. Deliberately a literal rather than
#: `min(RATES.values())`: that would silently follow the table if someone added
#: an even slower mode, and the point of this constant is to be pessimistic on
#: purpose.
#:
#: The cost of being pessimistic is real but bounded — an unknown mode gets
#: stages dropped that might have fit — while the cost of being optimistic is an
#: operator starting a run measured in days. So it errs slow.
FALLBACK_RATE = 1.0e3

#: Where wordlists and rules actually live on a Debian/Kali box. Named here
#: rather than inline in the ladder so the probe and the plan agree, and so a
#: host that keeps them elsewhere is one edit away.
ROCKYOU = Path("/usr/share/wordlists/rockyou.txt")
#: john ships best64 and hashcat can read it directly (`-r`), which is the
#: pragmatic choice: hashcat's own rules directory is not installed by every
#: package (it is absent on this host), and john's rule syntax is compatible.
BEST64 = Path("/usr/share/john/rules/best64.rule")
ROCKYOU_30000 = Path("/usr/share/john/rules/rockyou-30000.rule")
JOHN_WORDLIST = Path("/usr/share/wordlists/john.lst")


@dataclass(frozen=True)
class Stage:
    """One rung of the ladder: a method, its cost, and how to run it."""

    name: str
    #: Approximate candidates this stage will try, which is what the estimate is
    #: computed from. Real, not decorative — rockyou is 14.3M lines and best64
    #: multiplies by its rule count.
    candidates: float
    #: The hashcat *options*, minus the mode/hashfile/potfile the runner adds.
    args: tuple[str, ...] = ()
    #: The positional argument that must come **after** the hashfile: the
    #: wordlist for `-a 0`, or the mask for `-a 3`. hashcat parses positional
    #: arguments in order, so a mask emitted before the hashfile is read *as* the
    #: hashfile — which fails with "Separator unmatched" and cracks nothing.
    #: Keeping this separate from `args` is what makes that ordering impossible
    #: to get wrong.
    positional: tuple[str, ...] = ()
    #: Every file this stage needs to exist, for the availability probe. A mask
    #: needs nothing. This is about *availability*, not about the command line —
    #: a rule stage needs both the wordlist and the rule, but only the wordlist
    #: is positional.
    needs: tuple[Path, ...] = ()
    #: A ceiling on this stage's rate, for stages whose bottleneck is not the
    #: hash. A rule stage generates 77 mutated candidates per word, and on a
    #: *fast* hash that generator — not the hashing — is the bottleneck: measured
    #: at 12-24 MH/s across modes, where NTLM's own rate is 119 MH/s. So a rule
    #: stage against a fast hash runs about five times slower than its raw rate
    #: predicts. On a slow hash (Kerberos, ~1.2 MH/s) the hashing still dominates
    #: and the ceiling never binds — which is why it is a `min` rather than a
    #: multiplier.
    ceiling: float | None = None
    #: Only offered with --deep. The stages that cost hours or days belong behind
    #: an explicit flag, not in the default run.
    deep: bool = False

    @property
    def available(self) -> bool:
        return all(p.is_file() for p in self.needs)

    @property
    def missing(self) -> str:
        """The first required file that is not there, for the drop reason."""
        for p in self.needs:
            if not p.is_file():
                return str(p)
        return ""


#: rockyou's real line count, measured. Hard-coded rather than counted at run
#: time because reading a 140MB file to count lines, every time, to produce an
#: estimate that is approximate anyway, is the wrong trade.
#: The rate a rule stage tops out at, whatever the hash. Measured across modes
#: (best64 over rockyou): 24.0 MH/s on NTLM, 18.8 on MD5, 15.5 on SHA1, 11.2 on
#: SHA256, 2.6 on NetNTLMv2, 1.5 on Kerberos. The ceiling is the rule generator
#: — 77 mutated candidates per word — so it binds only where the hash itself is
#: faster than that. Set to the low end of the fast-mode range so a rule stage is
#: planned pessimistically; on slow modes the hash rate is lower anyway and the
#: ceiling never applies.
RULE_CEILING = 12e6

ROCKYOU_LINES = 14_344_392
#: best64's rule count, same reasoning.
BEST64_RULES = 77
#: rockyou-30000's, same.
ROCKYOU_30000_RULES = 30_000

#: The bounded escalation. Cheap and broad first, expensive and narrow last —
#: which is the order that cracks the most passwords for the least time, and the
#: order that makes the budget meaningful: a stage is worth running only if the
#: ones before it have had their turn.
LADDER: tuple[Stage, ...] = (
    Stage(
        name="dictionary",
        candidates=ROCKYOU_LINES,
        args=("-a", "0"),
        positional=(str(ROCKYOU),),
        needs=(ROCKYOU,),
    ),
    Stage(
        name="dictionary + best64 rules",
        candidates=ROCKYOU_LINES * BEST64_RULES,
        args=("-a", "0", "-r", str(BEST64)),
        positional=(str(ROCKYOU),),
        needs=(ROCKYOU, BEST64),
        ceiling=RULE_CEILING,
    ),
    Stage(
        # A corporate mask: an upper, five lowers, two digits — `Summer24`,
        # `Autumn99`. That is 26 * 26**5 * 100 = 30.9 billion candidates, which
        # is ~59s on an NTLM here and ~4.5h on a Kerberos hash — the same mask,
        # 275x apart, which is exactly the mode-dependent difference the estimate
        # exists to surface. Against Kerberos the budget drops it on its own; the
        # ladder does not need to know that, `plan` works it out.
        name="targeted mask (?u?l?l?l?l?l?d?d)",
        candidates=26 * 26**5 * 100,
        args=("-a", "3"),
        positional=("?u?l?l?l?l?l?d?d",),
    ),
    Stage(
        name="dictionary + rockyou-30000 rules",
        candidates=ROCKYOU_LINES * ROCKYOU_30000_RULES,
        args=("-a", "0", "-r", str(ROCKYOU_30000)),
        positional=(str(ROCKYOU),),
        needs=(ROCKYOU, ROCKYOU_30000),
        ceiling=RULE_CEILING,
        deep=True,
    ),
)


def rate_for(mode: int, stage: Stage | None = None) -> float:
    """Candidates per second for *mode*, or the conservative fallback.

    When *stage* is given and declares a `ceiling`, the effective rate is the
    slower of the two: a stage can never run faster than either its hash or its
    candidate generator. That is what keeps a rule stage against a fast hash from
    being planned at five times the speed it will actually run.
    """
    rate = RATES.get(mode, FALLBACK_RATE)
    if stage is not None and stage.ceiling is not None:
        return min(rate, stage.ceiling)
    return rate


#: hashcat's fixed cost per invocation: process start, OpenCL context on the CPU
#: device, kernel build and autotune. Measured at ~2.8s on this host, and it is
#: *per stage* because each stage is a separate hashcat process.
#:
#: It matters more than it looks for the cheap stages: the dictionary against a
#: Kerberoast hash is ~12s of actual work, so ignoring startup would understate
#: the wall time by a fifth. And it is the honest reason a run of four stages
#: costs ten seconds before any cracking happens.
HASHCAT_STARTUP = 2.8


def estimate(mode: int, stage: Stage) -> float:
    """Seconds *stage* is expected to take against a single hash of *mode*.

    Pure arithmetic over the rate table plus hashcat's fixed startup, which is
    the point: the number an operator reads in the banner is a function of a
    measured rate, a stated candidate count and a measured overhead — not a vibe.
    A wrong estimate here is the one thing in this command that actively
    misleads, so it is asserted directly in the tests.

    Startup is included because each stage is its own hashcat process and the
    cost is not negligible at this scale: a stage whose work is seconds is
    dominated by it.
    """
    return HASHCAT_STARTUP + stage.candidates / rate_for(mode, stage)


@dataclass(frozen=True)
class PlannedStage:
    """A stage together with the decision made about it."""

    stage: Stage
    seconds: float
    #: False when the stage was dropped for exceeding the budget. Kept in the
    #: plan rather than omitted, so the report can say "this was NOT run" — a
    #: silent omission reads as "found nothing".
    run: bool
    reason: str = ""


def plan(mode: int, max_time: float, *, deep: bool = False) -> list[PlannedStage]:
    """The stages to run, in order, each marked run or dropped.

    A stage that does not fit the *remaining* budget is dropped, and every later
    stage with it — the ladder is an escalation, so running the mask while
    skipping the dictionary would be a different and worse plan. Dropping rather
    than truncating is deliberate: hashcat's `--runtime` would cut a stage off
    mid-way, and a half-run rule stage is not a smaller version of a run rule
    stage, it is an arbitrary prefix of the wordlist.
    """
    out: list[PlannedStage] = []
    remaining = float(max_time)
    for stage in LADDER:
        secs = estimate(mode, stage)
        if stage.deep and not deep:
            out.append(PlannedStage(stage, secs, False,
                                    "needs --deep (hours to days on this host)"))
            continue
        if not stage.available:
            out.append(PlannedStage(stage, secs, False,
                                    f"missing {stage.missing}"))
            continue
        if secs > remaining:
            out.append(PlannedStage(
                stage, secs, False,
                f"exceeds the {human_time(remaining)} left"))
            continue
        out.append(PlannedStage(stage, secs, True))
        remaining -= secs
    return out


def human_time(seconds: float) -> str:
    """A duration a human reads at a glance."""
    if seconds < 1:
        return "<1s"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

#: hashcat's exit codes, from its own `exit_status_code.txt`. The split that
#: matters is *stopped* versus *failed*: 0-5 all mean hashcat did what it was
#: asked and stopped for a reason, while a negative code is a real failure
#: (self-test, backend, kernel) that must be surfaced rather than read as "found
#: nothing".
#:
#: 4 in particular is not an error and not an edge case — it is what a *bounded*
#: run returns. `--runtime` elapsed, hashcat aborted cleanly, and the budget
#: worked. Treating it as a failure would make every correctly-capped stage look
#: broken, which is the whole reason this table is written out rather than
#: inferred from 0/1.
EXIT_CRACKED = 0     # OK/cracked
EXIT_EXHAUSTED = 1   # exhausted — the keyspace ran out, nothing more to try
EXIT_ABORTED = 2     # aborted
EXIT_CHECKPOINT = 3  # aborted by checkpoint
EXIT_RUNTIME = 4     # aborted by runtime — the budget did its job
EXIT_FINISH = 5      # aborted by finish

#: Every code that means "hashcat ran and stopped", i.e. not a failure. A caller
#: checks `code in STOPPED` rather than enumerating, so a future hashcat that
#: adds a sixth stop code does not silently become an error here.
STOPPED: frozenset[int] = frozenset(
    {EXIT_CRACKED, EXIT_EXHAUSTED, EXIT_ABORTED, EXIT_CHECKPOINT,
     EXIT_RUNTIME, EXIT_FINISH})


def exit_meaning(code: int) -> str:
    """A phrase for a hashcat exit code, for the banner and the report."""
    return {
        EXIT_CRACKED: "cracked",
        EXIT_EXHAUSTED: "exhausted",
        EXIT_ABORTED: "aborted",
        EXIT_CHECKPOINT: "aborted by checkpoint",
        EXIT_RUNTIME: "aborted by runtime (the budget was reached)",
        EXIT_FINISH: "aborted by finish",
    }.get(code, f"hashcat failed (exit {code})")


#: hashcat allows exactly one instance at a time — a second refuses to start with
#: "Already an instance ... running on pid N" and exit 255. Verified on this box,
#: reproducibly. It is worth naming because it is an *operational* hazard rather
#: than a bug: an operator with a hashcat already running (or a `/crack` turn
#: that overlapped another) gets a 255 that would otherwise read as "hashcat is
#: broken", and the fix is to wait, not to debug.
INSTANCE_BUSY = re.compile(r"already an instance", re.IGNORECASE)


def instance_busy(output: str) -> bool:
    """True when hashcat refused to start because another instance holds it."""
    return bool(INSTANCE_BUSY.search(output or ""))


def hashcat_argv(hashfile: Path, mode: int, stage: Stage, potfile: Path,
                 runtime: float, hashcat: str = "hashcat") -> list[str]:
    """The command line for one stage. Separate from `run_stage` so it is
    assertable without running anything — the argument order is exactly where a
    rule file gets mistaken for a wordlist, and where a mask ends up in the
    hashfile's position.

    hashcat's positional order is ``<hashfile> [wordlist|mask]``. Options may
    appear anywhere; the two positionals may not. So the stage's `args` go before
    the hashfile and its `positional` after — the one arrangement that cannot
    misparse.
    """
    return [hashcat, "-m", str(mode), *stage.args, str(hashfile),
            *stage.positional,
            "--potfile-path", str(potfile), "--runtime", str(int(runtime)),
            "--quiet", "--force"]


def run_stage(
    hashfile: Path,
    mode: int,
    stage: Stage,
    potfile: Path,
    *,
    runtime: float,
    workdir: Path | None = None,
    hashcat: str = "hashcat",
) -> tuple[int, str]:
    """Run one stage against *hashfile*. Returns ``(exit_code, combined_output)``.

    ``--runtime`` bounds the stage in wall-clock seconds, which is what makes the
    budget real rather than advisory: a stage that turns out slower than its
    estimate is cut off by hashcat itself rather than running on. ``--status-json``
    is not used here — this is the synchronous path the tests drive — but the
    playbook asks the agent to add it so a long run reports progress.

    The potfile is passed explicitly on every call. Without it hashcat writes to
    its own default, which is shared with the operator's other hashcat work and
    is not this campaign's to touch.
    """
    argv = hashcat_argv(hashfile, mode, stage, potfile, runtime, hashcat)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=runtime + 60,
            cwd=str(workdir) if workdir else None, errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return EXIT_ABORTED, out + err
    except OSError as e:
        return 255, f"could not run hashcat: {e}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def recovered(hashfile: Path, mode: int, potfile: Path,
              hashcat: str = "hashcat") -> list[tuple[str, str]]:
    """``[(hash, plaintext)]`` for every hash in *hashfile* that is cracked.

    Read from `hashcat --show`, which is the only correct way to ask — the
    potfile holds hashes from *every* run that has ever used it, so reading it
    directly would attribute another run's cracks to this one.
    """
    try:
        proc = subprocess.run(
            [hashcat, "-m", str(mode), str(hashfile),
             "--potfile-path", str(potfile), "--show", "--quiet"],
            capture_output=True, text=True, timeout=120, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        # `--show` prints `hash:plaintext`, and the hash itself contains colons,
        # so the split is on the LAST one.
        if ":" in line:
            h, _, pw = line.rpartition(":")
            out.append((h.strip(), pw))
    return out


# ---------------------------------------------------------------------------
# Case directory
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def case_slug(target) -> str:
    """A filesystem-safe, recognisable label."""
    s = _SLUG_RE.sub("-", str(target).strip()).strip("-.")
    return (s[:64] or "hashes").lower()


def case_dir(cfg, target) -> Path:
    """Create and return this run's directory.

    ``<state_dir>/crack/<slug>-<stamp>/`` — beside the osint cases and the hunt
    campaigns, under state_dir so evidence lands with the sessions rather than in
    whatever repo the operator happens to be in.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = Path(cfg.state_dir) / "crack" / f"{case_slug(target)}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def potfile(cfg) -> Path:
    """The potfile every `/crack` run shares.

    Shared across runs on purpose: cracking is multi-session. A hash that did not
    fall in ten minutes may fall tomorrow with a bigger rule, and a run that
    redid the work it already did would be wasting the operator's time. One
    potfile per state_dir means a re-run picks up exactly where the last left off
    — and keeping it here rather than at hashcat's own default path means
    `/crack` never writes to the operator's global hashcat state.
    """
    d = Path(cfg.state_dir) / "crack"
    d.mkdir(parents=True, exist_ok=True)
    return d / "crack.potfile"


def reports_dir(cfg) -> Path:
    """Where finished reports are kept, beside the osint and hunt ones."""
    return Path(cfg.state_dir) / "reports"


def finalise_case(cfg, case: Path) -> Path | None:
    """Archive the case's report into the flat report store.

    Unlike `/osint`, the case directory is **not** deleted — the hash copy and
    the potfile are the evidence, and a potfile that vanished with the case would
    make the next run redo work. Only the report is copied out, which is what
    outlives the case.

    Returns None when there is no report: a run that failed or was interrupted
    keeps everything for inspection, exactly as osint's contract has it.
    """
    report = case / "report.md"
    if not report.is_file():
        candidates = [p for p in sorted(case.glob("*.md"))
                      if "report" in p.name.lower()]
        report = candidates[0] if candidates else None
    if report is None or not report.is_file():
        return None
    dest_dir = reports_dir(cfg)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{case.name}.md"
        shutil.copy2(report, dest)
    except OSError:
        return None
    return dest


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

def validate_path(raw: str) -> tuple[Path | None, str]:
    """Resolve the operator's argument to an existing file or directory.

    A path, not a hash and not a sentence. Typing the hash inline is a tempting
    mistake — it is what every hashcat tutorial shows — but the hash then lives
    in the shell history and the transcript, and a run needs it on disk anyway so
    a second attempt can reuse it. So the argument is a path and the error says
    so.
    """
    if not raw or not raw.strip():
        return None, "no path given — point /crack at a hash file or directory"
    p = Path(raw.strip()).expanduser()
    if not p.exists():
        if re.match(r"^\$?\w+\$", raw.strip()) or ":" in raw.strip():
            return None, (
                f"{raw.strip()[:40]}… looks like a hash, not a path. Write it to "
                f"a file first — it stays out of your shell history and a second "
                f"run can reuse it")
        return None, f"no such file or directory: {p}"
    return p, ""


# ---------------------------------------------------------------------------
# The playbook
# ---------------------------------------------------------------------------

PLAYBOOK = """\
Crack the password hashes in {source} and report what they reveal.

CASE DIRECTORY: {case_dir}
HASH COPY:     {hash_copy}
POTFILE:       {potfile}
BUDGET:        {budget}
FORMATS FOUND: {formats}

This host has NO GPU. Every figure below is CPU throughput, measured from real
runs, and it is the number that decides what is worth trying. Kerberos hashes
(13100, 18200) run at about 1.2 MH/s here — roughly 100x slower than NTLM — so a
rule set that takes seconds against an NTLM takes minutes against a Kerberoast,
and the biggest community rule sets take days. Do not start something you cannot
finish.

METHOD — work the ladder in order, stopping when the budget is spent:

1. Identify and confirm the mode before running anything:
     hashcat --identify {hash_copy}
   The mode numbers matter — 13100 is Kerberoast, 18200 is AS-REP, 5600 is
   NetNTLMv2, 1000 is NTLM. Using the wrong one wastes the whole budget.

2. Stage 1 — the dictionary. Fast, broad, and it cracks more real passwords than
   anything else:
     hashcat -m <mode> -a 0 {hash_copy} /usr/share/wordlists/rockyou.txt \\
       --potfile-path {potfile} --runtime <seconds> --status --status-json

3. Stage 2 — the same dictionary with rules, which is where corporate passwords
   actually fall (`Password1!`, `Summer2024`):
     hashcat -m <mode> -a 0 {hash_copy} /usr/share/wordlists/rockyou.txt \\
       -r /usr/share/john/rules/best64.rule \\
       --potfile-path {potfile} --runtime <seconds>

4. Stage 3 — only if the budget allows and the mode is fast enough: a targeted
   mask for the organisation's naming convention (`?u?l?l?l?l?l?d?d` matches
   `Summer24`). Check the estimate before starting. It is 30.9 billion
   candidates: ~4.3 minutes against an NTLM here, and ~7 HOURS against a
   Kerberos hash. The same mask. Do not start it on a Kerberos hash without
   meaning to.

Always pass `--potfile-path {potfile}`. It is shared between runs deliberately:
a hash that did not fall today may fall tomorrow with a bigger rule, and redoing
work already done wastes the operator's time. Always pass `--runtime` so a stage
cannot overrun the budget. Read progress with `--status-json` rather than
polling, and check results with:

     hashcat -m <mode> {hash_copy} --potfile-path {potfile} --show

ONE INSTANCE AT A TIME. hashcat refuses to start a second copy — it exits 255
with "Already an instance ... running on pid N". If you see that, another hashcat
(possibly a `/crack` turn, possibly the operator's own) is running: **wait for it
to finish and retry**, do not treat it as a failure and do not kill it. Check
with `pgrep -a hashcat` before starting a long stage. Exit code 4 is not an error
either — it means `--runtime` elapsed and the stage stopped at its budget, which
is the bound working.

ANALYSIS — this is the part that is worth more than the cracks
-------------------------------------------------------------
A recovered password is not the finding. The finding is what it says about the
organisation's password policy, and that is what the report has to argue:

- **Crack rate.** How many of how many fell to a plain dictionary? A high rate
  means the policy permits common passwords and the estate is one breach-dump
  away from compromise.
- **Patterns.** If several passwords are `Season+Year` or `Word+digits`, the
  policy is being satisfied in letter and defeated in spirit — say so, with the
  examples, and say what a length-based policy would have done instead.
- **Reuse and relationship.** A service account whose password is the company
  name plus a year is a different finding from a user's `Password1`. Name which
  accounts fell and what each one grants.
- **What did NOT crack, and what that means.** A hash that survived a full
  dictionary and a rule set is evidence the policy is working somewhere. Report
  it as a positive, not as a gap.

Write the report to `{case_dir}/report.md`:

    # Password cracking — <source>
    ## Headline          — the crack rate and the single most important pattern
    ## What was cracked  — per hash: the account it belongs to, the plaintext,
                            and the stage that recovered it
    ## What it means     — the policy finding, argued from the patterns above
    ## What did not crack— the survivors, and what their survival shows
    ## Method            — modes, wordlists, rules, and the time each stage took

Report the plaintexts. This is an authorised engagement and the recovered
credentials are the deliverable; redacting them makes the report useless to the
operator who has to remediate. But be precise about the account each one belongs
to — a cracked service account and a cracked helpdesk account are different
findings and must not be merged.

Authorised use only: hashes you are entitled to test — your own estate, a
consented audit, or a declared engagement.
"""


def build_prompt(cfg, source: Path, case: Path, detected: list[Detected],
                 budget: float) -> str:
    """The turn the agent actually runs.

    The formats and the budget are interpolated rather than left for the agent to
    work out, because both are facts this module already knows and asking a model
    to rediscover them is asking it to guess. The mode list in particular: an
    agent that picks 1000 for a Kerberoast hash burns the entire budget on a mode
    that cannot match.
    """
    if detected:
        formats = ", ".join(
            f"{d.name} (mode {d.mode}, {d.count} hash{'es' if d.count != 1 else ''})"
            for d in detected)
    else:
        formats = ("(none recognised by signature — run `hashcat --identify` on "
                   "the hash copy to establish the mode)")
    return PLAYBOOK.format(
        source=source,
        case_dir=case,
        hash_copy=case / "hashes.txt",
        potfile=potfile(cfg),
        budget=human_time(budget),
        formats=formats,
    )
