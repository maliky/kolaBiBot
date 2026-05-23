# -*- coding: utf-8 -*-
"""Runtime order payload and option utilities.

Purpose: normalize/toggle/validate legacy order mappings and construct runtime
order payloads consumed by interpreter shells.
Inputs: legacy order dicts, order-type keys, side/price/quantity parameters.
Outputs: normalized `OrderDict` payloads and helper scalar transformations.
Side effects: none beyond in-place dict normalization in documented helpers.
Important types: `OrderDict`, `Quantity`.
Role: pure logic plus utility boundary helpers.
"""
import re
from base64 import b64encode
from decimal import Decimal
from typing import Any, Optional, Sequence, cast
from uuid import uuid4

from kolabi.runtime.kola.settings import LOGNAME, ORDERID_PREFIX
from kolabi.runtime.kola.utils.pricefunc import get_prix_decl, setdef_stopPrice
from kolabi.runtime.kola.utils.general import contains, opt_add_to_
from kolabi.runtime.kola.utils.logfunc import get_logger
from kolabi.shared.core.runtime_types import (
    OrderDict,
    OrderQty,
    Price,
    Quantity,
    StopPrice,
    decimal_to_float,
    to_decimal,
)

mlogger = get_logger(name=f"{LOGNAME}.{__name__}")

# import logging
# mlogger = logging.getLogger('')
# mlogger.setLevel('INFO')


def build_order_payload(
    *,
    side: str,
    quantity: Quantity,
    op_type: str,
    ord_type: str,
    exec_inst: str,
    prices: tuple[float, float] | None,
    absdelta: float,
    text: str | None,
) -> OrderDict:
    if prices is None and ord_type != "Market":
        raise ValueError(f"prices are required for ord_type={ord_type}")
    order: OrderDict = {
        "side": side,
        "orderQty": cast(OrderQty, to_decimal(int(quantity))),
        "ordType": ord_type,
        "execInst": exec_inst,
        "text": text,
    }
    if ord_type == "Limit":
        order["price"] = cast(Price, Decimal(str(get_prix_decl(cast(tuple[float, float], prices), cast(Any, side), cast(Any, ord_type)))))
    elif ord_type in ["Stop", "MarketIfTouched"]:
        order["stopPx"] = cast(StopPrice, Decimal(str(get_prix_decl(cast(tuple[float, float], prices), cast(Any, side), cast(Any, ord_type)))))
    elif ord_type in ["StopLimit", "LimitIfTouched"]:
        price = Decimal(str(get_prix_decl(cast(tuple[float, float], prices), cast(Any, side), cast(Any, ord_type))))
        order["price"] = cast(Price, price)
        order["stopPx"] = cast(
            StopPrice,
            Decimal(str(setdef_stopPrice(decimal_to_float(price), cast(Any, side), cast(Any, ord_type), absdelta))),
        )
    _ = op_type
    return order


def toggle_order(order: OrderDict) -> OrderDict:
    """
    prendre un order (un dict) qui a une action et renvois ce dict avec l'action toggled
    """
    toggle_order = order.copy()
    # will pop action toggle it and put it back in dict
    if "side" in toggle_order.keys():
        toggle_order["side"] = toggle_sides(toggle_order["side"])
    else:
        toggle_order["action"] = toggle_sides(toggle_order["action"])

    return toggle_order


def toggle_sides(chaine: str) -> str:
    """
    renvois la chaine ou les buy et sell ont été échangés
    """
    if "buy" in chaine:
        return chaine.replace("buy", "sell")
    elif "sell" in chaine:
        return chaine.replace("sell", "buy")
    else:
        raise Exception("Il n'y a rien à toggeler dans %s" % chaine)


def is_valid_stop(side: str, price: float, stopPx: float) -> bool:
    """
    On sell orders, the order will trigger if the triggering price is lower
    than the stopPx. On buys, higher.

    Params:
    - side,
    - price: price for the new limit order that will be place,
    - stopPx: stop price that will trigger the limit order.
    """
    if side == "buy" and price > stopPx:
        raise Exception("will buy at price higer than max of market")
    elif side == "sell" and price < stopPx:
        raise Exception("will sell at price lower than min of market")
    else:
        return True


