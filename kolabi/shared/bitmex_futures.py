from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BitmexFuturesEnvironment:
    """Resolved public endpoints and shared DB lanes for BitMEX derivatives."""

    environment: str
    public_ws_url: str
    rest_url: str
    public_db_url: str
    private_db_url: str
    critical_private_db_url: str
    api_key_env: str
    api_secret_env: str


_BITMEX_FUTURES_ENVIRONMENTS = {
    "demo": BitmexFuturesEnvironment(
        environment="demo",
        public_ws_url="wss://testnet.bitmex.com/realtime",
        rest_url="https://testnet.bitmex.com/api/v1",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="BTX_DEMO_API_KEY",
        api_secret_env="BTX_DEMO_API_SECRET",
    ),
    "live": BitmexFuturesEnvironment(
        environment="live",
        public_ws_url="wss://www.bitmex.com/realtime",
        rest_url="https://www.bitmex.com/api/v1",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="BTX_API_KEY",
        api_secret_env="BTX_API_SECRET",
    ),
}


def bitmex_futures_environment(environment: str) -> BitmexFuturesEnvironment:
    """Return BitMEX derivative endpoints for `demo` or `live`."""

    normalised = (environment or "demo").strip().lower()
    if normalised not in _BITMEX_FUTURES_ENVIRONMENTS:
        raise ValueError(
            f"Unsupported BitMEX environment '{environment}'. Expected 'demo' or 'live'."
        )
    return _BITMEX_FUTURES_ENVIRONMENTS[normalised]


def bitmex_futures_public_db_url(environment: str, symbol: str) -> str:
    """Return the shared public DB URL for one BitMEX instrument."""

    del symbol
    return bitmex_futures_environment(environment).public_db_url


def _db_safe_symbol(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)
    return cleaned or "UNKNOWN"


def bitmex_futures_private_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default private account DB URL for one BitMEX account scope."""

    bitmex_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_account{suffix}"
    )


def bitmex_futures_critical_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default critical private DB URL for one BitMEX account scope."""

    bitmex_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_critical{suffix}"
    )


def bitmex_futures_audit_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default BitMEX REST audit DB URL for one account scope."""

    bitmex_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_audit{suffix}"
    )


def bitmex_futures_telemetry_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default BitMEX bot telemetry DB URL for one account scope."""

    bitmex_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_telemetry{suffix}"
    )
