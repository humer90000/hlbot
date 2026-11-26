from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Coin.asset is the wire id: perp index, 10000+spot pair, 110000+hip-3


@dataclass(frozen=True)
class Coin:
    name: str
    market: str
    asset: int
    sz_decimals: int
    max_leverage: int
    label: str
    api: str = ""
    base: str = ""
    quote: str = ""
    delisted: bool = False
    isolated_only: bool = False
    hip3: bool = False
    dex: str = ""


@dataclass(frozen=True)
class Intent:
    strategy: str
    coin: str
    side: str
    purpose: str
    bar_ts: int
    risk_frac: float | None = None
    qty: float | None = None
    stop_dist: float | None = None
    take_r: float | None = None
    note: str = ""


@dataclass(frozen=True)
class OrderPlan:
    coin: str
    asset: int
    market: str
    side: str
    sz: Decimal
    limit_px: Decimal
    tif: str
    reduce_only: bool
    cloid: str
    stop_px: Decimal | None
    take_px: Decimal | None
    purpose: str
    strategy: str
    hl_coin: str
    sz_decimals: int
    clipped: bool = False


@dataclass(frozen=True)
class Reject:
    coin: str
    reason: str


@dataclass
class Book:
    bid: float
    ask: float
    bid_sz: float
    ask_sz: float
    mark: float
    funding: float
    day_ntl: float
    book_ts: int
    bar_ts: int
    highs: tuple[float, ...] = ()
    lows: tuple[float, ...] = ()
    closes: tuple[float, ...] = ()

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return 1e9
        return (self.ask - self.bid) / m * 10_000


@dataclass
class Account:
    equity: Decimal
    day_start: Decimal
    synced: bool
    halted: bool
    armed: bool
    positions: dict[str, Decimal]
    spot: dict[str, Decimal]
    usdc: Decimal
    sent: set[str] = field(default_factory=set)
    liq: dict[str, Decimal] = field(default_factory=dict)
