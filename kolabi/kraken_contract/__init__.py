"""Kraken exchange contract helpers.

`kraken_contract` is a better name than `kraken_openapi` here because this
package does not consume an official OpenAPI artifact. It captures the wire
contract we have verified from Kraken docs and live errors: enums, parameter
ordering, and small request serializers.
"""

from .futures import (
    KrakenFuturesOrderType,
    KrakenFuturesStandardOrder,
    KrakenTrailingDeviationUnit,
    SendOrderContract,
    build_send_order_contract,
    normalize_standard_order,
    normalize_trailing_deviation_unit,
)

__all__ = [
    "KrakenFuturesOrderType",
    "KrakenFuturesStandardOrder",
    "KrakenTrailingDeviationUnit",
    "SendOrderContract",
    "build_send_order_contract",
    "normalize_standard_order",
    "normalize_trailing_deviation_unit",
]
