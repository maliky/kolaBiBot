from __future__ import annotations

from kolabi.runtime.legacy.kola.orders.orders import amend_prices
from kolabi.runtime.legacy.kola.utils.constantes import PRICELIST_DFT


class _FakeCryptoApi:
    def amend(self, order, **kwargs):
        raise RuntimeError("Kraken HTTP 502 on /editorder: {'raw_text': 'Bad gateway'}")


class _FakeBargain:
    symbol = "PI_XBTUSD"
    crypto_api = _FakeCryptoApi()


def test_index_price_is_allowed_in_condition_price_list() -> None:
    assert "indexPrice" in PRICELIST_DFT


def test_amend_prices_degrades_transient_kraken_gateway_error() -> None:
    reply = amend_prices(
        _FakeBargain(),
        "OID-1",
        79157.5,
        "amendStop",
        side="buy",
    )

    assert reply is not None
    assert reply["orderID"] == "OID-1"
    assert reply["error"] == "Transient amend gateway failure"
