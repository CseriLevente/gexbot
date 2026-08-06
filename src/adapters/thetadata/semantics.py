"""Whether a parsed response can supply the thing it exists to supply.

``PARSER_VALID`` used to mean "the CSV became a list of dictionaries with the
expected column names". That is a claim about punctuation. The v2.1.16 index
response demonstrated the gap exactly: the parser reported valid, and
``fetch_index_snapshot()`` returned ``None``, because the adapter was reading a
column the vendor does not send. Both statements were true at once.

So syntax and semantics are separated. Syntax is "these rows parsed". Semantics
is "the domain value this endpoint owes is present, well-formed, and about the
instrument we asked for". A response can be the first without being the second,
and a report that cannot say which is not telling an operator anything they can
act on.

Nothing here changes acquisition. A semantically invalid response is bytes that
were captured, verified, and found wanting -- which is a finding about the
vendor, and the reason the first session exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "ENDPOINT_VALIDATORS",
    "EndpointSemanticReport",
    "SemanticStatus",
    "validate_endpoint_semantics",
]


class SemanticStatus(str, Enum):
    """Three separable answers, because they send an operator to three places."""

    #: The bytes did not parse at all. Syntax failed, so semantics were never
    #: reached.
    SYNTAX_INVALID = "SYNTAX_INVALID"
    #: Rows and columns are as expected, and nothing has looked at the values.
    SYNTAX_VALID = "SYNTAX_VALID"
    #: The endpoint can supply what it owes: the value is present, well-formed
    #: and about the instrument that was requested.
    SEMANTIC_VALID = "SEMANTIC_VALID"
    #: The rows parsed and the endpoint cannot supply what it owes. **The
    #: v2.1.16 index case.**
    SEMANTIC_INVALID = "SEMANTIC_INVALID"

    @property
    def usable(self) -> bool:
        return self is SemanticStatus.SEMANTIC_VALID


@dataclass(frozen=True, slots=True)
class EndpointSemanticReport:
    """What one endpoint's stored rows can and cannot establish."""

    endpoint: str
    status: SemanticStatus
    row_count: int = 0
    accepted_rows: int = 0
    findings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "semantic_status": self.status.value,
            "row_count": self.row_count,
            "accepted_rows": self.accepted_rows,
            "findings": list(self.findings),
        }


def _finite(value: Any) -> float | None:
    try:
        held = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return held if math.isfinite(held) else None


def _identity_findings(row: dict[str, str], where: str) -> list[str]:
    """Whether a row names a contract at all.

    Four fields. A row that cannot say which contract it is about cannot be
    joined to another response, and joining is what a chain *is*.
    """
    found: list[str] = []
    for name in ("symbol", "expiration", "strike", "right"):
        value = str(row.get(name, "")).strip()
        if not value:
            found.append(f"{where}: {name} is empty")
    strike = _finite(row.get("strike"))
    if strike is not None and strike <= 0:
        found.append(f"{where}: strike {row.get('strike')!r} is not positive")
    right = str(row.get("right", "")).strip().lower()
    if right and right not in ("call", "put", "c", "p"):
        found.append(f"{where}: right {row.get('right')!r} is neither call nor put")
    return found


def _index_findings(rows: list[dict[str, str]], expected_symbol: str) -> list[str]:
    """The one value this endpoint exists for, and whether it is there.

    Deliberately duplicates nothing: it asks the same questions
    :func:`index_snapshot_from` asks, so a report saying ``SEMANTIC_VALID`` and
    a fetch returning a snapshot cannot disagree.
    """
    from src.adapters.thetadata.client import INDEX_PRICE_FIELD

    if not rows:
        return ["no rows: an index snapshot with no print supplies no spot"]

    wanted = str(expected_symbol).strip().upper()
    matching = [r for r in rows if str(r.get("symbol", "")).strip().upper() == wanted]
    if not matching:
        seen = sorted({str(r.get("symbol", "")) for r in rows})
        return [f"no row for {wanted!r}; the response names {seen}"]
    if len(matching) > 1:
        return [
            f"{len(matching)} rows for {wanted!r}; which one is the spot is ambiguous"
        ]

    row = matching[0]
    found: list[str] = []
    if INDEX_PRICE_FIELD not in row:
        # The v2.1.16 defect, as a finding rather than a silent None.
        found.append(
            f"no {INDEX_PRICE_FIELD!r} column; the row carries {sorted(row)}. "
            "The documented v3 response is timestamp,symbol,price"
        )
    else:
        price = _finite(row.get(INDEX_PRICE_FIELD))
        if price is None or price <= 0:
            found.append(
                f"{INDEX_PRICE_FIELD}={row.get(INDEX_PRICE_FIELD)!r} is not a "
                "finite positive price"
            )
    if not str(row.get("timestamp", "")).strip():
        found.append("timestamp is empty; the spot cannot be placed in time")
    return found


