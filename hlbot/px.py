from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _step(px: Decimal, max_decimals: int) -> Decimal:
    # above 1e5 the tick is 1. the 5-sig-fig cap does not apply
    if px > Decimal(100_000):
        return Decimal(1)
    sig = Decimal(10) ** (px.adjusted() - 4)
    dec = Decimal(10) ** (-max_decimals) if max_decimals > 0 else Decimal(1)
    return sig if sig > dec else dec


def round_px(px: Decimal, side: str, sz_decimals: int, spot: bool) -> Decimal:
    if px <= 0:
        raise ValueError("px")
    max_dec = (8 if spot else 6) - sz_decimals
    if max_dec < 0:
        max_dec = 0
    step = _step(px, max_dec)
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    ticks = (px / step).to_integral_value(rounding=rounding)
    out = ticks * step
    if out <= 0:
        raise ValueError("px")
    return out


def floor_sz(sz: Decimal, sz_decimals: int) -> Decimal:
    if sz < 0:
        raise ValueError("sz")
    step = Decimal(10) ** (-sz_decimals) if sz_decimals > 0 else Decimal(1)
    ticks = (sz / step).to_integral_value(rounding=ROUND_FLOOR)
    return ticks * step


def fmt_num(x: Decimal) -> str:
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
