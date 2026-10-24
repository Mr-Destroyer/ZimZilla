"""Live check for the OrcaRouter source — the only test here that uses a network.

Run:  python tests/test_orcarouter_live.py

Every other suite stubs ``urlopen``. This one deliberately does not: it is the
proof that the module can talk to the real gateway, through the same code the
TUI uses (``fetch_models`` for the catalog, ``verify_key`` for a tiny Messages
turn — not a hand-rolled curl).

The credential comes from ``ORCAROUTER_API_KEY`` in the environment and is
never printed. With no key in the environment the check reports SKIP and exits
0, so a normal ``python tests/test_orcarouter_live.py`` on a machine without an
account is not a failure; the campaign verifier grants the key, and then every
assertion below is real.

Model choice: the cyber tier first (``orca/orcacyber-ultra-1.0`` then
``orca/orcacyber-zero-1.5``), because that is the tier this provider is for.
A key whose scope excludes the tier answers 403 ``model_access_denied`` — that
is still a valid key, and ``verify_key`` is written to say so. The validation
fallback is a general model, used only to prove the wire path when the tier is
not on the account; it is never offered to users as a default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []

#: What the campaign grants. Named in one place so the test states its source.
KEY_ENV = "ORCAROUTER_API_KEY"

#: Cyber tier first, then a general model purely to prove connectivity.
PROBE_ORDER = (
    "orca/orcacyber-ultra-1.0",
    "orca/orcacyber-zero-1.5",
    "deepseek/deepseek-v4.1-flash",
)


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))


def main() -> int:
    token = (os.environ.get(KEY_ENV) or "").strip()
    if not token:
        print(f"live: SKIPPED ({KEY_ENV} is not set in this environment)")
        print("0/0 live checks run — no failure")
        return 0

    from zimzilla import orcarouter as o

    check("live: the key has the documented prefix",
          token.startswith(o.KEY_PREFIX))

    # 1. Catalog, through the module's own fetch path.
    models, err = o.fetch_models(token, use_cache=False)
    check("live: GET /v1/models succeeds through fetch_models",
          models is not None, err or f"{len(models or [])} entries")
    if models is None:
        return 1
    check("live: the catalog is not empty", len(models) > 0, len(models))
    check("live: the default router is in the catalog",
          any(m.id == o.DEFAULT_MODEL for m in models), o.DEFAULT_MODEL)
    chat, cerr = o.chat_models(token, use_cache=False)
    check("live: the capability filter returns chat entries",
          chat is not None and len(chat) > 0,
          cerr or f"{len(chat or [])} chat entries")
    check("live: every filtered entry declares a chat endpoint type",
          all(set(m.endpoint_types) & o.CHAT_ENDPOINT_TYPES for m in chat or []))

    # 2. A real Messages turn. verify_key says False only for a rejected
    #    credential; a tier/scope answer is reported and still counts as a
    #    working key.
    chosen = ""
    for model in PROBE_ORDER:
        ok, detail = o.verify_key(token, model)
        print(f"  probe {model}: {ok} — {detail}")
        if ok:
            chosen = model
            break
    check("live: one real Messages call is answered through the provider",
          bool(chosen), chosen or "no model answered")

    # 3. The error classifier, on a status the live endpoint produces.
    if chosen:
        ok, detail = o.verify_key(token, "definitely-not-a-model-000")
        check("live: an unknown model is reported without blaming the key",
              ok, detail)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} live checks passed "
              f"— FAILED: " + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} live checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
