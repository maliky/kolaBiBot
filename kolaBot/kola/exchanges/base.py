from __future__ import annotations

"""Abstract exchange interface used by the trading bot."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseExchange(ABC):
    """Common interface for all exchange adapters."""

    @abstractmethod
    def place_order(
        self,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        """Place an order on the exchange.

        Parameters
        ----------
        side:
            ``"buy"`` or ``"sell"``.
        quantity:
            Quantity to trade.
        price:
            Limit price for the order. ``None`` for market orders.
        **params:
            Additional exchange specific parameters.

        Returns
        -------
        dict
            Exchange specific response describing the created order.
        """

    @abstractmethod
    def amend_order(self, order_id: str, **params: Any) -> Dict[str, Any]:
        """Amend an existing order.

        Parameters
        ----------
        order_id:
            Identifier of the order to modify.
        **params:
            Fields to update.

        Returns
        -------
        dict
            Exchange specific response describing the amended order.
        """

    @abstractmethod
    def cancel_order(self, order_id: str | List[str] | Dict[str, Any]) -> Any:
        """Cancel an open order.

        Parameters
        ----------
        order_id:
            Identifier (or collection of identifiers) of the order(s) to cancel.

        Returns
        -------
        Any
            Exchange specific cancellation response.
        """

    @abstractmethod
    def margin(self, currency: str = "XBt") -> Dict[str, Any]:
        """Return margin information for an account.

        Parameters
        ----------
        currency:
            Currency code used by the exchange. Defaults to ``"XBt"``.

        Returns
        -------
        dict
            Current margin details.
        """

    @abstractmethod
    def instrument(self, symbol: str) -> Dict[str, Any]:
        """Return information about a trading instrument."""

    @abstractmethod
    def open_orders(self) -> List[Dict[str, Any]]:
        """Return the list of open orders for the account."""
