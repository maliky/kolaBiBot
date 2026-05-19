import pytest
from kolabi.shared.config import load_exchange_config


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

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="demo")

    assert cfg.api_key == "demo-key"
    assert cfg.base_url == "https://demo-futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "demo"


def test_load_exchange_config_kraken_live(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_API_KEY", "live-key")
    monkeypatch.setenv("KRAKEN_FUTURE_API_SECRET", "live-secret")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="live")

    assert cfg.api_key == "live-key"
    assert cfg.base_url == "https://futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "live"
