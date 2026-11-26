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


def test_touch_top_writes_the_live_bid(session):
    session.books["BTC"] = book()
    assert session.touch_top("BTC", [{"px": "101", "sz": "2"}, {"px": "101.2", "sz": "3"}], 50)
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


def test_page_hides_the_gate_and_offers_the_desk(client):
    page = client.get("/").text
    assert "[hidden]" in page
    assert "Account" in page
    assert "Perps" in page
    assert "Advanced" in page
    assert "flatten" in page
