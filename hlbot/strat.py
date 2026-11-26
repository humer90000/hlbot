from __future__ import annotations

from decimal import Decimal

from hlbot.models import Intent

_BREAKOUT_WHEN = {
    "close_gt_donchian_high",
    "close_lt_donchian_low",
}

_COMMON = (
    "name",
    "kind",
    "enabled",
    "coins",
    "risk_frac",
    "stop_atr",
    "take_r",
    "max_hold_bars",
    "funding_block",
    "atr_len",
)


def _load_common(raw):
    return {
        "name": raw["name"],
        "enabled": bool(raw.get("enabled", False)),
        "coins": list(raw.get("coins") or []),
        "risk_frac": float(raw["risk_frac"]),
        "stop_atr": float(raw["stop_atr"]),
        "take_r": _take(raw),
        "max_hold_bars": _count(raw, "max_hold_bars", 16, 1, 100000),
        "funding_block": _funding_block(raw),
        "atr_len": _count(raw, "atr_len", 14, 1, 500),
    }


def _dump_common(obj, kind):
    return {
        "name": obj.name,
        "kind": kind,
        "enabled": obj.enabled,
        "coins": list(obj.coins),
        "risk_frac": obj.risk_frac,
        "stop_atr": obj.stop_atr,
        "take_r": obj.take_r,
        "max_hold_bars": obj.max_hold_bars,
        "funding_block": obj.funding_block,
        "atr_len": obj.atr_len,
    }


class Breakout:
    kind = "breakout"

    def __init__(self, raw):
        self.__dict__.update(_load_common(raw))
        self.donchian = int(raw.get("donchian", 20))
        self.rules = []

    def as_dict(self):
        row = _dump_common(self, "breakout")
        row["donchian"] = self.donchian
        return row

    def generate(self, snap):
        return _entries(self, snap, lambda book: _breakout_side(book, self.donchian))


class Rules:
    kind = "rules"

    def __init__(self, raw):
        self.__dict__.update(_load_common(raw))
        self.rules = []
        for rule in raw.get("rules") or []:
            self.rules.append(
                {
                    "side": rule["side"],
                    "when": rule["when"],
                    "donchian": int(rule["donchian"]),
                }
            )

    def as_dict(self):
        row = _dump_common(self, "rules")
        row["rules"] = list(self.rules)
        return row

    def generate(self, snap):
        def signal(book):
            for rule in self.rules:
                if _rule_hit(book, rule):
                    return rule["side"]
            return None

        return _entries(self, snap, signal)


class Ema:
    kind = "ema"

    def __init__(self, raw):
        self.__dict__.update(_load_common(raw))
        self.fast = int(raw["fast"])
        self.slow = int(raw["slow"])
        self.rules = []

    def as_dict(self):
        row = _dump_common(self, "ema")
        row["fast"] = self.fast
        row["slow"] = self.slow
        return row

    def generate(self, snap):
        return _entries(self, snap, lambda book: _ema_side(book.closes, self.fast, self.slow))


class Spec:
    def __init__(self, raw, kind):
        self.__dict__.update(_load_common(raw))
        self.kind = kind
        self.rules = []
        if kind == "macd":
            self.fast = int(raw["fast"])
            self.slow = int(raw["slow"])
            self.signal = int(raw["signal"])
        elif kind == "rsi":
            self.rsi_len = int(raw["rsi_len"])
            self.rsi_buy = float(raw["rsi_buy"])
            self.rsi_sell = float(raw["rsi_sell"])
        elif kind == "bollinger":
            self.length = int(raw["length"])
            self.mult = float(raw["mult"])

    def as_dict(self):
        row = _dump_common(self, self.kind)
        if self.kind == "macd":
            row["fast"] = self.fast
            row["slow"] = self.slow
            row["signal"] = self.signal
        elif self.kind == "rsi":
            row["rsi_len"] = self.rsi_len
            row["rsi_buy"] = self.rsi_buy
            row["rsi_sell"] = self.rsi_sell
        else:
            row["length"] = self.length
            row["mult"] = self.mult
        return row

    def generate(self, snap):
        kind = self.kind

        def signal(book):
            c = book.closes
            if kind == "rsi":
                return _rsi_side(c, self.rsi_len, self.rsi_buy, self.rsi_sell)
            if kind == "macd":
                return _macd_side(c, self.fast, self.slow, self.signal)
            return _band_side(c, self.length, self.mult)

        return _entries(self, snap, signal)


_KIND_FIELDS = {
    "breakout": {"donchian"},
    "rules": {"rules"},
    "ema": {"fast", "slow"},
    "rsi": {"rsi_len", "rsi_buy", "rsi_sell"},
    "macd": {"fast", "slow", "signal"},
    "bollinger": {"length", "mult"},
}


