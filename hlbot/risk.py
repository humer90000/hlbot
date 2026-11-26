from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hlbot.models import Account, Book, Coin, Intent, OrderPlan, Reject
from hlbot.px import D, floor_sz, round_px
from hlbot.wire import make_cloid

MIN_NOTIONAL = Decimal(10)


@dataclass
class Gate:
    limits: object
    enabled: set
    forced: set


def _loss(account: Account, limits) -> bool:
    if account.day_start <= 0:
        return True
    drop = (account.day_start - account.equity) / account.day_start
    return drop >= D(limits.max_daily_loss_frac)


def _liquid(book: Book, limits) -> bool:
    return book.spread_bps <= limits.max_spread_bps and book.day_ntl >= limits.min_day_volume_usd


def _liq_room(use_lev: Decimal, max_lev: Decimal) -> Decimal:
    mmr = Decimal(1) / (Decimal(2) * max_lev)
    return (Decimal(1) / use_lev) - mmr


def decide(intent, book, coin, account, gate, now_ms, mids) -> OrderPlan | Reject:
    name = intent.coin
    if coin is None or book is None:
        return Reject(name, "unknown")
    if coin.delisted:
        return Reject(name, "delisted")
    if book.bid <= 0 or book.ask <= 0 or book.ask < book.bid:
        return Reject(name, "bad book")
    if intent.purpose not in ("open", "close"):
        return Reject(name, "purpose")
    if intent.side not in ("buy", "sell"):
        return Reject(name, "side")

    limits = gate.limits
    mid = (D(book.bid) + D(book.ask)) / 2
    pos = account.positions.get(name, Decimal(0))
    if coin.market == "spot":
        pos = account.spot.get(coin.base, Decimal(0))

    if intent.purpose == "open":
        blocked = _block_open(intent, book, coin, account, gate, now_ms, mids, pos)
        if blocked:
            return Reject(name, blocked)
    else:
        if pos == 0:
            return Reject(name, "flat")
        if pos > 0 and intent.side != "sell":
            return Reject(name, "side")
        if pos < 0 and intent.side != "buy":
            return Reject(name, "side")

    thin = not _liquid(book, limits)
    spot = coin.market == "spot"
    try:
        px = _limit_px(mid, intent.side, limits, thin, coin)
    except ValueError:
        return Reject(name, "bad px")

    dev = D(limits.max_entry_deviation)
    if intent.side == "buy" and px > mid * (1 + dev):
        return Reject(name, "deviation")
    if intent.side == "sell" and px < mid * (1 - dev):
        return Reject(name, "deviation")

    stop_px = None
    take_px = None
    dist = None
    if intent.purpose == "open":
        if intent.stop_dist is None:
            return Reject(name, "stop")
        dist = D(intent.stop_dist)
        if dist <= 0:
            return Reject(name, "stop")
        if dist / mid < Decimal("0.001"):
            return Reject(name, "stop too tight")
        try:
            stop_px, take_px = _targets(mid, dist, intent, coin, spot)
        except ValueError:
            return Reject(name, "bad px")

    if account.equity <= 0 and intent.purpose == "open":
        return Reject(name, "no equity")

    original, sz, err = _size(intent, account, coin, book, limits, mid, px, pos, thin, dist, mids)
    if err:
        return Reject(name, err)

    sz = floor_sz(sz, coin.sz_decimals)
    if sz <= 0:
        return Reject(name, "dust")
    if intent.purpose == "open" and sz * mid < MIN_NOTIONAL:
        return Reject(name, "min notional")

    cloid = make_cloid(intent.strategy, name, intent.bar_ts, intent.side, intent.purpose)
    if cloid in account.sent:
        return Reject(name, "duplicate cloid")

    step = Decimal(10) ** (-coin.sz_decimals) if coin.sz_decimals else Decimal(1)
    clipped = sz + step < original

    return OrderPlan(
        coin=name,
        asset=coin.asset,
        market=coin.market,
        side=intent.side,
        sz=sz,
        limit_px=px,
        tif="Ioc",
        reduce_only=intent.purpose == "close",
        cloid=cloid,
        stop_px=stop_px,
        take_px=take_px,
        purpose=intent.purpose,
        strategy=intent.strategy,
        hl_coin=coin.api or coin.name,
        sz_decimals=coin.sz_decimals,
        clipped=clipped,
    )


