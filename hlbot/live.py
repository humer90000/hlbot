import time

from hlbot.px import fmt_num
from hlbot.wire import make_cloid


def _f(x) -> float:
    return float(fmt_num(x))


def make_sender(base_url, account_address, vault_address, private_key):
    if not private_key:
        raise RuntimeError("HL_AGENT_KEY is empty")
    if not account_address:
        raise RuntimeError("account address missing")

    holder = {}

    def send(plan, action):
        ex = holder.get("ex")
        if ex is None:
            import eth_account
            from hyperliquid.exchange import Exchange

            wallet = eth_account.Account.from_key(private_key)
            vault = vault_address or None
            ex = Exchange(
                wallet,
                base_url,
                account_address=account_address,
                vault_address=vault,
            )
            holder["ex"] = ex

        from hyperliquid.utils.types import Cloid

        def row(is_buy, sz, px, order_type, reduce_only, cloid):
            return {
                "coin": plan.hl_coin,
                "is_buy": is_buy,
                "sz": _f(sz),
                "limit_px": _f(px),
                "order_type": order_type,
                "reduce_only": reduce_only,
                "cloid": Cloid.from_str(cloid),
            }

        orders = [
            row(
                plan.side == "buy",
                plan.sz,
                plan.limit_px,
                {"limit": {"tif": plan.tif}},
                plan.reduce_only,
                plan.cloid,
            )
        ]
        if plan.stop_px is not None and not plan.reduce_only:
            close_buy = plan.side != "buy"
            orders.append(
                row(
                    close_buy,
                    plan.sz,
                    plan.stop_px,
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": _f(plan.stop_px),
                            "tpsl": "sl",
                        }
                    },
                    True,
                    make_cloid(plan.cloid, "sl"),
                )
            )
            if plan.take_px is not None:
                orders.append(
                    row(
                        close_buy,
                        plan.sz,
                        plan.take_px,
                        {
                            "trigger": {
                                "isMarket": True,
                                "triggerPx": _f(plan.take_px),
                                "tpsl": "tp",
                            }
                        },
                        True,
                        make_cloid(plan.cloid, "tp"),
                    )
                )
        ex.set_expires_after(int(time.time() * 1000) + 5_000)
        return ex.bulk_orders(orders, grouping=action["grouping"])

    return send
