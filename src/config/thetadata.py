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

from src.domain.iv import IVSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import date, datetime

    from src.adapters.thetadata.client import ChainRequest, ThetaDataClient
    from src.domain.contracts import ChainSnapshot


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
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

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


class ThetaDataConfigError(ValueError):
    """Invalid ThetaData configuration."""


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
        fail(
            f"{base_url!r} is not a valid absolute http(s) URL",
            "base_url",
        )

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

    rate_type = text("rate_type", "sofr", optional=True)
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
    )


def build_thetadata_client(
    config: ThetaDataConfig,
    *,
    transport: Any = None,
    clock: Any = None,
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
    from src.adapters.raw_store import FileRawStore, NullRawStore
    from src.adapters.thetadata.client import (
        GreeksParameters,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.thetadata.endpoints import Tier
    from src.adapters.transport import RetryingTransport, RetryPolicy

    # Resolve credentials BEFORE anything is constructed, so that a missing
    # secret cannot yield a usable unauthenticated client.
    username, password = config.resolved_credentials()

    if transport is None:  # pragma: no cover - needs the http extra and a network
        from src.adapters.transport import HttpxTransport

        transport = HttpxTransport(
            connect_timeout_seconds=config.connect_timeout_seconds,
            read_timeout_seconds=config.timeout_seconds,
            basic_auth=(username, password) if username and password else None,
        )

    retrying = RetryingTransport(
        transport,
        policy=RetryPolicy(
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
        ),
        max_response_bytes=config.max_response_bytes,
    )

    store = (
        FileRawStore(config.raw_capture_path)
        if config.raw_capture_enabled and config.raw_capture_path
        else NullRawStore()
    )

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
            rate_type=config.rate_type or "sofr",
            rate_value=config.rate_value,
            annual_dividend=config.annual_dividend,
            use_market_value=config.use_market_value,
        ),
        transport=retrying,
        raw_store=store,
        clock=clock,
    )


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

    @classmethod
    def from_config(
        cls,
        config: ThetaDataConfig,
        *,
        symbol: str = "SPXW",
        transport: Any = None,
        clock: Any = None,
    ) -> ThetaDataRuntime:
        """The one sanctioned entry point. Nothing else assembles a session."""
        from src.adapters.thetadata.client import ChainRequest

        return cls(
            client=build_thetadata_client(config, transport=transport, clock=clock),
            default_chain_request=ChainRequest(
                symbol=symbol,
                max_dte=config.max_dte,
                strike_range=config.strike_range,
                min_time=config.min_time,
            ),
            iv_source=config.iv_source,
            duplicate_policy=config.duplicate_policy,
            config=config,
        )

    def fetch_chain(
        self,
        *,
        as_of: datetime,
        spot: float,
        request: ChainRequest | None = None,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        risk_free_rate: float = 0.0,
        dividend_yield: float = 0.0,
        capture: Any = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
    ) -> ChainSnapshot:
        """Fetch and assemble using the configured settings.

        Deliberately takes no ``iv_source``, ``duplicate_policy``, ``max_dte``,
        ``strike_range`` or ``min_time`` argument. Those are configuration; a
        caller who could pass them here could also disagree with the config, and
        then the YAML would be a suggestion rather than a setting.
        """
        if capture is None and self.config.raw_capture_enabled:
            # Configuring a capture path and then getting no audit trail because
            # nobody threaded a session through is the same class of defect as
            # a setting that never reaches a request.
            from src.adapters.raw_store import CaptureSession

            capture = CaptureSession(
                store=self.client.raw_store,
                session_id=f"{as_of.date().isoformat()}-{as_of.strftime('%H%M%S')}",
            )

        return self.client.fetch_chain(
            request if request is not None else self.default_chain_request,
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
        )
