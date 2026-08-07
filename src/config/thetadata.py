"""Typed ThetaData configuration and the single client factory.

v2 accepted a ``thetadata:`` YAML section but never surfaced it on
``LoadedConfig``: the values were validated as *unknown-key-free* and then
discarded, so every caller hand-assembled a partially configured client. A
setting could be present in the file, look applied in review, and never reach a
request.

Now there is exactly one construction path::

    client = build_thetadata_client(loaded_config.thetadata)

Anything the vendor does not accept is refused at load time rather than sent and
ignored.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.adapters.errors import ThetaDataError
from src.adapters.thetadata.instruments import InstrumentMapping
from src.config.compatibility import (
    AttestationError,
    EvidenceSource,
    PricingDimension,
    VendorObservation,
)
from src.domain.iv import IVSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import date, datetime

    from src.adapters.thetadata.client import ChainRequest, ThetaDataClient
    from src.config.pipeline import (
        DividendConvention,
        PricingMode,
        RateUnit,
        VendorGammaPolicy,
    )
    from src.domain.contracts import ChainSnapshot
    from src.domain.model_spec import ModelSpec


class AuthenticationMode(str, Enum):
    """How the client authenticates.

    ThetaData currently routes through a local Theta Terminal, which needs no
    remote credential. ``BASIC`` exists because the access model may change and
    the seam should already be there -- but selecting it requires the environment
    variables to be named, so a credential can never be a literal in the repo.
    """

    LOCAL_TERMINAL = "local_terminal"
    BASIC = "basic"

    @property
    def requires_credentials(self) -> bool:
        return self is AuthenticationMode.BASIC


#: Parameters the greeks endpoints accept. Anything outside this set is refused
#: at load time rather than sent and silently dropped by the vendor.
SUPPORTED_GREEKS_PARAMETERS = frozenset(
    {
        "greeks_version",
        "rate_type",
        "rate_value",
        "annual_dividend",
        "use_market_value",
    }
)

#: Server-side filters the snapshot endpoints accept.
SUPPORTED_FILTER_PARAMETERS = frozenset({"max_dte", "strike_range", "min_time"})

#: Rate types the vendor documents.
SUPPORTED_RATE_TYPES = frozenset(
    {
        "sofr",
        *(f"treasury_m{n}" for n in range(1, 7)),
        *(f"treasury_y{n}" for n in (1, 2, 3, 5, 7, 10, 20, 30)),
    }
)


@dataclass(frozen=True, slots=True)
class ThetaDataConfig:
    """Fully typed ThetaData settings.

    Every field here reaches either the transport or an outgoing request. A field
    that reached neither would be exactly the v2 defect.
    """

    base_url: str = "http://127.0.0.1:25503"
    authentication_mode: AuthenticationMode = AuthenticationMode.LOCAL_TERMINAL
    username_env: str | None = None
    password_env: str | None = None
    tier: str = "standard"

    # Transport
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    max_response_bytes: int = 64 * 1024 * 1024

    # Raw capture
    raw_capture_enabled: bool = False
    raw_capture_path: pathlib.Path | None = None

    # Vendor calculation parameters
    greeks_version: str | None = "latest"
    rate_type: str | None = "sofr"
    rate_value: float | None = None
    annual_dividend: float | None = None
    use_market_value: bool | None = None

    # Server-side filters
    max_dte: int | None = None
    strike_range: int | None = None
    min_time: str | None = None

    iv_source: IVSource = IVSource.VENDOR_DEFAULT_IV
    duplicate_policy: str = "reject"

    # -- pricing coherence (v2.1.2) ------------------------------------------
    #: Which numbers come from where. Only VENDOR_IV_LOCAL_GAMMA mixes vendor
    #: and local quantities inside one calculation, so only it needs agreement.
    pricing_mode: PricingMode = None  # type: ignore[assignment]
    #: What to do with the vendor's gamma. Orthogonal to ``pricing_mode``: in
    #: v2.1.3 this was a third value of that enum, so switching the comparison on
    #: moved the session out of VENDOR_IV_LOCAL_GAMMA and its compatibility
    #: checks -- while vendor IV still fed the local gamma.
    vendor_gamma_policy: VendorGammaPolicy = None  # type: ignore[assignment]
    #: How to read ``rate_value``. Undocumented by the vendor, so UNKNOWN is the
    #: honest default and it blocks pricing compatibility.
    rate_units: RateUnit = None  # type: ignore[assignment]
    #: What ``annual_dividend`` means. Cash and yield are different quantities.
    dividend_convention: DividendConvention = None  # type: ignore[assignment]
    #: Recorded answers to the vendor conventions ThetaData does not publish
    #: inline. The only production route from an UNKNOWN pricing dimension to a
    #: resolved one, and each carries its source and reference into the audit
    #: trail. An attestation cannot overturn a measured mismatch.
    pricing_attestations: tuple[VendorObservation, ...] = ()
    #: Local model settings that MUST match the ModelSpec, checked by
    #: ThetaDataResearchPipeline rather than left to drift.
    min_time_to_expiry_minutes: float = 60.0
    underlying_price_source: str = "vendor_index_snapshot"
    expiration_rule: str = "root_specific_settlement"
    #: How far the spot print may be from the chain instant before the pairing
    #: stops being meaningful.
    #:
    #: **Configuration, not an argument.** v2.1.7 read this off a caller-built
    #: ``SpotProvenance``, so one calculation could be granted a wider window
    #: than the session was configured for -- and the skew check is the only
    #: thing between a chain and an underlying it never saw. Living here means
    #: it enters the pipeline fingerprint, the capture-operation identity and
    #: the normalization recipe, so widening it is a configuration change that
    #: every stamped record disagrees with.
    #:
    #: One second: the index snapshot and the chain are separate reads issued
    #: back to back, and anything slower than that is a stall worth noticing.
    max_spot_skew_seconds: float = 1.0
    #: When true, an incompatible pricing configuration raises instead of
    #: proceeding with a warning.
    fail_on_incompatible_pricing: bool = False

    def __post_init__(self) -> None:
        """Resolve the derived fields, then refuse an incoherent object.

        Four fields carried ``= None`` with a lying type annotation, on the
        assumption that everything came through ``thetadata_config_from_dict``.
        ``ThetaDataConfig()`` therefore constructed happily and then raised
        ``AttributeError: 'NoneType' object has no attribute 'value'`` from
        ``as_dict`` -- inside the audit trail, which is the last place a config
        should first be found to be invalid.

        The pricing mode is *derived* from the IV source rather than defaulted,
        so the two cannot be constructed in disagreement at all.
        """
        from src.config.pipeline import (
            DividendConvention,
            IvGammaPricingMode,
            RateUnit,
            VendorGammaPolicy,
            derive_pricing_mode,
            reject_legacy_pricing_mode,
            require_coherent_pricing_mode,
            require_supported_iv_source,
        )

        # Before the enum coercion below, which would turn the v2.1.3 mode name
        # into an opaque ``ValueError: 'VENDOR_GAMMA_VALIDATION' is not a valid
        # IvGammaPricingMode`` instead of the message naming its replacement.
        # The YAML loader does this; a config rebuilt from a stored ``as_dict``
        # went straight past it.
        reject_legacy_pricing_mode(self.pricing_mode)

        def resolve(name: str, enum_type: Any, default: Any) -> Any:
            value = getattr(self, name)
            if value is None:
                value = default
            # Accept the string form so a config assembled from raw values is
            # still valid by construction rather than valid by convention.
            resolved = enum_type(value)
            object.__setattr__(self, name, resolved)
            return resolved

        iv_source = resolve("iv_source", IVSource, IVSource.VENDOR_DEFAULT_IV)
        # An unimplemented source resolves through the vendor-default fallback,
        # so the operator gets a different number than the one they selected.
        # The YAML loader refused this; direct construction did not, which meant
        # `ThetaDataConfig(iv_source=IVSource.TRADE_IV)` built happily and then
        # priced against a source with no implementation behind it.
        require_supported_iv_source(iv_source)
        mode = resolve(
            "pricing_mode", IvGammaPricingMode, derive_pricing_mode(iv_source=iv_source)
        )
        resolve("vendor_gamma_policy", VendorGammaPolicy, VendorGammaPolicy.DISABLED)
        resolve("rate_units", RateUnit, RateUnit.UNKNOWN)
        resolve(
            "dividend_convention",
            DividendConvention,
            DividendConvention.UNKNOWN_VENDOR_CONVENTION,
        )
        require_coherent_pricing_mode(iv_source=iv_source, pricing_mode=mode)

        attestations = tuple(self.pricing_attestations)
        for attestation in attestations:
            if not isinstance(attestation, VendorObservation):
                raise AttestationError(
                    "pricing_attestations must hold VendorObservation objects, "
                    f"got {type(attestation).__name__}. A string or a boolean "
                    "here would be an assertion that a question was answered, "
                    "with nothing recording the answer."
                )
            if attestation.source is EvidenceSource.LIVE_COMPARISON:
                # Configuration is static; a live comparison is an event. The
                # loader refuses this in YAML, and so must the object, or the
                # rule is one constructor call from being irrelevant.
                raise ThetaDataConfigError(
                    f"pricing_attestations[{attestation.dimension.value}]: "
                    "LIVE_COMPARISON records that a comparison against real "
                    "vendor output was run. A configuration object cannot "
                    "witness an event. Live evidence is emitted by "
                    "AdapterValidator and bound to the capture it was read from."
                )
        claimed = [a.dimension for a in attestations]
        duplicated = sorted({d.value for d in claimed if claimed.count(d) > 1})
        if duplicated:
            raise AttestationError(
                f"pricing_attestations names {duplicated} more than once. Two "
                "answers to one question is not more evidence; drop the stale one."
            )
        object.__setattr__(self, "pricing_attestations", attestations)

    def to_model_spec(self) -> ModelSpec:
        """The ModelSpec this configuration implies.

        The single place a ModelSpec is derived from ThetaData settings, so the
        two cannot drift apart the way they did in v2.1.1.
        """
        from src.domain.model_spec import (
            DividendSource,
            ExpirationTimestampRule,
            ModelSpec,
            RateSource,
            UnderlyingPriceSource,
        )

        rate_source = RateSource.CONFIGURED_CONSTANT
        rate = self.local_risk_free_rate
        if rate is None:
            rate_source, rate = RateSource.ZERO, 0.0

        from src.config.pipeline import DividendConvention

        dividend_source = DividendSource.CONFIGURED_CONSTANT
        dividend = self.local_dividend_yield
        if dividend is None:
            dividend_source, dividend = DividendSource.ZERO, 0.0
        elif self.dividend_convention is DividendConvention.ZERO_DIVIDEND:
            # A stated zero is a zero, not a continuous yield that happens to be
            # 0.0. The two are numerically identical and *declaratively*
            # different, and the compatibility check compares declarations: a
            # config saying ZERO_DIVIDEND derived a spec saying
            # CONFIGURED_CONSTANT, which the check then reported as the vendor
            # and the model disagreeing about the dividend convention. v2.1.3
            # never saw it because its tests asserted compatibility directly
            # instead of deriving it.
            dividend_source, dividend = DividendSource.ZERO, 0.0

        return ModelSpec(
            iv_price_source=self.iv_source,
            risk_free_rate_source=rate_source,
            risk_free_rate=rate,
            dividend_yield_source=dividend_source,
            dividend_yield=dividend,
            minimum_time_to_expiry_minutes=self.min_time_to_expiry_minutes,
            underlying_price_source=UnderlyingPriceSource(self.underlying_price_source),
            expiration_timestamp_rule=ExpirationTimestampRule(self.expiration_rule),
        )

    @property
    def local_risk_free_rate(self) -> float | None:
        """The vendor rate expressed as a decimal, when that is knowable.

        ``None`` when the units are undocumented -- guessing here is a factor of
        one hundred in every gamma.
        """
        from src.config.pipeline import RateUnit

        if self.rate_value is None:
            return None
        if self.rate_units is RateUnit.PERCENT_ANNUAL_RATE:
            return self.rate_value / 100.0
        if self.rate_units is RateUnit.DECIMAL_ANNUAL_RATE:
            return self.rate_value
        # UNKNOWN units cannot be normalised; guessing is a factor of a hundred
        # in every gamma.
        return None

    @property
    def local_dividend_yield(self) -> float | None:
        """The vendor dividend as a continuous yield, when that is knowable."""
        from src.config.pipeline import DividendConvention

        if self.dividend_convention is DividendConvention.ZERO_DIVIDEND:
            return 0.0
        if (
            self.dividend_convention is DividendConvention.CONTINUOUS_DIVIDEND_YIELD
            and self.annual_dividend is not None
        ):
            return self.annual_dividend
        # ANNUAL_CASH_DIVIDEND cannot be converted without the spot and the
        # payment schedule; UNKNOWN cannot be converted at all.
        return None

    def rate_type_policy(self) -> str:
        """What happens to ``rate_type`` on the wire, stated in one place."""
        if self.rate_type is None:
            return (
                "rate_type is null: the parameter is OMITTED from the request "
                "and the vendor default applies. Nothing is substituted locally."
            )
        return f"rate_type={self.rate_type} is sent explicitly."

    def credentials(self) -> tuple[str | None, str | None]:
        """Read credentials from the environment. Never from a file.

        Returns whatever is there, including ``None``. Use
        :meth:`resolved_credentials` where a missing value must be an error.
        """
        if not self.authentication_mode.requires_credentials:
            return None, None
        username = os.environ.get(self.username_env) if self.username_env else None
        password = os.environ.get(self.password_env) if self.password_env else None
        return username, password

    def resolved_credentials(self) -> tuple[str | None, str | None]:
        """Credentials, or a loud failure naming the variables that are empty.

        Whitespace counts as empty: an environment variable set to " " is a
        configuration accident, not a password.
        """
        if not self.authentication_mode.requires_credentials:
            return None, None
        username, password = self.credentials()
        missing = [
            name
            for name, value in (
                (self.username_env, username),
                (self.password_env, password),
            )
            if name and not (value or "").strip()
        ]
        if missing:
            raise MissingCredentialsError(
                f"authentication_mode is {self.authentication_mode.value} but "
                f"{sorted(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} unset or empty in the "
                "environment. Refusing to build an unauthenticated client: a "
                "silent downgrade turns a configuration error into an "
                "unexplained 401 from the vendor."
            )
        return username, password

    #: Keys that say *where bytes go*, not what the numbers mean. Excluded from
    #: the semantic identity: moving a capture from ``/disk-a`` to ``/disk-b``
    #: changes nothing about the market data, the request, the pricing model or
    #: the normalized chain, and until v2.1.13 it changed the pipeline
    #: fingerprint -- which every capture is stamped with and every replay
    #: compares. Two identical sessions written to two disks were two different
    #: pipelines.
    STORAGE_ONLY_KEYS = ("raw_capture_path", "raw_capture_enabled")

    def semantic_payload(self) -> dict[str, Any]:
        """What changes a number, and nothing about where it is stored."""
        return {
            key: value
            for key, value in self.as_dict().items()
            if key not in self.STORAGE_ONLY_KEYS
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialisable settings. Contains env-var *names*, never values."""
        return {
            "base_url": self.base_url,
            "authentication_mode": self.authentication_mode.value,
            "username_env": self.username_env,
            "password_env": self.password_env,
            "tier": self.tier,
            "timeout_seconds": self.timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "max_response_bytes": self.max_response_bytes,
            "raw_capture_enabled": self.raw_capture_enabled,
            "raw_capture_path": (
                str(self.raw_capture_path) if self.raw_capture_path else None
            ),
            "greeks_version": self.greeks_version,
            "rate_type": self.rate_type,
            "rate_value": self.rate_value,
            "annual_dividend": self.annual_dividend,
            "use_market_value": self.use_market_value,
            "max_dte": self.max_dte,
            "strike_range": self.strike_range,
            "min_time": self.min_time,
            "iv_source": self.iv_source.value,
            "duplicate_policy": self.duplicate_policy,
            "pricing_mode": self.pricing_mode.value,
            "vendor_gamma_policy": self.vendor_gamma_policy.value,
            "rate_units": self.rate_units.value,
            "dividend_convention": self.dividend_convention.value,
            "pricing_attestations": [a.as_dict() for a in self.pricing_attestations],
            "min_time_to_expiry_minutes": self.min_time_to_expiry_minutes,
            "underlying_price_source": self.underlying_price_source,
            "expiration_rule": self.expiration_rule,
            "max_spot_skew_seconds": self.max_spot_skew_seconds,
            "fail_on_incompatible_pricing": self.fail_on_incompatible_pricing,
        }


