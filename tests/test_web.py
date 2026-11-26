def test_page_and_auth(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "flatten" in page.text
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/state", headers={"Authorization": "bearer test-token"}).status_code == 401
    for path in ("/docs", "/redoc", "/openapi.json"):
        hidden = client.get(path)
        assert hidden.status_code == 401
        assert "openapi" not in hidden.text


def test_state_does_not_leak_the_agent_key(client, session):
    res = client.get("/api/state", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert "do-not-leak-this" not in res.text
    assert "private" not in res.text
    assert res.json()["ceilings"]["max_leverage"] == 3


def test_risk_ceiling_and_coin_toggles(client, session):
    from tests.conftest import book

    h = {"Authorization": "Bearer test-token"}
    bad = client.post("/api/risk", json={"max_leverage": 50}, headers=h)
    assert bad.status_code == 400
    session.books["BTC"] = book()
    session.books["DOGE"] = book(day_ntl=10, bid=0.2, ask=0.2)
    no = client.post("/api/coins/enable", json={"name": "DOGE", "on": True}, headers=h)
    assert no.status_code == 400
    yes = client.post(
        "/api/coins/enable",
        json={"name": "DOGE", "on": True, "force": True},
        headers=h,
    )
    assert yes.status_code == 200
    added = client.post("/api/coins/enable-liquid", headers=h)
    assert added.json()["added"] == 1
    names = {row["name"]: row for row in client.get("/api/coins", headers=h).json()}
    assert names["BTC"]["enabled"] and not names["OLD"]["enabled"]
    assert names["xyz:TSLA"]["hip3"]


def test_strategy_roundtrip_and_arm(client):
    h = {"Authorization": "Bearer test-token"}
    bad = client.post(
        "/api/strategies",
        json={"name": "custom", "risk_frac": 0.5, "stop_atr": 1},
        headers=h,
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/strategies",
        json={"name": "custom", "kind": "breakout", "enabled": False, "risk_frac": 0.01, "stop_atr": 1.2, "donchian": 15},
        headers=h,
    )
    assert ok.status_code == 200
    assert client.delete("/api/strategies/custom", headers=h).status_code == 200
    assert client.post("/api/arm", headers=h).status_code == 200
    state = client.get("/api/state", headers=h).json()
    assert state["armed"] is True
    client.post("/api/halt", headers=h)
    assert client.get("/api/state", headers=h).json()["halted"] is True
    assert client.post("/api/disarm", headers=h).status_code == 200
    assert client.get("/api/state", headers=h).json()["armed"] is False


def test_blank_risk_number_is_rejected(client):
    h = {"Authorization": "Bearer test-token"}
    res = client.post("/api/risk", json={"max_slippage_bps": None}, headers=h)
    assert res.status_code == 400
    assert res.json()["error"] == "max_slippage_bps not a number"
    assert client.get("/api/state", headers=h).json()["risk"]["max_slippage_bps"] == 8


def test_margin_switch_and_the_wider_size_ceiling(client):
    h = {"Authorization": "Bearer test-token"}
    assert client.post("/api/margin", json={"margin": "both"}, headers=h).status_code == 400
    ok = client.post("/api/margin", json={"margin": "isolated"}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["margin"] == "isolated"
    assert client.get("/api/state", headers=h).json()["margin"] == "isolated"
    back = client.post("/api/margin", json={"margin": "cross"}, headers=h)
    assert back.json()["margin"] == "cross"
    wide = {
        "max_leverage": 2,
        "max_order_frac": 2,
        "max_coin_frac": 2,
        "max_gross_frac": 2,
    }
    assert client.post("/api/risk", json=wide, headers=h).status_code == 200
    too = client.post("/api/risk", json={"max_order_frac": 4, "max_coin_frac": 4, "max_gross_frac": 4}, headers=h)
    assert too.status_code == 400
    assert "above ceiling" in too.json()["error"]


def test_each_coin_starts_at_1x_and_stops_at_its_own_max(client):
    h = {"Authorization": "Bearer test-token"}
    rows = client.get("/api/coins", headers=h).json()
    btc = next(row for row in rows if row["name"] == "BTC")
    assert btc["leverage"] == 1
    assert btc["max_leverage"] == 40
    assert client.post("/api/leverage", json={"coin": "BTC", "leverage": 41}, headers=h).status_code == 400
    assert client.post("/api/leverage", json={"coin": "BTC", "leverage": 2.5}, headers=h).status_code == 400
    assert client.post("/api/leverage", json={"coin": "PURR/USDC", "leverage": 2}, headers=h).status_code == 400
    top = client.post("/api/leverage", json={"coin": "BTC", "leverage": 40}, headers=h)
    assert top.status_code == 200
    assert top.json()["leverage"] == 40 and top.json()["maxed"] is True
    back = client.post("/api/leverage", json={"coin": "BTC", "leverage": 1}, headers=h)
    assert back.json()["leverage"] == 1 and back.json()["maxed"] is False
    again = next(row for row in client.get("/api/coins", headers=h).json() if row["name"] == "BTC")
    assert again["leverage"] == 1


def test_open_position_leverage_is_pushed_once(session):
    from decimal import Decimal

    from tests.conftest import book

    calls = []

    def apply(coin, lev, cross):
        calls.append((coin, lev, cross))

    def transport(plan, action):
        return None

    transport.apply = apply
    session.mode = "testnet"
    session.transport = transport
    session.strategies = []
    session.leverage["BTC"] = 2
    session.broker.pos["BTC"] = [Decimal("0.1"), Decimal("100")]
    session.books["BTC"] = book()
    session.step()
    assert calls == [("BTC", 2, True)]
    session.step()
    assert calls == [("BTC", 2, True)]
    assert session.set_margin("isolated") is None
    session.step()
    assert calls[-1] == ("BTC", 2, False)
    assert session.set_margin("nope") == "margin"

    def boom(coin, lev, cross):
        raise RuntimeError("nope")

    session.transport.apply = boom
    session._lev_applied.clear()
    session._lev_failed.clear()
    session.step()
    session.step()
    assert sum(1 for line in session.log if "leverage BTC RuntimeError" in line) == 1


def test_blank_strategy_fields_and_coin_case(client):
    h = {"Authorization": "Bearer test-token"}
    base = {
        "name": "custom2",
        "kind": "ema",
        "enabled": False,
        "risk_frac": 0.01,
        "stop_atr": 1.2,
        "fast": 9,
        "slow": 21,
    }
    for key in ("max_hold_bars", "atr_len"):
        res = client.post("/api/strategies", json={**base, key: None}, headers=h)
        assert res.status_code == 400
        assert res.json()["error"] == key
    zero = client.post("/api/strategies", json={**base, "max_hold_bars": 0}, headers=h)
    assert zero.status_code == 400
    blank = client.post("/api/strategies", json={**base, "funding_block": None, "coins": ["btc", " doge ", "btc", "purr/usdc", "nope"]}, headers=h)
    assert blank.status_code == 200
    saved = blank.json()["strategy"]
    assert saved["funding_block"] == 0
    assert saved["coins"] == ["BTC", "DOGE", "PURR/USDC", "nope"]
    assert client.delete("/api/strategies/custom2", headers=h).status_code == 200
    don = client.post(
        "/api/strategies",
        json={"name": "custom2", "kind": "breakout", "enabled": False, "risk_frac": 0.01, "stop_atr": 1.2, "donchian": None},
        headers=h,
    )
    assert don.status_code == 400
    assert don.json()["error"] == "donchian"
