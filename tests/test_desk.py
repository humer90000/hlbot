from hlbot.models import Book
from hlbot.strat import _band_side, _ema_side, _rsi_side, build_strategy, presets
from tests.conftest import book


def test_presets_leave_the_ema_on():
    rows = presets()
    assert [s.name for s in rows] == ["ema", "rsi", "macd", "bollinger", "donchian"]
    assert [s.enabled for s in rows] == [True, False, False, False, False]


def test_rsi_buys_a_slide_and_sells_a_climb():
    down = tuple(100 - i * 0.4 for i in range(40))
    up = tuple(100 + i * 0.4 for i in range(40))
    assert _rsi_side(down, 14, 30, 70) == "buy"
    assert _rsi_side(up, 14, 30, 70) == "sell"


def test_ema_cross_needs_the_fast_average_to_overtake():
    flat = (10.0,) * 30
    assert _ema_side(flat, 3, 8) is None
    series = list(flat)
    side = None
    for i in range(1, 8):
        series.append(10 + i)
        side = _ema_side(tuple(series), 3, 8)
        if side == "buy":
            break
    assert side == "buy"


def test_band_fades_a_close_outside():
    calm = (100.0,) * 20
    assert _band_side(calm, 20, 2) is None
    spiked = (100.0,) * 19 + (130.0,)
    assert _band_side(spiked, 20, 2) == "sell"


def test_rules_still_build_and_unknown_fields_fail():
    raw = {
        "name": "custom_rules",
        "kind": "rules",
        "enabled": False,
        "risk_frac": 0.004,
        "stop_atr": 1.5,
        "rules": [{"side": "buy", "when": "close_gt_donchian_high", "donchian": 20}],
    }
    assert build_strategy(raw).rules[0]["side"] == "buy"
    try:
        build_strategy({
            "name": "ema",
            "kind": "ema",
            "risk_frac": 0.01,
            "stop_atr": 1,
            "fast": 9,
            "slow": 21,
            "magic": 1,
        })
        raise AssertionError("accepted an unknown field")
    except ValueError as exc:
        assert "unknown" in str(exc)


def test_seed_turns_on_btc_and_eth_only(session):
    from hlbot.models import Coin

    session.coins["ETH"] = Coin("ETH", "perp", 1, 4, 25, "ETH")
    session.books["BTC"] = book()
    session.books["ETH"] = book(bid=2000, ask=2000.2, mark=2000)
    session.seed_coins()
    assert session.enabled == {"BTC", "ETH"}


def test_select_strategy_is_exclusive(session):
    session.strategies.append(build_strategy({
        "name": "rsi",
        "kind": "rsi",
        "enabled": False,
        "risk_frac": 0.004,
        "stop_atr": 1.5,
        "rsi_len": 14,
        "rsi_buy": 30,
        "rsi_sell": 70,
    }))
    assert session.select_strategy("rsi") is True
    flags = {s.name: s.enabled for s in session.strategies}
    assert flags["rsi"] is True
    assert flags["donchian"] is False
    assert session.select_strategy("missing") is False


def test_paper_connect_clears_a_key_and_a_bad_address_fails(session):
    session._agent_secret = "do-not-leak-this"
    out = session.connect_account("paper", "", "", "")
    assert out["mode"] == "paper"
    assert session._agent_secret == ""
    assert session.transport is None
    try:
        session.connect_account("live", "not-an-address", "", "0xabc")
        raise AssertionError("accepted a bad address")
    except ValueError as exc:
        assert str(exc) == "account"


def test_a_mid_only_spot_is_not_liquid():
    from hlbot.config import resolve_limits
    from hlbot.feed import books_from_spot
    from hlbot.risk import _liquid

    books = books_from_spot(
        [{"coin": "@1", "midPx": "10", "markPx": "10", "dayNtlVlm": "9000000"}],
        1_000,
    )
    spot = books["@1"]
    assert spot.synthetic and spot.bid == spot.ask
    assert _liquid(spot, resolve_limits({})) is False


def test_touch_top_writes_the_live_bid(session):
    session.books["BTC"] = book()
    session.books["BTC"].synthetic = True
    assert session.touch_top("BTC", [{"px": "101", "sz": "2"}, {"px": "101.2", "sz": "3"}], 50)
    assert session.books["BTC"].synthetic is False
    book_ = session.books["BTC"]
    assert book_.bid == 101
    assert book_.ask == 101.2
    assert book_.bid_sz == 2
    assert book_.book_ts == 50
    session.books["BTC"] = Book(
        bid=100, ask=100.1, bid_sz=1, ask_sz=1, mark=100, funding=0, day_ntl=2e7,
        book_ts=1, bar_ts=10, highs=(3,), lows=(1,), closes=(2,),
    )
    assert session.push_candle("BTC", {"t": 10, "h": "4", "l": "1", "c": "3.5"})
    assert session.books["BTC"].closes == (3.5,)
    assert session.push_candle("BTC", {"t": 20, "h": "5", "l": "3", "c": "4"})
    assert session.books["BTC"].closes == (3.5, 4.0)
    assert session.books["BTC"].bar_ts == 20
    assert session.push_candle("BTC", {"t": 30, "h": "6", "l": "4", "c": "5", "i": "1h"}) is False
    assert session.push_candle(
        "BTC",
        {"t": session.now_ms, "T": session.now_ms + 60_000, "h": "200", "l": "50", "c": "180", "i": "15m"},
    ) is False
    assert session.books["BTC"].closes[-1] == 4.0
    assert session.books["BTC"].bar_ts == 20
    assert session.books["BTC"].bar_ts == 20


