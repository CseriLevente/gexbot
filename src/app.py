"""Runnable demo: build a chain, compute a GEX snapshot, print it.

    python -m src.app

Uses the synthetic adapter, so it needs no subscription, no key and no network.
The point is to make the engine's output inspectable in one command -- including
the parts that are deliberately *not* ready, like the uncalibrated confidence
components and the convention spread on the zero-gamma level.
"""

from __future__ import annotations

from src.adapters.fixtures.synthetic import SyntheticOptionsDataSource
from src.domain.gex import ExpiryBucket
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern
from src.gex.walls import distance_pct


def _millions(value: float) -> str:
    return f"{value / 1e9:>12,.3f} bn"


def main() -> None:
    as_of = eastern(2026, 3, 17, 11, 0)
    source = SyntheticOptionsDataSource()
    chain = source.fetch_chain(as_of=as_of)

    # prefer_vendor_gamma=False mirrors a ThetaData Standard subscription: IV is
    # available, gamma is not, so the shadow pricer supplies it.
    config = GexEngineConfig(prefer_vendor_gamma=False)
    snapshot = compute_gex_snapshot(
        chain,
        config,
        expected_contract_count=source.expected_contract_count(as_of=as_of),
    )

    print(f"\n{'=' * 72}")
    print(f"GEX snapshot  {snapshot.as_of:%Y-%m-%d %H:%M %Z}   source={snapshot.source}")
    print(f"{'=' * 72}")
    print(f"spot                {snapshot.spot:>12,.2f}")
    print(f"contracts           {snapshot.contract_count:>12,}")
    print(f"total open interest {snapshot.total_open_interest:>12,}")
    print(f"sign convention     {snapshot.sign_convention.value}")

    print(f"\n-- views 1 & 2: chain totals (per 1% spot move) {'-' * 24}")
    print(f"unsigned GEX  {_millions(snapshot.total_unsigned_gex)}")
    print(f"signed GEX    {_millions(snapshot.total_signed_gex)}", end="")
    print("   (negative = short dealer gamma proxy)")

    print(f"\n-- view 3: expiry buckets {'-' * 46}")
    print(f"{'bucket':<12}{'unsigned':>16}{'signed':>16}{'contracts':>11}{'share':>8}")
    total = snapshot.total_unsigned_gex or 1.0
    for bucket in ExpiryBucket:
        entry = snapshot.bucket(bucket)
        print(
            f"{entry.bucket.value:<12}{entry.unsigned_gex / 1e9:>13,.3f} bn"
            f"{entry.signed_gex / 1e9:>13,.3f} bn{entry.contract_count:>11,}"
            f"{entry.unsigned_gex / total * 100:>7.1f}%"
        )

    print(f"\n-- view 4: structural levels {'-' * 43}")
    walls = snapshot.walls
    for label, level in (
        ("call wall", walls.call_wall),
        ("put wall", walls.put_wall),
        ("largest |gamma|", walls.largest_abs_gamma_strike),
    ):
        offset = distance_pct(snapshot.spot, level)
        shown = f"{level:,.0f}" if level is not None else "n/a"
        suffix = f" ({offset:+.2f}% from spot)" if offset is not None else ""
        print(f"{label:<16}{shown:>12}{suffix}")
    print(f"{'positive nodes':<16}{str(list(walls.positive_gamma_nodes)):>12}")
    print(f"{'negative nodes':<16}{str(list(walls.negative_gamma_nodes)):>12}")
    print(f"{'gamma voids':<16}", end="")
    print(
        ", ".join(f"{v.low_strike:,.0f}-{v.high_strike:,.0f}" for v in walls.gamma_voids)
        or "none"
    )

    print(f"\n-- view 5: zero gamma by IV convention {'-' * 33}")
    for result in snapshot.zero_gamma:
        if result.resolved:
            offset = distance_pct(snapshot.spot, result.zero_gamma_spot)
            detail = f"{result.zero_gamma_spot:>10,.1f}  ({offset:+.2f}%)"
            if result.sign_changes > 1:
                detail += f"  [{result.sign_changes} crossings -- ambiguous]"
        else:
            detail = f"{'unresolved':>10}  (no sign change in grid)"
        print(f"{result.convention.value:<16}{detail}")
    spread = snapshot.zero_gamma_spread_pct
    if spread is not None:
        print(f"{'convention spread':<16}{spread:>10.4f}% of spot  <- the error bar")

    print(f"\n-- confidence {'-' * 58}")
    print(f"score {snapshot.confidence.value:.1f} / 100", end="")
    if snapshot.confidence.calibrated:
        print("   [calibrated]")
    else:
        print("   [UNCALIBRATED -- live trading blocked]")
    for component in snapshot.confidence.components:
        flag = " *" if component.uncalibrated else "  "
        print(
            f"{flag}{component.name:<24}{component.score:>6.3f}"
            f"  w={component.weight:.2f}  {component.detail}"
        )
    if not snapshot.confidence.calibrated:
        print(
            "\n  * threshold still UNSPECIFIED_CALIBRATE -- scored with a "
            "pessimistic placeholder."
        )

    if snapshot.warnings:
        print(f"\n-- warnings {'-' * 60}")
        for warning in snapshot.warnings:
            print(f"  ! {warning}")
    print()


if __name__ == "__main__":
    main()
