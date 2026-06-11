from kolabi.tree.binance_account import (
    BinanceAccountConfig,
    BinancePrivateStream,
    binance_account_defaults,
    normalise_binance_private_event,
    normalise_balance_rows,
    normalise_order_status,
)


def test_binance_account_defaults_use_route_code_credentials(monkeypatch) -> None:
    for name in (
        "BINF_DEMO_API_KEY",
        "BINF_DEMO_API_SECRET",
        "BINS_DEMO_API_KEY",
        "BINS_DEMO_API_SECRET",
        "BINM_DEMO_API_KEY",
        "BINM_DEMO_API_SECRET",
        "BINI_DEMO_API_KEY",
        "BINI_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
        "BINANCE_MARGIN_DEMO_API_KEY",
        "BINANCE_MARGIN_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    assert binance_account_defaults("demo", "futures")["api_key_env"] == "BINF_DEMO_API_KEY"
    assert binance_account_defaults("demo", "spot")["api_key_env"] == "BINS_DEMO_API_KEY"
    assert binance_account_defaults("demo", "margin")["api_key_env"] == "BINM_DEMO_API_KEY"
    assert (
        binance_account_defaults("demo", "isolated_margin")["api_key_env"]
        == "BINI_DEMO_API_KEY"
    )


def test_binance_account_defaults_accept_legacy_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BINS_DEMO_API_KEY", raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "legacy-key")

    defaults = binance_account_defaults("demo", "spot")

    assert defaults["api_key_env"] == "BINANCE_SPOT_DEMO_API_KEY"


def test_order_trade_update_emits_order_and_fill_messages() -> None:
    messages = normalise_binance_private_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "s": "BTCUSDT",
                "c": "T1unit-260601000000",
                "S": "SELL",
                "o": "STOP_MARKET",
                "q": "2",
                "sp": "100.5",
                "x": "TRADE",
                "X": "FILLED",
                "i": 42,
                "l": "2",
                "z": "2",
                "L": "100.4",
                "t": 99,
                "T": 1_717_000_000_000,
                "R": True,
            },
        }
    )

    assert len(messages) == 2
    order_message, order_critical = messages[0]
    fill_message, fill_critical = messages[1]
    assert order_critical is True
    assert fill_critical is True
    assert order_message["feed"] == "open_orders"
    assert order_message["order"]["orderId"] == 42
    assert order_message["order"]["status"] == "filled"
    assert order_message["order"]["price"] == "100.5"
    assert fill_message["feed"] == "fills"
    assert fill_message["fill"]["price"] == "100.4"


def test_spot_execution_report_emits_order_and_fill_messages() -> None:
    messages = normalise_binance_private_event(
        {
            "e": "executionReport",
            "s": "BTCUSDT",
            "c": "T1spot-260601000000",
            "S": "SELL",
            "o": "STOP_LOSS",
            "q": "2",
            "P": "100.5",
            "x": "TRADE",
            "X": "FILLED",
            "i": 43,
            "l": "2",
            "z": "2",
            "L": "100.4",
            "t": 100,
            "T": 1_717_000_000_000,
        }
    )

    assert len(messages) == 2
    order_message, order_critical = messages[0]
    fill_message, fill_critical = messages[1]
    assert order_critical is True
    assert fill_critical is True
    assert order_message["order"]["orderId"] == 43
    assert order_message["order"]["price"] == "100.5"
    assert order_message["order"]["stop_price"] == "100.5"
    assert fill_message["fill"]["price"] == "100.4"


def test_account_update_uses_generic_balance_and_position_shapes() -> None:
    messages = normalise_binance_private_event(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "B": [{"a": "USDT", "wb": "1000", "cw": "900"}],
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0.5",
                        "ep": "100",
                        "cr": "0",
                        "up": "1",
                    }
                ],
            },
        }
    )

    balances, balance_critical = messages[0]
    positions, position_critical = messages[1]
    assert balance_critical is False
    assert position_critical is False
    assert balances["flex_futures"]["currencies"]["USDT"]["available_balance"] == "900"
    assert positions["positions"][0]["symbol"] == "BTCUSDT"
    assert positions["positions"][0]["size"] == 0.5


