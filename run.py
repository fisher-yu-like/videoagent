"""Run the offline whole-story suite with one positional configuration file."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from videoactagent.whole_story import SuiteError, load_suite, run_suite


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render and validate the offline whole-story proxy suite."
    )
    parser.add_argument("config", type=Path, help="whole-story suite JSON")
    args = parser.parse_args(argv)
    try:
        summary = run_suite(load_suite(args.config))
    except (OSError, SuiteError, ValueError) as exc:
        print(f"whole-story run failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
