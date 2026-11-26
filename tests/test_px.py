from decimal import Decimal

from hlbot.px import floor_sz, fmt_num, round_px


def test_btc_tick_and_sig_figs():
    assert round_px(Decimal("1234.56"), "buy", 1, False) == Decimal("1234.5")
    assert round_px(Decimal("1234.56"), "sell", 1, False) == Decimal("1234.6")
    assert round_px(Decimal("1234.5"), "sell", 1, False) == Decimal("1234.5")


def test_subdollar_perp_stops_at_six_decimals():
    px = round_px(Decimal("0.00123456"), "buy", 0, False)
    assert px == Decimal("0.001234")
    assert "e" not in fmt_num(px)


def test_integer_prices_over_100k():
    assert round_px(Decimal("100000.9"), "buy", 1, False) == Decimal("100000")
    assert round_px(Decimal("100000.9"), "sell", 1, False) == Decimal("100001")


def test_size_floors():
    assert floor_sz(Decimal("1.239"), 2) == Decimal("1.23")
    assert floor_sz(Decimal("0.019"), 3) == Decimal("0.019")
    assert floor_sz(Decimal("0.0004"), 3) == 0
