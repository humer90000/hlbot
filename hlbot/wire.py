import hashlib

from hlbot.models import OrderPlan
from hlbot.px import fmt_num


def make_cloid(*parts) -> str:
    raw = "|".join(str(p) for p in parts).encode()
    return "0x" + hashlib.sha256(raw).digest()[:16].hex()


def build_action(plan: OrderPlan) -> dict:
    def one(side_buy, px, sz, reduce_only, tif_or_trigger, cloid):
        row = {
            "a": plan.asset,
            "b": side_buy,
            "p": fmt_num(px),
            "s": fmt_num(sz),
            "r": reduce_only,
            "t": tif_or_trigger,
            "c": cloid,
        }
        return row

    entry = one(
        plan.side == "buy",
        plan.limit_px,
        plan.sz,
        plan.reduce_only,
        {"limit": {"tif": plan.tif}},
        plan.cloid,
    )
    orders = [entry]
    grouping = "na"
    if plan.stop_px is not None and not plan.reduce_only:
        close_buy = plan.side != "buy"
        orders.append(
            one(
                close_buy,
                plan.stop_px,
                plan.sz,
                True,
                {
                    "trigger": {
                        "isMarket": True,
                        "triggerPx": fmt_num(plan.stop_px),
                        "tpsl": "sl",
                    }
                },
                make_cloid(plan.cloid, "sl"),
            )
        )
        if plan.take_px is not None:
            orders.append(
                one(
                    close_buy,
                    plan.take_px,
                    plan.sz,
                    True,
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": fmt_num(plan.take_px),
                            "tpsl": "tp",
                        }
                    },
                    make_cloid(plan.cloid, "tp"),
                )
            )
        grouping = "normalTpsl"
    return {"type": "order", "orders": orders, "grouping": grouping}
