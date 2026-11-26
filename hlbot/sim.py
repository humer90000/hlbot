from dataclasses import dataclass
from decimal import Decimal

from hlbot.px import D


@dataclass
class Fill:
    coin: str
    side: str
    sz: Decimal
    px: Decimal
    cloid: str


class SimBroker:
    def __init__(self, cash):
        self.cash = D(cash)
        self.pos = {}
        self.spot = {}
        self.seen = set()
        self.resting = {}

    def exposure(self, name, coin) -> Decimal:
        if coin.market == "spot":
            return self.spot.get(coin.base, Decimal(0))
        row = self.pos.get(name)
        if not row:
            return Decimal(0)
        return row[0]

    def equity(self, mids: dict) -> Decimal:
        upnl = Decimal(0)
        for name, (szi, entry) in self.pos.items():
            mid = mids.get(name, entry)
            if szi > 0:
                upnl += (mid - entry) * szi
            elif szi < 0:
                upnl += (entry - mid) * abs(szi)
        return self.cash + upnl

    def submit(self, plan, book, coin=None) -> Fill | None:
        if plan.cloid in self.seen:
            return None
        self.seen.add(plan.cloid)
        if plan.side == "buy":
            touch = D(book.ask)
            there = D(book.ask_sz)
            crosses = touch <= plan.limit_px
        else:
            touch = D(book.bid)
            there = D(book.bid_sz)
            crosses = touch >= plan.limit_px
        if not crosses:
            if plan.tif != "Ioc":
                self.resting[plan.cloid] = plan
            return None
        sz = plan.sz
        if plan.tif == "Ioc":
            sz = min(sz, there)
        if plan.reduce_only and plan.market != "spot":
            sz = min(sz, self._room(plan))
        if sz <= 0:
            return None
        sz = self._apply(plan, sz, touch, coin)
        if sz <= 0:
            return None
        return Fill(plan.coin, plan.side, sz, touch, plan.cloid)

    def _room(self, plan) -> Decimal:
        row = self.pos.get(plan.coin)
        if not row:
            return Decimal(0)
        szi = row[0]
        if plan.side == "sell" and szi > 0:
            return szi
        if plan.side == "buy" and szi < 0:
            return abs(szi)
        return Decimal(0)

    def _apply(self, plan, sz, px, coin) -> Decimal:
        if plan.market == "spot":
            return self._spot(plan, sz, px, coin)
        signed = sz if plan.side == "buy" else -sz
        cur = self.pos.get(plan.coin)
        if cur is None or cur[0] == 0:
            self.pos[plan.coin] = [signed, px]
            return sz
        szi, entry = cur
        same = (szi > 0 and signed > 0) or (szi < 0 and signed < 0)
        if same:
            new = szi + signed
            entry = (abs(szi) * entry + sz * px) / abs(new)
            self.pos[plan.coin] = [new, entry]
            return sz
        close_sz = min(abs(signed), abs(szi))
        if szi > 0:
            self.cash += (px - entry) * close_sz
        else:
            self.cash += (entry - px) * close_sz
        new = szi + signed
        if new == 0:
            self.pos.pop(plan.coin, None)
        elif (szi > 0 and new > 0) or (szi < 0 and new < 0):
            self.pos[plan.coin] = [new, entry]
        else:
            self.pos[plan.coin] = [new, px]
        return sz

    def _spot(self, plan, sz, px, coin) -> Decimal:
        if coin is None or not coin.base:
            return Decimal(0)
        base = coin.base
        if plan.side == "buy":
            cost = sz * px
            if cost > self.cash and px > 0:
                sz = self.cash / px
                cost = sz * px
            if sz <= 0:
                return Decimal(0)
            self.cash -= cost
            self.spot[base] = self.spot.get(base, Decimal(0)) + sz
            return sz
        have = self.spot.get(base, Decimal(0))
        sz = min(sz, have)
        if sz <= 0:
            return Decimal(0)
        self.spot[base] = have - sz
        self.cash += sz * px
        return sz