@dataclass(frozen=True, slots=True)
class VendorParameterSet:
    """The five-way split the audit trail needs.

    v2 recorded "the parameters" as one bag, which could not distinguish a value
    that was configured from one that was actually sent. Only ``sent`` may be
    called effective.
    """

    #: What the operator asked for.
    requested_model_parameters: dict[str, Any] = field(default_factory=dict)
    #: What this endpoint accepts.
    supported_vendor_parameters: tuple[str, ...] = ()
    #: What actually went out on the wire.
    sent_vendor_parameters: dict[str, Any] = field(default_factory=dict)
    #: Applied by us after the response, not by the vendor.
    effective_local_parameters: dict[str, Any] = field(default_factory=dict)
    #: Requested but not accepted here. Non-empty is a configuration error.
    unsupported_requested_parameters: tuple[str, ...] = ()

    def parameter_hash(self) -> str:
        """Canonical hash of what was sent.

        Sorted keys, so query ordering cannot change the digest. Credentials
        never enter: they are not query parameters in any supported mode, and
        ``as_dict`` on the config carries only env-var names.
        """
        payload = json.dumps(
            self.sent_vendor_parameters, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_model_parameters": dict(
                sorted(self.requested_model_parameters.items())
            ),
            "supported_vendor_parameters": list(self.supported_vendor_parameters),
            "sent_vendor_parameters": dict(sorted(self.sent_vendor_parameters.items())),
            "effective_local_parameters": dict(
                sorted(self.effective_local_parameters.items())
            ),
            "unsupported_requested_parameters": list(
                self.unsupported_requested_parameters
            ),
            "parameter_hash": self.parameter_hash(),
        }


