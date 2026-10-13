"""`/crack` regression suite: identification, arithmetic, the ladder, the real crack.

Run:  python tests/test_crack.py   (from an activated venv)

Offline and model-free, like `test_ad.py` and `test_osint.py`. Four layers:

* **identification** — signature matching over written-down text, so a format is
  recognised (or not) by a pure function of the bytes;
* **the arithmetic** — `estimate` is a division, and it is the one number in this
  command that actively misleads an operator if it is wrong, so it is asserted
  against the module's own tables rather than trusted;
* **the ladder** — that a stage over budget is *dropped and named*, never run
  past the cap, and that a dropped stage does not consume the budget of the one
  after it;
* **the real crack** — hashcat's own example hash for mode 13100, with a known
  plaintext, run through the actual `run_stage`/`recovered` path. This is the
  test that proves the feature works rather than that it formats strings.

Two invariants worth stating, because they are the ones a refactor would break:

* **A rule file is never passed as a wordlist.** `hashcat -a 0 <hash> <wordlist>
  -r <rule>` — the positional argument is the dictionary. Getting this wrong
  produces a hashcat invocation that *looks* plausible and cracks nothing.
* **The scope guard does not block cracking.** A local hashcat run against a file
  whose *path* contains a deny-listed domain must pass, while a real network
  command against that domain is still blocked.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import crack as crack_mod  # noqa: E402
from zimzilla import scope as scope_mod  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


#: hashcat's own example hash for mode 13100, whose plaintext is `hashcat`. Used
#: by the end-to-end test: it is a real Kerberoast hash, it ships with hashcat,
#: and its plaintext is known, so the test asserts a crack rather than a code
#: path.
TGS_EXAMPLE = (
    "$krb5tgs$23$*user$realm$test/spn*$b548e10f5694ae018d7ad63c257af7dc$"
    "35e8e45658860bc31a859b41a08989265f4ef8afd75652ab4d7a30ef151bf6350d879"
    "ae189a8cb769e01fa573c6315232b37e4bcad9105520640a781e5fd85c09615e78267e"
    "494f433f067cc6958200a82f70627ce0eebc2ac445729c2a8a0255dc3ede2c4973d2d9"
    "3ac8c1a56b26444df300cb93045d05ff2326affaa3ae97f5cd866c14b78a459f0933a5"
    "50e0b6507bf8af27c2391ef69fbdd649dd059a4b9ae2440edd96c82479645ccdb06bae"
    "0eead3b7f639178a90cf24d9a"
)
TGS_PLAINTEXT = "hashcat"


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------

def test_identify_text() -> None:
    # Every signature the module claims to know, one line each. A format that
    # silently stops being recognised is a format the banner omits and the agent
    # never learns about.
    cases = [
        ("$krb5tgs$23$*svc$CORP.LOCAL$spn*$aabb$ccdd", 13100),
        ("$krb5asrep$23$user@CORP.LOCAL:deadbeef$aabbccdd", 18200),
        ("user::CORP:1122334455667788:0123456789abcdef0123456789abcdef:"
         "0101000000000000", 5600),
        ("$1$salt$abcdefghijklmnopqrstuv", 500),
        ("$6$salt$abcdefghijklmnopqrstuv", 1800),
        ("$2a$10$abcdefghijklmnopqrstuv", 3200),
        ("WPA*01*aabbccdd*112233445566*", 22000),
    ]
    for line, want in cases:
        got = crack_mod.identify_text(line)
        check(f"identify: {line[:28]!r} -> {want}",
              bool(got) and got[0].mode == want,
              str([d.mode for d in got]))

    # Bare hex digests, by length. 32 is NTLM and not MD5 — both are 32 chars and
    # NTLM is overwhelmingly the more likely source in an engagement, so the
    # module resolves the ambiguity that way and the test pins it.
    for digest, want in [
        ("a" * 32, 1000),
        ("a" * 40, 100),
        ("a" * 64, 1400),
        ("a" * 128, 1700),
    ]:
        got = crack_mod.identify_text(digest)
        check(f"identify: {len(digest)}-hex -> {want}",
              bool(got) and got[0].mode == want,
              str([d.mode for d in got]))

    # A pwdump line is `user:rid:lm:nt:::` — the hash to crack is the NTLM tail,
    # not the whole line, so the length test has to look past the colons.
    pwdump = f"Administrator:500:aad3b435b51404eeaad3b435b51404ee:{'b' * 32}:::"
    got = crack_mod.identify_text(pwdump)
    check("identify: a pwdump line yields the NTLM in it",
          bool(got) and got[0].mode == 1000, str([d.mode for d in got]))

    # Junk and comments are not hashes. A file of prose must identify as nothing
    # rather than as the first thing that happens to be 32 characters long.
    for junk in ["", "# a comment", "not a hash at all", "hello world"]:
        check(f"identify: {junk!r} is not a hash",
              crack_mod.identify_text(junk) == [])


def test_identify_mixed() -> None:
    """A mixed file yields *every* format in it, most prevalent first."""
    text = "\n".join([
        TGS_EXAMPLE,
        "$krb5asrep$23$user@CORP:aa$bb",
        "a" * 32,
        "b" * 32,
        "c" * 32,
        "",
        "# comment",
    ])
    got = crack_mod.identify_text(text)
    modes = {d.mode: d.count for d in got}
    check("identify: a mixed file finds all three formats",
          set(modes) == {13100, 18200, 1000}, str(modes))
    check("identify: counts are per-format",
          modes.get(1000) == 3 and modes.get(13100) == 1, str(modes))
    check("identify: the most prevalent format is first",
          got[0].mode == 1000, f"first={got[0].mode}")
    check("identify: the sample is an excerpt, not the whole hash",
          all(len(d.sample) <= 60 for d in got))
    check("identify: the name is the human one",
          crack_mod.HASH_MODES[13100][0] in [d.name for d in got])


def test_identify_path(tmp: Path) -> None:
    """`identify` on a file and on a directory, merging counts across files."""
    d = tmp / "hashes"
    d.mkdir()
    (d / "tgs.txt").write_text(TGS_EXAMPLE + "\n")
    (d / "ntlm.txt").write_text("a" * 32 + "\n" + "b" * 32 + "\n")
    got = crack_mod.identify(d)
    modes = {x.mode: x.count for x in got}
    check("identify: a directory merges counts across its files",
          modes == {1000: 2, 13100: 1}, str(modes))
    single = crack_mod.identify(d / "tgs.txt")
    check("identify: a single file yields just its format",
          [x.mode for x in single] == [13100], str([x.mode for x in single]))


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def test_estimate() -> None:
    """`estimate` is startup + candidates / effective rate, and both terms matter.

    The point of asserting the arithmetic is that the result is the product: an
    operator reads the duration and decides whether to start a run. Two terms are
    easy to get wrong and both were, during development:

    * **startup** — each stage is its own hashcat process, ~2.8s of context setup
      and autotune. Omitting it understates the cheap stages badly: the rockyou
      dictionary against a Kerberoast hash is ~12s of work, so startup is a fifth
      of the wall time.
    * **the rule ceiling** — a rule stage generates 77 candidates per word, and on
      a *fast* hash that generator is the bottleneck, so the stage runs at 12-24
      MH/s where NTLM's own rate is 119. Ignoring it plans a best64 run at five
      times the speed it will achieve.
    """
    for stage in crack_mod.LADDER:
        for mode in (1000, 13100):
            want = (crack_mod.HASHCAT_STARTUP
                    + stage.candidates / crack_mod.rate_for(mode, stage))
            got = crack_mod.estimate(mode, stage)
            check(f"estimate: {stage.name[:24]} @ {mode}",
                  abs(got - want) < 1e-9, f"{got} vs {want}")

    # Startup is a real, non-zero term — not a rounding detail.
    check("estimate: startup is included and non-zero",
          crack_mod.HASHCAT_STARTUP > 0, str(crack_mod.HASHCAT_STARTUP))
    tiny = crack_mod.Stage(name="tiny", candidates=1)
    check("estimate: a one-candidate stage still costs the startup",
          crack_mod.estimate(1000, tiny) >= crack_mod.HASHCAT_STARTUP,
          str(crack_mod.estimate(1000, tiny)))

    # The rule ceiling binds on a fast hash and not on a slow one. This is the
    # asymmetry that makes it a `min` rather than a flat multiplier.
    rule = [s for s in crack_mod.LADDER if "best64" in s.name][0]
    check("estimate: the rule ceiling is declared on the rule stages",
          rule.ceiling == crack_mod.RULE_CEILING, str(rule.ceiling))
    check("estimate: the ceiling binds on NTLM",
          crack_mod.rate_for(1000, rule) == crack_mod.RULE_CEILING,
          f"{crack_mod.rate_for(1000, rule):,}")
    check("estimate: the ceiling does NOT bind on Kerberos",
          crack_mod.rate_for(13100, rule) == crack_mod.RATES[13100],
          f"{crack_mod.rate_for(13100, rule):,}")
    check("estimate: no ceiling means the plain mode rate",
          crack_mod.rate_for(1000, crack_mod.LADDER[0]) == crack_mod.RATES[1000])

    # An unknown mode falls back to the conservative rate, not to a fast one.
    check("estimate: an unknown mode uses the fallback rate",
          crack_mod.rate_for(99999) == crack_mod.FALLBACK_RATE)
    check("estimate: the fallback is no faster than any measured mode",
          crack_mod.FALLBACK_RATE <= min(crack_mod.RATES.values()),
          f"{crack_mod.FALLBACK_RATE} vs {min(crack_mod.RATES.values())}")

    # The headline claim, asserted rather than merely written in prose: Kerberos
    # is two orders of magnitude slower than NTLM on this host, which is the
    # whole reason the estimate exists.
    ratio = crack_mod.rate_for(1000) / crack_mod.rate_for(13100)
    check("estimate: Kerberos is >100x slower than NTLM here", ratio > 100,
          f"{ratio:.0f}x")

    # rockyou against a Kerberoast is seconds, not minutes. This is the number
    # that makes stage 1 always affordable and stage 2 a decision.
    stage1 = crack_mod.estimate(13100, crack_mod.LADDER[0])
    check("estimate: rockyou on a Kerberoast is under a minute",
          stage1 < 60, f"{stage1:.1f}s")


def test_human_time() -> None:
    for secs, want in [(0.5, "<1s"), (8, "8s"), (600, "10m"),
                       (7200, "2.0h"), (172800, "2.0d")]:
        check(f"human_time: {secs} -> {want}",
              crack_mod.human_time(secs) == want, crack_mod.human_time(secs))


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def test_plan_budget() -> None:
    """The budget is a real bound, and a dropped stage is named as dropped."""
    # A generous budget runs every default stage and no more. The count is
    # asserted rather than written as a literal so adding a rung to the ladder
    # does not fail this for the wrong reason — what matters is that the deep
    # one is the only one held back.
    full = crack_mod.plan(1000, 86400)
    default = [s for s in crack_mod.LADDER if not s.deep]
    check("plan: a generous budget runs every default stage",
          [p.run for p in full] == [not s.deep for s in crack_mod.LADDER],
          str([(p.stage.name[:12], p.run) for p in full]))
    check("plan: the plan covers the whole ladder", len(full) == len(crack_mod.LADDER),
          f"{len(full)} vs {len(crack_mod.LADDER)}")
    check("plan: there is more than one default stage", len(default) > 1,
          str(len(default)))
    check("plan: the --deep stage is present but not run",
          any(p.stage.deep and not p.run for p in full))
    check("plan: the deep stage says why it is not running",
          any("--deep" in p.reason for p in full if p.stage.deep))

    # A tiny budget runs nothing — and says so for each, rather than silently
    # producing an empty plan that reads like "there was nothing to do".
    tiny = crack_mod.plan(13100, 1)
    check("plan: a 1-second budget runs nothing", not any(p.run for p in tiny))
    check("plan: every dropped stage carries a reason",
          all(p.reason for p in tiny if not p.run))

    # A budget that covers stage 1 only. The stage that fits runs; the ones that
    # do not are dropped; the deep one is still marked --deep rather than "over
    # budget", because the two reasons are different and the operator acts on
    # them differently.
    s1 = crack_mod.estimate(13100, crack_mod.LADDER[0])
    part = crack_mod.plan(13100, s1 * 2)
    check("plan: a budget covering only stage 1 runs only stage 1",
          [p.run for p in part] == [True, False, False, False],
          str([(p.stage.name[:12], p.run) for p in part]))

    # The mask is 30.9 billion candidates: minutes on NTLM, hours on Kerberos.
    # This is the mode-dependent decision the estimate exists to make, so it is
    # asserted for both — a budget generous enough for the mask on NTLM, and the
    # same budget on Kerberos where it must be dropped.
    mask_ntlm = [p for p in crack_mod.plan(1000, 3600)
                 if "mask" in p.stage.name][0]
    mask_krb = [p for p in crack_mod.plan(13100, 3600)
                if "mask" in p.stage.name][0]
    check("plan: the mask fits an hour on NTLM", mask_ntlm.run,
          crack_mod.human_time(mask_ntlm.seconds))
    check("plan: the same mask does NOT fit an hour on Kerberos",
          not mask_krb.run, crack_mod.human_time(mask_krb.seconds))
    check("plan: the mask candidate count is 26*26**5*100",
          mask_ntlm.stage.candidates == 26 * 26 ** 5 * 100,
          str(mask_ntlm.stage.candidates))
    # And the drop reason names the shortfall, so the operator knows how much
    # more budget would have bought the stage.
    check("plan: the dropped mask names the time it needed",
          "exceeds" in mask_krb.reason, mask_krb.reason)


def test_plan_deep() -> None:
    """--deep unlocks the expensive stage, and only that stage."""
    shallow = crack_mod.plan(1000, 86400)
    deep = crack_mod.plan(1000, 86400, deep=True)
    check("plan: --deep adds no stage, it unlocks one",
          len(shallow) == len(deep))
    check("plan: --deep runs the rockyou-30000 stage",
          all(p.run for p in deep),
          str([(p.stage.name[:16], p.run) for p in deep]))
    check("plan: without --deep that stage is dropped",
          not any(p.stage.deep and p.run for p in shallow))


# ---------------------------------------------------------------------------
# Argument construction — where a rule file gets mistaken for a wordlist
# ---------------------------------------------------------------------------

def test_hashcat_argv() -> None:
    """The command line for each stage, asserted without running anything.

    Two things here are worth more than the rest, because both produce a
    plausible-looking invocation that cracks nothing:

    * **The positional order is `<hashfile> [wordlist|mask]`.** A mask emitted
      before the hashfile is read *as* the hashfile — hashcat says "Separator
      unmatched" and loads no hashes. Found by the budget test below, which is
      why the ordering is pinned here rather than trusted.
    * **The rule is never the positional argument.** `-a 0 <hash> <wordlist> -r
      <rule>`; putting the rule where the wordlist goes parses fine and cracks
      nothing.
    """
    pot = Path("/tmp/p.pot")
    hf = Path("/tmp/h.txt")

    s1 = crack_mod.LADDER[0]
    argv = crack_mod.hashcat_argv(hf, 13100, s1, pot, 60)
    check("argv: the mode is passed", "-m" in argv and "13100" in argv)
    check("argv: the potfile is passed, never hashcat's default",
          "--potfile-path" in argv and str(pot) in argv)
    check("argv: the runtime cap is passed",
          "--runtime" in argv and "60" in argv)
    check("argv: stage 1's positional wordlist is rockyou",
          str(crack_mod.ROCKYOU) in argv)
    check("argv: the wordlist follows the hashfile",
          argv.index(str(crack_mod.ROCKYOU)) > argv.index(str(hf)),
          str(argv))

    s2 = crack_mod.LADDER[1]
    argv2 = crack_mod.hashcat_argv(hf, 13100, s2, pot, 60)
    check("argv: stage 2's rule is passed with -r",
          "-r" in argv2 and str(crack_mod.BEST64) in argv2)
    check("argv: stage 2's positional wordlist is still rockyou, not the rule",
          str(crack_mod.ROCKYOU) in argv2)
    check("argv: the rule file is never the positional argument",
          argv2.index(str(crack_mod.BEST64)) < argv2.index(str(hf)),
          str(argv2))

    s3 = crack_mod.LADDER[2]
    argv3 = crack_mod.hashcat_argv(hf, 1000, s3, pot, 60)
    check("argv: the mask stage passes -a 3 and the mask",
          "-a" in argv3 and "3" in argv3 and "?u?l?l?l?l?l?d?d" in argv3)
    # The bug this test exists for: the mask must come after the hashfile, or
    # hashcat reads the mask as the hashfile.
    check("argv: the mask follows the hashfile",
          argv3.index("?u?l?l?l?l?l?d?d") > argv3.index(str(hf)), str(argv3))

    # For every stage: exactly the hashfile then the positionals, in that order,
    # and nothing positional left in `args` to be emitted before the hashfile.
    for stage in crack_mod.LADDER:
        a = crack_mod.hashcat_argv(hf, 1000, stage, pot, 30)
        hi = a.index(str(hf))
        check(f"argv: {stage.name[:20]} puts nothing positional before the hash",
              all(p in a[hi:] for p in stage.positional), str(a))
        check(f"argv: {stage.name[:20]} carries a runtime cap",
              "--runtime" in a)


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------

def test_validate_path(tmp: Path) -> None:
    f = tmp / "tgs.txt"
    f.write_text(TGS_EXAMPLE + "\n")
    got, err = crack_mod.validate_path(str(f))
    check("validate: an existing file resolves", got == f and not err, err)

    got, err = crack_mod.validate_path(str(tmp))
    check("validate: an existing directory resolves", got == tmp and not err, err)

    got, err = crack_mod.validate_path("")
    check("validate: no argument is refused", got is None and bool(err), err)

    got, err = crack_mod.validate_path("/nope/not/here.txt")
    check("validate: a missing path is refused", got is None and bool(err), err)

    # A hash typed inline is the classic mistake, and the error has to name it
    # rather than say "no such file" — the operator needs to know *why*.
    got, err = crack_mod.validate_path(TGS_EXAMPLE)
    check("validate: an inline hash is refused with an explanation",
          got is None and "looks like a hash" in err, err)
    check("validate: the refusal suggests writing it to a file",
          "file" in err, err)


def test_slug() -> None:
    check("slug: a path becomes a safe label",
          crack_mod.case_slug("/home/zim/hashes/tgs.txt") ==
          "home-zim-hashes-tgs.txt",
          crack_mod.case_slug("/home/zim/hashes/tgs.txt"))
    check("slug: a domain-named path is safe",
          "/" not in crack_mod.case_slug("hunts/ad-corp.local/loot/tgs.txt"))
    check("slug: an empty target still yields a label",
          crack_mod.case_slug("") == "hashes", crack_mod.case_slug(""))


# ---------------------------------------------------------------------------
# Case directory and potfile
# ---------------------------------------------------------------------------

def test_case_dir(tmp: Path) -> None:
    cfg = _cfg(tmp)
    case = crack_mod.case_dir(cfg, "/some/tgs.txt")
    check("case: the directory is created", case.is_dir(), str(case))
    check("case: it lives under state_dir/crack",
          case.parent == Path(cfg.state_dir) / "crack", str(case.parent))
    check("case: the name carries a stamp",
          len(case.name.rsplit("-", 1)[-1]) == 6, case.name)

    pot = crack_mod.potfile(cfg)
    check("potfile: it lives beside the cases, not in ~/.hashcat",
          pot.parent == Path(cfg.state_dir) / "crack", str(pot))
    check("potfile: it is a single shared file across runs",
          crack_mod.potfile(cfg) == pot)
    check("potfile: its name says what it is", pot.name == "crack.potfile")

    # Archiving: a report is copied out; a case with no report is left alone
    # rather than archived empty.
    case.mkdir(parents=True, exist_ok=True)
    check("finalise: no report means no archive",
          crack_mod.finalise_case(cfg, case) is None)
    (case / "report.md").write_text("# Password cracking — tgs.txt\n")
    dest = crack_mod.finalise_case(cfg, case)
    check("finalise: a report is archived into reports/",
          dest is not None and dest.parent == crack_mod.reports_dir(cfg),
          str(dest))
    check("finalise: the archive keeps the report's content",
          dest is not None and "Password cracking" in dest.read_text())
    # Unlike osint, the case survives — the hash copy and the potfile are the
    # evidence, and deleting them would make the next run redo work.
    check("finalise: the case directory is NOT deleted",
          case.is_dir(), str(case))


# ---------------------------------------------------------------------------
# The playbook
# ---------------------------------------------------------------------------

def test_playbook(tmp: Path) -> None:
    cfg = _cfg(tmp)
    case = crack_mod.case_dir(cfg, "tgs.txt")
    detected = crack_mod.identify_text(TGS_EXAMPLE)
    prompt = crack_mod.build_prompt(cfg, Path("/x/tgs.txt"), case, detected, 900)

    check("playbook: it names the case directory", str(case) in prompt)
    check("playbook: it names the shared potfile",
          str(crack_mod.potfile(cfg)) in prompt)
    check("playbook: it states the budget", "15m" in prompt)
    check("playbook: it names the detected mode",
          "13100" in prompt and "Kerberoast" in prompt)
    check("playbook: it warns that there is no GPU", "NO GPU" in prompt)
    check("playbook: it insists on --potfile-path", "--potfile-path" in prompt)
    check("playbook: it insists on --runtime", "--runtime" in prompt)
    check("playbook: it names john's best64, which is where the rule is here",
          "/usr/share/john/rules/best64.rule" in prompt)
    check("playbook: it asks for the analysis, not just the cracks",
          "password policy" in prompt.lower())
    check("playbook: it carries the authorised-use notice",
          "Authorised use only" in prompt)
    check("playbook: it names the report path",
          str(case / "report.md") in prompt)

    # With nothing identified, the prompt must say so and tell the agent to
    # establish the mode itself — not silently omit the field.
    empty = crack_mod.build_prompt(cfg, Path("/x/u.txt"), case, [], 900)
    check("playbook: an unidentified file says so and asks for --identify",
          "--identify" in empty and "none recognised" in empty)


# ---------------------------------------------------------------------------
# The real crack
# ---------------------------------------------------------------------------

def test_real_crack(tmp: Path) -> None:
    """The end-to-end proof: a real hashcat run against hashcat's own example.

    Mode 13100, plaintext `hashcat`, a four-word dictionary that contains it. The
    stage runs through the same `run_stage`/`recovered` path the agent drives, so
    what is asserted is the feature rather than a string format. It is offline
    and takes about a second — the wordlist is four words.

    Skipped, with a PASS and a reason, when hashcat is not installed: a missing
    binary is not a regression in this repo.
    """
    if shutil.which("hashcat") is None:
        check("crack: hashcat is not installed, so the real crack is skipped",
              True, "skipped")
        return

    hashfile = tmp / "real-tgs.txt"
    hashfile.write_text(TGS_EXAMPLE + "\n")
    wordlist = tmp / "words.txt"
    wordlist.write_text("wrong\nnope\n" + TGS_PLAINTEXT + "\nother\n")
    pot = tmp / "real.potfile"

    # A wordlist stage built from the real class, pointed at the test's
    # dictionary instead of rockyou — the ladder entry is the shape under test,
    # not its specific wordlist.
    stage = crack_mod.Stage(name="test dictionary", candidates=4,
                            args=("-a", "0"), positional=(str(wordlist),))

    code, out = crack_mod.run_stage(hashfile, 13100, stage, pot, runtime=30)
    check("crack: the stage exits cleanly", code in crack_mod.STOPPED,
          f"exit={code} {out[-200:]}")

    got = crack_mod.recovered(hashfile, 13100, pot)
    check("crack: --show returns the recovered pair", len(got) == 1, str(got))
    if got:
        h, pw = got[0]
        check("crack: the plaintext is the expected one", pw == TGS_PLAINTEXT,
              repr(pw))
        check("crack: the hash round-trips", h == TGS_EXAMPLE, h[:40])

    # The potfile is the durable artefact: a second run must find the crack
    # without doing the work again. This is why it is shared across runs.
    before = pot.stat().st_mtime_ns
    crack_mod.run_stage(hashfile, 13100, stage, pot, runtime=30)
    check("crack: the potfile persists across runs",
          pot.is_file() and pot.stat().st_mtime_ns >= before)
    check("crack: a re-run still reports the crack",
          len(crack_mod.recovered(hashfile, 13100, pot)) == 1)

    # And a hash that is NOT in the potfile must not be reported as cracked —
    # `--show` filters to the hashes in the given file, which is the whole
    # reason `recovered` asks hashcat rather than reading the potfile.
    other = tmp / "other.txt"
    other.write_text("$krb5tgs$23$*x$Y$z*$" + "ab" * 16 + "$" + "cd" * 60 + "\n")
    check("crack: an uncracked hash is not reported",
          crack_mod.recovered(other, 13100, pot) == [])


def test_budget_holds(tmp: Path) -> None:
    """The `--runtime` cap is enforced by hashcat, so a stage cannot overrun.

    A hash that will not crack, a huge mask, a 3-second cap: the run must return
    in roughly the cap rather than working through the keyspace. This is what
    makes the printed estimate a bound rather than a wish.
    """
    if shutil.which("hashcat") is None:
        check("budget: hashcat is not installed, so the cap is not tested",
              True, "skipped")
        return

    hashfile = tmp / "hard.txt"
    # A Kerberoast-shaped hash whose plaintext is in no dictionary.
    hashfile.write_text("$krb5tgs$23$*x$Y$z*$" + "ab" * 16 + "$" + "cd" * 60 + "\n")
    pot = tmp / "budget.potfile"
    stage = crack_mod.Stage(name="big mask", candidates=26 ** 8,
                            args=("-a", "3"), positional=("?l?l?l?l?l?l?l?l",))

    start = time.monotonic()
    code, out = crack_mod.run_stage(hashfile, 13100, stage, pot, runtime=3)
    elapsed = time.monotonic() - start
    check("budget: the run returns within the cap plus startup",
          elapsed < 60, f"{elapsed:.1f}s")
    check("budget: it did not crack anything",
          crack_mod.recovered(hashfile, 13100, pot) == [])
    # The cap is reached, so hashcat stops — and *stopping is not failing*. Exit
    # 4 ("aborted by runtime") is the correct outcome of a bounded run, and the
    # distinction is asserted here because treating it as an error would make
    # every correctly-capped stage look broken.
    check("budget: hashcat stopped rather than failed",
          code in crack_mod.STOPPED, f"exit={code} {out[-160:]}")
    check("budget: the stop is the runtime cap, not an exhaustion",
          code == crack_mod.EXIT_RUNTIME,
          f"exit={code} ({crack_mod.exit_meaning(code)})")


def test_exit_codes() -> None:
    """The exit-code table, from hashcat's own `exit_status_code.txt`.

    Pinned as data because the whole point of `STOPPED` is that a bounded run is
    not a failure. If hashcat's numbering were misread — 4 as an error, say —
    every `--runtime`-capped stage would report as broken.
    """
    check("exit: 0 is cracked", crack_mod.EXIT_CRACKED == 0)
    check("exit: 1 is exhausted", crack_mod.EXIT_EXHAUSTED == 1)
    check("exit: 4 is the runtime cap", crack_mod.EXIT_RUNTIME == 4)
    check("exit: the runtime cap counts as stopped",
          crack_mod.EXIT_RUNTIME in crack_mod.STOPPED)
    check("exit: every non-negative code up to 5 is stopped",
          crack_mod.STOPPED == frozenset({0, 1, 2, 3, 4, 5}),
          str(sorted(crack_mod.STOPPED)))
    check("exit: a negative code is a failure, not a stop",
          -1 not in crack_mod.STOPPED and -11 not in crack_mod.STOPPED)
    check("exit: an unknown positive code is a failure",
          99 not in crack_mod.STOPPED)
    check("exit: the runtime code explains itself",
          "runtime" in crack_mod.exit_meaning(4).lower(),
          crack_mod.exit_meaning(4))
    check("exit: a negative code reads as a failure",
          "failed" in crack_mod.exit_meaning(-1), crack_mod.exit_meaning(-1))

    # hashcat allows one instance; the second refuses with this message. Named
    # rather than left as a bare 255, because the fix is "wait", not "debug".
    real = ("Already an instance '/usr/bin/hashcat' running on pid 138187")
    check("exit: the instance conflict is recognised",
          crack_mod.instance_busy(real), real)
    check("exit: the instance conflict is case-insensitive",
          crack_mod.instance_busy(real.upper()))
    check("exit: ordinary output is not an instance conflict",
          not crack_mod.instance_busy("Status...........: Exhausted"))
    check("exit: empty output is not an instance conflict",
          not crack_mod.instance_busy(""))


def test_rates_are_real_run_figures() -> None:
    """The rate table is real-run, not benchmark, and pessimistic on purpose.

    The table was built from `hashcat -b` first and that was wrong: the benchmark
    reports the kernel's best case, and a real run against a specific salt is
    consistently slower — 11x on NetNTLMv2. Planning from benchmark figures calls
    a stage affordable when it is not, which is the one way this command
    misleads. So these assertions pin the *shape* that makes the table honest.
    """
    # The salted/iterated crypt family is measured in single-digit kH/s here,
    # not the tens of thousands a benchmark suggests. If someone "corrects" these
    # upward from a benchmark, the estimate for a shadow-file crack becomes
    # fiction.
    for mode in (1800, 3200):
        check(f"rates: mode {mode} is measured in kH/s, not MH/s",
              crack_mod.rate_for(mode) < 1e6, f"{crack_mod.rate_for(mode):,}")

    # NetNTLMv2 is the biggest benchmark-vs-reality gap and worth pinning: it is
    # a *fast* mode in benchmark terms and a slow one in practice.
    check("rates: NetNTLMv2 is under 5 MH/s here",
          crack_mod.rate_for(5600) < 5e6, f"{crack_mod.rate_for(5600):,}")

    # The fallback is no faster than the slowest measured mode — the property
    # that makes an unknown mode plan conservatively.
    check("rates: the fallback is no faster than any measured mode",
          crack_mod.FALLBACK_RATE <= min(crack_mod.RATES.values()),
          f"{crack_mod.FALLBACK_RATE:,} vs {min(crack_mod.RATES.values()):,}")

    # Every mode the command claims to identify has a rate, so the banner can
    # always print an estimate rather than falling back for a format it knows.
    missing = sorted(set(crack_mod.HASH_MODES) - set(crack_mod.RATES))
    check("rates: every known mode has a measured rate", not missing, str(missing))


# ---------------------------------------------------------------------------
# The scope guard
# ---------------------------------------------------------------------------

def test_scope_does_not_block_cracking() -> None:
    """Cracking is local: a deny-listed domain in a *path* must not block it.

    Pin the finding from planning. `/ad` writes its hashes under a directory
    named for the domain (`hunts/ad-corp.local/loot/tgs.txt`), so a crack run
    against that file has a deny-listed string in its argument. `hashcat` is not
    a network tool and the guard must not treat it as one — while a real network
    command against the same domain is still blocked, which is the half that
    proves the guard is still working.
    """
    scope = scope_mod.Scope()
    scope.deny = scope_mod.HostSet.from_mapping({"domains": ["corp.local"]})
    scope.deny_path = Path("/dev/null")
    assert not scope.deny.empty, "the test's deny-list did not load"

    local = [
        "hashcat -m 13100 hunts/ad-corp.local/loot/tgs.txt rockyou.txt",
        "hashcat -m 13100 hashes.txt --potfile-path crack.potfile --show",
        "hashcat --identify hunts/ad-corp.local/loot/tgs.txt",
    ]
    for cmd in local:
        check(f"scope: {cmd[:44]!r} is NOT blocked",
              not scope.check_command(cmd),
              str([v.reason for v in scope.check_command(cmd)]))

    # The other half: the guard still sees the domain and blocks a real
    # connection to it. Without this, the test above would pass if the guard
    # were simply broken.
    for cmd in ["curl http://dc01.corp.local/",
                "smbclient -L //dc01.corp.local"]:
        check(f"scope: {cmd[:40]!r} is still BLOCKED",
              bool(scope.check_command(cmd)))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_arg_parse() -> None:
    """`/crack`'s flag parsing, which is where a typo becomes a wasted budget."""
    from zimzilla.ui.app import CRACK_DEFAULT_BUDGET, _parse_crack_args

    path, mode, mt, deep, err = _parse_crack_args(["hashes.txt"])
    check("parse: a bare path", path == "hashes.txt" and not err, err)
    check("parse: the default budget is applied",
          mt == CRACK_DEFAULT_BUDGET, str(mt))
    check("parse: --deep defaults off", deep is False)

    path, mode, mt, deep, err = _parse_crack_args(
        ["hashes.txt", "--mode", "13100", "--max-time", "300", "--deep"])
    check("parse: all three flags together",
          path == "hashes.txt" and mode == 13100 and mt == 300.0 and deep
          and not err, f"{path} {mode} {mt} {deep} {err}")

    # `--flag=value` is accepted as well as `--flag value`, matching /ad.
    path, mode, mt, deep, err = _parse_crack_args(
        ["h.txt", "--mode=1000", "--max-time=60"])
    check("parse: --flag=value form works",
          mode == 1000 and mt == 60.0 and not err, f"{mode} {mt} {err}")

    # A mode the command does not know is refused with the list, because a wrong
    # mode number does not fail — it burns the whole budget on a format that
    # cannot match.
    path, mode, mt, deep, err = _parse_crack_args(["h.txt", "--mode", "999999"])
    check("parse: an unknown mode is refused", bool(err), err)
    check("parse: the refusal lists the known modes", "13100" in err, err)

    for bad, why in [
        (["h.txt", "--mode"], "--mode with no value"),
        (["h.txt", "--max-time"], "--max-time with no value"),
        (["h.txt", "--max-time", "soon"], "a non-numeric budget"),
        (["h.txt", "--max-time", "0"], "a zero budget"),
        (["h.txt", "--max-time", "-5"], "a negative budget"),
        (["h.txt", "--wat"], "an unknown flag"),
        (["a.txt", "b.txt"], "two paths"),
    ]:
        path, mode, mt, deep, err = _parse_crack_args(bad)
        check(f"parse: {why} is refused", bool(err), err)


