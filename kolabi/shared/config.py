
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

from kolabi.shared.bitmex_futures import bitmex_futures_environment
from kolabi.shared.binance_futures import binance_futures_environment
from kolabi.shared.kraken_futures import kraken_futures_environment


@dataclass
class ExchangeConfig:
    """Container for adapter credentials and runtime options."""

    api_key: str
    api_secret: str
    base_url: str
    symbol: str
    adapter_kwargs: Dict[str, Any] = field(default_factory=dict)


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


EnvVarSpec = str | tuple[str, ...] | list[str]


def env_var_names(spec: object) -> tuple[str, ...]:
    """Return a normalized tuple of environment variable names."""

    if isinstance(spec, str):
        return (spec,) if spec else ()
    if isinstance(spec, (tuple, list)):
        return tuple(str(name) for name in spec if str(name))
    return ()


def first_configured_env_name(
    names: EnvVarSpec,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the first env name with a non-empty value, else the primary name."""

    candidates = env_var_names(names)
    env_mapping = env or os.environ
    for name in candidates:
        if env_mapping.get(name):
            return name
    return candidates[0] if candidates else ""


def resolve_env_value(
    names: EnvVarSpec,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return the first non-empty value and the env name that supplied it."""

    env_mapping = env or os.environ
    for name in env_var_names(names):
        value = env_mapping.get(name)
        if value:
            return value, name
    return "", ""


def _unique_env_names(*names: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _route_env(prefix: str, environment: str, suffix: str) -> str:
    return f"{prefix}_DEMO_{suffix}" if environment == "demo" else f"{prefix}_{suffix}"


def exchange_credential_env_names(
    exchange: str,
    market_type: str,
    environment: str,
    *,
    secret: bool = False,
) -> tuple[str, ...]:
    """Return primary route-code credential env names with legacy fallbacks."""

    normalized_exchange = (exchange or "").strip().lower()
    normalized_market = (market_type or "futures").strip().lower()
    normalized_env = (environment or "demo").strip().lower()
    suffix = "API_SECRET" if secret else "API_KEY"

    if normalized_exchange == "kraken":
        legacy_suffix = suffix
        if normalized_market == "futures":
            legacy = (
                f"KRAKEN_FUTURE_DEMO_{legacy_suffix}"
                if normalized_env == "demo"
                else f"KRAKEN_FUTURE_{legacy_suffix}"
            )
            return _unique_env_names(
                _route_env("KRKF", normalized_env, suffix),
                legacy,
            )
        if normalized_market == "spot":
            legacy = (
                f"KRAKEN_SPOT_DEMO_{legacy_suffix}"
                if normalized_env == "demo"
                else f"KRAKEN_SPOT_{legacy_suffix}"
            )
            return _unique_env_names(
                _route_env("KRKS", normalized_env, suffix),
                legacy,
            )
        if normalized_market == "margin":
            legacy = (
                f"KRAKEN_SPOT_DEMO_{legacy_suffix}"
                if normalized_env == "demo"
                else f"KRAKEN_SPOT_{legacy_suffix}"
            )
            return _unique_env_names(
                _route_env("KRKM", normalized_env, suffix),
                _route_env("KRKS", normalized_env, suffix),
                legacy,
            )
    if normalized_exchange == "binance":
        if normalized_market == "futures":
            legacy = (
                f"BINANCE_FUTURES_DEMO_{suffix}"
                if normalized_env == "demo"
                else f"BINANCE_FUTURES_{suffix}"
            )
            return _unique_env_names(
                _route_env("BINF", normalized_env, suffix),
                legacy,
            )
        if normalized_market == "spot":
            legacy = (
                f"BINANCE_SPOT_DEMO_{suffix}"
                if normalized_env == "demo"
                else f"BINANCE_SPOT_{suffix}"
            )
            return _unique_env_names(
                _route_env("BINS", normalized_env, suffix),
                legacy,
            )
        if normalized_market == "margin":
            legacy = (
                f"BINANCE_MARGIN_DEMO_{suffix}"
                if normalized_env == "demo"
                else f"BINANCE_MARGIN_{suffix}"
            )
            return _unique_env_names(
                _route_env("BINM", normalized_env, suffix),
                legacy,
            )
        if normalized_market == "isolated_margin":
            legacy = (
                f"BINANCE_MARGIN_DEMO_{suffix}"
                if normalized_env == "demo"
                else f"BINANCE_MARGIN_{suffix}"
            )
            return _unique_env_names(
                _route_env("BINI", normalized_env, suffix),
                _route_env("BINM", normalized_env, suffix),
                legacy,
            )
    if normalized_exchange == "bitmex":
        legacy = (
            "BITMEX_TEST_SECRET"
            if secret and normalized_env == "demo"
            else "BITMEX_TEST_KEY"
            if normalized_env == "demo"
            else "BITMEX_SECRET"
            if secret
            else "BITMEX_KEY"
        )
        return _unique_env_names(_route_env("BTX", normalized_env, suffix), legacy)

    return ()


_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "binance": {
        "base_url": "https://fapi.binance.com",
        "test_base_url": "https://testnet.binancefuture.com",
        "key_var": exchange_credential_env_names("binance", "futures", "live"),
        "secret_var": exchange_credential_env_names(
            "binance",
            "futures",
            "live",
            secret=True,
        ),
        "test_key_var": exchange_credential_env_names("binance", "futures", "demo"),
        "test_secret_var": exchange_credential_env_names(
            "binance",
            "futures",
            "demo",
            secret=True,
        ),
        "use_testnet_var": "BINANCE_FUTURES_USE_DEMO",
        "adapter_env": {
            "account_db_url": "BINANCE_FUTURES_ACCOUNT_DB_URL",
            "public_db_url": "BINANCE_FUTURES_PUBLIC_DB_URL",
            "audit_db_url": "BINANCE_FUTURES_AUDIT_DB_URL",
            "timeout": "BINANCE_FUTURES_TIMEOUT",
        },
    },
    "bitmex": {
        "base_url": "https://www.bitmex.com/api/v1/",
        "test_base_url": "https://testnet.bitmex.com/api/v1/",
        "key_var": exchange_credential_env_names("bitmex", "futures", "live"),
        "secret_var": exchange_credential_env_names(
            "bitmex",
            "futures",
            "live",
            secret=True,
        ),
        "test_key_var": exchange_credential_env_names("bitmex", "futures", "demo"),
        "test_secret_var": exchange_credential_env_names(
            "bitmex",
            "futures",
            "demo",
            secret=True,
        ),
        "use_testnet_var": "BITMEX_USE_TESTNET",
        "adapter_env": {
            "audit_db_url": "BITMEX_AUDIT_DB_URL",
            "orderIDPrefix": "BITMEX_ORDER_ID_PREFIX",
            "postOnly": "BITMEX_POST_ONLY",
            "timeout": "BITMEX_TIMEOUT",
        },
    },
    "kraken": {
        "base_url": "https://futures.kraken.com",
        "test_base_url": "https://demo-futures.kraken.com",
        "key_var": exchange_credential_env_names("kraken", "futures", "live"),
        "secret_var": exchange_credential_env_names(
            "kraken",
            "futures",
            "live",
            secret=True,
        ),
        "test_key_var": exchange_credential_env_names("kraken", "futures", "demo"),
        "test_secret_var": exchange_credential_env_names(
            "kraken",
            "futures",
            "demo",
            secret=True,
        ),
        "use_testnet_var": "KRAKEN_FUTURE_USE_DEMO",
        "adapter_env": {
            "postOnly": "KRAKEN_FUTURE_POST_ONLY",
            "timeout": "KRAKEN_FUTURE_TIMEOUT",
            "account_db_url": "KRAKEN_FUTURE_ACCOUNT_DB_URL",
            "public_db_url": "KRAKEN_FUTURE_PUBLIC_DB_URL",
            "audit_db_url": "KRAKEN_FUTURE_AUDIT_DB_URL",
        },
    },
}


def _route_defaults_for_market(
    exchange: str,
    market_type: str,
    environment: str,
) -> Dict[str, Any]:
    normalized_exchange = (exchange or "").strip().lower()
    normalized_market = (market_type or "futures").strip().lower()
    if normalized_exchange == "binance":
        return _binance_defaults_for_market(normalized_market, environment)
    if normalized_exchange == "kraken":
        if normalized_market == "futures":
            return dict(_DEFAULTS["kraken"])
        return _kraken_defaults_for_market(normalized_market)
    if normalized_exchange == "bitmex":
        if normalized_market not in {"futures", "spot"}:
            raise ValueError("BitMEX supports futures and spot market types")
        return dict(_DEFAULTS["bitmex"])
    defaults = _DEFAULTS.get(normalized_exchange)
    if defaults is None:
        raise ValueError(f"Unknown exchange '{exchange}'")
    return dict(defaults)


def exchange_base_url_env_names(
    exchange: str,
    market_type: str,
    environment: str,
) -> tuple[str, ...]:
    """Return base URL env names consulted for one exchange route."""

    normalized_exchange = (exchange or "").strip().lower()
    normalized_env = (environment or "demo").strip().lower()
    testnet = normalized_env == "demo"
    defaults = _route_defaults_for_market(
        normalized_exchange,
        market_type,
        normalized_env,
    )
    specific_key = defaults.get("test_base_url_var" if testnet else "base_url_var")
    generic_key = f"{normalized_exchange.upper()}_{'TEST_' if testnet else ''}BASE_URL"
    return _unique_env_names(str(specific_key or ""), generic_key)


def exchange_requires_explicit_base_url(
    exchange: str,
    market_type: str,
    environment: str,
) -> bool:
    """Return whether a route needs an explicit market-capable REST URL."""

    normalized_env = (environment or "demo").strip().lower()
    if normalized_env != "demo":
        return False
    defaults = _route_defaults_for_market(
        exchange,
        market_type,
        normalized_env,
    )
    return bool(defaults.get("requires_explicit_test_base_url"))


def load_exchange_config(
    name: str,
    *,
    symbol: str,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> ExchangeConfig:
    """Return a validated config object for the given exchange."""
    normalized = name.lower()
    env_mapping = env or os.environ
    market_type = str(overrides.pop("market_type", "futures") or "futures").strip().lower()
    defaults = _DEFAULTS.get(normalized)
    if not defaults:
        raise ValueError(f"Unknown exchange '{name}'")
    if normalized not in {"binance", "kraken", "bitmex"} and market_type != "futures":
        raise ValueError(
            f"Exchange '{name}' does not support market type '{market_type}'"
        )
    if normalized == "bitmex" and market_type not in {"futures", "spot"}:
        raise ValueError("BitMEX supports futures and spot market types")

    environment = str(overrides.pop("environment", "") or "").strip().lower()
    if normalized == "kraken":
        if market_type == "futures":
            kraken_env = kraken_futures_environment(environment or "demo")
            if "testnet" not in overrides:
                testnet = kraken_env.environment == "demo"
            else:
                testnet = bool(overrides.pop("testnet"))
            defaults = dict(defaults)
            defaults["base_url"] = kraken_env.rest_url.removesuffix("/derivatives/api/v3")
            defaults["test_base_url"] = kraken_env.rest_url.removesuffix(
                "/derivatives/api/v3"
            )
            key_names = exchange_credential_env_names(
                "kraken",
                "futures",
                kraken_env.environment,
            )
            secret_names = exchange_credential_env_names(
                "kraken",
                "futures",
                kraken_env.environment,
                secret=True,
            )
            defaults["key_var"] = key_names
            defaults["secret_var"] = secret_names
            defaults["test_key_var"] = key_names
            defaults["test_secret_var"] = secret_names
        else:
            defaults = _kraken_defaults_for_market(market_type)
            if "testnet" not in overrides:
                testnet = (environment or "live") == "demo"
            else:
                testnet = bool(overrides.pop("testnet"))
    elif normalized == "binance":
        defaults = _binance_defaults_for_market(market_type, environment or "demo")
        if market_type == "futures":
            binance_env = binance_futures_environment(environment or "demo")
            if "testnet" not in overrides:
                testnet = binance_env.environment == "demo"
            else:
                testnet = bool(overrides.pop("testnet"))
            defaults["base_url"] = binance_env.rest_url
            defaults["test_base_url"] = binance_env.rest_url
            key_names = exchange_credential_env_names(
                "binance",
                "futures",
                binance_env.environment,
            )
            secret_names = exchange_credential_env_names(
                "binance",
                "futures",
                binance_env.environment,
                secret=True,
            )
            defaults["key_var"] = key_names
            defaults["secret_var"] = secret_names
            defaults["test_key_var"] = key_names
            defaults["test_secret_var"] = secret_names
        elif "testnet" not in overrides:
            if environment:
                testnet = environment == "demo"
            else:
                testnet = _truthy(
                    env_mapping.get(defaults.get("use_testnet_var", ""), "0")
                )
        else:
            testnet = bool(overrides.pop("testnet"))
    elif normalized == "bitmex":
        bitmex_env = bitmex_futures_environment(environment or "demo")
        if "testnet" not in overrides:
            testnet = bitmex_env.environment == "demo"
        else:
            testnet = bool(overrides.pop("testnet"))
        defaults = dict(defaults)
        defaults["base_url"] = bitmex_env.rest_url + "/"
        defaults["test_base_url"] = bitmex_env.rest_url + "/"
        key_names = exchange_credential_env_names(
            "bitmex",
            market_type,
            bitmex_env.environment,
        )
        secret_names = exchange_credential_env_names(
            "bitmex",
            market_type,
            bitmex_env.environment,
            secret=True,
        )
        defaults["key_var"] = key_names
        defaults["secret_var"] = secret_names
        defaults["test_key_var"] = key_names
        defaults["test_secret_var"] = secret_names
    else:
        testnet = overrides.pop("testnet", None)
        if testnet is None:
            testnet = _truthy(env_mapping.get(defaults.get("use_testnet_var", ""), "0"))

    base_url = overrides.pop("base_url", None)
    base_url_source = "override" if base_url else ""
    if not base_url:
        key = f"{normalized.upper()}_{'TEST_' if testnet else ''}BASE_URL"
        default_url = defaults["test_base_url"] if testnet else defaults["base_url"]
        specific_key = defaults.get(
            "test_base_url_var" if testnet else "base_url_var"
        )
        if specific_key and env_mapping.get(str(specific_key)):
            base_url = env_mapping[str(specific_key)]
            base_url_source = str(specific_key)
        elif env_mapping.get(key):
            base_url = env_mapping[key]
            base_url_source = key
        else:
            base_url = str(default_url)
            base_url_source = "default"
    if defaults.get("requires_explicit_test_base_url") and testnet and base_url_source == "default":
        raise ValueError(
            f"{normalized.capitalize()} {market_type} demo requires an explicit "
            "market-capable base_url. Do not route demo margin or spot to live by default."
        )

    key = overrides.pop("api_key", None)
    secret = overrides.pop("api_secret", None)
    key_var_override = overrides.pop("api_key_env", None)
    secret_var_override = overrides.pop("api_secret_env", None)

    if not key:
        key_vars = (
            (key_var_override,)
            if key_var_override
            else env_var_names(defaults["test_key_var"] if testnet else defaults["key_var"])
        )
        key, _key_source = resolve_env_value(key_vars, env=env_mapping)
    if not secret:
        secret_vars = (
            (secret_var_override,)
            if secret_var_override
            else env_var_names(
                defaults["test_secret_var"] if testnet else defaults["secret_var"]
            )
        )
        secret, _secret_source = resolve_env_value(secret_vars, env=env_mapping)

    if not key or not secret:
        expected_key_vars = (
            (key_var_override,)
            if key_var_override
            else env_var_names(defaults["test_key_var"] if testnet else defaults["key_var"])
        )
        expected_secret_vars = (
            (secret_var_override,)
            if secret_var_override
            else env_var_names(
                defaults["test_secret_var"] if testnet else defaults["secret_var"]
            )
        )
        raise ValueError(
            f"Missing credentials for exchange '{name}'. "
            "Provide api_key/api_secret overrides or set one of "
            f"{', '.join(expected_key_vars)} and one of {', '.join(expected_secret_vars)}."
        )

    adapter_kwargs: Dict[str, Any] = {}
    adapter_env = defaults.get("adapter_env", {})
    for option, env_var in adapter_env.items():
        raw_value = env_mapping.get(env_var)
        if raw_value is None:
            continue
        if option.lower().endswith("only"):
            adapter_kwargs[option] = _truthy(raw_value)
        elif option.lower().endswith("timeout"):
            adapter_kwargs[option] = float(raw_value)
        else:
            adapter_kwargs[option] = raw_value

    extra_kwargs = overrides.pop("adapter_kwargs", {})
    adapter_kwargs.update(extra_kwargs)
    adapter_kwargs.update(overrides)
    if environment:
        adapter_kwargs.setdefault("environment", environment)
    if normalized == "binance":
        adapter_kwargs.setdefault("market_type", market_type)
        if market_type == "isolated_margin":
            adapter_kwargs.setdefault("is_isolated", True)
        if market_type in {"margin", "isolated_margin"}:
            adapter_kwargs.setdefault(
                "side_effect_type",
                env_mapping.get("BINANCE_MARGIN_SIDE_EFFECT_TYPE", "NO_SIDE_EFFECT"),
            )
    elif normalized == "kraken":
        adapter_kwargs.setdefault("market_type", market_type)
        if market_type == "margin":
            leverage = env_mapping.get("KRAKEN_SPOT_MARGIN_LEVERAGE")
            if leverage:
                adapter_kwargs.setdefault("leverage", leverage)
    elif normalized == "bitmex":
        adapter_kwargs.setdefault("market_type", market_type)

    return ExchangeConfig(
        api_key=key,
        api_secret=secret,
        base_url=base_url,
        symbol=symbol,
        adapter_kwargs=adapter_kwargs,
    )


def _binance_defaults_for_market(market_type: str, environment: str) -> Dict[str, Any]:
    if market_type == "futures":
        return dict(_DEFAULTS["binance"])
    if market_type == "spot":
        return {
            "base_url": "https://api.binance.com",
            "test_base_url": "https://testnet.binance.vision",
            "base_url_var": "BINANCE_SPOT_BASE_URL",
            "test_base_url_var": "BINANCE_SPOT_TEST_BASE_URL",
            "key_var": exchange_credential_env_names("binance", "spot", "live"),
            "secret_var": exchange_credential_env_names(
                "binance",
                "spot",
                "live",
                secret=True,
            ),
            "test_key_var": exchange_credential_env_names(
                "binance",
                "spot",
                "demo",
            ),
            "test_secret_var": exchange_credential_env_names(
                "binance",
                "spot",
                "demo",
                secret=True,
            ),
            "use_testnet_var": "BINANCE_SPOT_USE_DEMO",
            "adapter_env": {
                "account_db_url": "BINANCE_SPOT_ACCOUNT_DB_URL",
                "public_db_url": "BINANCE_SPOT_PUBLIC_DB_URL",
                "audit_db_url": "BINANCE_SPOT_AUDIT_DB_URL",
                "timeout": "BINANCE_SPOT_TIMEOUT",
            },
        }
    if market_type in {"margin", "isolated_margin"}:
        return {
            "base_url": "https://api.binance.com",
            "test_base_url": "",
            "base_url_var": "BINANCE_MARGIN_BASE_URL",
            "test_base_url_var": "BINANCE_MARGIN_TEST_BASE_URL",
            "key_var": exchange_credential_env_names(
                "binance",
                market_type,
                "live",
            ),
            "secret_var": exchange_credential_env_names(
                "binance",
                market_type,
                "live",
                secret=True,
            ),
            "test_key_var": exchange_credential_env_names(
                "binance",
                market_type,
                environment,
            ),
            "test_secret_var": exchange_credential_env_names(
                "binance",
                market_type,
                environment,
                secret=True,
            ),
            "use_testnet_var": "BINANCE_MARGIN_USE_DEMO",
            "requires_explicit_test_base_url": True,
            "adapter_env": {
                "account_db_url": "BINANCE_MARGIN_ACCOUNT_DB_URL",
                "public_db_url": "BINANCE_MARGIN_PUBLIC_DB_URL",
                "audit_db_url": "BINANCE_MARGIN_AUDIT_DB_URL",
                "timeout": "BINANCE_MARGIN_TIMEOUT",
                "side_effect_type": "BINANCE_MARGIN_SIDE_EFFECT_TYPE",
            },
        }
    raise ValueError(
        "Unsupported Binance market type "
        f"'{market_type}'. Use futures, spot, margin, or isolated_margin."
    )


def _kraken_defaults_for_market(market_type: str) -> Dict[str, Any]:
    if market_type == "spot":
        return {
            "base_url": "https://api.kraken.com",
            "test_base_url": "",
            "base_url_var": "KRAKEN_SPOT_BASE_URL",
            "test_base_url_var": "KRAKEN_SPOT_TEST_BASE_URL",
            "key_var": exchange_credential_env_names("kraken", "spot", "live"),
            "secret_var": exchange_credential_env_names(
                "kraken",
                "spot",
                "live",
                secret=True,
            ),
            "test_key_var": exchange_credential_env_names("kraken", "spot", "demo"),
            "test_secret_var": exchange_credential_env_names(
                "kraken",
                "spot",
                "demo",
                secret=True,
            ),
            "use_testnet_var": "KRAKEN_SPOT_USE_DEMO",
            "requires_explicit_test_base_url": True,
            "adapter_env": {
                "account_db_url": "KRAKEN_SPOT_ACCOUNT_DB_URL",
                "public_db_url": "KRAKEN_SPOT_PUBLIC_DB_URL",
                "audit_db_url": "KRAKEN_SPOT_AUDIT_DB_URL",
                "timeout": "KRAKEN_SPOT_TIMEOUT",
                "leverage": "KRAKEN_SPOT_MARGIN_LEVERAGE",
            },
        }
    if market_type == "margin":
        defaults = _kraken_defaults_for_market("spot")
        defaults["key_var"] = exchange_credential_env_names("kraken", "margin", "live")
        defaults["secret_var"] = exchange_credential_env_names(
            "kraken",
            "margin",
            "live",
            secret=True,
        )
        defaults["test_key_var"] = exchange_credential_env_names(
            "kraken",
            "margin",
            "demo",
        )
        defaults["test_secret_var"] = exchange_credential_env_names(
            "kraken",
            "margin",
            "demo",
            secret=True,
        )
        defaults["adapter_env"] = dict(defaults["adapter_env"])
        defaults["adapter_env"]["leverage"] = "KRAKEN_SPOT_MARGIN_LEVERAGE"
        return defaults
    raise ValueError(
        "Unsupported Kraken market type "
        f"'{market_type}'. Use futures, spot, or margin."
    )


__all__ = [
    "ExchangeConfig",
    "env_var_names",
    "exchange_base_url_env_names",
    "exchange_credential_env_names",
    "exchange_requires_explicit_base_url",
    "first_configured_env_name",
    "load_exchange_config",
    "resolve_env_value",
]
