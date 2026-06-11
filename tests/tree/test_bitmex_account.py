from __future__ import annotations

from kolabi.shared.persistence import AccountBalance, ExchangeOrder
from kolabi.tree.bitmex_account import (
    BitmexAccountConfig,
    BitmexRestReconciler,
    account_config,
    bitmex_market_has_positions,
    build_parser,
    config_from_args,
    map_bitmex_balances,
    map_bitmex_order,
    map_bitmex_position,
)
from kolabi.tree.account import (
    AccountStateStore,
    KrakenFuturesCredentials,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


class _DummyResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _DummySession:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> _DummyResponse:
        self.calls.append(dict(kwargs))
        return _DummyResponse(self.payloads.pop(0))


def test_map_bitmex_private_payloads() -> None:
    order = map_bitmex_order(
        {
            "symbol": "XBTUSD",
            "side": "Sell",
            "ordType": "Stop",
            "ordStatus": "New",
            "orderQty": 2,
            "cumQty": 0.5,
            "orderID": "OID",
            "clOrdID": "T1bitmex-260610000000",
            "stopPx": 90,
            "execInst": "ReduceOnly",
        }
    )
    position = map_bitmex_position(
        {
            "symbol": "XBTUSD",
            "currentQty": -3,
            "avgEntryPrice": 100,
            "leverage": 2,
        }
    )
    balances = map_bitmex_balances(
        {"currency": "XBt", "marginBalance": 1000, "availableMargin": 750}
    )

    assert order.symbol == "XBTUSD"
    assert order.status == "open"
    assert order.client_order_id == "T1bitmex-260610000000"
    assert order.price == 90.0
    assert order.reduce_only is True
    assert position.side == "short"
    assert position.size == -3.0
    assert balances[0].asset == "XBT"
    assert balances[0].locked == 250.0


def test_bitmex_reconciler_calls_private_rest_and_writes_state(postgres_url_factory) -> None:
    account_db = postgres_url_factory("bitmex-account")
    critical_db = postgres_url_factory("bitmex-critical")
    config = BitmexAccountConfig(
        db_url=account_db,
        critical_db_url=critical_db,
        rest_url="https://bitmex.test/api/v1",
        symbol="XBTUSD",
    )
    account_store = AccountStateStore(account_config(config))
    critical_store = AccountStateStore(account_config(config, critical=True))
    session = _DummySession(
        [
            [
                {
                    "symbol": "XBTUSD",
                    "side": "Buy",
                    "ordType": "Limit",
                    "ordStatus": "New",
                    "orderQty": 2,
                    "cumQty": 0,
                    "orderID": "OID-BMX",
                    "clOrdID": "H1bitmex-260610000000",
                    "price": 100,
                }
            ],
            [{"symbol": "XBTUSD", "currentQty": 2, "avgEntryPrice": 100}],
            {"currency": "XBt", "marginBalance": 1000, "availableMargin": 750},
        ]
    )
    reconciler = BitmexRestReconciler(
        config,
        account_store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        critical_store=critical_store,
        session=session,  # type: ignore[arg-type]
    )

    stats = reconciler.reconcile_once()

    assert stats == {"orders": 1, "positions": 1, "balances": 1}
    assert session.calls[0]["url"] == "https://bitmex.test/api/v1/order"
    assert session.calls[1]["url"] == "https://bitmex.test/api/v1/position"
    assert session.calls[2]["url"] == "https://bitmex.test/api/v1/user/margin"
    with Session(account_store.engine) as db_session:
        order = db_session.execute(select(ExchangeOrder)).scalar_one()
        balance = db_session.execute(select(AccountBalance)).scalar_one()
    assert order.exchange_order_id == "OID-BMX"
    assert order.client_order_id == "H1bitmex-260610000000"
    assert balance.asset == "XBT"
    assert critical_store.latest_status("private_ws")["status"] == "healthy"


def test_bitmex_spot_reconciler_skips_futures_position_endpoint(
    postgres_url_factory,
) -> None:
    account_db = postgres_url_factory("bitmex-spot-account")
    critical_db = postgres_url_factory("bitmex-spot-critical")
    config = BitmexAccountConfig(
        db_url=account_db,
        critical_db_url=critical_db,
        rest_url="https://bitmex.test/api/v1",
        symbol="BMEXUSDT",
        market_type="spot",
    )
    account_store = AccountStateStore(account_config(config))
    critical_store = AccountStateStore(account_config(config, critical=True))
    session = _DummySession(
        [
            [
                {
                    "symbol": "BMEXUSDT",
                    "side": "Buy",
                    "ordType": "Limit",
                    "ordStatus": "New",
                    "orderQty": 25,
                    "cumQty": 0,
                    "orderID": "OID-SPOT",
                    "clOrdID": "H1bitmexspot-260610000000",
                    "price": 0.1,
                }
            ],
            {"currency": "USDT", "walletBalance": 1000, "availableMargin": 900},
        ]
    )
    reconciler = BitmexRestReconciler(
        config,
        account_store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        critical_store=critical_store,
        session=session,  # type: ignore[arg-type]
    )

    stats = reconciler.reconcile_once()

    assert stats == {"orders": 1, "positions": 0, "balances": 1}
    assert bitmex_market_has_positions("futures") is True
    assert bitmex_market_has_positions("spot") is False
    assert [call["url"] for call in session.calls] == [
        "https://bitmex.test/api/v1/order",
        "https://bitmex.test/api/v1/user/margin",
    ]
    with Session(account_store.engine) as db_session:
        order = db_session.execute(select(ExchangeOrder)).scalar_one()
        balance = db_session.execute(select(AccountBalance)).scalar_one()
    assert order.exchange_order_id == "OID-SPOT"
    assert order.market_type == "spot"
    assert balance.asset == "USDT"
    assert critical_store.latest_status("private_ws")["status"] == "healthy"


def test_bitmex_account_cli_defaults_are_account_scoped(monkeypatch) -> None:
    for name in (
        "BTX_DEMO_API_KEY",
        "BTX_DEMO_API_SECRET",
        "BITMEX_TEST_KEY",
        "BITMEX_TEST_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--environment",
            "demo",
            "--market-type",
            "spot",
            "--symbol",
            "XBT_USDT",
            "--account-scope",
            "advers",
        ]
    )

    config = config_from_args(args)

    assert config.db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers"
    )
    assert config.critical_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers"
    )
    assert config.market_type == "spot"
    assert config.symbol == "XBT_USDT"
    assert config.api_key_env == "BTX_DEMO_API_KEY"
    assert config.api_secret_env == "BTX_DEMO_API_SECRET"


def test_bitmex_account_cli_defaults_accept_legacy_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BTX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BTX_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BITMEX_TEST_KEY", "legacy-key")
    monkeypatch.setenv("BITMEX_TEST_SECRET", "legacy-secret")
    parser = build_parser()
    args = parser.parse_args(["run", "--environment", "demo"])

    config = config_from_args(args)

    assert config.api_key_env == "BITMEX_TEST_KEY"
    assert config.api_secret_env == "BITMEX_TEST_SECRET"