def build_strategy(raw: dict):
    if not isinstance(raw, dict):
        raise ValueError("strategy")
    kind = raw.get("kind") or "breakout"
    if kind not in _KIND_FIELDS:
        raise ValueError("kind")
    extra = set(raw) - set(_COMMON) - _KIND_FIELDS[kind]
    if extra:
        raise ValueError("unknown " + ",".join(sorted(extra)))
    name = raw.get("name")
    if not isinstance(name, str) or not name or name == "guard":
        raise ValueError("name")
    for ch in name:
        if not (ch.isalnum() or ch in "_-"):
            raise ValueError("name")
    if "risk_frac" not in raw or "stop_atr" not in raw:
        raise ValueError("missing field")
    risk = raw["risk_frac"]
    stop = raw["stop_atr"]
    if isinstance(risk, bool) or not isinstance(risk, (int, float)):
        raise ValueError("risk_frac")
    if not 0 < float(risk) <= 0.02:
        raise ValueError("risk_frac")
    if isinstance(stop, bool) or not isinstance(stop, (int, float)) or float(stop) <= 0:
        raise ValueError("stop_atr")
    _take(raw)
    coins = raw.get("coins") or []
    if not isinstance(coins, list) or not all(isinstance(c, str) for c in coins):
        raise ValueError("coins")
    if kind == "rules":
        rules = raw.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("rules")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("rules")
            if set(rule) - {"side", "when", "donchian"}:
                raise ValueError("unknown rule")
            if rule.get("side") not in ("buy", "sell"):
                raise ValueError("side")
            if rule.get("when") not in _BREAKOUT_WHEN:
                raise ValueError("when")
            n = rule.get("donchian")
            if not isinstance(n, int) or isinstance(n, bool) or not 2 <= n <= 500:
                raise ValueError("donchian")
        return Rules(raw)
    if kind == "breakout":
        raw = dict(raw)
        raw["donchian"] = _count(raw, "donchian", 20, 2, 500)
        return Breakout(raw)
    if kind == "ema":
        fast, slow = _whole(raw.get("fast"), 2, 200, "fast"), _whole(raw.get("slow"), 2, 200, "slow")
        if fast >= slow:
            raise ValueError("fast")
        raw = dict(raw)
        raw["fast"], raw["slow"] = fast, slow
        return Ema(raw)
    if kind == "rsi":
        length = _whole(raw.get("rsi_len"), 2, 100, "rsi_len")
        buy, sell = raw.get("rsi_buy"), raw.get("rsi_sell")
        if isinstance(buy, bool) or isinstance(sell, bool) or not isinstance(buy, (int, float)) or not isinstance(sell, (int, float)):
            raise ValueError("rsi")
        if not 0 < float(buy) < float(sell) < 100:
            raise ValueError("rsi")
        raw = dict(raw)
        raw["rsi_len"] = length
        return Spec(raw, "rsi")
    if kind == "macd":
        fast = _whole(raw.get("fast"), 2, 200, "fast")
        slow = _whole(raw.get("slow"), 2, 200, "slow")
        signal = _whole(raw.get("signal"), 2, 100, "signal")
        if fast >= slow:
            raise ValueError("fast")
        raw = dict(raw)
        raw["fast"], raw["slow"], raw["signal"] = fast, slow, signal
        return Spec(raw, "macd")
    length = _whole(raw.get("length"), 5, 200, "length")
    mult = raw.get("mult")
    if isinstance(mult, bool) or not isinstance(mult, (int, float)) or float(mult) <= 0:
        raise ValueError("mult")
    raw = dict(raw)
    raw["length"] = length
    return Spec(raw, "bollinger")


def _take(raw):
    if "take_r" not in raw or raw["take_r"] in (None, ""):
        return None
    value = raw["take_r"]
    if isinstance(value, bool):
        raise ValueError("take_r")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("take_r")
    if value <= 0:
        raise ValueError("take_r")
    return value


def _funding_block(raw):
    # blank turns the cutoff off
    if "funding_block" not in raw or raw["funding_block"] in (None, ""):
        return 0.0
    value = raw["funding_block"]
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError("funding_block")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("funding_block")
    return float(value)


def _count(raw, key, default, lo, hi):
    if key not in raw:
        return _whole(default, lo, hi, key)
    value = raw[key]
    if value in (None, ""):
        raise ValueError(key)
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError(key)
    return _whole(value, lo, hi, key)


