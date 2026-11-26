import random
from decimal import Decimal

import pytest

from hlbot.config import ConfigError, check_bind, load_app, resolve_limits
from hlbot.models import Intent
from hlbot.risk import Gate, decide
from tests.conftest import account, book, coins


def _gate(enabled=("BTC",), forced=(), **over):
    return Gate(resolve_limits(over), set(enabled), set(forced))


def _open(**kw):
    base = dict(strategy="t", coin="BTC", side="buy", purpose="open", bar_ts=1, qty=0.2, stop_dist=5, take_r=2)
    base.update(kw)
    return Intent(**base)


def test_open_builds_a_protected_ioc():
    d = decide(_open(), book(), coins()["BTC"], account(), _gate(), 1_000_000, {"BTC": Decimal(100)})
    assert d.purpose == "open"
    assert d.reduce_only is False
    assert d.tif == "Ioc"
    assert d.sz == Decimal("0.2")
    assert d.stop_px is not None and d.stop_px < 100
    assert d.take_px is not None and d.take_px > 100
    assert d.limit_px <= Decimal("101")


def test_stale_book_blocks_entry_but_not_the_exit():
    b = book(book_ts=0)
    coin = coins()["BTC"]
    acct = account(positions={"BTC": Decimal("0.2")})
    assert decide(_open(), b, coin, account(), _gate(), 5_000, {"BTC": Decimal(100)}).reason == "stale book"
    close = _open(purpose="close", side="sell", qty=0.2, stop_dist=None, take_r=None)
    d = decide(close, b, coin, acct, _gate(), 5_000, {"BTC": Decimal(100)})
    assert d.reduce_only is True
    assert d.sz == Decimal("0.2")


def test_not_armed_halt_desync_daily_loss():
    coin = coins()["BTC"]
    b = book()
    mids = {"BTC": Decimal(100)}
    assert decide(_open(), b, coin, account(armed=False), _gate(), 1_000_000, mids).reason == "not armed"
    assert decide(_open(), b, coin, account(halted=True), _gate(), 1_000_000, mids).reason == "halted"
    assert decide(_open(), b, coin, account(synced=False), _gate(), 1_000_000, mids).reason == "desynced"
    poor = account(equity=Decimal("9600"))
    assert decide(_open(), b, coin, poor, _gate(), 1_000_000, mids).reason == "daily loss"


def test_disabled_delisted_illiquid_and_force():
    cat = coins()
    b = book()
    mids = {"BTC": Decimal(100)}
    assert decide(_open(), b, cat["BTC"], account(), _gate(enabled=()), 1_000_000, mids).reason == "disabled"
    assert decide(_open(coin="OLD"), b, cat["OLD"], account(), _gate(enabled=("OLD",)), 1_000_000, mids).reason == "delisted"
    thin = book(day_ntl=1_000)
    assert decide(_open(), thin, cat["BTC"], account(), _gate(), 1_000_000, mids).reason == "illiquid"
    forced = decide(_open(qty=100), thin, cat["BTC"], account(), _gate(forced=("BTC",)), 1_000_000, mids)
    liquid = decide(_open(qty=100), b, cat["BTC"], account(), _gate(), 1_000_000, mids)
    assert forced.sz < liquid.sz


def test_other_coin_near_liquidation_blocks_a_new_entry():
    mids = {"BTC": Decimal(100), "DOGE": Decimal("0.2")}
    acct = account(positions={"DOGE": Decimal(1000)}, liq={"DOGE": Decimal("0.18")})
    d = decide(_open(), book(), coins()["BTC"], acct, _gate(), 1_000_000, mids)
    assert d.reason == "liq distance"


def test_duplicate_cloid_and_min_notional():
    coin = coins()["BTC"]
    b = book()
    first = decide(_open(), b, coin, account(), _gate(), 1_000_000, {"BTC": Decimal(100)})
    again = decide(_open(), b, coin, account(sent={first.cloid}), _gate(), 1_000_000, {"BTC": Decimal(100)})
    assert again.reason == "duplicate cloid"
    dust = decide(_open(qty=0.00001), b, coin, account(), _gate(), 1_000_000, {"BTC": Decimal(100)})
    assert dust.reason == "min notional"


def test_spot_short_is_rejected():
    coin = coins()["PURR/USDC"]
    d = decide(
        _open(coin="PURR/USDC", side="sell"),
        book(bid=1, ask=1),
        coin,
        account(),
        _gate(enabled=("PURR/USDC",)),
        1_000_000,
        {},
    )
    assert d.reason == "spot short"


def test_random_opens_stay_inside_the_caps():
    rng = random.Random(3)
    lim = resolve_limits({})
    coin = coins()["BTC"]
    gate = Gate(lim, {"BTC"}, set())
    accepted = 0
    for i in range(300):
        equity = Decimal(rng.choice([2000, 8000, 15000]))
        ask_sz = Decimal(str(round(rng.uniform(0.05, 40), 4)))
        intent = Intent(
            "t",
            "BTC",
            rng.choice(["buy", "sell"]),
            "open",
            i,
            qty=float(rng.uniform(0.0001, 30)),
            stop_dist=float(rng.uniform(0.2, 30)),
            take_r=2,
        )
        b = book(bid_sz=float(ask_sz), ask_sz=float(ask_sz))
        acct = account(equity=equity, day_start=equity, usdc=equity)
        d = decide(intent, b, coin, acct, gate, 1_000_000, {"BTC": Decimal(100)})
        if d.__class__.__name__ == "Reject":
            continue
        accepted += 1
        assert d.sz * Decimal(100) <= equity * Decimal(str(lim.max_order_frac)) + Decimal("0.02")
        assert d.sz <= ask_sz * Decimal(str(lim.book_participation)) + Decimal("0.0001")
        assert d.sz * Decimal(100) / equity <= Decimal(str(min(lim.max_leverage, coin.max_leverage))) + Decimal("0.01")
    assert accepted > 50


def test_config_refuses_a_loose_ceiling():
    with pytest.raises(ConfigError):
        resolve_limits({"max_leverage": 50})
    with pytest.raises(ConfigError):
        resolve_limits({"max_slippage_bps": 25})
    with pytest.raises(ConfigError):
        resolve_limits({"min_liq_distance": 0.01})
    with pytest.raises(ConfigError):
        resolve_limits({"stale_ms": 10_000})
    with pytest.raises(ConfigError):
        check_bind("0.0.0.0", expose=False)
    assert check_bind("127.0.0.1", False) == "127.0.0.1"
    assert check_bind("0.0.0.0", True) == "0.0.0.0"


def test_example_config_loads():
    cfg = load_app("config.example.toml")
    assert cfg.mode == "paper"
    assert [s.name for s in cfg.strategies] == ["ema", "rsi", "macd", "bollinger", "donchian"]
    assert [s.enabled for s in cfg.strategies] == [True, False, False, False, False]
    assert cfg.strategies[0].fast == 9 and cfg.strategies[0].slow == 21
