#!/usr/bin/env python3
"""Prove that neither platform's behaviour changed.

Runs harness.py once per platform against this checkout and diffs the report
with the frozen reference:

  os4.golden.txt       captured from main @ 563d2f9 (pre-merge RGB-Pi OS4)
  replayos.golden.txt  captured from replayos @ 8f453b7 (pre-merge RePlayOS)

Any difference means a change leaked into a platform's behaviour (mpv
command line, child env, launcher env, display handling, settings menu...).
Exit status 1 + a unified diff in that case.

  python3 tests/platform_golden/check.py            # verify both
  python3 tests/platform_golden/check.py os4        # verify one
  python3 tests/platform_golden/check.py --update   # accept the new behaviour

Only use --update for an INTENDED behaviour change, and review the diff it
prints before committing the new reference.
"""
from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PLATFORMS = ("os4", "replayos")


def report(platform: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(HERE / "harness.py"), platform, str(REPO)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"harness crashed for {platform} (exit {proc.returncode})")
    return proc.stdout


def main(argv: list[str]) -> int:
    update = "--update" in argv
    wanted = [a for a in argv if a in PLATFORMS] or list(PLATFORMS)
    failed = False
    for platform in wanted:
        golden = HERE / f"{platform}.golden.txt"
        actual = report(platform)
        expected = golden.read_text() if golden.exists() else ""
        if actual == expected:
            print(f"{platform}: OK — identical to {golden.name}")
            continue
        diff = list(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{golden.name} (reference)",
                tofile=f"{platform} (this checkout)",
            )
        )
        sys.stdout.writelines(diff)
        if update:
            golden.write_text(actual)
            print(f"{platform}: reference UPDATED ({len(diff)} diff lines above)")
        else:
            print(f"{platform}: CHANGED — {len(diff)} diff lines above")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