class ThetaDataConfigError(ThetaDataError, ValueError):
    """Invalid ThetaData configuration.

    Sits under ``ThetaDataError`` as of v2.1.4, so ``except ThetaDataError``
    catches every way the adapter can fail rather than every way it can fail
    *after* the configuration was accepted. v2.1.3 unified the runtime failures
    -- transport, retries, store, parser -- and left the configuration failures
    on ``ValueError``, which is the half a caller is most likely to want to
    handle separately and least likely to guess at.

    ``ValueError`` is kept as a second base so existing ``except ValueError``
    handlers, and the loader's own translation into ``ConfigError``, continue to
    work.
    """


class MissingCredentialsError(ThetaDataConfigError):
    """BASIC auth was selected but the environment holds no usable credential.

    Raised at construction rather than at the first request. v2.1 built the
    client with ``basic_auth=... if username and password else None``, so an
    unset environment variable produced a perfectly good *unauthenticated*
    client and the resulting 401 looked like a vendor outage rather than a
    configuration mistake.

    Names the environment variables. Never their values.
    """


ALLOWED_KEYS = frozenset(
    {
        "base_url",
        "authentication_mode",
        "auth_mode",
        "username_env",
        "password_env",
        "tier",
        "timeout_seconds",
        "connect_timeout_seconds",
        "max_retries",
        "backoff_base_seconds",
        "max_response_bytes",
        "raw_capture_enabled",
        "raw_capture_path",
        "greeks_version",
        "rate_type",
        "rate_value",
        "annual_dividend",
        "use_market_value",
        "max_dte",
        "strike_range",
        "min_time",
        "iv_source",
        "duplicate_policy",
        "pricing_mode",
        "vendor_gamma_policy",
        "rate_units",
        "dividend_convention",
        "pricing_attestations",
        "min_time_to_expiry_minutes",
        "underlying_price_source",
        "expiration_rule",
        "max_spot_skew_seconds",
        "fail_on_incompatible_pricing",
        "stock_price_source",
    }
)

