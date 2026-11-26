def test_page_and_auth(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "flatten" in page.text
    assert client.get("/api/state").status_code == 401


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
