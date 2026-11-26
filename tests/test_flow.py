import ast
import re
from decimal import Decimal
from pathlib import Path

from hlbot.catalog import fetch_catalog, parse_catalog
from hlbot.guard import flatten_intents, parse_perp_positions
from hlbot.models import OrderPlan
from hlbot.sim import SimBroker
from hlbot.strat import build_strategy
from hlbot.wire import build_action, make_cloid
from tests.conftest import PERP, SPOT, DEX, book, coins, donchian


def _break_book():
    n = 21
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    highs[-1] = 104
    lows[-1] = 101
    closes[-1] = 103
    return book(
        bid=103,
        ask=103,
        mark=103,
        book_ts=5_000,
        bar_ts=5_000,
        highs=tuple(highs),
        lows=tuple(lows),
        closes=tuple(closes),
    )


def test_catalog_asset_ids_cover_perp_spot_and_hip3():
    cat = parse_catalog(PERP, SPOT, DEX)
    assert cat["BTC"].asset == 0
    assert cat["DOGE"].asset == 1
    assert cat["OLD"].delisted and cat["OLD"].asset == 2
    assert cat["xyz:TSLA"].hip3 and cat["xyz:TSLA"].asset == 110_000
    assert cat["PURR/USDC"].market == "spot" and cat["PURR/USDC"].asset == 10_000
    assert cat["PURR/USDC"].base == "PURR"

    def post(payload):
        if payload["type"] == "meta" and "dex" not in payload:
            return PERP
        if payload["type"] == "spotMeta":
            return SPOT
        if payload["type"] == "perpDexs":
            return [None, {"name": "xyz"}]
        if payload.get("dex") == "xyz":
            return DEX[0][1]
        raise AssertionError(payload)

    assert "xyz:TSLA" in fetch_catalog(post)


def test_breakout_then_a_second_step_does_not_add():
    from hlbot.config import resolve_limits
    from hlbot.session import Session

    s = Session(
        limits=resolve_limits({}),
        coins=coins(),
        strategies=[donchian()],
        mode="paper",
        equity=10_000,
        frozen=True,
    )
    s.now_ms = 5_000
    s.books["BTC"] = _break_book()
    s.enabled.add("BTC")
    s.armed = True
    first = s.step()
    plans = [r for r in first if r.__class__.__name__ == "OrderPlan"]
    assert len(plans) == 1
    assert plans[0].side == "buy"
    assert s.broker.pos["BTC"][0] == Decimal(5)
    held = s.broker.pos["BTC"][0]
    s.step()
    assert s.broker.pos["BTC"][0] == held


def test_funding_blocks_the_long_and_a_held_bar_exits():
    cat = coins()
    strat = donchian()
    b = _break_book()
    b.funding = 0.001

    class Snap:
        pass

    snap = Snap()
    snap.books = {"BTC": b}
    snap.positions = {}
    snap.spot = {}
    snap.pos_bars = {}
    snap.coins = cat
    snap.enabled = {"BTC"}
    assert strat.generate(snap) == []

    b.funding = 0
    snap.positions = {"BTC": Decimal("1")}
    snap.pos_bars = {"BTC": 20}
    out = strat.generate(snap)
    assert out[0].purpose == "close" and out[0].side == "sell"


def test_unknown_strategy_field_is_an_error():
    try:
        build_strategy({"name": "x", "risk_frac": 0.01, "stop_atr": 1, "magic": 1})
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("expected")


def test_close_wins_when_two_intents_hit_one_coin(session):
    from hlbot.models import Intent

    session.books["BTC"] = book()
    session.broker.pos["BTC"] = [Decimal("1"), Decimal("100")]
    session.armed = True
    session.enabled.add("BTC")
    close = Intent("donchian", "BTC", "sell", "close", 1_000_000, qty=1, note="exit")
    opener = Intent("other", "BTC", "buy", "open", 1_000_000, qty=0.2, stop_dist=5, take_r=2)
    out = session._apply([opener, close])
    assert out[0].purpose == "close"
    assert out[1].reason == "one coin"
    assert "BTC" not in session.broker.pos


def test_stale_heartbeat_flattens_instead_of_opening(session):
    session.books["BTC"] = _break_book()
    session.now_ms = 50_000
    session.beat_ms = 1
    session.beat_max_ms = 1_000
    session.broker.pos["BTC"] = [Decimal("1.5"), Decimal("100")]
    session.armed = True
    session.enabled.add("BTC")
    out = session.step()
    assert out and out[0].strategy == "guard"
    assert out[0].reduce_only
    assert "BTC" not in session.broker.pos


