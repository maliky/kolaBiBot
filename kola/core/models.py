from dataclasses import dataclass
from typing import Optional

@dataclass
class OrderAck:
    """Simple acknowledgement for order operations."""
    order_id: str
    status: str
    price: Optional[float] = None
    orig_qty: Optional[float] = None
    executed_qty: Optional[float] = None
    side: Optional[str] = None

@dataclass
class Position:
    """Simplified position information."""
    symbol: str
    qty: float
    entry_price: Optional[float] = None
