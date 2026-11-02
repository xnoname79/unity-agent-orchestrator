#!/usr/bin/env python3
"""Run every check in the repo, the same way on a laptop, in CI, and for a coding agent.

    python verify.py            # all checks
    python verify.py boards mcp # only check_boards.py and check_mcp.py

Checks are DISCOVERED (check_*.py at the root + static/orchestrator/check_ui.py), never listed
by hand. A hand-kept list in CI is how check_cli_sessions, check_groups, check_model_switch and
check_skill_sync were written, passed locally, and never ran in CI. Exit code is non-zero if any
check fails, and the tail of each failure is printed so the reason is visible without a rerun.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The Windows console is cp1252: printing a failing check's output (Vietnamese log lines, ⚠) would
# raise UnicodeEncodeError here and hide the very failure we are trying to show.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def discover(only=()):
    found = sorted(ROOT.glob("check_*.py")) + [ROOT / "static" / "orchestrator" / "check_ui.py"]
    if only:
        found = [p for p in found if p.stem.removeprefix("check_") in only]
    return [p for p in found if p.is_file()]


def main(argv):
    checks = discover(set(argv))
    if not checks:
        print("no checks matched", argv)
        return 2
    failed = []
    for p in checks:
        t0 = time.monotonic()
        # Run directly in a Windows console, a check prints Unicode fine; through a pipe its stdout
        # falls back to cp1252 and the first ⚠ kills it. PYTHONIOENCODING only touches stdio, so
        # CI's PYTHONUTF8=0 still catches files opened without encoding="utf-8".
        r = subprocess.run([sys.executable, str(p)], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        dt = time.monotonic() - t0
        ok = r.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {p.relative_to(ROOT)}  ({dt:.1f}s)", flush=True)
        if not ok:
            failed.append(p)
            tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
            print("      " + "\n      ".join(tail), flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
