from hlbot.models import Book


def books_from_perp(meta, ctxs, now_ms: int) -> dict:
    out = {}
    for row, ctx in zip(meta.get("universe", []), ctxs):
        if row.get("isDelisted"):
            continue
        book = _book(ctx, now_ms)
        if book is None:
            continue
        out[row["name"]] = book
    return out


def books_from_spot(ctxs, now_ms: int) -> dict:
    out = {}
    for ctx in ctxs:
        name = ctx.get("coin")
        if not name:
            continue
        book = _book(ctx, now_ms)
        if book is None:
            continue
        out[name] = book
    return out


def _book(ctx, now_ms: int):
    raw = ctx.get("midPx") or ctx.get("markPx")
    if not raw:
        return None
    try:
        mid = float(raw)
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    # spot ctx has a mid and no impact. treating that as a zero spread marks fat names liquid
    bid = ask = None
    impact = ctx.get("impactPxs") or [None, None]
    try:
        if impact[0] not in (None, "") and impact[1] not in (None, ""):
            bid = float(impact[0])
            ask = float(impact[1])
    except (TypeError, ValueError, IndexError):
        bid = ask = None
    synthetic = not (bid and ask and bid > 0 and ask > 0)
    if synthetic:
        bid = ask = mid
    elif ask < bid:
        bid, ask = ask, bid
    try:
        day = float(ctx.get("dayNtlVlm") or 0)
    except (TypeError, ValueError):
        day = 0.0
    try:
        funding = float(ctx.get("funding") or 0)
    except (TypeError, ValueError):
        funding = 0.0
    # no size on this payload. bbo replaces it. till then dayNtl / mid / 10000
    sz = 0.0
    if day > 0:
        sz = day / mid / 10_000
    return Book(
        bid=bid,
        ask=ask,
        bid_sz=sz,
        ask_sz=sz,
        mark=float(ctx.get("markPx") or mid),
        funding=funding,
        day_ntl=day,
        book_ts=now_ms,
        bar_ts=0,
        synthetic=synthetic,
    )


# hl candleSnapshot intervals. 1M (month) is left out on purpose, it collides with 1m.
INTERVALS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def candle_body(coin: str, now_ms: int, interval: str = "15m", bars: int = 40) -> dict:
    step = INTERVALS.get(interval, INTERVALS["15m"])
    return {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": now_ms - step * bars,
            "endTime": now_ms,
        },
    }


def _candle_closed(row, interval, now_ms) -> bool:
    # T is the candle's own end. a missing T means open time plus the bar size.
    end = row.get("T")
    if end not in (None, ""):
        try:
            return int(end) <= now_ms
        except (TypeError, ValueError):
            pass
    step = INTERVALS.get(interval or "", 0)
    if not step:
        return True
    try:
        return int(row["t"]) + step <= now_ms
    except (TypeError, ValueError, KeyError):
        return False


def apply_candles(book: Book, rows, interval=None, now_ms=None) -> None:
    if interval:
        rows = [r for r in rows if not r.get("i") or r.get("i") == interval]
    if now_ms:
        rows = [r for r in rows if _candle_closed(r, interval, now_ms)]
    rows = sorted(rows, key=lambda r: int(r["t"]))
    if not rows:
        return
    book.highs = tuple(float(r["h"]) for r in rows)
    book.lows = tuple(float(r["l"]) for r in rows)
    book.closes = tuple(float(r["c"]) for r in rows)
    book.bar_ts = int(rows[-1]["t"])
