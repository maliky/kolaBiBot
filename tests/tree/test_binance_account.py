from kolabi.tree.binance_account import (
    normalise_binance_private_event,
    normalise_order_status,
)


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


def test_normalise_order_status() -> None:
    assert normalise_order_status("NEW") == "open"
    assert normalise_order_status("PARTIALLY_FILLED") == "partial_fill"
    assert normalise_order_status("FILLED") == "filled"
    assert normalise_order_status("CANCELED") == "canceled"
