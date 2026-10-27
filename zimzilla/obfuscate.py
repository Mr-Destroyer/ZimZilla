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

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("--help", "-h"):
        print_usage()
        return 0

    filepath = argv[0]

    if not os.path.isfile(filepath):
        print(f"error: '{filepath}' is not a file"); print_usage(); return 1

    # Analyze the file
    analysis = analyze_payload_file(filepath)

    print(f"=== Obfuscation Analysis: {filepath} ===")
    print(f"  Extension: .{detect_extension(filepath)}")
    print(f"  Category: {analysis['category'].upper()}")
    print(f"  Description: {analysis['description']}")
    if analysis['default_payload']:
        print(f"  Default payload: {analysis['default_payload']}")
    print()

    # Show recommended encoders
    print("  Recommended encoders for this file type:")
    for rec in analysis['encoders']:
        enc = rec["name"]
        it = rec["iterations"]
        notes = rec["notes"] if rec["notes"] else ""
        print(f"    - {enc} (iterations={it})")
        if notes:
            print(f"      Notes: {notes}")

    print()
    print("  Would you like to run an encoder now? (This would be integrated")
    print("  into the full /obfuscate command in the TUI.)")
    print()

    return 0


def print_usage():
    print("""/obfuscate <file> -- obfuscate a payload file

  /obfuscate virus.apk     obfuscate an Android APK
  /obfuscate payload.exe  obfuscate a Windows EXE
  /obfuscate shell.ps1    obfuscate a PowerShell script
  /obfuscate web.php      obfuscate a PHP script
  /obfuscate app.hta      obfuscate an HTA application
  /obfuscate shell.elf    obfuscate a Linux ELF binary

  The command auto-detects the file type and recommends encoders.
  No need to specify payload IDs or encoder names.
""")

if __name__ == "__main__":
    sys.exit(main())