def newClID(prefix: str = ORDERID_PREFIX, abbv_: str = "") -> str:
    """
    Génère un nouvel identifiant avec un prefix 'mlk_' par défaut.

    Ajout l'abbreviation to facilitate hook.
    les _ sont dans les prefix et abbv_ (eg Bl1-P).
    """
    return prefix + abbv_ + b64encode(uuid4().bytes).decode("utf8").rstrip("=\n")


def get_abbv_from_ID(oClOrdID_: str):
    """Identify dans oClOrdID_ ce qui ressemble à une abbrevation de hook.

    le préfix a été étendu avec nomT-PO ou nomT-SO
    """
    return oClOrdID_.split(ORDERID_PREFIX)[-1].split("-O")[0]


def normalize_order_dict(order: OrderDict) -> OrderDict:
    """Normalize order keys between BitMEX and Binance styles.

    Converts ``quantity`` to ``orderQty`` and ``stopPrice`` to ``stopPx``
    when the BitMEX style keys are missing.  The original dictionary is
    modified and returned for convenience.
    """

    if "orderQty" not in order and "quantity" in order:
        order["orderQty"] = order.pop("quantity")
    if "stopPx" not in order and "stopPrice" in order:
        order["stopPx"] = order.pop("stopPrice")

    return order


# @log_args()
def create_order(
    side: str,
    _q: Quantity,
    opType: str,
    ordtype: str,
    execinst: str,
    prices: tuple[float, float] | None = None,
    absdelta: float = 0.5,
    text: str | None = None,
    min_qty: Quantity = 30,
) -> OrderDict:
    """
    Crée un 'side' ordre de type ordtype et de volume '_q'.
    Ajoute les options 'execinst'.
    Si ordtype is stopLimit ou LimitIfTouched, absdelta détermine l'écart entre
    le prix d'entrée sur le marché et le stopPrice.
    """
    _q = int(_q)
    if _q < min_qty:
        raise Exception(f"qty is too small _q={_q}; min_qty={min_qty}")

    if prices is None and ordtype != "Market":
        msg = (
            f"if ordtype {ordtype} need prices to immediatly place limit or stop."
            f" but prices={prices}."
        )
        raise Exception(msg)

    order = build_order_payload(
        side=side,
        quantity=_q,
        op_type=opType,
        ord_type=ordtype,
        exec_inst="",
        prices=prices,
        absdelta=absdelta,
        text=text,
    )

    # on traduit le nom lastMidPrice en un nom de prix reconnu par Bitmex.
    # lastMidPrice nous sert pour définir correctement le stop price (il me semble)
    opType = "lastPrice" if opType == "lastMidPrice" else opType
    order["execInst"] = opt_add_to_(
        opType, execinst
    )  # ': 'ReduceOnly',  #'ParticipateDoNotInitiate',
    order["text"] = text
    return order


def get_order_from(
    rcvLoad: object,
) -> OrderDict | dict[str, Any] | list[object] | bool:
    """
    Find an order in the received load and return it,
    if the load is empty juste return it like that
    """

    if not rcvLoad:
        # we handle the case of empty loads
        return cast(OrderDict | dict[str, Any] | list[object] | bool, rcvLoad)

    # cas de [{'order': ...}]
    ret: OrderDict | dict[str, Any] | bool = False
    if isinstance(rcvLoad, list):
        if len(rcvLoad) == 1:
            ret = rcvLoad[0]
        elif len(rcvLoad) > 1:
            mlogger.warning("On retourne le 1er elt de {recvLoad}.")
            ret = rcvLoad[0]

    elif isinstance(rcvLoad, dict):
        ret = (
            rcvLoad.get("order", False) or rcvLoad.get("orders", [False])[0] or rcvLoad
        )

    if not ret:
        raise Exception(f'Problème dans le format du rcvLoad "{rcvLoad}" so ret={ret}')

    return cast(OrderDict | dict[str, Any] | list[object] | bool, ret)


