from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BinanceFuturesEnvironment:
    """Resolved URLs, DB names, and env vars for Binance USD-M Futures."""

    environment: str
    public_ws_url: str
    private_ws_url: str
    rest_url: str
    public_db_url: str
    private_db_url: str
    critical_private_db_url: str
    api_key_env: str
    api_secret_env: str


_BINANCE_FUTURES_ENVIRONMENTS = {
    "demo": BinanceFuturesEnvironment(
        environment="demo",
        public_ws_url="wss://stream.binancefuture.com/stream",
        private_ws_url="wss://stream.binancefuture.com/ws",
        rest_url="https://testnet.binancefuture.com",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="BINF_DEMO_API_KEY",
        api_secret_env="BINF_DEMO_API_SECRET",
    ),
    "live": BinanceFuturesEnvironment(
        environment="live",
        public_ws_url="wss://fstream.binance.com/stream",
        private_ws_url="wss://fstream.binance.com/ws",
        rest_url="https://fapi.binance.com",
        public_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
        private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account",
        critical_private_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical",
        api_key_env="BINF_API_KEY",
        api_secret_env="BINF_API_SECRET",
    ),
}


def _db_safe_symbol(symbol: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in symbol)
    return cleaned or "UNKNOWN"


def binance_futures_environment(environment: str) -> BinanceFuturesEnvironment:
    """Return Binance USD-M Futures endpoints for `demo` or `live`."""

    normalized = (environment or "demo").strip().lower()
    if normalized not in _BINANCE_FUTURES_ENVIRONMENTS:
        raise ValueError(
            f"Unsupported Binance Futures environment '{environment}'. "
            "Expected 'demo' or 'live'."
        )
    return _BINANCE_FUTURES_ENVIRONMENTS[normalized]


def binance_futures_public_db_url(environment: str, symbol: str) -> str:
    """Return the default public DB URL for one Binance Futures instrument."""

    del symbol
    return binance_futures_environment(environment).public_db_url


def binance_futures_private_db_url(environment: str, account_scope: str = "default") -> str:
    """Return the default private account DB URL for one Binance account scope."""

    binance_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_account{suffix}"
    )


def binance_futures_critical_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default critical private DB URL for one Binance account scope."""

    binance_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_critical{suffix}"
    )


def binance_futures_audit_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default Binance REST audit DB URL for one account scope."""

    binance_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_audit{suffix}"
    )


def binance_futures_telemetry_db_url(
    environment: str,
    account_scope: str = "default",
) -> str:
    """Return the default Binance bot telemetry DB URL for one account scope."""

    binance_futures_environment(environment)
    safe_scope = _db_safe_symbol(account_scope.strip() or "default")
    suffix = "" if safe_scope == "default" else f"_{safe_scope}"
    return (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/"
        f"kolabi_telemetry{suffix}"
    )
