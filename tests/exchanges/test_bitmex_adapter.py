from __future__ import annotations

from typing import Any, Dict, List

from kolabi.shared.exchanges.bitmex_adapter import BitmexAdapter


class _FakeBitmexClient:
    last_instance: "_FakeBitmexClient | None" = None

    def __init__(self, **kwargs: Any) -> None:
        _FakeBitmexClient.last_instance = self
        self.kwargs = kwargs

    def place(self, orderQty: float, side: str | None = None, **opts: Any) -> Dict[str, Any]:
        self.last_place = {"orderQty": orderQty, "side": side, **opts}
        return {
            "orderID": "abc",
            "ordStatus": "New",
            "price": opts.get("price"),
            "orderQty": orderQty,
            "cumQty": 0,
            "side": side,
        }

    def amend(self, order: Dict[str, Any], **params: Any) -> List[Dict[str, Any]]:
        self.last_amend = {"order": order, **params}
        return [
            {
                "orderID": order["orderID"],
                "ordStatus": "Replaced",
                "price": params.get("price"),
                "orderQty": params.get("orderQty"),
            }
        ]

    def cancel(self, order_id: str) -> List[Dict[str, Any]]:
        self.last_cancel = order_id
        return [{"orderID": order_id}]

    def position(self, symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol, "currentQty": 5, "avgEntryPrice": 10000}

    def margin(self) -> Dict[str, Any]:
        return {"availableMargin": 12345}


def test_bitmex_adapter_wraps_client() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        orderIDPrefix="abc",
    )

    ack = adapter.place_order("buy", 1, price=100, type_="limit")
    assert ack.order_id == "abc"
    assert _FakeBitmexClient.last_instance.last_place["ordType"] == "Limit"

    ack = adapter.amend_order("abc", price=101)
    assert ack.status == "Replaced"

    ack = adapter.cancel_order("abc")
    assert ack.status.lower() == "canceled"

    pos = adapter.get_position()
    assert pos.qty == 5
    assert adapter.get_balance() == 12345