def _whole(value, lo, hi, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(label)
    n = int(value)
    if not lo <= n <= hi:
        raise ValueError(label)
    return n


def _preset(name, kind, enabled, **extra):
    row = {
        "name": name,
        "kind": kind,
        "enabled": enabled,
        "coins": [],
        "risk_frac": 0.004,
        "stop_atr": 1.8,
        "take_r": 3,
        "max_hold_bars": 16,
        "funding_block": 0.0003,
        "atr_len": 14,
    }
    row.update(extra)
    return row


def preset_rows():
    return [
        _preset("ema", "ema", True, fast=9, slow=21),
        _preset("rsi", "rsi", False, rsi_len=14, rsi_buy=30, rsi_sell=70, stop_atr=1.5, take_r=2),
        _preset("macd", "macd", False, fast=12, slow=26, signal=9),
        _preset("bollinger", "bollinger", False, length=20, mult=2, stop_atr=1.4, take_r=2),
        _preset("donchian", "breakout", False, donchian=20),
    ]


def presets():
    return [build_strategy(row) for row in preset_rows()]


def _selected(strat, snap):
    if strat.coins:
        names = [n for n in strat.coins if n in snap.enabled and n in snap.coins]
    else:
        names = [n for n in snap.enabled if n in snap.coins]
    return names


def _entries(strat, snap, signal):
    out = []
    for name in _selected(strat, snap):
        book = snap.books.get(name)
        coin = snap.coins.get(name)
        if book is None or coin is None:
            continue
        if len(book.closes) < 3:
            continue
        pos = snap.positions.get(name, Decimal(0))
        if coin.market == "spot":
            pos = snap.spot.get(coin.base, Decimal(0))
        bars = snap.pos_bars.get(name, 0)
        sig = signal(book)
        if pos > 0:
            if bars >= strat.max_hold_bars or sig == "sell":
                out.append(_close(strat, name, "sell", pos, book))
            continue
        if pos < 0:
            if bars >= strat.max_hold_bars or sig == "buy":
                out.append(_close(strat, name, "buy", pos, book))
            continue
        if sig is None:
            continue
        if coin.market == "spot" and sig == "sell":
            continue
        if not _funding_ok(sig, book.funding, strat.funding_block, coin.market):
            continue
        atr = _atr(book, strat.atr_len)
        if atr is None or atr <= 0:
            continue
        out.append(
            Intent(
                strategy=strat.name,
                coin=name,
                side=sig,
                purpose="open",
                bar_ts=book.bar_ts,
                risk_frac=strat.risk_frac,
                stop_dist=strat.stop_atr * atr,
                take_r=strat.take_r,
                note=strat.kind,
            )
        )
    return out


def _close(strat, name, side, pos, book):
    return Intent(
        strategy=strat.name,
        coin=name,
        side=side,
        purpose="close",
        bar_ts=book.bar_ts,
        qty=float(abs(pos)),
        note="exit",
    )


def _funding_ok(side, funding, block, market) -> bool:
    if market != "perp" or block <= 0:
        return True
    if side == "buy" and funding > block:
        return False
    if side == "sell" and funding < -block:
        return False
    return True


def _breakout_side(book, n):
    if len(book.closes) < n + 1 or len(book.highs) < n + 1 or len(book.lows) < n + 1:
        return None
    hh = max(book.highs[-(n + 1) : -1])
    ll = min(book.lows[-(n + 1) : -1])
    last = book.closes[-1]
    if last > hh:
        return "buy"
    if last < ll:
        return "sell"
    return None


def _rule_hit(book, rule) -> bool:
    side = _breakout_side(book, rule["donchian"])
    if rule["when"] == "close_gt_donchian_high":
        return side == "buy"
    if rule["when"] == "close_lt_donchian_low":
        return side == "sell"
    return False


def _atr(book, n):
    if len(book.closes) < n + 1:
        return None
    trs = []
    start = len(book.closes) - n
    for i in range(start, len(book.closes)):
        prev = book.closes[i - 1]
        tr = max(
            book.highs[i] - book.lows[i],
            abs(book.highs[i] - prev),
            abs(book.lows[i] - prev),
        )
        trs.append(tr)
    if not trs:
        return None
    return sum(trs) / len(trs)


def _ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(values[:n]) / n
    out = [ema]
    for value in values[n:]:
        ema = value * k + ema * (1 - k)
        out.append(ema)
    return out


def _cross(fast, slow):
    if not fast or not slow or len(fast) < 2 or len(slow) < 2:
        return None
    # ema() eats its seed window, so the two lists aren't the same length
    span = min(len(fast), len(slow))
    a, b = fast[-span:], slow[-span:]
    if a[-2] <= b[-2] and a[-1] > b[-1]:
        return "buy"
    if a[-2] >= b[-2] and a[-1] < b[-1]:
        return "sell"
    return None


def _ema_side(closes, fast_n, slow_n):
    return _cross(_ema(closes, fast_n), _ema(closes, slow_n))


def _macd_side(closes, fast_n, slow_n, signal_n):
    fast = _ema(closes, fast_n)
    slow = _ema(closes, slow_n)
    if not fast or not slow:
        return None
    span = min(len(fast), len(slow))
    macd = [fast[-span + i] - slow[-span + i] for i in range(span)]
    return _cross(macd, _ema(macd, signal_n))


def _rsi(closes, n):
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / n
    avg_loss = losses / n
    for i in range(n + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _rsi_side(closes, n, buy, sell):
    value = _rsi(closes, n)
    if value is None:
        return None
    if value < buy:
        return "buy"
    if value > sell:
        return "sell"
    return None


def _band_side(closes, n, mult):
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    width = var ** 0.5 * mult
    last = closes[-1]
    if last < mid - width:
        return "buy"
    if last > mid + width:
        return "sell"
    return None
