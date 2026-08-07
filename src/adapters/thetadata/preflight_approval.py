"""What the operator read, in a form the live command can be held to.

The v2.1.18 workflow had a gap in the middle of it::

    dry run
      -> operator reads request_plan_hash
      -> rerun with --execute-live
      -> the live command derives a *new* plan and authorises itself

Every individual step was checked. The join between them was not: nothing tied
the plan a human looked at to the plan that was sent. Change the profile, roll
past midnight, correct the instrument mapping, edit the pinned document -- the
live run would derive the consequences correctly and send something the
operator had never seen. The request-plan binding added in v2.1.16 proves the
*requests* match *a* plan; it cannot say whose.

So the dry run now ends in a value, and the live run will not start without it.
The value is a digest over what determines the requests and nothing else:
change any of it and the old approval stops matching, which is the point.

**The session date is inside it, deliberately.** The contract-list request
carries the market session date, so a Friday approval refuses a Monday
execution. That is not an inconvenience to work around -- an approval is a
statement about a specific session's requests, and Monday's are different
requests. The operator reruns the dry run during the session they are about to
capture, which is a few seconds and is when they should be looking anyway.

There is no ``--force``. A flag that skipped this would be the gap again with a
name, and the remedy for a stale approval is a new dry run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final

__all__ = [
    "APPROVAL_TRANSPORT_FIELDS",
    "CAPTURE_PREFLIGHT_APPROVAL_SCHEMA_VERSION",
    "CapturePreflightApproval",
    "PreflightApprovalError",
    "approval_for",
]

#: Bumped when what an approval covers changes. An approval computed under
#: older rules must not match a live run checked under newer ones: the digest
#: would agree while the two sides disagreed about what it promised.
CAPTURE_PREFLIGHT_APPROVAL_SCHEMA_VERSION = "capture-preflight-approval/2.1.19"


class PreflightApprovalError(ValueError):
    """An approval that cannot be trusted to describe the run about to happen."""


#: The transport settings an approval covers, as an **allowlist**.
#:
#: Allowlist rather than denylist because the failure modes are not symmetric.
#: A wire setting nobody listed here is missing from the approval, which is
#: visible the first time somebody changes it and the approval still matches. A
#: credential field that arrived in ``effective_transport_settings`` later and
#: was not in a denylist would go straight into a digest an operator pastes into
#: a shell, and nothing would report it.
#:
#: So: the settings that decide what goes on the wire, and nothing about who is
#: sending it. ``credentials_resolved``, ``username_present``, ``username_env``
#: and ``password_env`` are all excluded -- none of them changes a request, and
#: the last two are names an operator might reasonably not want in a paste
#: buffer.
APPROVAL_TRANSPORT_FIELDS: Final[tuple[str, ...]] = (
    "base_url",
    "connect_timeout_seconds",
    "read_timeout_seconds",
    "max_response_bytes",
    "max_retries",
    "backoff_base_seconds",
)

#: Field -> what to tell an operator whose approval no longer matches.
#:
#: Ordered most specific first. ``request_plan_hash`` covers the session date
#: and both symbols, so a date roll changes it too -- reporting "request plan
#: changed" when the real answer is "it is Monday now" would send somebody
#: hunting through a diff for something they already know.
_DIFFERENCE_ORDER: Final[tuple[tuple[str, str], ...]] = (
    ("market_session_date", "market session date changed"),
    ("instrument_mapping_fingerprint", "instrument mapping changed"),
    ("subscription_tier", "subscription tier changed"),
    ("effective_transport_fingerprint", "effective transport settings changed"),
    ("documentation_bundle_fingerprint", "documentation bundle changed"),
    ("capture_plan_fingerprint", "capture plan changed"),
    ("pipeline_fingerprint", "pipeline fingerprint changed"),
    ("request_plan_hash", "request plan changed"),
    ("schema_version", "approval schema changed"),
)


@dataclass(frozen=True, slots=True)
class CapturePreflightApproval:
    """The semantic identity of one session's planned capture.

    Carries no destination, no run id, no timestamp beyond the session date and
    no credential. Those are facts about *an execution*; this is a statement
    about *what will be requested*, and an approval that moved when the output
    directory changed would train an operator to stop reading it.
    """

    market_session_date: date
    request_plan_hash: str
    capture_plan_fingerprint: str
    pipeline_fingerprint: str
    documentation_bundle_fingerprint: str
    effective_transport_fingerprint: str
    instrument_mapping_fingerprint: str
    subscription_tier: str
    schema_version: str = CAPTURE_PREFLIGHT_APPROVAL_SCHEMA_VERSION
    #: Derived, and recomputed here. A field because it round-trips through the
    #: run evidence; a supplied value that disagrees with the contents is
    #: refused, so a reader may treat it as derived even though it stores.
    approval_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.market_session_date, date):
            raise PreflightApprovalError(
                f"market_session_date must be a date, got "
                f"{type(self.market_session_date).__name__}"
            )
        computed = self._compute_hash()
        if self.approval_hash and self.approval_hash != computed:
            raise PreflightApprovalError(
                f"the approval carries hash {self.approval_hash} and its own "
                f"contents hash to {computed}. A hash that does not follow from "
                "the fields it covers approves nothing."
            )
        object.__setattr__(self, "approval_hash", computed)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "market_session_date": self.market_session_date.isoformat(),
            "request_plan_hash": self.request_plan_hash,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "documentation_bundle_fingerprint": self.documentation_bundle_fingerprint,
            "effective_transport_fingerprint": self.effective_transport_fingerprint,
            "instrument_mapping_fingerprint": self.instrument_mapping_fingerprint,
            "subscription_tier": self.subscription_tier,
        }

    def _compute_hash(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.semantic_payload())

    def matches(self, approved: str) -> bool:
        """Whether a value an operator pasted is this approval.

        Compared case-insensitively and whitespace-stripped: a digest copied out
        of a terminal picks up neither, and refusing a correct approval because
        it arrived with a trailing space would teach the operator that the check
        is noise.
        """
        return str(approved or "").strip().lower() == self.approval_hash

    def differences_from(self, other: CapturePreflightApproval) -> tuple[str, ...]:
        """Which semantic fields differ, most specific first."""
        mine, theirs = self.semantic_payload(), other.semantic_payload()
        return tuple(
            message
            for field, message in _DIFFERENCE_ORDER
            if mine.get(field) != theirs.get(field)
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "approval_hash": self.approval_hash}


def approval_for(
    *, pipeline: Any, config: Any, moment: Any
) -> CapturePreflightApproval:
    """Derive the approval for one pipeline at one moment.

    The single place it is computed, called by both the dry run and the live
    preflight. Two derivations that could drift would put the operator back
    where they started, approving one thing and sending another.
    """
    from src.config.thetadata import effective_transport_settings
    from src.domain.digests import digest_of
    from src.gex.sessions import market_session_date

    settings = effective_transport_settings(config)
    return CapturePreflightApproval(
        market_session_date=market_session_date(moment),
        request_plan_hash=pipeline.raw_request_plan(as_of=moment).request_plan_hash,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
        documentation_bundle_fingerprint=pipeline.documentation_fingerprint,
        effective_transport_fingerprint=digest_of(
            {name: settings.get(name) for name in APPROVAL_TRANSPORT_FIELDS}
        ),
        instrument_mapping_fingerprint=digest_of(
            pipeline.runtime.instruments.semantic_payload()
        ),
        subscription_tier=str(config.tier),
    )
