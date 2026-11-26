from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from hlbot.catalog import parse_catalog
from hlbot.config import resolve_limits
from hlbot.models import Account, Book
from hlbot.session import Session
from hlbot.strat import build_strategy
from hlbot.web import create_app

PERP = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10},
        {"name": "OLD", "szDecimals": 0, "maxLeverage": 5, "isDelisted": True},
    ]
}
SPOT = {
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "index": 0},
        {"name": "PURR", "szDecimals": 0, "index": 1},
    ],
    "universe": [{"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True}],
}
DEX = [("xyz", {"universe": [{"name": "xyz:TSLA", "szDecimals": 3, "maxLeverage": 10}]})]


def coins():
    return parse_catalog(PERP, SPOT, DEX)


def book(**kw):
    base = dict(
        bid=100.0,
        ask=100.0,
        bid_sz=50.0,
        ask_sz=50.0,
        mark=100.0,
        funding=0.0,
        day_ntl=20_000_000.0,
        book_ts=1_000_000,
        bar_ts=1_000_000,
        highs=tuple([101.0] * 21),
        lows=tuple([99.0] * 21),
        closes=tuple([100.0] * 21),
    )
    base.update(kw)
    return Book(**base)


def account(**kw):
    base = dict(
        equity=Decimal("10000"),
        day_start=Decimal("10000"),
        synced=True,
        halted=False,
        armed=True,
        positions={},
        spot={},
        usdc=Decimal("10000"),
        sent=set(),
        liq={},
    )
    base.update(kw)
    if "sent" not in kw:
        base["sent"] = set()
    return Account(**base)


def donchian():
    return build_strategy(
        {
            "name": "donchian",
            "kind": "breakout",
            "enabled": True,
            "coins": [],
            "donchian": 20,
            "atr_len": 14,
            "stop_atr": 1.8,
            "take_r": 3,
            "risk_frac": 0.004,
            "max_hold_bars": 16,
            "funding_block": 0.0003,
        }
    )


@pytest.fixture
def session(tmp_path):
    s = Session(
        limits=resolve_limits({}),
        coins=coins(),
        strategies=[donchian()],
        mode="paper",
        equity=10_000,
        frozen=True,
        journal_path=str(tmp_path / "j.db"),
        state_dir=str(tmp_path / "state"),
    )
    s.now_ms = 1_000_000
    return s


@pytest.fixture
def client(session):
    session.agent_key = "do-not-leak-this"
    app = create_app(session, "test-token")
    with TestClient(app) as c:
        yield c
