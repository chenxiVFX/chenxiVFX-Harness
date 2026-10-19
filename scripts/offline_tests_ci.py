from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _annotation(message: str) -> str:
    # GitHub workflow command data must escape percent before line endings.
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    if result.wasSuccessful():
        print(
            f"Offline tests: total={result.testsRun}, "
            f"passed={result.testsRun - len(result.skipped)}, "
            f"skipped={len(result.skipped)}",
            flush=True,
        )
        return 0

    # Only this repository's synthetic tests run here. Keep diagnostics on the
    # normal check interface so reading a failure never requires a log download.
    problems = result.errors + result.failures
    for test, details in problems[:10]:
        message = f"{test.id()}\n{details}"[:16 * 1024]
        print(f"::error title=Offline test failure::{_annotation(message)}", flush=True)
    for test in result.unexpectedSuccesses[:10]:
        print(
            f"::error title=Unexpected test success::{_annotation(test.id())}",
            flush=True,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
