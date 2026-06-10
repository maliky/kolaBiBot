import pytest
from kolabi.shared.binance_futures import (
    binance_futures_audit_db_url,
    binance_futures_critical_db_url,
    binance_futures_environment,
    binance_futures_private_db_url,
    binance_futures_public_db_url,
    binance_futures_telemetry_db_url,
)
from kolabi.shared.config import load_exchange_config
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
    kraken_futures_public_db_url,
    kraken_futures_telemetry_db_url,
)


def test_load_exchange_config_binance_defaults(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_FUTURES_USE_DEMO", raising=False)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")

    cfg = load_exchange_config("binance", symbol="BTCUSDT", environment="demo")

    assert cfg.api_key == "key"
    assert cfg.base_url == "https://testnet.binancefuture.com"
    assert cfg.symbol == "BTCUSDT"
    assert cfg.adapter_kwargs["environment"] == "demo"


def test_load_exchange_config_bitmex_testnet(monkeypatch) -> None:
    monkeypatch.setenv("BITMEX_TEST_KEY", "k_test")
    monkeypatch.setenv("BITMEX_TEST_SECRET", "s_test")
    monkeypatch.setenv("BITMEX_USE_TESTNET", "1")
    monkeypatch.setenv("BITMEX_TIMEOUT", "15")

    cfg = load_exchange_config("bitmex", symbol="XBTUSD")

    assert cfg.api_key == "k_test"
    assert cfg.base_url == "https://testnet.bitmex.com/api/v1/"
    assert cfg.adapter_kwargs["timeout"] == 15.0


def test_load_exchange_config_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    with pytest.raises(ValueError):
        load_exchange_config("binance", symbol="BTCUSDT", environment="demo", env={})


def test_load_exchange_config_kraken_demo(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("KRAKEN_FUTURE_AUDIT_DB_URL", "postgresql://audit")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="demo")

    assert cfg.api_key == "demo-key"
    assert cfg.base_url == "https://demo-futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "demo"
    assert cfg.adapter_kwargs["audit_db_url"] == "postgresql://audit"


def test_load_exchange_config_accepts_credential_env_name_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO2_API_KEY", "demo2-key")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO2_API_SECRET", "demo2-secret")

    cfg = load_exchange_config(
        "kraken",
        symbol="PI_XBTUSD",
        environment="demo",
        api_key_env="KRAKEN_FUTURE_DEMO2_API_KEY",
        api_secret_env="KRAKEN_FUTURE_DEMO2_API_SECRET",
    )

    assert cfg.api_key == "demo2-key"
    assert cfg.api_secret == "demo2-secret"
    assert "api_key_env" not in cfg.adapter_kwargs
    assert "api_secret_env" not in cfg.adapter_kwargs


def test_load_exchange_config_kraken_live(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_API_KEY", "live-key")
    monkeypatch.setenv("KRAKEN_FUTURE_API_SECRET", "live-secret")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="live")

    assert cfg.api_key == "live-key"
    assert cfg.base_url == "https://futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "live"


def test_kraken_futures_default_postgres_lanes() -> None:
    demo = kraken_futures_environment("demo")
    live = kraken_futures_environment("live")

    assert demo.public_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    assert demo.private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    assert demo.critical_private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
    assert live.public_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    assert live.private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    assert live.critical_private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"


def test_kraken_futures_public_db_url_uses_shared_market_lane() -> None:
    assert (
        kraken_futures_public_db_url("demo", "PI_ADAUSD")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )
    assert (
        kraken_futures_public_db_url("live", "PF_SOLUSD")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )


def test_kraken_futures_audit_and_telemetry_paths_are_account_scoped() -> None:
    assert (
        kraken_futures_audit_db_url("demo", "default")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit"
    )
    assert (
        kraken_futures_telemetry_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_telemetry_advers"
    )


def test_binance_futures_default_paths_are_exchange_scoped() -> None:
    demo = binance_futures_environment("demo")

    assert demo.rest_url == "https://testnet.binancefuture.com"
    assert demo.api_key_env == "BINANCE_FUTURES_DEMO_API_KEY"
    assert (
        binance_futures_public_db_url("demo", "BTCUSDT")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )
    assert (
        binance_futures_private_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers"
    )
    assert (
        binance_futures_critical_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers"
    )
    assert (
        binance_futures_audit_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit_advers"
    )
    assert (
        binance_futures_telemetry_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_telemetry_advers"
    )