def test_spot_outbound_account_position_tracks_locked_balance() -> None:
    messages = normalise_binance_private_event(
        {
            "e": "outboundAccountPosition",
            "B": [
                {"a": "BTC", "f": "1.5", "l": "0.25"},
                {"a": "USDT", "f": "20", "l": "0"},
            ],
        }
    )

    balances, balance_critical = messages[0]

    assert balance_critical is False
    assert balances["flex_futures"]["currencies"]["BTC"] == {
        "available_balance": "1.5",
        "balance_value": "1.75",
    }
    assert balances["flex_futures"]["currencies"]["USDT"] == {
        "available_balance": "20",
        "balance_value": "20",
    }


def test_rest_balance_rows_normalise_spot_and_margin_assets() -> None:
    rows = normalise_balance_rows(
        {
            "balances": [{"asset": "BTC", "free": "1.5", "locked": "0.25"}],
        }
    )
    margin_rows = normalise_balance_rows(
        {
            "userAssets": [
                {
                    "asset": "USDT",
                    "free": "100",
                    "locked": "5",
                    "borrowed": "20",
                    "interest": "1",
                    "netAsset": "84",
                }
            ]
        }
    )
    isolated_rows = normalise_balance_rows(
        {
            "assets": [
                {
                    "baseAsset": {"asset": "BTC", "free": "0.1", "locked": "0.02"},
                    "quoteAsset": {"asset": "USDT", "free": "50", "locked": "1.5"},
                }
            ]
        }
    )

    assert rows[0]["asset"] == "BTC"
    assert rows[0]["availableBalance"] == "1.5"
    assert rows[0]["balance"] == "1.75"
    assert margin_rows[0]["asset"] == "USDT"
    assert margin_rows[0]["availableBalance"] == "100"
    assert margin_rows[0]["balance"] == "105"
    assert [row["balance"] for row in isolated_rows] == ["0.12", "51.5"]


def test_normalise_order_status() -> None:
    assert normalise_order_status("NEW") == "open"
    assert normalise_order_status("PARTIALLY_FILLED") == "partial_fill"
    assert normalise_order_status("FILLED") == "filled"
    assert normalise_order_status("CANCELED") == "canceled"


class _FakeStore:
    def __init__(self) -> None:
        self.messages = []
        self.statuses = []

    def ingest_message(self, message, **kwargs) -> None:
        self.messages.append((message, kwargs))

    def record_connection_status(self, *args, **kwargs) -> None:
        self.statuses.append((args, kwargs))


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.headers = {}
        self.calls = []

    def request(self, method, url, *, data=None, params=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "data": data,
                "params": params,
                "timeout": timeout,
            }
        )
        if url.endswith("/sapi/v1/margin/openOrders"):
            return _FakeResponse([])
        if url.endswith("/sapi/v1/margin/isolated/account"):
            return _FakeResponse(
                {
                    "assets": [
                        {
                            "baseAsset": {"asset": "BTC", "free": "0", "locked": "0"},
                            "quoteAsset": {
                                "asset": "USDT",
                                "free": "100",
                                "locked": "0",
                            },
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected url {url}")


def test_isolated_margin_reconcile_scopes_rest_calls_to_symbol() -> None:
    account_store = _FakeStore()
    critical_store = _FakeStore()
    stream = BinancePrivateStream(
        BinanceAccountConfig(
            market_type="isolated_margin",
            symbol="BTCUSDT",
            rest_url="https://binance.test",
        ),
        account_store,
        critical_store,
        api_key="key",
        api_secret="secret",
    )
    session = _FakeSession()
    stream.session = session

    stats = stream.reconcile_once()

    assert stats == {"orders": 0, "positions": 0, "balances": 1}
    assert [call["url"] for call in session.calls] == [
        "https://binance.test/sapi/v1/margin/openOrders",
        "https://binance.test/sapi/v1/margin/isolated/account",
    ]
    assert session.calls[0]["params"]["symbol"] == "BTCUSDT"
    assert session.calls[1]["params"]["symbols"] == "BTCUSDT"
    assert all("signature" in call["params"] for call in session.calls)
    assert critical_store.messages[0][0]["feed"] == "open_orders_snapshot"
    assert account_store.messages[0][0]["feed"] == "open_positions_snapshot"
    assert account_store.messages[1][0]["feed"] == "balances_snapshot"
    assert set(account_store.messages[1][0]["flex_futures"]["currencies"]) == {
        "BTC",
        "USDT",
    }
