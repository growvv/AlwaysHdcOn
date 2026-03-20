#!/usr/bin/env python3
"""Compatibility wrapper for the legacy entrypoint.

This wrapper keeps old invocations working and forwards to always-hdc-on.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    new_script = here / "always-hdc-on.py"

    print(
        "[DEPRECATED] scripts/watch-hdc-devices.py has been replaced by "
        "scripts/always-hdc-on.py",
        file=sys.stderr,
    )

    # Legacy behavior: default continuous foreground loop; --once for single loop.
    mapped = [sys.executable, str(new_script), "run"]
    if "--once" in argv:
        mapped.append("--once")
        argv = [a for a in argv if a != "--once"]

    # Pass through compatible common options when present.
    passthrough_flags = {
        "--devices-json",
        "--pid-file",
        "--status-file",
        "--log-file",
        "--interval",
        "--scan-timeout",
        "--scan-concurrency",
        "--max-scan-hosts",
        "--common-ports",
    }

    i = 0
    while i < len(argv):
        token = argv[i]
        if token in passthrough_flags:
            mapped.append(token)
            if i + 1 < len(argv):
                mapped.append(argv[i + 1])
                i += 2
                continue
        i += 1

    return subprocess.call(mapped)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
