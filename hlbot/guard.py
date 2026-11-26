from decimal import Decimal

from hlbot.models import Intent


def heartbeat_due(now_ms: int, beat_ms: int, max_age_ms: int, loss_hit: bool) -> bool:
    if loss_hit:
        return True
    if beat_ms <= 0:
        return False
    return now_ms - beat_ms > max_age_ms


def flatten_intents(positions: dict, reason: str, bar_ts: int) -> list[Intent]:
    out = []
    for coin, szi in positions.items():
        if szi == 0:
            continue
        out.append(
            Intent(
                strategy="guard",
                coin=coin,
                side="sell" if szi > 0 else "buy",
                purpose="close",
                bar_ts=bar_ts,
                qty=float(abs(szi)),
                note=reason,
            )
        )
    return out


def parse_perp_positions(state: dict) -> dict[str, Decimal]:
    out = {}
    for row in state.get("assetPositions") or []:
        pos = row.get("position") or {}
        coin = pos.get("coin")
        if not coin:
            continue
        sz = Decimal(str(pos.get("szi", "0")))
        if sz != 0:
            out[coin] = sz
    return out
