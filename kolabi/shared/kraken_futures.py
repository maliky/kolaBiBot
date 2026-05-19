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
    api_key_env: str
    api_secret_env: str


_KRAKEN_FUTURES_ENVIRONMENTS = {
    "demo": KrakenFuturesEnvironment(
        environment="demo",
        public_ws_url="wss://demo-futures.kraken.com/ws/v1",
        private_ws_url="wss://demo-futures.kraken.com/ws/v1",
        rest_url="https://demo-futures.kraken.com/derivatives/api/v3",
        public_db_url="sqlite:///pub-futures-demo.sqlite",
        private_db_url="sqlite:///prv-futures-demo.sqlite",
        api_key_env="KRAKEN_FUTURE_DEMO_API_KEY",
        api_secret_env="KRAKEN_FUTURE_DEMO_API_SECRET",
    ),
    "live": KrakenFuturesEnvironment(
        environment="live",
        public_ws_url="wss://futures.kraken.com/ws/v1",
        private_ws_url="wss://futures.kraken.com/ws/v1",
        rest_url="https://futures.kraken.com/derivatives/api/v3",
        public_db_url="sqlite:///pub-futures-live.sqlite",
        private_db_url="sqlite:///prv-futures-live.sqlite",
        api_key_env="KRAKEN_FUTURE_API_KEY",
        api_secret_env="KRAKEN_FUTURE_API_SECRET",
    ),
}


def kraken_futures_environment(environment: str) -> KrakenFuturesEnvironment:
    """Return the resolved configuration for Kraken Futures `demo` or `live`."""

    normalized = (environment or "demo").strip().lower()
    if normalized not in _KRAKEN_FUTURES_ENVIRONMENTS:
        raise ValueError(
            f"Unsupported Kraken Futures environment '{environment}'. "
            "Expected 'demo' or 'live'."
        )
    return _KRAKEN_FUTURES_ENVIRONMENTS[normalized]