def _quote_findings(rows: list[dict[str, str]], expected_symbol: str) -> list[str]:
    found: list[str] = []
    for index, row in enumerate(rows):
        where = f"row {index}"
        found.extend(_identity_findings(row, where))
        for name in ("bid", "ask"):
            held = str(row.get(name, "")).strip()
            if held and _finite(held) is None:
                found.append(f"{where}: {name}={held!r} is not finite")
    return found


def _open_interest_findings(
    rows: list[dict[str, str]], expected_symbol: str
) -> list[str]:
    """Open interest is the linear weight on every GEX term.

    A row whose open interest is absent, negative or fractional is a row that
    cannot weight anything, and the sum over a chain of such rows is a number
    with no meaning.
    """
    found: list[str] = []
    for index, row in enumerate(rows):
        where = f"row {index}"
        found.extend(_identity_findings(row, where))
        raw = str(row.get("open_interest", "")).strip()
        if not raw:
            found.append(f"{where}: open_interest is empty")
            continue
        try:
            value = int(raw)
        except ValueError:
            found.append(f"{where}: open_interest={raw!r} is not an integer")
            continue
        if value < 0:
            found.append(f"{where}: open_interest={value} is negative")
    return found


def _first_order_findings(
    rows: list[dict[str, str]], expected_symbol: str
) -> list[str]:
    found: list[str] = []
    for index, row in enumerate(rows):
        where = f"row {index}"
        found.extend(_identity_findings(row, where))
        held = str(row.get("implied_vol", "")).strip()
        if held and _finite(held) is None:
            found.append(f"{where}: implied_vol={held!r} is not finite")
        underlying = str(row.get("underlying_price", "")).strip()
        if underlying and _finite(underlying) is None:
            found.append(f"{where}: underlying_price={underlying!r} is not finite")
    return found


def _contract_list_findings(
    rows: list[dict[str, str]], expected_symbol: str
) -> list[str]:
    """A listing that cannot identify its contracts lists nothing."""
    found: list[str] = []
    for index, row in enumerate(rows):
        found.extend(_identity_findings(row, f"row {index}"))
    return found


#: Endpoint path -> the questions its rows must answer. Keyed by string so a
#: report can be produced for an endpoint this table does not know, which reads
#: as ``SYNTAX_VALID`` and nothing more: a response nobody has characterised
#: proves nothing, and defaulting the other way is how an endpoint would
#: silently acquire authority.
ENDPOINT_VALIDATORS: dict[str, Any] = {
    "/v3/index/snapshot/price": _index_findings,
    "/v3/option/snapshot/quote": _quote_findings,
    "/v3/option/snapshot/open_interest": _open_interest_findings,
    "/v3/option/snapshot/greeks/first_order": _first_order_findings,
    "/v3/option/list/contracts/quote": _contract_list_findings,
}


def validate_endpoint_semantics(
    *, endpoint: str, rows: list[dict[str, str]], expected_symbol: str
) -> EndpointSemanticReport:
    """Whether these rows can supply what this endpoint owes.

    ``SYNTAX_VALID`` for an endpoint with no validator: the rows parsed, and
    this code has no opinion about what they mean. Never ``SEMANTIC_VALID`` by
    default -- an unexamined response is not a verified one.
    """
    validator = ENDPOINT_VALIDATORS.get(endpoint)
    if validator is None:
        return EndpointSemanticReport(
            endpoint=endpoint,
            status=SemanticStatus.SYNTAX_VALID,
            row_count=len(rows),
            accepted_rows=len(rows),
            findings=("no semantic validator is defined for this endpoint",),
        )
    findings = list(validator(rows, expected_symbol))
    return EndpointSemanticReport(
        endpoint=endpoint,
        status=(
            SemanticStatus.SEMANTIC_VALID
            if not findings
            else SemanticStatus.SEMANTIC_INVALID
        ),
        row_count=len(rows),
        accepted_rows=len(rows) - len({f.split(":")[0] for f in findings if ":" in f}),
        # Bounded: a malformed response should not produce a report longer than
        # the response.
        findings=tuple(findings[:20]),
    )