def test_wiring() -> None:
    """The command is reachable from every surface that lists commands.

    Read from the source rather than importing a copy, so a rename in one place
    cannot silently orphan the command in another — the same check `test_osint`
    makes for `/osint`.
    """
    from zimzilla.ui.complete import SLASH_COMMANDS

    names = [c for c, _ in SLASH_COMMANDS]
    check("wiring: /crack is in the completion menu", "/crack" in names, str(names))

    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: dispatch has a crack entry",
          '"crack": lambda a: self._cmd_crack(a)' in app_src)
    check("wiring: _cmd_crack is defined", "def _cmd_crack(" in app_src)
    check("wiring: _finalise_crack is defined", "def _finalise_crack(" in app_src)
    check("wiring: help table lists it", '("/crack <file|dir>"' in app_src)
    check("wiring: palette lists it",
          '("/crack", "crack hashes offline' in app_src)
    check("wiring: crack.py is imported",
          "from .. import crack as crack_mod" in app_src)
    # The refactor that lets /crack and /osint share one finalise hook.
    check("wiring: _run_turn takes a finalise callable",
          "finalise: \"Callable[[], None] | None\" = None" in app_src)
    check("wiring: _run_turn still honours osint_case",
          "elif osint_case is not None:" in app_src)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    test_identify_text()
    test_identify_mixed()
    test_estimate()
    test_human_time()
    test_plan_budget()
    test_plan_deep()
    test_hashcat_argv()
    test_exit_codes()
    test_rates_are_real_run_figures()
    test_slug()
    test_arg_parse()
    test_wiring()
    test_scope_does_not_block_cracking()

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_identify_path(tmp)
        test_validate_path(tmp)
        test_case_dir(tmp)
        test_playbook(tmp)
        test_real_crack(tmp)
        test_budget_holds(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