def _block_open(intent, book, coin, account, gate, now_ms, mids, pos) -> str | None:
    limits = gate.limits
    if not account.armed:
        return "not armed"
    if account.halted:
        return "halted"
    if not account.synced:
        return "desynced"
    if coin.name not in gate.enabled:
        return "disabled"
    if now_ms - book.book_ts > limits.stale_ms:
        return "stale book"
    if _loss(account, limits):
        return "daily loss"
    if pos != 0:
        return "in position"
    if coin.market == "spot" and intent.side == "sell":
        return "spot short"
    liquid = _liquid(book, limits)
    if not liquid and coin.name not in gate.forced:
        return "illiquid"
    if coin.market == "perp":
        if coin.max_leverage <= 0:
            return "liq distance"
        use = min(D(limits.max_leverage), D(coin.max_leverage))
        if _liq_room(use, D(coin.max_leverage)) < D(limits.min_liq_distance):
            return "liq distance"
        for other, lp in account.liq.items():
            mark = mids.get(other)
            if mark is None or mark <= 0:
                return "liq distance"
            if abs(mark - lp) / mark < D(limits.min_liq_distance):
                return "liq distance"
    return None


def _limit_px(mid, side, limits, thin, coin) -> Decimal:
    slip = D(limits.max_slippage_bps) / Decimal(10_000)
    if thin:
        slip = slip / 2
    dev = D(limits.max_entry_deviation)
    if slip > dev:
        slip = dev
    raw = mid * (1 + slip) if side == "buy" else mid * (1 - slip)
    return round_px(raw, side, coin.sz_decimals, coin.market == "spot")


def _targets(mid, dist, intent, coin, spot):
    if intent.side == "buy":
        stop_raw = mid - dist
        stop_px = round_px(stop_raw, "sell", coin.sz_decimals, spot)
        take_px = None
        if intent.take_r:
            take_raw = mid + dist * D(intent.take_r)
            take_px = round_px(take_raw, "buy", coin.sz_decimals, spot)
    else:
        stop_raw = mid + dist
        stop_px = round_px(stop_raw, "buy", coin.sz_decimals, spot)
        take_px = None
        if intent.take_r:
            take_raw = mid - dist * D(intent.take_r)
            take_px = round_px(take_raw, "sell", coin.sz_decimals, spot)
    if stop_px <= 0 or (take_px is not None and take_px <= 0):
        raise ValueError("px")
    return stop_px, take_px


def _size(intent, account, coin, book, limits, mid, px, pos, thin, dist, mids):
    if intent.purpose == "close":
        want = abs(pos) if intent.qty is None else min(D(intent.qty), abs(pos))
        if want <= 0:
            return want, want, "dust"
        return want, want, None

    if intent.qty is not None:
        want = D(intent.qty)
    elif intent.risk_frac:
        risk = D(intent.risk_frac) * account.equity
        if risk <= 0 or dist is None or dist <= 0:
            return Decimal(0), Decimal(0), "risk"
        want = risk / dist
    else:
        return Decimal(0), Decimal(0), "size"
    if want <= 0:
        return want, want, "size"

    original = want
    equity = account.equity
    lev = min(D(limits.max_leverage), D(coin.max_leverage))
    if coin.market == "spot":
        lev = Decimal(1)
    coin_cap = min(D(limits.max_coin_frac) * equity, lev * equity)
    order_cap = D(limits.max_order_frac) * equity
    if thin:
        coin_cap *= Decimal("0.25")
        order_cap *= Decimal("0.25")

    used = Decimal(0)
    for other, szi in account.positions.items():
        if other == coin.name or szi == 0:
            continue
        mark = mids.get(other)
        if mark is None:
            return original, Decimal(0), "no mid"
        used += abs(szi) * mark
    # spot inventory is already inside equity. gross here is perp notionals only
    room = D(limits.max_gross_frac) * equity - used
    ntl_cap = min(order_cap, coin_cap, room)
    if ntl_cap <= 0:
        return original, Decimal(0), "gross"
    want = min(want, ntl_cap / mid)

    if coin.market == "spot" and intent.side == "buy":
        if coin.quote in ("", "USDC"):
            free = account.usdc
        else:
            free = account.spot.get(coin.quote, Decimal(0))
        if px <= 0:
            return original, Decimal(0), "bad px"
        want = min(want, free / px)

    part = D(limits.book_participation)
    if thin:
        part *= Decimal("0.5")
    avail = D(book.ask_sz if intent.side == "buy" else book.bid_sz)
    want = min(want, avail * part)
    if want <= 0:
        return original, want, "dust"
    return original, want, None
