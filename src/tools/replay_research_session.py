"""Offline replay of normalized research bytes and counterfactual fill probes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from src.replay.session import replay_bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        root, output = args.bundle.resolve(), args.json.resolve()
        if output.exists() or output.is_relative_to(root):
            raise ValueError("output must be unused and outside source bundle")
        report = replay_bundle(root)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
            )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"research replay refused: {error}", file=sys.stderr)
        return 2
    print(f"report_hash: {report['report_hash']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