def test_sim_fills_at_the_touch_partials_and_ignores_a_resting_limit():
    broker = SimBroker(10_000)
    b = book(ask=100, bid=100, ask_sz=0.25, bid_sz=0.25)

    def plan(side, sz, px, tif="Ioc", reduce=False, cloid="aa"):
        return OrderPlan(
            coin="BTC",
            asset=0,
            market="perp",
            side=side,
            sz=Decimal(sz),
            limit_px=Decimal(px),
            tif=tif,
            reduce_only=reduce,
            cloid=make_cloid(cloid),
            stop_px=None,
            take_px=None,
            purpose="open",
            strategy="t",
            hl_coin="BTC",
            sz_decimals=5,
        )

    buy = plan("buy", "1", "101", cloid="buy")
    fill = broker.submit(buy, b)
    assert fill.px == 100 and fill.sz == Decimal("0.25")
    assert broker.submit(buy, b) is None
    assert broker.cash == Decimal(10_000)

    miss = plan("buy", "1", "99", tif="Gtc", cloid="rest")
    assert broker.submit(miss, book(ask=100, bid=99)) is None
    assert miss.cloid in broker.resting

    broker.pos["BTC"] = [Decimal("1"), Decimal("100")]
    sell = plan("sell", "1", "90", reduce=True, cloid="sell")
    # bid on the passed book is 100, so the fill is 100 and pnl is zero.
    # use a higher bid.
    sold = broker.submit(sell, book(bid=110, ask=110, bid_sz=5, ask_sz=5))
    assert sold.px == 110
    assert broker.cash == Decimal("10010")


def test_paper_stop_and_take_fill_when_price_trades_through(session):
    session.books["BTC"] = _break_book()
    session.now_ms = 5_000
    session.enabled.add("BTC")
    session.armed = True
    session.step()
    assert "BTC" in session.broker.pos
    side, stop_px, take_px = session.broker.exits["BTC"]
    assert side == "buy" and take_px > stop_px

    between = (stop_px + take_px) / 2
    session.books["BTC"] = book(bid=float(between), ask=float(between) + 0.1, mark=float(between))
    session.now_ms = 6_000
    session.step()
    assert "BTC" in session.broker.pos

    session.books["BTC"] = book(bid=float(stop_px) - 5, ask=float(stop_px) - 4, mark=float(stop_px) - 5)
    session.now_ms = 7_000
    session.step()
    assert "BTC" not in session.broker.pos
    assert "BTC" not in session.broker.exits
    stopped = session.broker.cash
    assert stopped < Decimal(10_000)
    assert any(body.startswith("stop ") for _, kind, _, body in session.journal.recent() if kind == "fill")

    again = _break_book()
    again.bar_ts = 8_000
    again.book_ts = 8_000
    session.books["BTC"] = again
    session.now_ms = 8_000
    session.settled.clear()
    session.step()
    _, _, take_px = session.broker.exits["BTC"]
    session.books["BTC"] = book(bid=float(take_px) + 2, ask=float(take_px) + 3, mark=float(take_px) + 2)
    session.now_ms = 9_000
    session.step()
    assert "BTC" not in session.broker.pos
    assert session.broker.cash > stopped
    assert session.broker.cash > Decimal(10_000)


def test_a_short_stop_buys_back():
    broker = SimBroker(10_000)
    broker.pos["BTC"] = [Decimal("-1"), Decimal("100")]
    broker.arm_exit(
        OrderPlan(
            coin="BTC",
            asset=0,
            market="perp",
            side="sell",
            sz=Decimal("1"),
            limit_px=Decimal("100"),
            tif="Ioc",
            reduce_only=False,
            cloid=make_cloid("short"),
            stop_px=Decimal("110"),
            take_px=Decimal("80"),
            purpose="open",
            strategy="t",
            hl_coin="BTC",
            sz_decimals=5,
        )
    )
    quiet = book(bid=105, ask=106)
    assert broker.check_exits({"BTC": quiet}, coins(), 1) == []
    assert "BTC" in broker.pos
    hit = book(bid=111, ask=112)
    kind, fill = broker.check_exits({"BTC": hit}, coins(), 2)[0]
    assert kind == "stop" and fill.px == 112
    assert "BTC" not in broker.pos
    assert broker.cash == Decimal("9988")


def test_wire_groups_the_stop_with_the_entry():
    plan = OrderPlan(
        coin="BTC",
        asset=0,
        market="perp",
        side="buy",
        sz=Decimal("0.2"),
        limit_px=Decimal("100"),
        tif="Ioc",
        reduce_only=False,
        cloid=make_cloid("parent"),
        stop_px=Decimal("95"),
        take_px=Decimal("110"),
        purpose="open",
        strategy="t",
        hl_coin="BTC",
        sz_decimals=5,
    )
    action = build_action(plan)
    assert action["grouping"] == "normalTpsl"
    assert action["orders"][0]["r"] is False
    assert action["orders"][1]["r"] is True
    assert action["orders"][1]["t"]["trigger"]["tpsl"] == "sl"
    assert action["orders"][2]["t"]["trigger"]["tpsl"] == "tp"
    assert len({o["c"] for o in action["orders"]}) == 3


