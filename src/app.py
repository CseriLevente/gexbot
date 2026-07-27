"""Runnable demo: build a chain, compute a GEX snapshot, print it.

    python -m src.app

Uses the synthetic adapter and the research profile, so it needs no
subscription, no key and no network. The point is to make the engine's output
inspectable in one command -- including the parts that are deliberately *not*
ready: the uncalibrated confidence components, the convention spread on the
zero-gamma level, and the option-universe difference between the chain totals and
the grid.
"""

from __future__ import annotations

import pathlib

from src.adapters.synthetic.source import SyntheticOptionsDataSource
from src.config.schema import load_config
from src.domain.gex import ExpiryBucket, GexSnapshot
from src.gex.engine import compute_floor_sensitivity, compute_gex_snapshot
from src.gex.walls import distance_pct
from src.synthetic.chains import (
    DEFAULT_AS_OF,
    LATE_SESSION_AS_OF,
    SyntheticChainSpec,
    build_synthetic_chain,
)

CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "research.yaml"
RULE = "-" * 76


def _bn(value: float) -> str:
    return f"{value / 1e9:>13,.3f} bn"


def _level(spot: float, level: float | None) -> str:
    if level is None:
        return f"{'n/a':>12}"
    offset = distance_pct(spot, level)
    return (
        f"{level:>12,.0f} ({offset:+.2f}%)" if offset is not None else f"{level:,.0f}"
    )


def _print_header(snapshot: GexSnapshot, config_path: pathlib.Path) -> None:
    print(f"\n{'=' * 76}")
    print(
        f"GEX snapshot  {snapshot.as_of:%Y-%m-%d %H:%M %Z}   source={snapshot.source}"
    )
    print("=" * 76)
    print(f"spot                 {snapshot.spot:>14,.2f}")
    print(f"contracts            {snapshot.contract_count:>14,}")
    print(f"total open interest  {snapshot.total_open_interest:>14,}")
    print(f"sign convention      {snapshot.sign_convention.value}")
    print(f"model                {snapshot.model_spec.describe()}")
    print(f"config               {config_path.name} @ {snapshot.config_fingerprint}")


def _print_totals(snapshot: GexSnapshot) -> None:
    print(f"\n-- views 1 & 2: chain totals (per 1% spot move) {RULE[:29]}")
    print(f"unsigned GEX  {_bn(snapshot.total_unsigned_gex)}")
    print(
        f"signed GEX    {_bn(snapshot.total_signed_gex)}"
        "   (negative = short dealer gamma under the stated proxy)"
    )


def _print_buckets(snapshot: GexSnapshot) -> None:
    print(f"\n-- view 3: expiry buckets {RULE[:50]}")
    print(f"{'bucket':<12}{'unsigned':>16}{'signed':>16}{'contracts':>11}{'share':>8}")
    total = snapshot.total_unsigned_gex or 1.0
    for bucket in ExpiryBucket:
        entry = snapshot.bucket(bucket)
        if entry is None:  # pragma: no cover - buckets are always zero-filled
            continue
        print(
            f"{entry.bucket.value:<12}{_bn(entry.unsigned_gex)}"
            f"{_bn(entry.signed_gex)}{entry.contract_count:>11,}"
            f"{entry.unsigned_gex / total * 100:>7.1f}%"
        )


def _print_levels(snapshot: GexSnapshot) -> None:
    walls = snapshot.walls
    spot = snapshot.spot
    print(f"\n-- view 4: structural levels {RULE[:47]}")
    print("  neutral observations:")
    print(f"    largest call gamma {_level(spot, walls.largest_call_gamma_strike)}")
    print(f"    largest put gamma  {_level(spot, walls.largest_put_gamma_strike)}")
    print(f"    largest |gamma|    {_level(spot, walls.largest_unsigned_gamma_strike)}")
    print("  directional interpretations (n/a when nothing qualifies):")
    print(f"    upside call wall   {_level(spot, walls.upside_call_wall)}")
    print(f"    downside put wall  {_level(spot, walls.downside_put_wall)}")
    print(f"  positive nodes  {list(walls.positive_gamma_nodes)}")
    print(f"  negative nodes  {list(walls.negative_gamma_nodes)}")
    if walls.gamma_voids:
        print("  gamma voids:")
        for void in walls.gamma_voids:
            marker = "tradable" if void.is_tradable_structure else "NOT tradable"
            print(
                f"    {void.low_strike:,.0f}-{void.high_strike:,.0f}  "
                f"{void.kind.value:<24} [{marker}]"
            )
    else:
        print("  gamma voids     none")


