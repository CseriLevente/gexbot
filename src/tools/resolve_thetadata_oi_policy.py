"""Create a privacy-safe OI policy report from email and capture evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence

from src.adapters.thetadata.oi_policy import (
    OpenInterestPolicyEvidenceError,
    resolve_open_interest_policy,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.resolve_thetadata_oi_policy",
        description=(
            "Bind a preserved ThetaData support email to an existing capture "
            "certification without copying private correspondence into output."
        ),
    )
    parser.add_argument("email", type=pathlib.Path)
    parser.add_argument("certification", type=pathlib.Path)
    parser.add_argument("--json", dest="output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)

    try:
        certification = json.loads(args.certification.read_text(encoding="utf-8"))
        report = resolve_open_interest_policy(args.email, certification)
        payload = report.as_dict()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        OpenInterestPolicyEvidenceError,
    ) as exc:
        print(f"OI policy resolution failed: {exc}", file=sys.stderr)
        return 2

    policy = payload["policy_resolution"]
    coverage = payload["open_interest_coverage"]
    review = payload["full_expiration_review"]
    print(f"OI policy report: {payload['report_hash']}")
    print(f"Missing-row treatment: {policy['missing_row_treatment']}")
    print(
        f"Coverage: {coverage['oi_covered']} answered, "
        f"{coverage['oi_missing']} unavailable"
    )
    print(
        "Trusted full-universe OI aggregate: "
        f"{policy['permits_trusted_full_universe_aggregate']}"
    )
    print(
        "Full-expiration follow-up: "
        f"{review['status']} {review['symbol']} {review['expiration']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
