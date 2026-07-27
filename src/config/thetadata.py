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
import os
import pathlib
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.domain.iv import IVSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.adapters.thetadata.client import ThetaDataClient


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
        """Read credentials from the environment. Never from a file."""
        if not self.authentication_mode.requires_credentials:
            return None, None
        username = os.environ.get(self.username_env) if self.username_env else None
        password = os.environ.get(self.password_env) if self.password_env else None
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
VALID_DUPLICATE_POLICIES = ("reject", "newest_timestamp")


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
        key: str, default: float, *, low: float, high: float | None = None
    ) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            fail(f"expected a number, got {type(value).__name__}", key)
        if value < low:
            fail(f"value {value} is below the minimum {low}", key)
        if high is not None and value > high:
            fail(f"value {value} is above the maximum {high}", key)
        return float(value)

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

    base_url = raw.get("base_url", "http://127.0.0.1:25503")
    if not isinstance(base_url, str):
        fail("expected a string", "base_url")
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

    username_env = raw.get("username_env")
    password_env = raw.get("password_env")
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

    rate_type = raw.get("rate_type", "sofr")
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

    return ThetaDataConfig(
        base_url=base_url,
        authentication_mode=mode,
        username_env=username_env,
        password_env=password_env,
        tier=tier,
        timeout_seconds=number("timeout_seconds", 30.0, low=0.001, high=600.0),
        connect_timeout_seconds=number(
            "connect_timeout_seconds", 5.0, low=0.001, high=600.0
        ),
        max_retries=integer("max_retries", 3, low=0, high=20) or 0,
        backoff_base_seconds=number("backoff_base_seconds", 0.25, low=0.0, high=60.0),
        max_response_bytes=integer(
            "max_response_bytes", 64 * 1024 * 1024, low=1024, high=2**31
        )
        or 0,
        raw_capture_enabled=capture_enabled,
        raw_capture_path=(
            pathlib.Path(str(capture_path_raw)) if capture_path_raw else None
        ),
        greeks_version=raw.get("greeks_version", "latest"),
        rate_type=rate_type,
        rate_value=raw.get("rate_value"),
        annual_dividend=raw.get("annual_dividend"),
        use_market_value=use_market_value,
        max_dte=integer("max_dte", None, low=0, high=3650),
        strike_range=integer("strike_range", None, low=1, high=10_000),
        min_time=raw.get("min_time"),
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

    if transport is None:  # pragma: no cover - needs the http extra and a network
        from src.adapters.transport import HttpxTransport

        username, password = config.credentials()
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