def _print_zero_gamma(snapshot: GexSnapshot) -> None:
    print(f"\n-- view 5: zero gamma by IV convention {RULE[:37]}")
    for result in snapshot.zero_gamma:
        label = result.convention.value
        if result.unimplemented_reason:
            print(
                f"{label:<18}{'NOT IMPLEMENTED':>14}  ({result.unimplemented_reason})"
            )
            continue
        if result.selected_root is None:
            reason = (
                "curve identically zero"
                if result.identically_zero_curve
                else f"no crossing after {result.grid_expansions} expansion(s)"
            )
            print(f"{label:<18}{'unresolved':>14}  ({reason})")
            continue
        offset = result.selected_root_distance_from_spot_pct or 0.0
        flags: list[str] = []
        if result.root_count > 1:
            roots = [round(r, 1) for r in result.all_roots]
            flags.append(f"{result.root_count} roots {roots}")
        if result.root_near_boundary:
            flags.append("near grid boundary")
        if result.normalised_slope is not None:
            flags.append(f"slope {result.normalised_slope:.3f}")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        print(f"{label:<18}{result.selected_root:>14,.1f}  ({offset:+.2f}%){suffix}")
    spread = snapshot.zero_gamma_spread_pct
    if spread is not None:
        print(f"{'convention spread':<18}{spread:>14.4f}% of spot  <- the error bar")
    print(f"{'selection rule':<18}nearest root to spot -- a convention, not a claim")


def _print_universe(snapshot: GexSnapshot) -> None:
    grid = snapshot.zero_gamma_universe
    chain = snapshot.chain_universe
    print(f"\n-- option universe {RULE[:57]}")
    print(f"chain totals cover   {chain.included_contract_count:>5} contracts")
    share = grid.included_unsigned_gex_share
    covered = f", {share:.1%} of chain unsigned GEX" if share is not None else ""
    print(
        f"zero-gamma grid      {grid.included_contract_count:>5} contracts  "
        f"(max_dte={grid.max_dte_used}{covered})"
    )
    if grid.excluded_contract_count:
        print(f"excluded expirations {list(grid.excluded_expirations)}")


def _print_validation(snapshot: GexSnapshot) -> None:
    report = snapshot.validation
    print(f"\n-- validation {RULE[:62]}")
    print(
        f"accepted {report.accepted}  with-warning {report.accepted_with_warning}  "
        f"rejected {report.rejected}  (of {report.total})"
    )
    if report.error_counts:
        errors = {code.value: n for code, n in sorted(report.error_counts.items())}
        print(f"errors   {errors}")
    if report.warning_counts:
        warnings = {code.value: n for code, n in sorted(report.warning_counts.items())}
        print(f"warnings {warnings}")


def _print_confidence(snapshot: GexSnapshot) -> None:
    confidence = snapshot.confidence
    print(f"\n-- confidence {RULE[:62]}")
    state = "calibrated" if confidence.calibrated else "UNCALIBRATED"
    print(f"score {confidence.value:.1f} / 100   [{state}]")
    for component in confidence.components:
        if component.hard_failure:
            flag = "!"
        elif component.uncalibrated:
            flag = "*"
        else:
            flag = " "
        # "?" rather than a number when the component could not be evaluated at
        # all -- printing 0.000 would read as "measured, and bad".
        shown = f"{component.score:>6.3f}" if component.score is not None else "     ?"
        print(
            f" {flag}{component.name:<32}{shown}"
            f"  w={component.weight:.2f}  {component.detail}"
        )
    if confidence.hard_failures:
        print(f"\n  ! HARD FAILURE: {list(confidence.hard_failures)}")
    if not confidence.calibrated:
        print(
            "\n  * threshold still UNSPECIFIED_CALIBRATE -- scored with a "
            "pessimistic placeholder.\n"
            "    This is a research flag, NOT an enforcement mechanism: there is "
            "no risk engine\n    and no broker in this repository."
        )


def _print_floor_sensitivity() -> None:
    """Run the sensitivity sweep near expiry, where the floor actually binds."""
    late = build_synthetic_chain(SyntheticChainSpec(as_of=LATE_SESSION_AS_OF))
    report = compute_floor_sensitivity(late)
    print(f"\n-- 0DTE time-floor sensitivity (15 min to settlement) {RULE[:23]}")
    print(f"{'floor (min)':<14}{'0DTE unsigned':>18}{'zero gamma':>16}")
    for entry in report.entries:
        level = (
            f"{entry.zero_gamma_spot:,.1f}"
            if entry.zero_gamma_spot is not None
            else "unresolved"
        )
        print(f"{entry.floor_minutes:<14g}{_bn(entry.dte0_unsigned_gex)}{level:>16}")
    if report.dte0_range_pct is not None:
        print(
            f"0DTE bucket varies by {report.dte0_range_pct:.1f}% across floors -- "
            "the floor is a model choice, not a fact"
        )


def main() -> None:
    loaded = load_config(CONFIG_PATH)
    spec = SyntheticChainSpec()
    engine_config = loaded.engine.with_(model_spec=spec.model_spec())

    source = SyntheticOptionsDataSource(spec)
    chain = source.fetch_chain(as_of=DEFAULT_AS_OF)
    snapshot = compute_gex_snapshot(chain, engine_config)

    _print_header(snapshot, CONFIG_PATH)
    _print_totals(snapshot)
    _print_buckets(snapshot)
    _print_levels(snapshot)
    _print_zero_gamma(snapshot)
    _print_universe(snapshot)
    _print_validation(snapshot)
    _print_confidence(snapshot)
    _print_floor_sensitivity()

    if snapshot.warnings:
        print(f"\n-- warnings {RULE[:64]}")
        for warning in snapshot.warnings:
            print(f"  ! {warning}")

    print(f"\noutput hash  {snapshot.output_hash()}")
    print("This repository is research-only: it cannot place an order.\n")


if __name__ == "__main__":
    main()