def test_an_open_candle_is_left_out_of_the_signal(session):
    from hlbot.feed import apply_candles

    session.books["BTC"] = book()
    apply_candles(
        session.books["BTC"],
        [
            {"t": 0, "h": "2", "l": "1", "c": "1.5", "i": "15m"},
            {"t": 900_000, "T": 1_800_000, "h": "9", "l": "1", "c": "8", "i": "15m"},
        ],
        "15m",
        session.now_ms,
    )
    assert session.books["BTC"].closes == (1.5,)
    assert session.books["BTC"].bar_ts == 0


def test_a_live_top_survives_the_mid_refresh(session):
    session.books["BTC"] = book()
    assert session.touch_top("BTC", [{"px": "101", "sz": "2"}, {"px": "101.2", "sz": "3"}], 50)
    fresh = book(bid=50, ask=50)
    fresh.synthetic = True
    session.ingest_books({"BTC": fresh})
    kept = session.books["BTC"]
    assert (kept.bid, kept.ask, kept.synthetic) == (101, 101.2, False)


def test_interval_switch_drops_the_old_bars(session):
    session.books["BTC"] = Book(
        bid=100, ask=100.1, bid_sz=1, ask_sz=1, mark=100, funding=0, day_ntl=2e7,
        book_ts=1, bar_ts=10, highs=(3,), lows=(1,), closes=(2,),
    )
    session.bar_seen["BTC"] = 10
    session.pos_bars["BTC"] = 4
    assert session.set_interval("nope") == "interval"
    assert session.interval == "15m"
    assert session.books["BTC"].closes == (2,)
    assert session.set_interval("1h") is None
    assert session.interval == "1h"
    assert session.books["BTC"].closes == ()
    assert session.books["BTC"].bar_ts == 0
    assert session.bar_seen == {}
    assert session.pull_candles is True
    assert session.set_interval("1h") is None


def test_the_same_unarmed_bar_is_not_logged_twice(session):
    highs = [101.0] * 21
    lows = [99.0] * 21
    closes = [100.0] * 21
    highs[-1], lows[-1], closes[-1] = 104, 101, 103
    session.books["BTC"] = book(
        bid=103, ask=103, mark=103, book_ts=1_000_000, bar_ts=5_000,
        highs=tuple(highs), lows=tuple(lows), closes=tuple(closes),
    )
    session.enabled.add("BTC")
    session.step()
    session.step()
    notes = [line.split(" ", 1)[1] for line in session.log]
    assert notes.count("BTC open not armed") == 1
    rejects = [kind for _, kind, _, _ in session.journal.recent(20)]
    assert rejects.count("reject") == 1
    session.armed = True
    session.step()
    assert session.broker.pos["BTC"][0] > 0


def test_state_hides_the_agent_secret(client, session):
    session._agent_secret = "do-not-leak-this"
    res = client.get("/api/state", headers={"Authorization": "Bearer test-token"})
    assert "do-not-leak-this" not in res.text
    assert res.json()["link"]["key_loaded"] is True
    paper = client.post("/api/account", json={"mode": "paper"}, headers={"Authorization": "Bearer test-token"})
    assert paper.status_code == 200
    assert paper.json()["mode"] == "paper"
    bad = client.post(
        "/api/account",
        json={"mode": "live", "account": "0x" + "11" * 20},
        headers={"Authorization": "Bearer test-token"},
    )
    assert bad.status_code == 400


def test_depth_inside_the_slip_counts_and_a_dust_top_does_not(session):
    from decimal import Decimal

    from hlbot.models import Intent
    from hlbot.risk import Gate, decide
    from tests.conftest import account

    session.books["BTC"] = book(bid=61546, ask=61566, bid_sz=0.00017, ask_sz=0.00017, mark=61556, day_ntl=2e7)
    levels = [
        [
            {"px": "61546", "sz": "0.00017"},
            {"px": "61533", "sz": "0.00224"},
        ],
        [
            {"px": "61566", "sz": "0.00017"},
            {"px": "61591", "sz": "0.00244"},
            {"px": "61600", "sz": "0.00020"},
            {"px": "61608", "sz": "0.00324"},
        ],
    ]
    assert session.touch_depth("BTC", levels, 50)
    assert abs(session.books["BTC"].ask_sz - 0.00281) < 1e-9
    assert session.touch_top("BTC", [{"px": "61546", "sz": "0.00001"}, {"px": "61566", "sz": "0.00001"}], 60)
    assert abs(session.books["BTC"].ask_sz - 0.00281) < 1e-9
    session.ingest_books({"BTC": book(bid=61540, ask=61570, bid_sz=9, ask_sz=9, day_ntl=2e7)})
    kept = session.books["BTC"]
    assert abs(kept.ask_sz - 0.00281) < 1e-9
    assert kept.ask == 61570
    intent = Intent("t", "BTC", "buy", "open", 1, qty=1, stop_dist=1800, take_r=2)
    d = decide(
        intent,
        kept,
        session.coins["BTC"],
        account(),
        Gate(session.limits, {"BTC"}, set()),
        kept.book_ts,
        {"BTC": Decimal("61555")},
    )
    assert d.sz > Decimal("0.0002")
    assert d.sz <= Decimal("0.00029")


def test_page_hides_the_gate_and_offers_the_desk(client):
    page = client.get("/").text
    assert "[hidden]" in page
    assert "Account" in page
    assert "Perps" in page
    assert "Advanced" in page
    assert "flatten" in page