def test_guard_only_flattens():
    intents = flatten_intents({"BTC": Decimal("1.2"), "ETH": Decimal("-0.4"), "SOL": Decimal(0)}, "heartbeat", 5)
    assert {(i.coin, i.side, i.purpose, i.qty) for i in intents} == {
        ("BTC", "sell", "close", 1.2),
        ("ETH", "buy", "close", 0.4),
    }
    text = Path("hlbot/guard.py").read_text(encoding="utf-8")
    assert "bulk_orders" not in text
    tree = ast.parse(text)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "hlbot.live" not in imported
    state = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.5"}},
            {"position": {"coin": "ETH", "szi": "0"}},
        ]
    }
    assert parse_perp_positions(state) == {"BTC": Decimal("0.5")}


def test_strategy_module_does_not_touch_the_exchange():
    text = Path("hlbot/strat.py").read_text(encoding="utf-8")
    assert "hyperliquid" not in text
    assert "urllib" not in text


def test_sources_are_english():
    for path in list(Path("hlbot").rglob("*.py")) + [Path("hlbot/console.html")]:
        text = path.read_text(encoding="utf-8")
        assert re.search(r"[А-Яа-я]", text) is None, path.name


def test_journal_rows(session):
    session.books["BTC"] = _break_book()
    session.now_ms = 5_000
    session.enabled.add("BTC")
    session.armed = True
    session.step()
    rows = session.journal.recent()
    assert any(kind == "fill" for _, kind, _, _ in rows)


def test_live_arm_needs_the_file(tmp_path):
    from hlbot.config import resolve_limits
    from hlbot.session import Session

    arm = tmp_path / "ARM"
    s = Session(
        limits=resolve_limits({}),
        coins={},
        strategies=[],
        mode="live",
        arm_path=str(arm),
        transport=lambda plan, action: None,
        frozen=True,
    )
    assert s.arm() == "no ARM file"
    arm.write_text("yes")
    assert s.arm() is None


def test_price_snapshot_keeps_candles_and_can_be_enabled(session):
    from hlbot.feed import apply_candles, books_from_perp, books_from_spot

    meta = {"universe": [{"name": "BTC", "szDecimals": 5, "isDelisted": False}, {"name": "OLD", "isDelisted": True}]}
    ctxs = [
        {
            "midPx": "100",
            "markPx": "100",
            "impactPxs": ["99.99", "100.01"],
            "dayNtlVlm": "20000000",
            "funding": "0.0001",
        },
        {"midPx": "1", "dayNtlVlm": "99999999", "funding": "0"},
    ]
    books = books_from_perp(meta, ctxs, 5_000)
    assert "OLD" not in books
    session.ingest_books(books)
    apply_candles(
        session.books["BTC"],
        [{"t": 10, "h": "3", "l": "1", "c": "2"}, {"t": 20, "h": "4", "l": "2", "c": "3"}],
    )
    session.ingest_books(books_from_perp(meta, ctxs, 9_000))
    assert session.books["BTC"].book_ts == 9_000
    assert session.books["BTC"].closes == (2.0, 3.0)
    assert session.books["BTC"].bar_ts == 20
    spot = books_from_spot([{"coin": "PURR/USDC", "midPx": "0.07", "markPx": "0.07", "dayNtlVlm": "2000000"}], 5_000)
    session.ingest_books(spot)
    assert session.books["PURR/USDC"].bid == 0.07
    added = session.enable_liquid()
    assert added == 1
    assert "BTC" in session.enabled
    assert "PURR/USDC" not in session.enabled


def test_touch_mids_shifts_the_book_and_keeps_bars(session):
    from hlbot.feed import apply_candles, books_from_perp

    meta = {"universe": [{"name": "BTC", "szDecimals": 5, "isDelisted": False}]}
    ctxs = [{
        "midPx": "100",
        "markPx": "100",
        "impactPxs": ["99.99", "100.01"],
        "dayNtlVlm": "20000000",
        "funding": "0.0001",
    }]
    session.ingest_books(books_from_perp(meta, ctxs, 5_000))
    apply_candles(session.books["BTC"], [{"t": 10, "h": "3", "l": "1", "c": "2"}])
    n = session.touch_mids({"BTC": "110", "nope": "1", "DOGE": "bad"}, 12_000)
    book = session.books["BTC"]
    assert n == 1
    assert book.book_ts == 12_000
    assert book.closes == (2.0,)
    assert abs(book.bid - 109.99) < 1e-9
    assert abs(book.ask - 110.01) < 1e-9
    assert book.mark == 110


def test_sdk_accepts_our_cloid():
    from hyperliquid.utils.types import Cloid

    c = make_cloid("donchian", "BTC", 1, "buy", "open")
    assert Cloid.from_str(c).to_raw() == c
