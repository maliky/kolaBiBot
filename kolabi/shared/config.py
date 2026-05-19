
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

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


_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "binance": {
        "base_url": "https://api.binance.com/api",
        "test_base_url": "https://testnet.binance.vision/api",
        "key_var": "BINANCE_KEY",
        "secret_var": "BINANCE_SECRET",
        "test_key_var": "BINANCE_TEST_KEY",
        "test_secret_var": "BINANCE_TEST_SECRET",
        "use_testnet_var": "BINANCE_USE_TESTNET",
    },
    "bitmex": {
        "base_url": "https://www.bitmex.com/api/v1/",
        "test_base_url": "https://testnet.bitmex.com/api/v1/",
        "key_var": "BITMEX_KEY",
        "secret_var": "BITMEX_SECRET",
        "test_key_var": "BITMEX_TEST_KEY",
        "test_secret_var": "BITMEX_TEST_SECRET",
        "use_testnet_var": "BITMEX_USE_TESTNET",
        "adapter_env": {
            "orderIDPrefix": "BITMEX_ORDER_ID_PREFIX",
            "postOnly": "BITMEX_POST_ONLY",
            "timeout": "BITMEX_TIMEOUT",
        },
    },
    "kraken": {
        "base_url": "https://futures.kraken.com",
        "test_base_url": "https://demo-futures.kraken.com",
        "key_var": "KRAKEN_FUTURE_API_KEY",
        "secret_var": "KRAKEN_FUTURE_API_SECRET",
        "test_key_var": "KRAKEN_FUTURE_DEMO_API_KEY",
        "test_secret_var": "KRAKEN_FUTURE_DEMO_API_SECRET",
        "use_testnet_var": "KRAKEN_FUTURE_USE_DEMO",
        "adapter_env": {
            "postOnly": "KRAKEN_FUTURE_POST_ONLY",
            "timeout": "KRAKEN_FUTURE_TIMEOUT",
            "account_db_url": "KRAKEN_FUTURE_ACCOUNT_DB_URL",
            "public_db_url": "KRAKEN_FUTURE_PUBLIC_DB_URL",
        },
    },
}


def load_exchange_config(
    name: str,
    *,
    symbol: str,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> ExchangeConfig:
    """Return a validated config object for the given exchange."""
    normalized = name.lower()
    defaults = _DEFAULTS.get(normalized)
    if not defaults:
        raise ValueError(f"Unknown exchange '{name}'")

    env_mapping = env or os.environ

    environment = str(overrides.pop("environment", "") or "").strip().lower()
    if normalized == "kraken":
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
        defaults["key_var"] = kraken_env.api_key_env
        defaults["secret_var"] = kraken_env.api_secret_env
        defaults["test_key_var"] = kraken_env.api_key_env
        defaults["test_secret_var"] = kraken_env.api_secret_env
    else:
        testnet = overrides.pop("testnet", None)
        if testnet is None:
            testnet = _truthy(env_mapping.get(defaults.get("use_testnet_var", ""), "0"))

    base_url = overrides.pop("base_url", None)
    if not base_url:
        key = f"{normalized.upper()}_{'TEST_' if testnet else ''}BASE_URL"
        default_url = defaults["test_base_url"] if testnet else defaults["base_url"]
        base_url = env_mapping.get(key, default_url)

    key = overrides.pop("api_key", None)
    secret = overrides.pop("api_secret", None)

    if not key:
        key_var = defaults["test_key_var"] if testnet else defaults["key_var"]
        key = env_mapping.get(key_var)
    if not secret:
        secret_var = defaults["test_secret_var"] if testnet else defaults["secret_var"]
        secret = env_mapping.get(secret_var)

    if not key or not secret:
        raise ValueError(
            f"Missing credentials for exchange '{name}'. "
            "Provide api_key/api_secret overrides or set the expected env vars."
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

    return ExchangeConfig(
        api_key=key,
        api_secret=secret,
        base_url=base_url,
        symbol=symbol,
        adapter_kwargs=adapter_kwargs,
    )


__all__ = ["ExchangeConfig", "load_exchange_config"]
