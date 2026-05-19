"""Runtime hardening regression tests for active legacy route.

Purpose: protect sacred amend and condition-route behaviour while migration
removes compatibility layers.
Inputs: fake bargain/crypto API stubs and runtime helper imports.
Outputs: assertions on error degradation and active shared runtime compatibility
bindings.
Side effects: none.
Important types: runtime amend reply mappings and shared runtime_compat
functions.
Role: test module (boundary behaviour guard).
"""
from __future__ import annotations

from kolabi.runtime.kola import bargain as runtime_bargain
from kolabi.runtime.kola.orders.orders import amend_prices
from kolabi.runtime.kola.utils.constantes import PRICELIST_DFT
from kolabi.shared.exchanges import runtime_compat


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


def test_runtime_bargain_uses_shared_exchange_runtime_compat() -> None:
    assert runtime_bargain.place_order is runtime_compat.place_order
    assert runtime_bargain.cancel_order is runtime_compat.cancel_order
    assert runtime_bargain.exch_balance is runtime_compat.get_balance
    assert runtime_bargain.exch_prices is runtime_compat.get_prices
