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
    margin: str = "cross"
    # coin name -> the leverage the desk will set. missing means 1.
    leverage: dict | None = None


def _loss(account: Account, limits) -> bool:
    if account.day_start <= 0:
        return True
    drop = (account.day_start - account.equity) / account.day_start
    return drop >= D(limits.max_daily_loss_frac)


def _liquid(book: Book, limits) -> bool:
    if book.synthetic:
        return False
    return book.spread_bps <= limits.max_spread_bps and book.day_ntl >= limits.min_day_volume_usd


def _picked(gate, name):
    raw = getattr(gate, "leverage", None)
    if isinstance(raw, dict) and name in raw:
        return raw[name]
    return 1


def liq_price(entry, szi, lev, max_lev, cross, cash):
    # had this as entry * (1 - 1/lev) and then blocked the trade when that was under 15%.
    # that made 10x on btc unusable. maintenance is half the initial margin at the coin max.
    entry = D(entry)
    sz = abs(D(szi))
    if entry <= 0 or sz <= 0:
        return None
    try:
        lev = int(lev)
        max_lev = int(max_lev)
    except (TypeError, ValueError):
        return None
    if lev < 1 or max_lev < 1:
        return None
    mmr = Decimal(1) / (Decimal(2) * Decimal(max_lev))
    if mmr > Decimal("0.5"):
        mmr = Decimal("0.5")
    if not cross:
        imr = Decimal(1) / Decimal(lev)
        px = entry * (1 - imr + mmr) if szi > 0 else entry * (1 + imr - mmr)
        return px if px > 0 else None
    if szi > 0:
        denom = sz * (mmr - 1)
        if denom == 0:
            return None
        px = (D(cash) - entry * sz) / denom
    else:
        px = (D(cash) + entry * sz) / (sz * (1 + mmr))
    return px if px > 0 else None


def use_leverage(limits, coin, margin="cross", chosen=1):
    # spot never borrows. a perp uses the number picked for that coin, and never past the exchange max.
    if coin is None or coin.market != "perp":
        return 1, True
    if coin.max_leverage <= 0:
        return 1, False
    try:
        n = int(chosen)
    except (TypeError, ValueError):
        n = 1
    cap = int(coin.max_leverage)
    if n > cap:
        n = cap
    if n < 1:
        n = 1
    cross = margin != "isolated" and not coin.isolated_only
    return n, cross


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
    if intent.purpose == "open" and book.synthetic:
        return Reject(name, "no book")

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
    closing = intent.purpose == "close"
    try:
        px = _limit_px(mid, intent.side, limits, thin, coin, closing)
    except ValueError:
        return Reject(name, "bad px")

    # entries stay near the mid. a close is allowed through, or a wide book never flattens.
    if not closing:
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
        # a stop that rounds onto the mid is rejected by the exchange as a bad trigger,
        # and the entry is rejected with it.
        if intent.side == "buy" and stop_px >= mid:
            return Reject(name, "stop too tight")
        if intent.side == "sell" and stop_px <= mid:
            return Reject(name, "stop too tight")
        if take_px is not None and (
            (intent.side == "buy" and take_px <= mid) or (intent.side == "sell" and take_px >= mid)
        ):
            take_px = None

    if account.equity <= 0 and intent.purpose == "open":
        return Reject(name, "no equity")

    chosen = _picked(gate, name)
    original, sz, err = _size(intent, account, coin, book, limits, mid, px, pos, thin, dist, mids, chosen)
    if err:
        return Reject(name, err)

    sz = floor_sz(sz, coin.sz_decimals)
    if sz <= 0:
        return Reject(name, "dust")
    # the exchange measures the limit, and a short limit sits under the mid
    if intent.purpose == "open" and sz * px < MIN_NOTIONAL:
        if original * px >= MIN_NOTIONAL:
            return Reject(name, "thin book")
        return Reject(name, "min notional")

    cloid = make_cloid(intent.strategy, name, intent.bar_ts, intent.side, intent.purpose)
    if cloid in account.sent:
        return Reject(name, "duplicate cloid")

    step = Decimal(10) ** (-coin.sz_decimals) if coin.sz_decimals else Decimal(1)
    clipped = sz + step < original
    lev_n, cross = use_leverage(limits, coin, getattr(gate, "margin", "cross"), chosen)

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
        leverage=lev_n,
        cross=cross,
    )


def _block_open(intent, book, coin, account, gate, now_ms, mids, pos) -> str | None:
    limits = gate.limits
    if not account.armed:
        return "not armed"
    if account.halted:
        return "halted"
    if not account.synced:
        return "desynced"
    if coin.market == "spot" and not account.spot_known:
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
        # the coin's own max is a choice, with a warning on the page. an open position
        # that is already close to its liquidation still blocks a new entry.
        for other, lp in account.liq.items():
            mark = mids.get(other)
            if mark is None or mark <= 0:
                return "liq distance"
            if abs(mark - lp) / mark < D(limits.min_liq_distance):
                return "liq distance"
    return None


def _limit_px(mid, side, limits, thin, coin, closing=False) -> Decimal:
    # 8 bps is for getting in. ZRO on testnet sits ~30 bps wide, and that exit never reached the bid.
    if closing:
        slip = Decimal("0.05")
    else:
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


def _size(intent, account, coin, book, limits, mid, px, pos, thin, dist, mids, chosen=1):
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
    lev_n, _cross = use_leverage(limits, coin, "cross", chosen)
    lev = Decimal(lev_n)
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
    margin_cap = None
    if coin.market == "perp" and account.free_margin is not None:
        budget = account.free_margin if account.free_margin > 0 else Decimal(0)
        # leverage 1 can use the free margin. above 1, the liq cushion stays unspent.
        if lev_n > 1:
            keep = D(limits.min_liq_distance)
            if keep < 0:
                keep = Decimal(0)
            if keep > Decimal("0.9"):
                keep = Decimal("0.9")
            budget = budget * (Decimal(1) - keep)
        margin_cap = budget * lev
        ntl_cap = min(ntl_cap, margin_cap)
    if ntl_cap <= 0:
        if margin_cap is not None and margin_cap <= 0:
            return original, Decimal(0), "margin"
        return original, Decimal(0), "gross"
    want = min(want, ntl_cap / mid)

    if coin.market == "spot" and intent.side == "buy":
        if coin.quote in ("", "USDC"):
            free = account.usdc
        else:
            free = account.spot.get(coin.quote, Decimal(0))
        if px <= 0:
            return original, Decimal(0), "bad px"
        if free <= 0:
            return original, Decimal(0), "margin"
        want = min(want, free / px)

    part = D(limits.book_participation)
    if thin:
        part *= Decimal("0.5")
    avail = D(book.ask_sz if intent.side == "buy" else book.bid_sz)
    want = min(want, avail * part)
    if want <= 0:
        return original, want, "dust"
    return original, want, None