# @log_args(logopt=__name__)
def set_order_type(ordkey, _extype):
    """
    Given the ordkey L, M, S, T, SL or TL, renvois le type d'ordre OrdType
    to be used par passer les ordres
    """
    # order type translation
    OT = {
        "L": "Limit",
        "M": "Market",
        "S": "Stop",
        "MT": "MarketIfTouched",
        "SL": "StopLimit",
        "LT": "LimitIfTouched",
    }

    ordType = OT.get(ordkey, None)

    if ordType is None:
        raise Exception(f"ordkey={ordkey}, _extype={_extype}")

    return ordType


def set_exec_instructions(
    extrakey: str | None,
    execinst: str,
    ordtype: str,
    pricetype: str,
) -> str:
    """
    Renvois execInst correctement formaté et valide avec les ordtype

    """

    execInst = ""
    if ordtype and contains(["Stop", "Touched"], ordtype):
        # pour Stop, StopLimit, MarketIfTouched, LimiteIfTouched. ordre avec stopPx
        _priceType = re.sub(r"(ask|bid)", "Last", pricetype)
        execInst = opt_add_to_(execInst, _priceType)

    if extrakey is None:
        return execInst

    if ("!" in extrakey) and ("Market" in ordtype):
        raise Exception(f"ExcInst {extrakey, ordtype} incompatibles")

    execInst = execinst
    if "!" in extrakey:
        execInst = opt_add_to_(execInst, "ParticipateDoNotInitiate")

    if "-" in extrakey:
        execInst = opt_add_to_(execInst, "ReduceOnly")

    return execInst


def set_price_type(pricekey: str | None, side: str | None) -> str:
    """
    avec la pricekey i, l ou m renvois le type de prix pour le suivi du déclenchement de la peg.
    Attention dans condition,  j'utilise ask et bid mais pour exec if faut lastPrice
    qui ont des stopPx.
    priceType can be None if exist but not in PT
    """
    if pricekey is None:
        # defaultPriceType = 'bidPrice' if side is None or side == 'buy' else 'askPrice'
        defaultPriceType: Optional[str] = "lastMidPrice"
    else:
        defaultPriceType = None

    PT = {
        "f": "fairPrice",
        "i": "indexPrice",
        "l": "lastMidPrice",
        "m": "markPrice",
    }
    priceType = defaultPriceType if pricekey is None else PT.get(pricekey)

    if priceType is None:
        raise Exception(f"priceType={priceType} is not in PT={PT}")

    return priceType


def is_valid_order_options(
    ordtype: str,
    pricetype: str,
    execinst: str = "",
) -> bool:
    """
    Check the validity of the options.

    Si le type est market ou limit, il faut que le prix soit le prix du marché.
    car l'ordre va être passé immédiatement.
    """
    if ordtype in ["Market", "Limit"] and pricetype not in [
        "lastPrice",
        "bidPrice",
        "askPrice",
        "lastMidPrice",
    ]:
        return False
        #        raise Exception(f"ordtype={ordtype} and pricetype={pricetype} incompatible")
    return True


def remove_execInst(cslist: str, val: str) -> str:
    """Remove from the list of comma separated option the val."""
    _cslist = cslist.split(",")
    if val in _cslist:
        _cslist.remove(val)
        return ",".join(_cslist)
    return cslist


def split_ids(idlist: Sequence[str]) -> dict[str, list[str]]:
    """
    Given a list of ids, returns two list one with oid and the other with clorid based on ORDERID_PREFIX
    """
    clIDList = []
    oIDList = []
    for anID in idlist:
        if anID.startswith(ORDERID_PREFIX):
            clIDList.append(anID)
        else:
            oIDList.append(anID)

    return {"clIDList": clIDList, "oIDList": oIDList}