VALID_TIERS = ("free", "value", "standard", "pro")
#: How to resolve two rows claiming the same contract.
#:
#: ``reject`` and ``collapse_exact`` both collapse byte-identical rows -- a
#: duplicate that carries no conflicting information has nothing to arbitrate --
#: and both refuse rows that disagree. ``collapse_exact`` exists as the explicit
#: spelling of that behaviour, so a config can state it rather than rely on a
#: reader knowing what ``reject`` quietly permits. See docs/OPEN_DECISIONS.md
#: OD-13.
VALID_DUPLICATE_POLICIES = ("reject", "collapse_exact", "newest_timestamp")


#: ThetaData's ``min_time`` filter is a wall-clock time of day, HH:MM:SS with
#: optional milliseconds. Accepting anything serialisable would let "nine
#: thirty" reach the vendor and come back as an unexplained empty chain.
MIN_TIME_GRAMMAR = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,3})?$")

#: Keys a single ``pricing_attestations`` entry may carry. Unknown keys are
#: refused rather than ignored: a misspelt ``refrence`` would otherwise produce
#: an attestation with no reference, which is the one thing that must not load.
ATTESTATION_KEYS = frozenset(
    {"dimension", "source", "reference", "observed_at", "vendor_value", "note"}
)


def _parse_attestations(
    raw_value: Any, fail: Any, *, key: str = "pricing_attestations"
) -> tuple[VendorObservation, ...]:
    """Build typed attestations from the configuration file.

    Every field is required to be present and non-empty in the file, because the
    object this produces is what lets a load-bearing pricing dimension count as
    resolved. There is no shorthand form and no boolean form.
    """
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        fail(f"expected a list of attestations, got {type(raw_value).__name__}", key)
    parsed: list[VendorObservation] = []
    for index, entry in enumerate(raw_value):
        where = f"{key}[{index}]"
        if not isinstance(entry, dict):
            fail(f"expected a mapping, got {type(entry).__name__}", where)
            continue
        unknown = sorted(set(entry) - ATTESTATION_KEYS)
        if unknown:
            fail(
                f"unknown keys {unknown}; valid keys are {sorted(ATTESTATION_KEYS)}",
                where,
            )
        try:
            dimension = PricingDimension(entry.get("dimension"))
        except ValueError:
            fail(
                f"{entry.get('dimension')!r} is not a pricing dimension; valid "
                f"values are {[d.value for d in PricingDimension]}",
                f"{where}.dimension",
            )
            continue
        try:
            source = EvidenceSource(entry.get("source"))
        except ValueError:
            fail(
                f"{entry.get('source')!r} is not an evidence source; valid values "
                f"are {[s.value for s in EvidenceSource]}",
                f"{where}.source",
            )
            continue
        if source is EvidenceSource.LIVE_COMPARISON:
            fail(
                "LIVE_COMPARISON records that a comparison against real vendor "
                "output was run. A configuration file cannot witness an event: "
                "editing this line does not make a request. Live evidence is "
                "emitted by AdapterValidator and bound to the capture it was "
                "read from. Use VENDOR_DOCUMENTATION for what the vendor says.",
                f"{where}.source",
            )
            continue
        reference = str(entry.get("reference", ""))
        if source is EvidenceSource.VENDOR_DOCUMENTATION:
            _require_resolvable_reference(reference, fail, where=f"{where}.reference")
        try:
            parsed.append(
                VendorObservation(
                    dimension=dimension,
                    observed_value=entry.get("vendor_value"),
                    source=source,
                    reference=reference,
                    observed_at=str(entry.get("observed_at", "")),
                    note=str(entry.get("note", "")),
                )
            )
        except AttestationError as error:
            fail(str(error), where)
    return tuple(parsed)


def _require_resolvable_reference(reference: str, fail: Any, *, where: str) -> None:
    """A documentation reference has to point at something a reader can open.

    Not a URL check and not a content check -- just the difference between a
    path into this repository that exists and one that does not. An
    unresolvable reference is the same as no reference: nobody reviewing the
    certification report can go and look.
    """
    text = reference.strip()
    if not text:
        fail("a documentation reference is required", where)
        return
    if text.startswith(("http://", "https://")):
        return
    target = pathlib.Path(text.split("#", 1)[0])
    if target.is_absolute() or ".." in target.parts:
        fail(
            f"{text!r} must be a path inside the repository or a URL",
            where,
        )
        return
    root = pathlib.Path(__file__).resolve().parents[2]
    if not (root / target).exists():
        fail(
            f"{text!r} does not exist. A reference nobody can open is the same "
            "as no reference: the certification report names a source that "
            "cannot be checked.",
            where,
        )


def _required(value: float | None) -> float:
    """Narrow ``float | None`` where the parser has already refused ``None``."""
    assert value is not None
    return value


