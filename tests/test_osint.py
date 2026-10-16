"""Regression suite for `/osint`.

Run:  python tests/test_osint.py   (from an activated venv)

Two layers, matching the split in zimzilla/osint.py itself:

* the pure logic — registry shape, target validation, slugging, the case
  directory, the playbook prompt — exercised directly and with no terminal;
* the wiring — that the slash dispatch, the help table, the palette and the
  completion popup all actually expose the command, so a rename in one place
  cannot silently orphan it in another.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import osint  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry() -> None:
    # Every kind the operator named is present, and the display order is the
    # order they asked for — a missing entry here is a missing menu row.
    want = ["email", "phone", "facebook", "tiktok", "instagram", "discord", "github"]
    check("registry: all seven kinds exist", set(osint.OSINT_KINDS) == set(want),
          f"{sorted(osint.OSINT_KINDS)}")
    check("registry: KIND_ORDER lists them in the operator's order",
          osint.KIND_ORDER == want, f"{osint.KIND_ORDER}")

    # The built kinds, listed deliberately. If a third is flipped on later,
    # this test should be updated on purpose rather than passing by accident —
    # it is the reminder that a new playbook needs its own coverage.
    built = [n for n, k in osint.OSINT_KINDS.items() if k.built]
    check("registry: all but phone are the built kinds",
          sorted(built) == ["discord", "email", "facebook", "github",
                            "instagram", "tiktok"], f"{built}")

    # The stubs must NOT carry a playbook — otherwise a stub would look
    # runnable to build_prompt and the "not built" guard would be the only
    # thing standing between the operator and an empty briefing.
    for name, kind in osint.OSINT_KINDS.items():
        if not kind.built:
            check(f"registry: stub '{name}' has no playbook", not kind.playbook)

    # Names are unique keys with non-empty blurbs, so the menu renders.
    for name, kind in osint.OSINT_KINDS.items():
        check(f"registry: '{name}' is well-formed",
              kind.name == name and bool(kind.blurb) and bool(kind.target_label))


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------

def test_validation() -> None:
    email = osint.OSINT_KINDS["email"]

    good = [
        "a@b.co",
        "first.last@example.com",
        "user+tag@gmail.com",
        "x@sub.domain.example.org",
    ]
    for addr in good:
        ok, err = osint.validate(email, addr)
        check(f"validate: accepts {addr}", ok, err)

    bad = [
        "",              # empty
        "nope",          # no @
        "a@b",           # no dot in domain
        "a b@c.com",     # whitespace in local part
        "a@b..com",      # empty label
        "@example.com",  # no local part
        "a@.com",        # empty domain label
    ]
    for addr in bad:
        ok, _ = osint.validate(email, addr)
        check(f"validate: rejects {addr!r}", not ok)

    # A missing target is reported against the kind's own label, so the
    # operator reads "no email address given", not a generic message.
    ok, err = osint.validate(email, "   ")
    check("validate: empty target names the kind's label",
          not ok and "email address" in err, err)

    # ---- github ----------------------------------------------------------
    gh = osint.OSINT_KINDS["github"]

    for handle in ["torvalds", "Mr-Destroyer", "a", "x" * 39,
                   "user123", "user-name-123"]:
        ok, err = osint.validate(gh, handle)
        check(f"validate: accepts github {handle!r}", ok, err)

    # A pasted profile URL is the common way this gets typed, so it is
    # accepted and unwrapped rather than refused.
    for pasted in ["https://github.com/torvalds", "http://github.com/torvalds",
                   "github.com/torvalds", "https://github.com/torvalds/"]:
        ok, err = osint.validate(gh, pasted)
        check(f"validate: accepts the URL form {pasted!r}", ok, err)

    for handle in ["", "-leading", "trailing-", "has space", "x" * 40,
                   "user@example.com", "two--hyphens", "under_score",
                   # A URL with a path is a repo, not an account.
                   "https://github.com/torvalds/linux"]:
        ok, _ = osint.validate(gh, handle)
        check(f"validate: rejects github {handle!r}", not ok)

    ok, err = osint.validate(gh, "   ")
    check("validate: github names its own label in the error",
          not ok and "GitHub username" in err, err)

    # ---- discord ---------------------------------------------------------
    # Two legitimate target forms: a snowflake id and a username. Both must
    # pass, because the operator has no way to know which one they were given
    # and either is a real identifier.
    dc = osint.OSINT_KINDS["discord"]

    for handle in ["someuser", "user.name", "user_name", "ab", "x" * 32,
                   "80351110224678912", "613425648685547541",
                   # A legacy discriminator dates the account, so it is kept.
                   "olduser#1234"]:
        ok, err = osint.validate(dc, handle)
        check(f"validate: accepts discord {handle!r}", ok, err)

    # A pasted profile URL carries the *numeric id* on Discord (unlike GitHub,
    # where it carries the handle), so it unwraps to an id.
    for pasted in ["https://discord.com/users/80351110224678912",
                   "http://discord.com/users/80351110224678912",
                   "discord.com/users/80351110224678912",
                   "https://discord.com/users/80351110224678912/"]:
        ok, err = osint.validate(dc, pasted)
        check(f"validate: accepts the URL form {pasted!r}", ok, err)

    # A mention is another way an id arrives in a paste.
    for mention in ["<@80351110224678912>", "<@!80351110224678912>"]:
        ok, err = osint.validate(dc, mention)
        check(f"validate: accepts the mention form {mention!r}", ok, err)

    for handle in ["", "a", "x" * 33, "Has Space", "user#12", "user#12345",
                   "not a handle!", "a@b.com"]:
        ok, _ = osint.validate(dc, handle)
        check(f"validate: rejects discord {handle!r}", not ok)

    ok, err = osint.validate(dc, "   ")
    check("validate: discord names its own label in the error",
          not ok and "Discord" in err, err)

    # A bare number is ambiguous and *both* readings are legitimate: Discord
    # usernames may be all digits (2-32 chars), and an id is 17-20 digits. So
    # a short number is accepted as a username, and a 17-20 digit one is
    # accepted as either. The distinction is the playbook's job, not
    # validation's — refusing a digits-only username would refuse a real one.
    ok, err = osint.validate(dc, "12345")
    check("validate: a digits-only username is accepted", ok, err)
    ok, err = osint.validate(dc, "80351110224678912")
    check("validate: a snowflake is accepted", ok, err)

    # ---- facebook --------------------------------------------------------
    # Four legitimate shapes: a numeric id, a vanity username, a `pfbid`
    # opaque id, and any of those as a pasted URL. All four must pass, because
    # the operator has no way to know which one they were handed.
    fb = osint.OSINT_KINDS["facebook"]

    for value in ["100012345678901", "4", "54971236771", "nasa",
                  "mark.zuckerberg", "NASA", "pfbid0AbCdefghijklmnopqrstuvwxyz"]:
        ok, err = osint.validate(fb, value)
        check(f"validate: accepts facebook {value!r}", ok, err)

    # Every host Facebook serves the same profile from, and the two URL shapes
    # that carry a name *and* an id — where the id is what must survive.
    for pasted in ["https://www.facebook.com/nasa",
                   "http://www.facebook.com/nasa",
                   "https://web.facebook.com/nasa",
                   "https://m.facebook.com/nasa",
                   "https://mbasic.facebook.com/nasa",
                   "https://business.facebook.com/nasa",
                   "facebook.com/nasa",
                   "https://www.facebook.com/nasa/",
                   "https://www.facebook.com/profile.php?id=4",
                   "https://www.facebook.com/people/Mark-Zuckerberg/4/"]:
        ok, err = osint.validate(fb, pasted)
        check(f"validate: accepts the URL form {pasted!r}", ok, err)

    # A URL with tracking cruft still reduces to the identity.
    ok, err = osint.validate(fb, "https://web.facebook.com/nasa?__cft__[0]=abc#frag")
    check("validate: accepts a facebook URL with tracking cruft", ok, err)
    # A pasted subpage recovers the account rather than being refused.
    ok, err = osint.validate(fb, "https://www.facebook.com/nasa/photos")
    check("validate: a facebook subpage reduces to its account", ok, err)

    # Site furniture is not a profile, and must not become one silently.
    for junk in ["", "   ", "login", "groups", "marketplace", "not a handle!",
                 "has space", "a@b.com"]:
        ok, _ = osint.validate(fb, junk)
        check(f"validate: rejects facebook {junk!r}", not ok)

    ok, err = osint.validate(fb, "   ")
    check("validate: facebook names its own label in the error",
          not ok and "Facebook" in err, err)

    # ---- tiktok ----------------------------------------------------------
    tk = osint.OSINT_KINDS["tiktok"]

    for value in ["charlidamelio", "@charlidamelio", "user.name_1", "ab",
                  "x" * 24]:
        ok, err = osint.validate(tk, value)
        check(f"validate: accepts tiktok {value!r}", ok, err)

    # The URL forms an operator actually pastes, including the query strings
    # and the dead `m.` host, all of which were measured to serve the same
    # profile as the bare handle.
    for pasted in ["https://www.tiktok.com/@charlidamelio",
                   "https://www.tiktok.com/@charlidamelio?lang=en",
                   "https://www.tiktok.com/@charlidamelio?is_from_webapp=1&sender_device=pc",
                   "https://www.tiktok.com/@charlidamelio/",
                   "https://m.tiktok.com/@charlidamelio",
                   "tiktok.com/@charlidamelio"]:
        ok, err = osint.validate(tk, pasted)
        check(f"validate: accepts the URL form {pasted!r}", ok, err)

    for junk in ["", "   ", "a", "x" * 25, "has space", "not a handle!",
                 "a@b.com"]:
        ok, _ = osint.validate(tk, junk)
        check(f"validate: rejects tiktok {junk!r}", not ok)

    ok, err = osint.validate(tk, "   ")
    check("validate: tiktok names its own label in the error",
          not ok and "TikTok" in err, err)

    # ---- instagram -------------------------------------------------------
    # A handle, or the numeric IGID the crawler record yields.
    ig = osint.OSINT_KINDS["instagram"]

    for value in ["nasa", "@nasa", "user.name_1", "a", "x" * 30,
                  "528817151", "17841401474538262"]:
        ok, err = osint.validate(ig, value)
        check(f"validate: accepts instagram {value!r}", ok, err)

    for pasted in ["https://www.instagram.com/nasa",
                   "https://www.instagram.com/nasa/",
                   "instagram.com/nasa",
                   "www.instagram.com/nasa?hl=en",
                   "m.instagram.com/nasa"]:
        ok, err = osint.validate(ig, pasted)
        check(f"validate: accepts the URL form {pasted!r}", ok, err)

    # A pasted *post* URL reduces to its author, who is the account.
    ok, err = osint.validate(ig, "https://www.instagram.com/nasa/p/CxYzAbCdEf/")
    check("validate: an instagram post URL reduces to its author", ok, err)

    for junk in ["", "   ", "x" * 31, "has space", "not a handle!", "a@b.com"]:
        ok, _ = osint.validate(ig, junk)
        check(f"validate: rejects instagram {junk!r}", not ok)

    # Site furniture must not silently become a handle — `/p/...` is a post,
    # `/explore/...` is a feed, and neither is an account.
    for furniture in ["https://www.instagram.com/p/CxYzAbCdEf/",
                      "https://www.instagram.com/explore/tags/nasa/",
                      "https://www.instagram.com/reel/CxYzAbCdEf/"]:
        ok, _ = osint.validate(ig, furniture)
        check(f"validate: rejects instagram furniture {furniture!r}", not ok)

    ok, err = osint.validate(ig, "   ")
    check("validate: instagram names its own label in the error",
          not ok and "Instagram" in err, err)


def test_normalise() -> None:
    # Domain lowercased, local part's case preserved — the local part is
    # technically case-sensitive and lowercasing it would change the address.
    check("normalise: lowercases the domain only",
          osint.normalise_email("  Bob.Smith@GMail.COM ") == "Bob.Smith@gmail.com",
          osint.normalise_email("  Bob.Smith@GMail.COM "))
    check("normalise: leaves a non-address untouched",
          osint.normalise_email("  just-a-handle ") == "just-a-handle")
    # rpartition, not split: a quoted local part may itself contain '@'.
    check("normalise: splits on the last '@'",
          osint.normalise_email('"a@b"@Example.com') == '"a@b"@example.com')

    # The github normaliser unwraps a pasted profile URL, case preserved (a
    # handle's case is cosmetic, so it is left as typed).
    gh = osint.OSINT_KINDS["github"]
    check("normalise: a bare github handle is unchanged",
          osint.normalise_github("torvalds") == "torvalds")
    check("normalise: strips a github profile URL",
          osint.normalise_github("https://github.com/torvalds") == "torvalds",
          osint.normalise_github("https://github.com/torvalds"))
    check("normalise: strips a trailing slash and surrounding space",
          osint.normalise_github("  github.com/Torvalds/  ") == "Torvalds",
          osint.normalise_github("  github.com/Torvalds/  "))
    # A URL with a path is a repo, and is deliberately NOT unwrapped — it must
    # then fail validation rather than silently recon the wrong thing.
    check("normalise: a repo URL is left to fail validation",
          osint.normalise_github("https://github.com/torvalds/linux")
          == "torvalds/linux")

    # The discord normaliser unwraps a pasted profile URL, which on Discord
    # carries the numeric id rather than a handle.
    check("normalise: a bare discord handle is unchanged",
          osint.normalise_discord("someuser") == "someuser")
    check("normalise: strips a discord profile URL to its id",
          osint.normalise_discord("https://discord.com/users/80351110224678912")
          == "80351110224678912",
          osint.normalise_discord("https://discord.com/users/80351110224678912"))
    check("normalise: strips a discordapp URL too",
          osint.normalise_discord("https://discordapp.com/users/80351110224678912")
          == "80351110224678912")
    check("normalise: strips a trailing slash and surrounding space",
          osint.normalise_discord("  discord.com/users/80351110224678912/  ")
          == "80351110224678912")
    # A mention (`<@id>` / `<@!id>`) is another way an id arrives in a paste.
    check("normalise: unwraps a user mention",
          osint.normalise_discord("<@80351110224678912>") == "80351110224678912"
          and osint.normalise_discord("<@!80351110224678912>")
          == "80351110224678912")
    # The legacy discriminator is part of the identity and is NOT stripped —
    # it dates the account, which is a finding.
    check("normalise: keeps a legacy discriminator",
          osint.normalise_discord("  OldUser#1234  ") == "OldUser#1234",
          osint.normalise_discord("  OldUser#1234  "))

    # The shared dispatch picks the right one per kind, so the UI label, the
    # evidence path and the playbook cannot disagree.
    check("normalise: dispatches by kind",
          osint.normalise(gh, "https://github.com/torvalds") == "torvalds"
          and osint.normalise(osint.OSINT_KINDS["email"], " Bob@GMail.COM ")
          == "Bob@gmail.com"
          and osint.normalise(osint.OSINT_KINDS["phone"], "  +1555  ") == "+1555"
          and osint.normalise(osint.OSINT_KINDS["discord"],
                              "https://discord.com/users/80351110224678912")
          == "80351110224678912"
          and osint.normalise(osint.OSINT_KINDS["facebook"],
                              "https://www.facebook.com/nasa") == "nasa"
          and osint.normalise(osint.OSINT_KINDS["tiktok"],
                              "https://www.tiktok.com/@charlidamelio?lang=en")
          == "charlidamelio"
          and osint.normalise(osint.OSINT_KINDS["instagram"],
                              "https://www.instagram.com/nasa/") == "nasa")

    # The facebook normaliser picks the *durable* identifier out of a URL: the
    # numeric id survives a rename, the display name does not.
    fb = osint.OSINT_KINDS["facebook"]
    check("normalise: facebook takes the id from /people/<name>/<id>/",
          osint.normalise_facebook(
              "https://www.facebook.com/people/Mark-Zuckerberg/4/") == "4",
          osint.normalise_facebook("https://www.facebook.com/people/Mark-Zuckerberg/4/"))
    check("normalise: facebook takes the id from profile.php?id=",
          osint.normalise_facebook("https://www.facebook.com/profile.php?id=4") == "4")
    check("normalise: facebook drops tracking query and fragment",
          osint.normalise_facebook(
              "https://web.facebook.com/nasa?__cft__[0]=abc#frag") == "nasa",
          osint.normalise_facebook("https://web.facebook.com/nasa?__cft__[0]=abc#frag"))
    check("normalise: facebook recovers the account from a subpage",
          osint.normalise_facebook("https://www.facebook.com/nasa/photos") == "nasa")
    check("normalise: facebook leaves a bare id alone",
          osint.normalise_facebook("  100012345678901  ") == "100012345678901")

    # The tiktok normaliser unwraps every host and strips the query strings,
    # including the dead `m.` host, which is unwrapped so a stale paste still
    # becomes a handle that works on `www.`.
    check("normalise: strips a tiktok profile URL",
          osint.normalise_tiktok("https://www.tiktok.com/@charlidamelio") == "charlidamelio")
    check("normalise: strips a tiktok query string",
          osint.normalise_tiktok(
              "https://www.tiktok.com/@charlidamelio?is_from_webapp=1") == "charlidamelio")
    check("normalise: strips the dead m. tiktok host",
          osint.normalise_tiktok("https://m.tiktok.com/@charlidamelio") == "charlidamelio")
    check("normalise: strips a leading @ from a tiktok handle",
          osint.normalise_tiktok("@charlidamelio") == "charlidamelio")

    # The instagram normaliser unwraps the host, the `@`, and the query, and
    # reduces a post URL to its author.
    check("normalise: strips an instagram profile URL",
          osint.normalise_instagram("https://www.instagram.com/nasa/") == "nasa")
    check("normalise: strips an instagram query string",
          osint.normalise_instagram("www.instagram.com/nasa?hl=en") == "nasa")
    check("normalise: strips a leading @ from an instagram handle",
          osint.normalise_instagram("@nasa") == "nasa")
    check("normalise: reduces an instagram post URL to its author",
          osint.normalise_instagram(
              "https://www.instagram.com/nasa/p/CxYzAbCdEf/") == "nasa",
          osint.normalise_instagram("https://www.instagram.com/nasa/p/CxYzAbCdEf/"))


# ---------------------------------------------------------------------------
# Slug and case directory
# ---------------------------------------------------------------------------

def test_case_dir(tmp: Path) -> None:
    email = osint.OSINT_KINDS["email"]
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "state"

    slug = osint.case_slug("Bob.Smith+news@Gmail.com")
    check("slug: keeps safe characters, drops the rest",
          slug == "bob.smith-news-gmail.com", slug)

    # A target that is all symbols must not produce an empty path segment.
    check("slug: never empty", osint.case_slug("!!!") == "target",
          osint.case_slug("!!!"))

    d = osint.case_dir(cfg, email, "Bob@Example.com")
    check("case_dir: is created", d.is_dir(), str(d))
    check("case_dir: lives under state_dir/osint",
          d.parent == cfg.state_dir / "osint", str(d.parent))
    check("case_dir: named <kind>-<slug>-<stamp>",
          d.name.startswith("email-bob-example.com-"), d.name)

    # The timestamp is second-resolution; two calls in the same second collide
    # by design (same case), so assert the shape rather than uniqueness. The
    # stamp is the last TWO dash-separated segments (the slug itself contains
    # dashes, so a plain rsplit("-", 1) would return only the clock half).
    stamp = "-".join(d.name.rsplit("-", 2)[-2:])
    check("case_dir: stamp is YYYYMMDD-HHMMSS",
          len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit(),
          stamp)

    # A github case is slugged from the NORMALISED handle, so a pasted URL
    # does not produce a path segment like "github.com-torvalds".
    gh = osint.OSINT_KINDS["github"]
    gd = osint.case_dir(cfg, gh, "https://github.com/torvalds")
    check("case_dir: github slugs the bare handle, not the URL",
          gd.name.startswith("github-torvalds-"), gd.name)
    check("case_dir: no URL fragments in the path",
          "github.com" not in gd.name, gd.name)


# ---------------------------------------------------------------------------
# Finalising a case — archive the report, delete the rest
# ---------------------------------------------------------------------------

def test_finalise(tmp: Path) -> None:
    email = osint.OSINT_KINDS["email"]
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "fin-state"

    # ---- a completed run: report archived, case dir gone -------------------
    case = osint.case_dir(cfg, email, "done@example.com")
    (case / "report.md").write_text("# Email OSINT — done@example.com\n")
    # Bulky evidence that must NOT survive the cleanup.
    (case / "clone").mkdir()
    (case / "clone" / "big.git").write_text("x" * 5000)

    report, cleaned = osint.finalise_case(cfg, case)
    check("finalise: returns the archived report path", report is not None,
          str(report))
    check("finalise: the report is archived under reports/",
          report is not None and report.parent == cfg.state_dir / "reports",
          str(report) if report else "")
    check("finalise: the archived report keeps the case name",
          report is not None and report.name == f"{case.name}.md",
          report.name if report else "")
    check("finalise: the report content survived the move",
          report is not None and "done@example.com" in report.read_text())
    check("finalise: the case directory was deleted", not case.exists())
    check("finalise: reports that cleaned is True", cleaned is True)

    # ---- a failed run: no report, so nothing is deleted --------------------
    # The whole point: a run that produced no report must keep its evidence,
    # never be tidied away on the assumption it finished.
    case2 = osint.case_dir(cfg, email, "failed@example.com")
    (case2 / "profile.json").write_text("{}")
    report2, cleaned2 = osint.finalise_case(cfg, case2)
    check("finalise: no report yields no path", report2 is None)
    check("finalise: no report leaves the case dir in place", case2.exists())
    check("finalise: no report reports cleaned False", cleaned2 is False)
    check("finalise: no report keeps the collected evidence",
          (case2 / "profile.json").is_file())

    # ---- a report under a different name or in a subdir is still found -----
    # The agent does not always follow the filename to the letter; a report it
    # did write must still be archived rather than deleted as evidence.
    case3 = osint.case_dir(cfg, email, "nested@example.com")
    (case3 / "out").mkdir()
    (case3 / "out" / "Report.md").write_text("# nested\n")
    report3, cleaned3 = osint.finalise_case(cfg, case3)
    check("finalise: finds a report in a subdirectory", report3 is not None,
          str(report3))
    check("finalise: nested case dir still deleted", not case3.exists())
    check("finalise: nested run cleaned True", cleaned3 is True)

    # A non-report .md must not be mistaken for the report.
    case4 = osint.case_dir(cfg, email, "notes@example.com")
    (case4 / "notes.md").write_text("scratch\n")
    report4, cleaned4 = osint.finalise_case(cfg, case4)
    check("finalise: a stray .md is not treated as the report", report4 is None)
    check("finalise: a stray .md leaves the case dir alone", case4.exists())

    # A report.md that belongs to a CLONED repo is not this run's report. The
    # run's own report must win, and the clone's must not be archived as it —
    # mistaking them would archive the wrong file and delete the real evidence.
    case5 = osint.case_dir(cfg, email, "cloned@example.com")
    (case5 / "report.md").write_text("# the run's report\n")
    repo = case5 / "clone" / "some.git"
    repo.mkdir(parents=True)
    (repo / "report.md").write_text("# the repo's own report\n")
    report5, cleaned5 = osint.finalise_case(cfg, case5)
    check("finalise: the run's report wins over a clone's",
          report5 is not None and "the run's report" in report5.read_text(),
          report5.name if report5 else "")

    # With NO top-level report, a clone's report.md must not be promoted: the
    # case dir is kept so the operator can see the run never produced one.
    case6 = osint.case_dir(cfg, email, "cloneonly@example.com")
    repo6 = case6 / "clone" / "only.git"
    repo6.mkdir(parents=True)
    (repo6 / "report.md").write_text("# not the run's\n")
    report6, cleaned6 = osint.finalise_case(cfg, case6)
    check("finalise: a clone's report is never promoted", report6 is None)
    check("finalise: a clone-only case dir is kept", case6.exists())

    # reports_dir is the documented location and agrees with what finalise used.
    check("finalise: reports_dir matches the archive parent",
          osint.reports_dir(cfg) == cfg.state_dir / "reports")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt(tmp: Path) -> None:
    email = osint.OSINT_KINDS["email"]
    case = tmp / "case"
    p = osint.build_prompt(email, "Bob@Example.com", case)

    # The target must be normalised in the prompt, and the case dir must be an
    # absolute path the agent can write to from its first tool call.
    check("prompt: carries the normalised target", "Bob@example.com" in p)
    check("prompt: carries the case dir", str(case) in p)

    # The placeholders must be fully substituted — a stray brace would reach
    # the model as literal text.
    check("prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)

    # The authorised-use condition is stated in every prompt, not just the UI.
    check("prompt: states the authorised-use condition",
          "Authorised investigation only" in p)

    # Spot-check that the phases the operator asked for actually survive into
    # the briefing, so an edit that drops one is caught here.
    for needle in ("Validation", "Gravatar", "Breach", "Account discovery",
                   "Identity correlation", "Domain intelligence", "Paste"):
        check(f"prompt: covers the {needle.split()[0]} phase", needle in p)
    check("prompt: asks for report.md", "report.md" in p)
    check("prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    # The report format is shared with the github playbook, so both kinds must
    # carry the same five-line finding block, coverage table and closing
    # section. A drift between the two is what this catches.
    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"prompt: report format has {needle!r}", needle in p)

    # An unbuilt kind still yields a string rather than raising on .format().
    stub = osint.OSINT_KINDS["phone"]
    s = osint.build_prompt(stub, "+15551234567", case)
    check("prompt: an unbuilt kind degrades gracefully",
          "phone" in s and "+15551234567" in s)


def test_prompt_github(tmp: Path) -> None:
    gh = osint.OSINT_KINDS["github"]
    case = tmp / "ghcase"
    p = osint.build_prompt(gh, "torvalds", case)

    check("github prompt: carries the handle", "torvalds" in p)
    check("github prompt: carries the case dir", str(case) in p)
    check("github prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)
    check("github prompt: states the authorised-use condition",
          "Authorised investigation only" in p)

    # A pasted URL must be normalised out of the briefing, so the agent and
    # the report title both see a bare handle.
    p_url = osint.build_prompt(gh, "https://github.com/torvalds", case)
    check("github prompt: normalises a pasted profile URL",
          "github — GitHub username: torvalds" in p_url
          and "github.com/torvalds" not in p_url)

    # The phases the operator asked for — email discovery above all, since
    # that is the stated purpose of the command.
    for needle in ("Account profiling", "Email discovery", "commit",
                   ".patch", ".mailmap", "GPG", "Repository inventory",
                   "Secrets", "Social graph", "Timeline"):
        check(f"github prompt: covers {needle!r}", needle in p)

    check("github prompt: asks for report.md", "report.md" in p)
    check("github prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    # The one restraint that matters: a leaked secret is described, never
    # reproduced into the report.
    check("github prompt: says not to reproduce secrets",
          "without reproducing the secret" in p)

    # Same shared report format as the email playbook — this is the assertion
    # that keeps the two kinds' reports structurally identical.
    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"github prompt: report format has {needle!r}", needle in p)

    # The speed work: the run must lead with the unmetered git path and batch
    # its API calls rather than spending one round-trip per fact. These pin the
    # instructions that make the difference, so an edit cannot quietly undo it.
    check("github prompt: checks the rate limit up front",
          "rate_limit" in p)
    check("github prompt: promotes the clone over the API",
          "not metered" in p or "not\n    metered" in p or "unmetered" in p)
    check("github prompt: tells the agent to batch per phase",
          "ONE script" in p or "One script" in p)
    check("github prompt: says the API budget is 60/hour",
          "60 requests" in p)


def test_prompt_discord(tmp: Path) -> None:
    dc = osint.OSINT_KINDS["discord"]
    case = tmp / "dccase"
    p = osint.build_prompt(dc, "someuser", case)

    check("discord prompt: carries the handle", "someuser" in p)
    check("discord prompt: carries the case dir", str(case) in p)
    check("discord prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)
    check("discord prompt: states the authorised-use condition",
          "Authorised investigation only" in p)
    check("discord prompt: asks for report.md", "report.md" in p)
    check("discord prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    # Same shared report format as the other two playbooks — this is the
    # assertion that keeps every kind's report structurally identical, and so
    # parseable by the same tree reader.
    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"discord prompt: report format has {needle!r}", needle in p)

    # A pasted URL must be normalised to the bare id, so the agent and the
    # report title both see the identifier rather than a URL.
    p_url = osint.build_prompt(dc, "https://discord.com/users/80351110224678912",
                               case)
    check("discord prompt: normalises a pasted profile URL",
          "80351110224678912" in p_url and "discord.com/users/8" not in p_url)

    # ---- the measured facts -------------------------------------------
    # Discord is the most closed kind, and the playbook's value is that it says
    # so. These pin the facts established by probing the live API, so that an
    # edit cannot quietly reintroduce an endpoint that returns 401 and cost a
    # whole run rediscovering it.
    check("discord prompt: warns there is no unauthenticated user endpoint",
          "401" in p and "no unauthenticated user endpoint" in p.lower())
    check("discord prompt: names the rate-limited username probe",
          "username-attempt-unauthed" in p)
    check("discord prompt: warns the rate limit escalates",
          "retry_after" in p and "climbs" in p)
    check("discord prompt: says the probe is one call, not a loop",
          "Never put it in a loop" in p)
    check("discord prompt: warns the widget is anonymised",
          "anonymised" in p and '"0","1","2"' in p)
    check("discord prompt: says the profile page is a JS shell",
          "JavaScript shell" in p)

    # The free, local, exact derivation — the anchor fact of the run.
    check("discord prompt: gives the snowflake epoch arithmetic",
          "(id >> 22) + 1420070400000" in p)
    check("discord prompt: gives the default-avatar derivation",
          "embed/avatars/" in p and "(id >> 22) % 6" in p)

    # The pivot, which is where the run actually earns its keep.
    for needle in ("sherlock", "maigret", "cross-platform", "reverse-image"):
        check(f"discord prompt: covers {needle!r}", needle in p.lower())

    # The restraint that matters, same as github's: a leaked token is
    # described, never reproduced.
    check("discord prompt: says not to reproduce leaked tokens",
          "without\n     reproducing the token itself" in p
          or "reproducing the token itself" in p)

    # The token tier is offered honestly — used if the operator supplied one,
    # never scraped, and reported as a gap when absent.
    check("discord prompt: mentions the token tier", "DISCORD_TOKEN" in p)
    check("discord prompt: forbids a scraped token",
          "never a scraped" in p)

    # The report must end by saying what was NOT reachable. For this kind that
    # is one of the most useful sections, so it is pinned.
    check("discord prompt: requires a limits section",
          "LIMITS" in p and "a token would have added" in p)


def test_prompt_facebook(tmp: Path) -> None:
    fb = osint.OSINT_KINDS["facebook"]
    case = tmp / "fbcase"
    p = osint.build_prompt(fb, "nasa", case)

    check("facebook prompt: carries the target", "nasa" in p)
    check("facebook prompt: carries the case dir", str(case) in p)
    check("facebook prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)
    check("facebook prompt: states the authorised-use condition",
          "Authorised investigation only" in p)
    check("facebook prompt: asks for report.md", "report.md" in p)
    check("facebook prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"facebook prompt: report format has {needle!r}", needle in p)

    # A pasted URL must reduce to the identifier the agent and the report
    # title both use.
    p_url = osint.build_prompt(fb, "https://www.facebook.com/nasa", case)
    check("facebook prompt: normalises a pasted profile URL",
          "nasa" in p_url and "facebook.com/nasa" not in p_url)

    # ---- the measured facts -------------------------------------------
    # Facebook is the most closed surface, and the playbook's whole value is
    # that it says so. These pin the walls established by probing the live
    # site, so an edit cannot quietly reintroduce a dead endpoint.
    check("facebook prompt: says the web surface is an error, not a login wall",
          "400" in p and "not** a profile and **not** a login" in p.replace("\n", " "))
    check("facebook prompt: warns the crawler trick is dead",
          "crawler User-Agent trick is dead" in p.replace("\n", " "))
    check("facebook prompt: names both Graph walls and their distinct codes",
          "code\":104" in p and "code\":200" in p and "403" in p)
    check("facebook prompt: names the avatar endpoint as the one live source",
          "graph.facebook.com" in p and "/picture" in p)
    check("facebook prompt: says the avatar endpoint is an existence oracle",
          "existence oracle" in p.lower())
    check("facebook prompt: gives the default-avatar hash to test against",
          "3e8f62364b0f574a7d18a6c8b26730f1" in p)
    check("facebook prompt: names the Internet Archive as the richest source",
          "Internet Archive" in p and "wayback" in p.lower())
    check("facebook prompt: warns off the refuted avatar-filename pivot",
          "middle and last fields both return 400" in p
          and "not the page's own" in p)

    # A leaked credential is described, never copied into the report — the
    # same restraint every other kind's playbook carries.
    check("facebook prompt: says to describe not reproduce a secret",
          "never reproduce it" in p and "do not write\n     the value" in p)

    check("facebook prompt: requires a limits section",
          "LIMITS" in p and "without a token" in p)


def test_prompt_tiktok(tmp: Path) -> None:
    tk = osint.OSINT_KINDS["tiktok"]
    case = tmp / "tkcase"
    p = osint.build_prompt(tk, "charlidamelio", case)

    check("tiktok prompt: carries the handle", "charlidamelio" in p)
    check("tiktok prompt: carries the case dir", str(case) in p)
    check("tiktok prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)
    check("tiktok prompt: states the authorised-use condition",
          "Authorised investigation only" in p)
    check("tiktok prompt: asks for report.md", "report.md" in p)
    check("tiktok prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"tiktok prompt: report format has {needle!r}", needle in p)

    p_url = osint.build_prompt(tk, "https://www.tiktok.com/@charlidamelio?lang=en",
                               case)
    check("tiktok prompt: normalises a pasted profile URL",
          "charlidamelio" in p_url and "?lang=en" not in p_url)

    # ---- the measured facts -------------------------------------------
    check("tiktok prompt: names the rehydration blob as the payload",
          "__UNIVERSAL_DATA_FOR_REHYDRATION__" in p)
    check("tiktok prompt: warns there are no og: tags in the raw HTML",
          "og:" in p and "JavaScript" in p)
    check("tiktok prompt: names oembed as the rate-limited wall",
          "oembed" in p.lower() and "429" in p and "ratelimit triggered" in p)
    check("tiktok prompt: says the oembed limiter is independent of the HTML one",
          "independent" in p.lower())
    check("tiktok prompt: says the HTTP status carries no existence signal",
          "200" in p and "userInfo" in p and "10221" in p)
    check("tiktok prompt: warns statusMsg lies",
          "user banned" in p)
    check("tiktok prompt: says the mobile surface is gone",
          "404" in p and "m.tiktok.com" in p)
    check("tiktok prompt: says the internal API returns an empty body",
          "zero-byte" in p or "empty body" in p.lower())
    check("tiktok prompt: says to use statsV2 not stats",
          "statsV2" in p)
    check("tiktok prompt: says the snowflake decode does not work",
          "snowflake" in p.lower() and "does not work" in p.lower())
    check("tiktok prompt: says createTime is plain Unix seconds",
          "createTime" in p and "seconds" in p.lower())
    check("tiktok prompt: says secUid is opaque",
          "secUid" in p and "opaque" in p.lower())

    check("tiktok prompt: requires a limits section",
          "LIMITS" in p)


def test_prompt_instagram(tmp: Path) -> None:
    ig = osint.OSINT_KINDS["instagram"]
    case = tmp / "igcase"
    p = osint.build_prompt(ig, "nasa", case)

    check("instagram prompt: carries the handle", "nasa" in p)
    check("instagram prompt: carries the case dir", str(case) in p)
    check("instagram prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)
    check("instagram prompt: states the authorised-use condition",
          "Authorised investigation only" in p)
    check("instagram prompt: asks for report.md", "report.md" in p)
    check("instagram prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"instagram prompt: report format has {needle!r}", needle in p)

    p_url = osint.build_prompt(ig, "https://www.instagram.com/nasa/", case)
    check("instagram prompt: normalises a pasted profile URL",
          "nasa" in p_url and "instagram.com/nasa" not in p_url)

    # ---- the measured facts -------------------------------------------
    # Instagram is the kind where the *obvious* route fails in a way that looks
    # like success, so the walls are the most important part of the playbook.
    check("instagram prompt: says ?__a=1 is a zero-byte 201",
          "__a=1" in p and "201" in p and "zero-byte" in p)
    check("instagram prompt: says every JSON API endpoint needs a login",
          "401" in p and "require_login" in p)
    check("instagram prompt: names the enumeration oracles as 429",
          "429" in p and "web_create_ajax" in p)
    check("instagram prompt: says the profile HTML is identical real vs fake",
          "byte-identical" in p and "634" in p)
    check("instagram prompt: says the profile URL carries no existence signal",
          "tells you nothing" in p and "byte-identical" in p)
    check("instagram prompt: names the crawler surface as the one live route",
          "Googlebot" in p and "RelayPreloader" in p)
    check("instagram prompt: gives the exact Googlebot UA string",
          "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)" in p)
    check("instagram prompt: warns the gate is the exact string, not the word",
          "bare `Googlebot/2.1`" in p or "bare Googlebot" in p)
    check("instagram prompt: names the record key to parse",
          "xig_user_by_igid_v2" in p)
    check("instagram prompt: names the record fields",
          "follower_count" in p and "biography" in p and "is_private" in p)
    check("instagram prompt: says the crawler size difference is the oracle",
          "1.3 MB" in p and "634 KB" in p)
    check("instagram prompt: says the IGID prefix is a fixed namespace",
          "178414" in p and "namespace" in p)
    check("instagram prompt: says there is no creation timestamp",
          "no creation timestamp" in p.lower() or "creation timestamp" in p)
    check("instagram prompt: warns off the snowflake decode",
          "snowflake" in p.lower())
    check("instagram prompt: warns third-party viewers are unreliable",
          "third-party" in p.lower() and "hijacked" in p)
    check("instagram prompt: says the Facebook picture pivot does not resolve",
          "graph.facebook.com" in p and "silhouette" in p)

    check("instagram prompt: requires a limits section",
          "LIMITS" in p)


# ---------------------------------------------------------------------------
# Wiring — the command must be reachable from every surface
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    # Completion popup lists it...
    names = [c for c, _ in SLASH_COMMANDS]
    check("wiring: /osint is in the completion menu", "/osint" in names, f"{names}")

    # ...and the dispatch, help and palette tables reference it. Read from the
    # source rather than importing a copy, so a rename cannot drift past this.
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: dispatch has an osint entry", '"osint": lambda a:' in app_src)
    check("wiring: _cmd_osint is defined", "def _cmd_osint(" in app_src)
    check("wiring: help table lists it", '("/osint [kind] <target>"' in app_src)
    check("wiring: palette lists it",
          '("/osint", "open-source recon on a target", "osint")' in app_src)


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


async def test_ui(wd: Path) -> None:
    """Drive the command through the live app: menu, refusal, bad target.

    The model is stubbed so no request leaves the machine, and the turn is
    held open on an event so `busy` can be observed while it is genuinely in
    flight rather than raced against an instant completion.
    """
    cfg = _cfg(wd, boot_rain=False)
    # Point state_dir at the temp dir — the default is ~/.zimzilla, and this
    # test must not scatter case directories into the operator's home.
    #
    # A state_dir of its own, not the one test_case_dir uses: the case-dir
    # stamp is second-resolution, so two tests creating a directory for the
    # same target within the same second land on the SAME path (mkdir with
    # exist_ok=True reuses it). Sharing would make any count- or delta-based
    # assertion here depend on how fast the suite runs.
    cfg.state_dir = wd / "ui-state"

    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        gate = asyncio.Event()

        async def held_stream(max_tokens=None):
            await gate.wait()
            yield (None, _Msg([_Blk(type="text", text="done")]))

        app.agent._stream_once = held_stream

        # ---- every refusal path must leave the harness idle ----------------
        app._handle_command("/osint")
        await pilot.pause()
        check("ui: bare /osint does not start a turn", app.busy is False)

        app._handle_command("/osint nosuchkind foo")
        await pilot.pause()
        check("ui: unknown kind is refused", app.busy is False)

        app._handle_command("/osint phone +15551234567")
        await pilot.pause()
        check("ui: a stub kind does not start a turn", app.busy is False)

        app._handle_command("/osint email not-an-address")
        await pilot.pause()
        check("ui: a bad email is refused", app.busy is False)
        # No case dir for the *refused* target — checked by its own glob, not
        # by the parent existing, which an earlier test in this file shares.
        check("ui: a refused run creates no case dir",
              not list((cfg.state_dir / "osint").glob("email-not-an-address-*")))

        app._handle_command("/osint github not a handle")
        await pilot.pause()
        check("ui: a bad github handle is refused", app.busy is False)
        check("ui: a refused github run creates no case dir",
              not list((cfg.state_dir / "osint").glob("github-not*")))

        # ---- the happy path launches a turn --------------------------------
        app._handle_command("/osint email target@example.com")
        await pilot.pause()
        check("ui: a valid email launches the recon turn", app.busy is True)

        made = list((cfg.state_dir / "osint").glob("email-target-example.com-*"))
        check("ui: the case directory was created", len(made) == 1,
              f"{[p.name for p in made]}")

        # Let the stubbed turn finish and confirm the harness returns to idle.
        gate.set()
        for _ in range(5):
            await pilot.pause()
        check("ui: the turn completes and the harness idles", app.busy is False)

        # The stub produced no report, so the case dir must be KEPT — a run
        # that did not finish writing is never tidied away.
        check("ui: a run with no report keeps its case dir",
              len(made) == 1 and made[0].is_dir(),
              f"{[p.name for p in made]}")

        # ---- github, launched from a pasted profile URL -------------------
        # The URL must be unwrapped before it reaches the case directory, so
        # the evidence path is named after the handle and not the host. The
        # glob is enough on its own: this test has its own state_dir, so any
        # match here is one this run created.
        #
        # This run's stub writes a report into the case dir before finishing,
        # so the finalise path is exercised end to end: report archived,
        # case dir cleared.
        gate.clear()

        async def reporting_stream(max_tokens=None):
            await gate.wait()
            # The case dir is the only one under osint/ for this state_dir.
            case = next((cfg.state_dir / "osint").glob("github-torvalds-*"))
            (case / "report.md").write_text("# GitHub OSINT — torvalds\n")
            (case / "clone").mkdir(exist_ok=True)
            yield (None, _Msg([_Blk(type="text", text="done")]))

        app.agent._stream_once = reporting_stream

        app._handle_command("/osint github https://github.com/torvalds")
        await pilot.pause()
        check("ui: a github URL launches the recon turn", app.busy is True)

        gmade = list((cfg.state_dir / "osint").glob("github-torvalds-*"))
        check("ui: the github case dir is named after the handle",
              len(gmade) == 1, f"{[q.name for q in gmade]}")

        gate.set()
        for _ in range(5):
            await pilot.pause()
        check("ui: the github turn completes", app.busy is False)

        # The report was archived and the bulky case dir removed.
        check("ui: the case dir is cleared after a reported run",
              not gmade[0].exists(), str(gmade[0]))
        archived = list((cfg.state_dir / "reports").glob("github-torvalds-*.md"))
        check("ui: the report is archived under reports/",
              len(archived) == 1, f"{[p.name for p in archived]}")


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_registry()
        test_validation()
        test_normalise()
        test_case_dir(tmp)
        test_finalise(tmp)
        test_prompt(tmp)
        test_prompt_github(tmp)
        test_prompt_discord(tmp)
        test_prompt_facebook(tmp)
        test_prompt_tiktok(tmp)
        test_prompt_instagram(tmp)
        test_wiring()
        await test_ui(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
