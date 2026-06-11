import pytest
from kolabi.shared.bitmex_futures import (
    bitmex_futures_audit_db_url,
    bitmex_futures_critical_db_url,
    bitmex_futures_environment,
    bitmex_futures_private_db_url,
    bitmex_futures_public_db_url,
    bitmex_futures_telemetry_db_url,
)
from kolabi.shared.binance_futures import (
    binance_futures_audit_db_url,
    binance_futures_critical_db_url,
    binance_futures_environment,
    binance_futures_private_db_url,
    binance_futures_public_db_url,
    binance_futures_telemetry_db_url,
)
from kolabi.shared.config import (
    exchange_base_url_env_names,
    exchange_requires_explicit_base_url,
    load_exchange_config,
)
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
    kraken_futures_public_db_url,
    kraken_futures_telemetry_db_url,
)


def test_load_exchange_config_binance_defaults(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_FUTURES_USE_DEMO", raising=False)
    monkeypatch.setenv("BINF_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINF_DEMO_API_SECRET", "secret")

    cfg = load_exchange_config("binance", symbol="BTCUSDT", environment="demo")

    assert cfg.api_key == "key"
    assert cfg.base_url == "https://testnet.binancefuture.com"
    assert cfg.symbol == "BTCUSDT"
    assert cfg.adapter_kwargs["environment"] == "demo"


def test_load_exchange_config_binance_spot_demo(monkeypatch) -> None:
    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")

    cfg = load_exchange_config(
        "binance",
        symbol="BTCUSDT",
        environment="demo",
        market_type="spot",
    )

    assert cfg.api_key == "spot-key"
    assert cfg.base_url == "https://testnet.binance.vision"
    assert cfg.adapter_kwargs["market_type"] == "spot"


def test_load_exchange_config_binance_margin_demo_requires_explicit_url(monkeypatch) -> None:
    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")

    with pytest.raises(ValueError, match="margin demo requires"):
        load_exchange_config(
            "binance",
            symbol="BTCUSDT",
            environment="demo",
            market_type="margin",
        )


def test_binance_margin_demo_base_url_requirement_metadata() -> None:
    assert exchange_requires_explicit_base_url("binance", "margin", "demo") is True
    assert exchange_requires_explicit_base_url("binance", "spot", "demo") is False
    assert exchange_requires_explicit_base_url("binance", "margin", "live") is False
    assert exchange_base_url_env_names("binance", "margin", "demo") == (
        "BINANCE_MARGIN_TEST_BASE_URL",
        "BINANCE_TEST_BASE_URL",
    )


def test_load_exchange_config_binance_isolated_margin(monkeypatch) -> None:
    monkeypatch.setenv("BINI_API_KEY", "margin-key")
    monkeypatch.setenv("BINI_API_SECRET", "margin-secret")

    cfg = load_exchange_config(
        "binance",
        symbol="BTCUSDT",
        environment="live",
        market_type="isolated_margin",
    )

    assert cfg.base_url == "https://api.binance.com"
    assert cfg.adapter_kwargs["market_type"] == "isolated_margin"
    assert cfg.adapter_kwargs["is_isolated"] is True
    assert cfg.adapter_kwargs["side_effect_type"] == "NO_SIDE_EFFECT"


def test_load_exchange_config_bitmex_testnet(monkeypatch) -> None:
    monkeypatch.setenv("BTX_DEMO_API_KEY", "k_test")
    monkeypatch.setenv("BTX_DEMO_API_SECRET", "s_test")
    monkeypatch.setenv("BITMEX_USE_TESTNET", "1")
    monkeypatch.setenv("BITMEX_TIMEOUT", "15")
    monkeypatch.setenv("BITMEX_AUDIT_DB_URL", "postgresql://bitmex-audit")

    cfg = load_exchange_config("bitmex", symbol="XBTUSD")

    assert cfg.api_key == "k_test"
    assert cfg.base_url == "https://testnet.bitmex.com/api/v1/"
    assert cfg.adapter_kwargs["timeout"] == 15.0
    assert cfg.adapter_kwargs["audit_db_url"] == "postgresql://bitmex-audit"


def test_load_exchange_config_bitmex_live(monkeypatch) -> None:
    monkeypatch.setenv("BTX_API_KEY", "k_live")
    monkeypatch.setenv("BTX_API_SECRET", "s_live")

    cfg = load_exchange_config("bitmex", symbol="XBTUSD", environment="live")

    assert cfg.api_key == "k_live"
    assert cfg.base_url == "https://www.bitmex.com/api/v1/"
    assert cfg.adapter_kwargs["environment"] == "live"


def test_load_exchange_config_bitmex_spot(monkeypatch) -> None:
    monkeypatch.setenv("BTX_DEMO_API_KEY", "k_test")
    monkeypatch.setenv("BTX_DEMO_API_SECRET", "s_test")

    cfg = load_exchange_config(
        "bitmex",
        symbol="XBT_USDT",
        environment="demo",
        market_type="spot",
    )

    assert cfg.adapter_kwargs["market_type"] == "spot"
    assert cfg.base_url == "https://testnet.bitmex.com/api/v1/"


def test_load_exchange_config_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    with pytest.raises(ValueError):
        load_exchange_config("binance", symbol="BTCUSDT", environment="demo", env={})


def test_load_exchange_config_kraken_demo(monkeypatch) -> None:
    monkeypatch.setenv("KRKF_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("KRKF_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("KRAKEN_FUTURE_AUDIT_DB_URL", "postgresql://audit")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="demo")

    assert cfg.api_key == "demo-key"
    assert cfg.base_url == "https://demo-futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "demo"
    assert cfg.adapter_kwargs["audit_db_url"] == "postgresql://audit"


def test_load_exchange_config_kraken_spot_live(monkeypatch) -> None:
    monkeypatch.setenv("KRKS_API_KEY", "spot-key")
    monkeypatch.setenv("KRKS_API_SECRET", "spot-secret")
    monkeypatch.setenv("KRAKEN_SPOT_AUDIT_DB_URL", "postgresql://spot-audit")

    cfg = load_exchange_config(
        "kraken",
        symbol="XBT/USD",
        environment="live",
        market_type="spot",
    )

    assert cfg.api_key == "spot-key"
    assert cfg.base_url == "https://api.kraken.com"
    assert cfg.adapter_kwargs["market_type"] == "spot"
    assert cfg.adapter_kwargs["audit_db_url"] == "postgresql://spot-audit"


def test_load_exchange_config_kraken_spot_demo_requires_explicit_url(monkeypatch) -> None:
    monkeypatch.setenv("KRKS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("KRKS_DEMO_API_SECRET", "spot-secret")

    with pytest.raises(ValueError, match="Kraken spot demo requires"):
        load_exchange_config(
            "kraken",
            symbol="XBT/USD",
            environment="demo",
            market_type="spot",
        )


def test_load_exchange_config_kraken_margin_uses_explicit_leverage(monkeypatch) -> None:
    monkeypatch.setenv("KRKM_API_KEY", "spot-key")
    monkeypatch.setenv("KRKM_API_SECRET", "spot-secret")
    monkeypatch.setenv("KRAKEN_SPOT_MARGIN_LEVERAGE", "2")

    cfg = load_exchange_config(
        "kraken",
        symbol="XBT/USD",
        environment="live",
        market_type="margin",
    )

    assert cfg.adapter_kwargs["market_type"] == "margin"
    assert cfg.adapter_kwargs["leverage"] == "2"


def test_load_exchange_config_accepts_credential_env_name_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KRKF_DEMO2_API_KEY", "demo2-key")
    monkeypatch.setenv("KRKF_DEMO2_API_SECRET", "demo2-secret")

    cfg = load_exchange_config(
        "kraken",
        symbol="PI_XBTUSD",
        environment="demo",
        api_key_env="KRKF_DEMO2_API_KEY",
        api_secret_env="KRKF_DEMO2_API_SECRET",
    )

    assert cfg.api_key == "demo2-key"
    assert cfg.api_secret == "demo2-secret"
    assert "api_key_env" not in cfg.adapter_kwargs
    assert "api_secret_env" not in cfg.adapter_kwargs


def test_load_exchange_config_kraken_live(monkeypatch) -> None:
    monkeypatch.setenv("KRKF_API_KEY", "live-key")
    monkeypatch.setenv("KRKF_API_SECRET", "live-secret")

    cfg = load_exchange_config("kraken", symbol="PI_XBTUSD", environment="live")

    assert cfg.api_key == "live-key"
    assert cfg.base_url == "https://futures.kraken.com"
    assert cfg.adapter_kwargs["environment"] == "live"


def test_load_exchange_config_accepts_legacy_credential_env_names() -> None:
    cfg = load_exchange_config(
        "kraken",
        symbol="PI_XBTUSD",
        environment="demo",
        env={
            "KRAKEN_FUTURE_DEMO_API_KEY": "legacy-key",
            "KRAKEN_FUTURE_DEMO_API_SECRET": "legacy-secret",
        },
    )

    assert cfg.api_key == "legacy-key"
    assert cfg.api_secret == "legacy-secret"


def test_kraken_futures_default_postgres_lanes() -> None:
    demo = kraken_futures_environment("demo")
    live = kraken_futures_environment("live")

    assert demo.public_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    assert demo.private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    assert demo.critical_private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
    assert demo.api_key_env == "KRKF_DEMO_API_KEY"
    assert demo.api_secret_env == "KRKF_DEMO_API_SECRET"
    assert live.public_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    assert live.private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    assert live.critical_private_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
    assert live.api_key_env == "KRKF_API_KEY"
    assert live.api_secret_env == "KRKF_API_SECRET"


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
    assert demo.api_key_env == "BINF_DEMO_API_KEY"
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


def test_bitmex_futures_default_paths_are_exchange_scoped() -> None:
    demo = bitmex_futures_environment("demo")
    live = bitmex_futures_environment("live")

    assert demo.public_ws_url == "wss://testnet.bitmex.com/realtime"
    assert live.public_ws_url == "wss://www.bitmex.com/realtime"
    assert demo.api_key_env == "BTX_DEMO_API_KEY"
    assert live.api_key_env == "BTX_API_KEY"
    assert (
        bitmex_futures_public_db_url("demo", "XBTUSD")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )
    assert (
        bitmex_futures_private_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers"
    )
    assert (
        bitmex_futures_critical_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers"
    )
    assert (
        bitmex_futures_audit_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit_advers"
    )
    assert (
        bitmex_futures_telemetry_db_url("demo", "advers")
        == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_telemetry_advers"
    )
