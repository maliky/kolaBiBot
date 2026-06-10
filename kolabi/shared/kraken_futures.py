from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KrakenFuturesEnvironment:
    """Resolved URLs, DB names, and env vars for one Futures environment."""

    environment: str
    public_ws_url: str
    private_ws_url: str
    rest_url: str
    public_db_url: str
    private_db_url: str
    critical_private_db_url: str
    api_key_env: str
    api_secret_env: str


_KRAKEN_FUTURES_ENVIRONMENTS = {
    "demo": KrakenFuturesEnvironment(
        environment="demo",
        public_ws_url="wss://demo-futures.kraken.com/ws/v1",
        private_ws_url="wss://demo-futures.kraken.com/ws/v1",
        rest_url="https://demo-futures.kraken.com/derivatives/api/v3",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="KRAKEN_FUTURE_DEMO_API_KEY",
        api_secret_env="KRAKEN_FUTURE_DEMO_API_SECRET",
    ),
    "live": KrakenFuturesEnvironment(
        environment="live",
        public_ws_url="wss://futures.kraken.com/ws/v1",
        private_ws_url="wss://futures.kraken.com/ws/v1",
        rest_url="https://futures.kraken.com/derivatives/api/v3",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="KRAKEN_FUTURE_API_KEY",
        api_secret_env="KRAKEN_FUTURE_API_SECRET",
    ),
}


def _db_safe_symbol(symbol: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in symbol)
    return cleaned or "UNKNOWN"


def kraken_futures_public_db_url(environment: str, symbol: str) -> str:
    """Return the default public DB URL for one Kraken Futures instrument."""

    del symbol
    return kraken_futures_environment(environment).public_db_url


def kraken_futures_audit_db_url(environment: str, account_scope: str = "default") -> str:
    """Return the default forensic REST audit DB URL for one account scope."""

    kraken_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_audit{suffix}"
    )


def kraken_futures_telemetry_db_url(environment: str, account_scope: str = "default") -> str:
    """Return the default bot telemetry DB URL for one account scope."""

    kraken_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_telemetry{suffix}"
    )


def kraken_futures_environment(environment: str) -> KrakenFuturesEnvironment:
    """Return the resolved configuration for Kraken Futures `demo` or `live`."""

    normalized = (environment or "demo").strip().lower()
    if normalized not in _KRAKEN_FUTURES_ENVIRONMENTS:
        raise ValueError(
            f"Unsupported Kraken Futures environment '{environment}'. "
            "Expected 'demo' or 'live'."
        )
    return _KRAKEN_FUTURES_ENVIRONMENTS[normalized]