def parse_thetadata_config(raw: Any, *, path: str = "thetadata") -> ThetaDataConfig:
    """Validate and type the ``thetadata:`` section. Fails fast on anything odd.

    ``raw`` is deliberately typed ``Any``: it arrives straight from YAML, so the
    isinstance check below is a real runtime guard rather than a formality.
    """

    def fail(message: str, key: str = "") -> Any:
        location = f"{path}.{key}" if key else path
        raise ThetaDataConfigError(f"{location}: {message}")

    if not isinstance(raw, dict):
        fail("expected a mapping")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        fail(
            f"unknown key(s) {sorted(unknown)}; allowed keys are "
            f"{sorted(ALLOWED_KEYS)}. A silently-ignored typo is a setting that "
            "never took effect."
        )

    def number(
        key: str,
        default: float | None,
        *,
        low: float,
        high: float | None = None,
        optional: bool = False,
    ) -> float | None:
        value = raw.get(key, default)
        if value is None:
            if optional:
                return None
            fail("expected a number, got null", key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            fail(f"expected a number, got {type(value).__name__}", key)
        # isfinite before the range checks, not after: NaN compares False
        # against every bound, so `value < low` and `value > high` are both
        # False and a range check alone waves it straight through.
        if not math.isfinite(float(value)):
            fail(f"expected a finite number, got {value!r}", key)
        if value < low:
            fail(f"value {value} is below the minimum {low}", key)
        if high is not None and value > high:
            fail(f"value {value} is above the maximum {high}", key)
        return float(value)

    def text(key: str, default: str | None, *, optional: bool = False) -> str | None:
        """A configured string must be a non-empty string.

        An empty ``greeks_version`` or ``username_env`` is not a value, it is a
        typo that survives serialisation.
        """
        value = raw.get(key, default)
        if value is None:
            if optional:
                return None
            fail("expected a string, got null", key)
        if isinstance(value, bool) or not isinstance(value, str):
            fail(f"expected a string, got {type(value).__name__}", key)
        text_value = str(value)
        if not text_value.strip():
            fail("expected a non-empty string", key)
        return text_value

    def integer(
        key: str, default: int | None, *, low: int, high: int | None = None
    ) -> int | None:
        value = raw.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            fail(f"expected an integer, got {type(value).__name__}", key)
        if value < low:
            fail(f"value {value} is below the minimum {low}", key)
        if high is not None and value > high:
            fail(f"value {value} is above the maximum {high}", key)
        return int(value)

    base_url = text("base_url", "http://127.0.0.1:25503") or ""

    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        fail(f"{base_url!r} is not a valid absolute http(s) URL", "base_url")
    # Userinfo in a base URL means a credential in every logged URL, every
    # exception message and every raw-capture index entry. The error deliberately
    # does not echo the URL back.
    if parsed_url.username or parsed_url.password or "@" in parsed_url.netloc:
        fail(
            "base_url must not contain userinfo (user:password@host); "
            "credentials come from the environment via username_env/password_env",
            "base_url",
        )
    if parsed_url.query:
        fail(
            "base_url must not carry query parameters; per-request parameters "
            "are built by the client so they can be audited",
            "base_url",
        )
    if parsed_url.fragment:
        fail("base_url must not carry a fragment", "base_url")

    mode_raw = raw.get("authentication_mode", raw.get("auth_mode", "local_terminal"))
    try:
        mode = AuthenticationMode(mode_raw)
    except ValueError:
        fail(
            f"{mode_raw!r} is not a supported authentication mode; valid values "
            f"are {[m.value for m in AuthenticationMode]}",
            "authentication_mode",
        )

    username_env = text("username_env", None, optional=True)
    password_env = text("password_env", None, optional=True)
    if mode.requires_credentials and not (username_env and password_env):
        fail(
            "basic authentication requires username_env and password_env to name "
            "the environment variables holding the credentials; credentials are "
            "never read from the config file itself",
            "authentication_mode",
        )

    tier = raw.get("tier", "standard")
    if tier not in VALID_TIERS:
        fail(f"{tier!r} is not one of {list(VALID_TIERS)}", "tier")

    duplicate_policy = raw.get("duplicate_policy", "reject")
    if duplicate_policy not in VALID_DUPLICATE_POLICIES:
        fail(
            f"{duplicate_policy!r} is not one of {list(VALID_DUPLICATE_POLICIES)}",
            "duplicate_policy",
        )

    # An explicit null means "do not send the parameter", which is different
    # from "send sofr". v2.1.1 stored None and then substituted "sofr" when
    # building the client, so the stored config and the outgoing request
    # disagreed and only the request was true.
    rate_type = (
        text("rate_type", "sofr", optional=True)
        if "rate_type" not in raw or raw["rate_type"] is not None
        else None
    )
    if rate_type is not None and rate_type not in SUPPORTED_RATE_TYPES:
        fail(
            f"{rate_type!r} is not a documented ThetaData rate type; supported "
            f"values are {sorted(SUPPORTED_RATE_TYPES)}",
            "rate_type",
        )

    iv_source_raw = raw.get("iv_source", IVSource.VENDOR_DEFAULT_IV.value)
    try:
        iv_source = IVSource(iv_source_raw)
    except ValueError:
        fail(
            f"{iv_source_raw!r} is not a valid IVSource; valid values are "
            f"{[s.value for s in IVSource]}",
            "iv_source",
        )

    capture_enabled = raw.get("raw_capture_enabled", False)
    if not isinstance(capture_enabled, bool):
        fail("expected a boolean", "raw_capture_enabled")
    capture_path_raw = raw.get("raw_capture_path")
    if capture_path_raw is not None and not isinstance(
        capture_path_raw, str | pathlib.Path
    ):
        fail(
            f"raw_capture_path must be a string or a path, got "
            f"{type(capture_path_raw).__name__}. v2.1.1 ran str() over whatever "
            "was supplied, so 42 became the directory '42'.",
            "raw_capture_path",
        )
    if isinstance(capture_path_raw, str) and not capture_path_raw.strip():
        fail("raw_capture_path must not be empty", "raw_capture_path")
    if capture_enabled and not capture_path_raw:
        fail(
            "raw_capture_enabled is true but raw_capture_path is not set; there "
            "is nowhere to write the audit trail",
            "raw_capture_path",
        )

    use_market_value = raw.get("use_market_value")
    if use_market_value is not None and not isinstance(use_market_value, bool):
        fail("expected a boolean or null", "use_market_value")

    # ThetaData has no documented `stock_price_source` query parameter. Accepting
    # it silently would let an operator believe they had selected one.
    if raw.get("stock_price_source") not in (None, "vendor_default"):
        fail(
            "ThetaData exposes no stock_price_source query parameter; the "
            "underlying price is selected locally via "
            "model.underlying_price_source instead",
            "stock_price_source",
        )

    min_time = text("min_time", None, optional=True)
    if min_time is not None and not MIN_TIME_GRAMMAR.match(min_time):
        fail(
            f"{min_time!r} is not a valid min_time; expected HH:MM:SS or "
            "HH:MM:SS.mmm in 24-hour wall-clock form",
            "min_time",
        )

    from src.config.pipeline import (
        DividendConvention,
        PricingMode,
        RateUnit,
        VendorGammaPolicy,
        reject_legacy_pricing_mode,
        require_supported_iv_source,
    )

    # Refuse an IV source with no implementation *here*, at load time. v2.1.1
    # accepted TRADE_IV and LOCALLY_SOLVED_MID_IV and then resolved them via the
    # vendor-default fallback, so the operator silently got a different number.
    require_supported_iv_source(iv_source, where=f"{path}.iv_source")

    def enumerated(key: str, enum_type: Any, default: Any) -> Any:
        raw_value = raw.get(key, default)
        try:
            return enum_type(raw_value)
        except ValueError:
            fail(
                f"{raw_value!r} is not a valid {key}; valid values are "
                f"{[m.value for m in enum_type]}",
                key,
            )

    # Derived from IV provenance unless the operator states one. v2.1.2
    # defaulted to LOCAL_IV_LOCAL_GAMMA regardless of iv_source, which paired
    # vendor-computed IV with the one mode that requires no vendor/local
    # agreement -- so every compatibility check was skipped by default.
    from src.config.pipeline import derive_pricing_mode, require_coherent_pricing_mode

    # Named before it is parsed: an old file saying VENDOR_GAMMA_VALIDATION must
    # not be silently reinterpreted, because the checks it used to skip now run.
    reject_legacy_pricing_mode(raw.get("pricing_mode"), where=f"{path}.pricing_mode")

    pricing_mode = enumerated(
        "pricing_mode",
        PricingMode,
        derive_pricing_mode(iv_source=iv_source).value,
    )
    require_coherent_pricing_mode(
        iv_source=iv_source, pricing_mode=pricing_mode, where=f"{path}.pricing_mode"
    )
    vendor_gamma_policy = enumerated(
        "vendor_gamma_policy", VendorGammaPolicy, VendorGammaPolicy.DISABLED.value
    )
    pricing_attestations = _parse_attestations(raw.get("pricing_attestations"), fail)

    # The tier has to expose what the mode *and* the policy need. Two additive
    # requirements: in v2.1.3 the gamma requirement could only be expressed by
    # replacing the vendor-IV mode, which dropped the vendor-IV requirement.
    from src.adapters.thetadata.capabilities import assess_tier
    from src.adapters.thetadata.endpoints import Tier
    from src.config.pipeline import required_capabilities

    capability = assess_tier(
        Tier(tier), required_capabilities(pricing_mode, policy=vendor_gamma_policy)
    )
    if not capability.satisfied:
        fail(
            f"the {tier!r} tier does not expose what {pricing_mode.value} with "
            f"vendor_gamma_policy={vendor_gamma_policy.value} needs: "
            f"missing={list(capability.missing)} "
            f"uncertain={list(capability.uncertain)}",
            "tier",
        )
    rate_units = enumerated("rate_units", RateUnit, RateUnit.UNKNOWN.value)
    dividend_convention = enumerated(
        "dividend_convention",
        DividendConvention,
        DividendConvention.UNKNOWN_VENDOR_CONVENTION.value,
    )

    underlying_price_source = text("underlying_price_source", "vendor_index_snapshot")
    expiration_rule = text("expiration_rule", "root_specific_settlement")
    fail_on_incompatible = raw.get("fail_on_incompatible_pricing", False)
    if not isinstance(fail_on_incompatible, bool):
        fail("expected a boolean", "fail_on_incompatible_pricing")

    return ThetaDataConfig(
        base_url=base_url,
        authentication_mode=mode,
        username_env=username_env,
        password_env=password_env,
        tier=tier,
        timeout_seconds=_required(
            number("timeout_seconds", 30.0, low=0.001, high=600.0)
        ),
        connect_timeout_seconds=_required(
            number("connect_timeout_seconds", 5.0, low=0.001, high=600.0)
        ),
        max_retries=integer("max_retries", 3, low=0, high=20) or 0,
        backoff_base_seconds=_required(
            number("backoff_base_seconds", 0.25, low=0.0, high=60.0)
        ),
        max_response_bytes=integer(
            "max_response_bytes", 64 * 1024 * 1024, low=1024, high=2**31
        )
        or 0,
        raw_capture_enabled=capture_enabled,
        raw_capture_path=(
            pathlib.Path(str(capture_path_raw)) if capture_path_raw else None
        ),
        greeks_version=text("greeks_version", "latest"),
        rate_type=rate_type,
        rate_value=number("rate_value", None, low=-100.0, high=100.0, optional=True),
        annual_dividend=number(
            "annual_dividend", None, low=0.0, high=10_000.0, optional=True
        ),
        use_market_value=use_market_value,
        max_dte=integer("max_dte", None, low=0, high=3650),
        strike_range=integer("strike_range", None, low=1, high=10_000),
        min_time=min_time,
        iv_source=iv_source,
        duplicate_policy=duplicate_policy,
        pricing_mode=pricing_mode,
        vendor_gamma_policy=vendor_gamma_policy,
        pricing_attestations=pricing_attestations,
        rate_units=rate_units,
        dividend_convention=dividend_convention,
        min_time_to_expiry_minutes=_required(
            number("min_time_to_expiry_minutes", 60.0, low=0.0, high=1440.0)
        ),
        underlying_price_source=underlying_price_source or "vendor_index_snapshot",
        expiration_rule=expiration_rule or "root_specific_settlement",
        # Bounded above at a minute: a spot print a minute from the chain is not
        # a synchronisation tolerance, it is a different market state, and a
        # configuration that asks for one is a mistake worth refusing at load.
        max_spot_skew_seconds=float(
            number("max_spot_skew_seconds", 1.0, low=0.0, high=60.0) or 1.0
        ),
        fail_on_incompatible_pricing=fail_on_incompatible,
    )


def httpx_transport_kwargs(config: ThetaDataConfig) -> dict[str, Any]:
    """Everything the real transport needs, including the response cap.

    v2.1.1 passed ``max_response_bytes`` to ``RetryingTransport`` only.
    ``HttpxTransport`` is the layer that reads chunks off the socket, so the
    configured cap did not govern the streaming read at all -- it governed a
    check performed after the body was already in memory.

    One function, so the inner and outer limits cannot drift apart.
    """
    username, password = config.resolved_credentials()
    return {
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "read_timeout_seconds": config.timeout_seconds,
        "basic_auth": (username, password) if username and password else None,
        "max_response_bytes": config.max_response_bytes,
        # **Where routing may come from.** ``False``: this repository targets a
        # local Theta Terminal, and an ambient ``ALL_PROXY`` must not be able to
        # send a capture somewhere else while the origin classification -- which
        # is derived from the URL -- keeps saying the bytes came from localhost.
        # Proxy support would be configuration an operator approves.
        "trust_env": False,
    }


def build_thetadata_client(
    config: ThetaDataConfig,
    *,
    transport: Any = None,
    clock: Any = None,
    attempt_observer: Any = None,
    default_raw_store: Any = None,
) -> ThetaDataClient:
    """The one sanctioned way to construct a configured client.

    Every transport setting -- timeouts, retries, backoff, response cap, auth --
    and every vendor calculation parameter is applied here. No caller assembles a
    client by hand, which is what let v2 settings exist in YAML and never reach a
    request.

    ``transport`` is injectable so tests can pass a deterministic fake; when it
    is omitted a real HTTP transport is built from the config, wrapped in the
    retry policy.
    """
    from src.adapters.raw_store import NullRawStore
    from src.adapters.thetadata.client import (
        GreeksParameters,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.thetadata.endpoints import Tier
    from src.adapters.transport import RetryingTransport, RetryPolicy

    # Resolve credentials BEFORE anything is constructed, so that a missing
    # secret cannot yield a usable unauthenticated client. The values themselves
    # are read again by httpx_transport_kwargs; this call is the gate.
    config.resolved_credentials()

    if transport is None:  # pragma: no cover - needs the http extra and a network
        from src.adapters.transport import HttpxTransport

        transport = HttpxTransport(**httpx_transport_kwargs(config))

    retrying = RetryingTransport(
        transport,
        policy=RetryPolicy(
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
        ),
        max_response_bytes=config.max_response_bytes,
        # Every attempt, not only the one that succeeded. A retryable 429 or 503
        # body is consumed inside the retry loop, so without this the responses
        # that explain a partial capture are exactly the ones nobody keeps.
        attempt_observer=attempt_observer,
    )

    # **No filesystem store is created from a configuration path.**
    #
    # v2.1.12 built ``FileRawStore(config.raw_capture_path)`` here, during
    # pipeline construction, for every caller. The operator writes its capture to
    # ``<output>/raw`` and passes that store to the session, so this one received
    # nothing -- and because the shipped profile says ``artifacts/raw``, merely
    # *constructing a pipeline* created a directory inside the checkout. The dry
    # run, whose whole promise is that it writes nothing, created it too.
    #
    # A path in configuration is a statement about where a store *would* go. It
    # is not an instruction to make one. Ownership is now explicit: whoever wants
    # a durable store constructs it and hands it over.
    store = default_raw_store if default_raw_store is not None else NullRawStore()

    return ThetaDataClient(
        settings=ThetaDataSettings(
            base_url=config.base_url,
            tier=Tier(config.tier),
            timeout_seconds=config.timeout_seconds,
            connect_timeout_seconds=config.connect_timeout_seconds,
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            username_env=config.username_env or "THETADATA_USERNAME",
            password_env=config.password_env or "THETADATA_PASSWORD",
            auth_mode=config.authentication_mode.value,
        ),
        greeks=GreeksParameters(
            greeks_version=config.greeks_version or "latest",
            # No substitution. A null rate_type means the parameter is
            # omitted and the vendor default applies -- see rate_type_policy().
            rate_type=config.rate_type,
            rate_value=config.rate_value,
            annual_dividend=config.annual_dividend,
            use_market_value=config.use_market_value,
        ),
        transport=retrying,
        raw_store=store,
        clock=clock,
    )


def effective_transport_settings(config: ThetaDataConfig) -> dict[str, Any]:
    """What the configured transport will actually use, with no secrets in it.

    Derived from the same ``httpx_transport_kwargs`` the real transport is
    constructed from, so a report of the effective settings cannot drift from
    the settings. Credentials are reported as *whether* they resolved and from
    which environment variable, never as values: an operator needs to know that
    authentication is configured, and a report is a file.
    """
    kwargs = httpx_transport_kwargs(config)
    username, _ = config.resolved_credentials()
    return {
        "base_url": _safe_base_url(config.base_url),
        # What the profile *names* as a fallback destination, and whether
        # anything is using it. v2.1.12 reported one path and wrote to another.
        "configured_fallback_raw_capture_path": config.raw_capture_path,
        "configured_raw_capture_enabled": config.raw_capture_enabled,
        "authentication_mode": config.authentication_mode.value,
        "credentials_resolved": bool(kwargs["basic_auth"]),
        "username_env": config.username_env or "THETADATA_USERNAME",
        "password_env": config.password_env or "THETADATA_PASSWORD",
        "username_present": bool(username),
        "connect_timeout_seconds": kwargs["connect_timeout_seconds"],
        "read_timeout_seconds": kwargs["read_timeout_seconds"],
        "max_response_bytes": kwargs["max_response_bytes"],
        "max_retries": config.max_retries,
        "backoff_base_seconds": config.backoff_base_seconds,
        # Reported because it decides the path the bytes take, and a capture
        # that says LOCAL_TERMINAL_CAPTURE should have gone to the local
        # terminal. Inside the approval's transport fingerprint, so changing
        # the routing policy invalidates an approval rather than quietly
        # redirecting an approved run.
        "trust_env": bool(kwargs.get("trust_env", False)),
    }


def _safe_base_url(url: str) -> str:
    """A base URL with any embedded userinfo removed.

    ``https://user:secret@host/`` is a legal URL and a credential leak the
    moment it reaches a report.
    """
    text = str(url or "")
    scheme, separator, remainder = text.partition("://")
    if not separator or "@" not in remainder.partition("/")[0]:
        return text
    authority, _, path = remainder.partition("/")
    host = authority.rpartition("@")[2]
    return f"{scheme}://***@{host}" + (f"/{path}" if path else "")


@dataclass(frozen=True, slots=True)
class ThetaDataRuntime:
    """Everything a configured ThetaData session needs, assembled once.

    v2.1 introduced the typed config and a client factory, which fixed half the
    problem: the ``thetadata:`` section stopped being validated and discarded.
    But five settings -- ``iv_source``, ``duplicate_policy``, ``max_dte``,
    ``strike_range`` and ``min_time`` -- reached the typed object and stopped
    there. ``build_thetadata_client`` never read them, and nothing told a caller
    they had to re-supply them by hand on every ``fetch_chain``.

    A setting that is parsed, validated, type-checked and hashed into the config
    fingerprint but never applied is worse than a missing one, because it
    survives review: somebody reads ``max_dte: 45`` in the YAML and believes it.

    This object is the single place where configuration becomes behaviour. The
    client carries the transport-level settings, ``default_chain_request``
    carries the server-side filters, and the assembly settings carry the ones
    that only matter once rows come back.
    """

    client: ThetaDataClient
    default_chain_request: ChainRequest
    iv_source: IVSource
    duplicate_policy: str
    config: ThetaDataConfig
    #: Which option root, and which index its options are written on. Carried
    #: as a pair because they are two facts: v2.1.15 held one symbol and sent
    #: ``SPXW`` to ``/v3/index/snapshot/price``, which asks for the price of an
    #: instrument that does not exist.
    instruments: InstrumentMapping = field(
        default_factory=lambda: InstrumentMapping(
            option_symbol="SPXW", underlying_index_symbol="SPX"
        )
    )

    @property
    def index_symbol(self) -> str:
        """The symbol the index snapshot must be asked with."""
        return self.instruments.underlying_index_symbol

    @property
    def option_symbol(self) -> str:
        """The root every option-market request must be asked with."""
        return self.instruments.option_symbol

    @classmethod
    def from_config(
        cls,
        config: ThetaDataConfig,
        *,
        symbol: str = "SPXW",
        transport: Any = None,
        clock: Any = None,
        attempt_observer: Any = None,
        default_raw_store: Any = None,
    ) -> ThetaDataRuntime:
        """The one sanctioned entry point. Nothing else assembles a session."""
        from src.adapters.thetadata.client import ChainRequest
        from src.adapters.thetadata.instruments import mapping_for

        # Refuses a root with no declared underlying rather than defaulting to
        # the root itself, which is precisely how ``SPXW`` reached the index
        # endpoint.
        instruments = mapping_for(symbol)

        return cls(
            client=build_thetadata_client(
                config,
                transport=transport,
                clock=clock,
                attempt_observer=attempt_observer,
                default_raw_store=default_raw_store,
            ),
            default_chain_request=ChainRequest(
                symbol=symbol,
                max_dte=config.max_dte,
                strike_range=config.strike_range,
                min_time=config.min_time,
            ),
            iv_source=config.iv_source,
            duplicate_policy=config.duplicate_policy,
            config=config,
            instruments=instruments,
        )

    def effective_limits(self) -> dict[str, Any]:
        """The limits actually in force, for metadata and for assertions."""
        return {
            "max_response_bytes": self.config.max_response_bytes,
            "timeout_seconds": self.config.timeout_seconds,
            "connect_timeout_seconds": self.config.connect_timeout_seconds,
            "max_retries": self.config.max_retries,
        }

    def capture_index_snapshot(
        self, *, as_of: datetime, capture: Any
    ) -> tuple[float, datetime | None, Any] | None:
        """Fetch the vendor's index print into this capture session.

        The underlying every gamma is computed against, read from the vendor
        rather than accepted from a caller, and stored in the same session as
        the chain it prices -- so the manifest links the two.

        Returns ``(spot, timestamp, observation)``. The observation is read back
        out of the stored payload, so the number in the snapshot and the number
        in the audit trail are the same number by construction.
        """
        from src.adapters.thetadata.endpoints import Endpoint
        from src.adapters.validation import AdapterValidator

        record = self.client.fetch_index_snapshot(
            # **The index symbol, not the option root.** The whole point of
            # the mapping: this request is about SPX, and the chain around it
            # is about SPXW.
            symbol=self.index_symbol,
            as_of=as_of,
            capture=capture,
        )
        if record is None:
            return None
        if capture is None:
            # No capture session, so nothing was stored and there is nothing to
            # read back. The value is still usable; it just cannot be attributed.
            return record.spot, record.timestamp, None

        from src.adapters.raw_store import RawCaptureManifest

        manifest = RawCaptureManifest.from_session(capture)
        observation = AdapterValidator.observe_field(
            manifest=manifest,
            # The session's own store, not the client's default: a fetch may be
            # directed elsewhere, and reading the observation back from a place
            # the bytes were never written is how a spot silently loses its
            # attribution.
            store=capture.store,
            endpoint=Endpoint.INDEX_PRICE_SNAPSHOT,
            field_path="price",
        )
        price = observation.observed_value
        if isinstance(price, bool) or not isinstance(price, int | float):
            raise ThetaDataConfigError(
                f"the index snapshot returned {price!r} for price, which "
                "is not a number. Every gamma in the chain is computed against "
                "this value."
            )
        return float(price), observation.source_timestamp, observation

    def fetch_chain(
        self,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        risk_free_rate: float = 0.0,
        dividend_yield: float = 0.0,
        capture: Any = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
        universe_evidence: dict[str, Any] | None = None,
        pipeline: Any = None,
        manifest_since: int = 0,
        capture_plan_fingerprint: str = "",
    ) -> ChainSnapshot:
        """Fetch and assemble using the configured settings.

        Deliberately takes no ``iv_source``, ``duplicate_policy``, ``max_dte``,
        ``strike_range`` or ``min_time`` argument. Those are configuration; a
        caller who could pass them here could also disagree with the config, and
        then the YAML would be a suggestion rather than a setting.

        v2.1.3 also accepted a whole ``request``, which reopened every one of
        those seams at once: symbol, DTE window and strike range could all be
        replaced, so the snapshot's own provenance record could describe a
        session that had not happened. The request is now the session's alone.

        Prefer ``ThetaDataResearchPipeline.fetch_chain``. Reaching the runtime
        directly means supplying the rate and dividend by hand, and those are
        the numbers the compatibility check compared against the vendor's.
        """
        if capture is None and self.config.raw_capture_enabled:
            # Configuring a capture path and then getting no audit trail because
            # nobody threaded a session through is the same class of defect as
            # a setting that never reaches a request.
            from src.adapters.raw_store import CaptureSession, new_capture_session_id
            from src.adapters.thetadata.client import capture_origin_of

            capture = CaptureSession(
                store=self.client.raw_store,
                # Market time is audit metadata inside the id; the nonce is what
                # makes it unique. Deriving uniqueness from as_of meant two
                # fetches at the same market instant collided in an append-only
                # store.
                session_id=new_capture_session_id(as_of=as_of),
                # Read off the transport actually in use, so an offline fixture
                # cannot present itself as a live capture.
                capture_origin=capture_origin_of(
                    self.client.transport, self.client.settings.base_url
                ),
            )

        chain = self.client.fetch_chain(
            self.default_chain_request,
            as_of=as_of,
            spot=spot,
            spot_timestamp=spot_timestamp,
            open_interest_as_of=open_interest_as_of,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            iv_source=self.iv_source,
            duplicate_policy=self.duplicate_policy,
            capture=capture,
            expected_contract_ids=expected_contract_ids,
            expected_source=expected_source,
            universe_evidence=universe_evidence,
        )

        # Link the normalized chain to the exact raw records it came from.
        # Without this a stored chain and a directory of payloads are two
        # artefacts that merely share a timestamp.
        from dataclasses import replace as _replace

        from src.adapters.raw_store import RawCaptureManifest

        # ``manifest_since`` is a mark taken before this fetch. Without it a
        # session reused for a second chain pull gives the second snapshot a
        # manifest naming the first snapshot's responses.
        manifest = (
            RawCaptureManifest.from_session(
                capture,
                since=manifest_since,
                capture_plan_fingerprint=capture_plan_fingerprint,
                pipeline_fingerprint=(
                    pipeline.fingerprint() if pipeline is not None else ""
                ),
            )
            if capture is not None
            else RawCaptureManifest.disabled()
        )
        extra: dict[str, Any] = {"raw_capture_manifest": manifest.as_dict()}
        if pipeline is not None:
            # Which compatibility decision permitted this calculation. A GEX
            # number should be able to show what allowed it to be computed.
            extra["pipeline"] = pipeline.as_dict()
        return _replace(chain, meta={**chain.meta, **extra})
