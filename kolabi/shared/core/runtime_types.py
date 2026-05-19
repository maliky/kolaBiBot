from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Iterable, NewType, Protocol, TypedDict

Symbol = NewType("Symbol", str)
ClientOrderId = NewType("ClientOrderId", str)
ExchangeOrderId = NewType("ExchangeOrderId", str)
Price = NewType("Price", float)
Quantity = float | int


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderRole(StrEnum):
    PRIMARY = "primary"
    TAIL = "tail"
    HOOK = "hook"
    AMEND = "amend"
    CANCEL = "cancel"


class OrderStatus(StrEnum):
    NEW = "New"
    FILLED = "Filled"
    CANCELED = "Canceled"
    TRIGGERED = "Triggered"
    REPLACED = "Replaced"
    PARTIALLY_FILLED = "PartiallyFilled"


class ExecType(StrEnum):
    NEW = "New"
    TRADE = "Trade"
    CANCELED = "Canceled"
    REPLACED = "Replaced"
    TRIGGERED = "TriggeredOrActivatedBySystem"


class RuntimeEventKind(StrEnum):
    ORDER_REQUESTED = "order_requested"
    ORDER_ACK = "order_ack"
    ORDER_VALIDATED = "order_validated"
    PRICE_TICK = "price_tick"
    TIMER = "timer"
    ERROR = "error"


class RuntimeCommandKind(StrEnum):
    PLACE = "place"
    AMEND = "amend"
    CANCEL = "cancel"
    VALIDATE = "validate"
    NOOP = "noop"


class OrderDict(TypedDict, total=False):
    side: str
    action: str
    orderQty: Quantity
    quantity: Quantity
    price: float
    stopPx: float
    stopPrice: float
    ordType: str
    execInst: str
    clOrdID: str
    orderID: str
    newPrice: float
    text: str | None
    oDelta: float
    cumQty: float
    executedQty: float
    filledQty: float


class OrderLoad(TypedDict):
    sender: object
    timeOut: object
    symbol: str
    order: OrderDict


class BrokerReply(TypedDict, total=False):
    orderID: str
    clOrdID: str
    ordStatus: str
    execType: str
    side: str
    error: object
    transactTime: str
    price: float
    stopPx: float
    orderQty: Quantity
    cumQty: Quantity
    executedQty: Quantity
    filledQty: Quantity


class ValidationCondition(TypedDict):
    exectype: str
    orderstatus: str


class ValidationLoad(TypedDict):
    brokerReply: BrokerReply | bool | None
    exgLoad: OrderLoad
    execValidation: BrokerReply | bool


class CryptoApiLike(Protocol):
    dummy: bool
    dummyID: str

    def exec_orders(self) -> Iterable[BrokerReply]: ...


class BargainLike(Protocol):
    """Contrat minimal attendu par le runtime de trading.

    Cette abstraction sert de surface commune pour les objets "bargain" ou
    "broker context" utilises par le runtime legacy. Les classes qui
    l'implementent doivent fournir les acces utilises pour:
    - lire les prix de marche et les contraintes de taille minimale,
    - lire l'etat de compte et d'execution,
    - retrouver les ordres lies a un hook ou a une validation,
    - verifier si un ordre a atteint un statut attendu.

    Ce n'est pas une implementation concrete. C'est un contrat de typage
    structurel: tout objet qui expose les attributs et methodes attendus est
    accepte par le type-checker, meme s'il n'herite pas explicitement de cette
    classe.
    """

    symbol: str
    crypto_api: CryptoApiLike

    def prices(self, price_type: str | None = None, side: str | None = None) -> float: ...
    def get_balance(self, ref: str | None = None) -> float: ...
    def minimum_order_quantity(self, symbol: str | None = None) -> float: ...
    def execution(self, *args: object, **kwargs: object) -> object: ...
    def get_exec_clID_with_(self, *args: object, **kwargs: object) -> Iterable[str]: ...
    def get_open_orders(self, *args: object, **kwargs: object) -> object: ...
    def get_position(self, *args: object, **kwargs: object) -> object: ...
    def order_reached_status(self, *args: object, **kwargs: object) -> bool: ...


@dataclass(frozen=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    at: datetime
    symbol: Symbol
    order: OrderDict | None = None
    reply: BrokerReply | None = None
    note: str | None = None


@dataclass(frozen=True)
class RuntimeCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    order: OrderDict | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RuntimeState:
    symbol: Symbol
    active_order_id: ClientOrderId | None = None
    active_exchange_order_id: ExchangeOrderId | None = None
    status: OrderStatus | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def step_runtime(
    state: RuntimeState,
    event: RuntimeEvent,
) -> tuple[RuntimeState, list[RuntimeCommand]]:
    _ = event
    return state, []
