import pytest
from kolabi.shared.config import load_exchange_config
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
    kraken_futures_public_db_url,
    kraken_futures_telemetry_db_url,
)


def test_load_exchange_config_binance_defaults(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_USE_TESTNET", raising=False)
    monkeypatch.setenv("BINANCE_KEY", "key")
    monkeypatch.setenv("BINANCE_SECRET", "secret")

    cfg = load_exchange_config("binance", symbol="BTCUSDT")

    assert cfg.api_key == "key"
    assert cfg.base_url == "https://api.binance.com/api"
    assert cfg.symbol == "BTCUSDT"
    assert cfg.adapter_kwargs == {}


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
    monkeypatch.delenv("BINANCE_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    with pytest.raises(ValueError):
        load_exchange_config("binance", symbol="BTCUSDT", env={})


def test_load_exchange_config_kraken_demo(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("KRAKEN_FUTURE_AUDIT_DB_URL", "sqlite:///audit.sqlite")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="demo")

    assert cfg.api_key == "demo-key"
    assert cfg.base_url == "https://demo-futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "demo"
    assert cfg.adapter_kwargs["audit_db_url"] == "sqlite:///audit.sqlite"


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


def test_kraken_futures_default_sqlite_paths_live_under_dbs() -> None:
    demo = kraken_futures_environment("demo")
    live = kraken_futures_environment("live")

    assert demo.public_db_url == "sqlite:///dbs/pub-futures-demo-PI_XBTUSD.sqlite"
    assert demo.private_db_url == "sqlite:///dbs/prv-futures-demo.sqlite"
    assert demo.critical_private_db_url == "sqlite:///dbs/prv-futures-demo-critical.sqlite"
    assert live.public_db_url == "sqlite:///dbs/pub-futures-live-PI_XBTUSD.sqlite"
    assert live.private_db_url == "sqlite:///dbs/prv-futures-live.sqlite"
    assert live.critical_private_db_url == "sqlite:///dbs/prv-futures-live-critical.sqlite"


def test_kraken_futures_public_db_path_is_instrument_scoped() -> None:
    assert (
        kraken_futures_public_db_url("demo", "PI_ADAUSD")
        == "sqlite:///dbs/pub-futures-demo-PI_ADAUSD.sqlite"
    )
    assert (
        kraken_futures_public_db_url("live", "PF_SOLUSD")
        == "sqlite:///dbs/pub-futures-live-PF_SOLUSD.sqlite"
    )


def test_kraken_futures_audit_and_telemetry_paths_are_account_scoped() -> None:
    assert (
        kraken_futures_audit_db_url("demo", "default")
        == "sqlite:///dbs/audit-futures-demo-default.sqlite"
    )
    assert (
        kraken_futures_telemetry_db_url("demo", "advers")
        == "sqlite:///dbs/telemetry-futures-demo-advers.sqlite"
    )
