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
    impact = ctx.get("impactPxs") or [None, None]
    try:
        bid = float(impact[0]) if impact and impact[0] else mid
        ask = float(impact[1]) if impact and impact[1] else mid
    except (TypeError, ValueError, IndexError):
        bid, ask = mid, mid
    if ask < bid:
        bid, ask = ask, bid
    if bid <= 0:
        bid = mid
    if ask <= 0:
        ask = mid
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
    )


def candle_body(coin: str, now_ms: int, interval: str = "15m", bars: int = 40) -> dict:
    step = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "1h": 3_600_000,
    }.get(interval, 900_000)
    return {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": now_ms - step * bars,
            "endTime": now_ms,
        },
    }


def apply_candles(book: Book, rows) -> None:
    rows = sorted(rows, key=lambda r: int(r["t"]))
    if not rows:
        return
    book.highs = tuple(float(r["h"]) for r in rows)
    book.lows = tuple(float(r["l"]) for r in rows)
    book.closes = tuple(float(r["c"]) for r in rows)
    book.bar_ts = int(rows[-1]["t"])
