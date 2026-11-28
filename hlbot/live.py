import time

from hlbot.px import fmt_num
from hlbot.wire import make_cloid


def _f(x) -> float:
    return float(fmt_num(x))


def order_problem(result) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("status") not in (None, "ok"):
        return "rejected"
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        return None
    for row in statuses:
        if isinstance(row, str):
            if row not in ("waitingForTrigger", "waitingForFill"):
                return "rejected"
            continue
        if not isinstance(row, dict) or not row.get("error"):
            continue
        text = str(row["error"]).replace("\n", " ").strip()
        if not text or "0x" in text or len(text) > 80:
            return "rejected"
        return text
    return None


def _statuses(result):
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    statuses = data.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        return None
    return statuses


def stop_child_failed(result) -> bool:
    # the entry can fill while the stop line in the same batch comes back as an error.
    # treating that as "the send failed" left the position with no stop.
    statuses = _statuses(result)
    if not statuses or len(statuses) < 2:
        return False
    first = statuses[0]
    if not isinstance(first, dict) or not isinstance(first.get("filled"), dict):
        return False
    for row in statuses[1:]:
        if isinstance(row, dict) and row.get("error"):
            return True
        if isinstance(row, str) and row not in ("waitingForTrigger", "waitingForFill"):
            return True
    return False


def filled_sz(result):
    # the first status is the entry. children come after it.
    statuses = _statuses(result)
    if not statuses or not isinstance(statuses[0], dict):
        return None
    filled = statuses[0].get("filled")
    if not isinstance(filled, dict) or filled.get("totalSz") in (None, ""):
        return None
    from decimal import Decimal

    try:
        return Decimal(str(filled["totalSz"]))
    except Exception:
        return None


def _stop_is_marketable(plan) -> bool:
    # a stop on the wrong side of the limit fills the entry and fires at once.
    # the exchange took that fill on testnet instead of rejecting the batch.
    stop = getattr(plan, "stop_px", None)
    if stop is None or plan.reduce_only or getattr(plan, "trigger", None):
        return False
    if plan.side == "buy":
        return stop >= plan.limit_px
    return stop <= plan.limit_px


def _claim_cloid(holder, cloid) -> bool:
    # testnet rested a second order on a cloid it had already accepted
    seen = holder.setdefault("cloids", set())
    if not cloid or cloid in seen:
        return False
    seen.add(cloid)
    return True


def ensure_market(info, name):
    # the sdk only loads the main dex unless told. a hip-3 name would miss the map.
    if not name or name in getattr(info, "name_to_coin", {}):
        return
    if ":" not in name:
        raise KeyError(name)
    dex = name.split(":", 1)[0]
    found = None
    for i, row in enumerate(info.perp_dexs()[1:]):
        if isinstance(row, dict) and row.get("name") == dex:
            found = i
            break
    if found is None:
        raise KeyError(name)
    info.set_perp_meta(info.meta(dex=dex), 110_000 + found * 10_000)
    if name not in info.name_to_coin:
        raise KeyError(name)


def leverage_action(plan):
    # closes don't touch the leverage. a bad number is refused, not clamped up.
    if plan.market != "perp" or plan.reduce_only:
        return None
    try:
        lev = int(plan.leverage)
    except (TypeError, ValueError):
        lev = 0
    # the coin's exchange max is applied before this. 100 is just a bad-number guard.
    if lev < 1 or lev > 100:
        raise RuntimeError("leverage")
    return lev, bool(plan.cross)


def make_sender(base_url, account_address, vault_address, private_key):
    if not private_key:
        raise RuntimeError("HL_AGENT_KEY is empty")
    if not account_address:
        raise RuntimeError("account address missing")

    holder = {}

    def _exchange():
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
        return ex

    def apply(hl_coin, leverage, is_cross):
        lev = int(leverage)
        if lev < 1 or lev > 100:
            raise RuntimeError("leverage")
        ex = _exchange()
        ex.set_expires_after(int(time.time() * 1000) + 5_000)
        got = ex.update_leverage(lev, hl_coin, bool(is_cross))
        if not isinstance(got, dict) or got.get("status") != "ok":
            raise RuntimeError("leverage")
        holder.setdefault("lev", {})[hl_coin] = (lev, bool(is_cross))
        return got

    def send(plan, action):
        if _stop_is_marketable(plan):
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "stop too tight"}]}}}
        if not _claim_cloid(holder, plan.cloid):
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "duplicate cloid"}]}}}
        ex = _exchange()
        try:
            ensure_market(ex.info, plan.hl_coin)
        except KeyError:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "unknown coin"}]}}}
        try:
            wanted = leverage_action(plan)
        except RuntimeError:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "leverage"}]}}}
        if wanted is not None:
            lev, cross = wanted
            known = holder.get("lev", {}).get(plan.hl_coin)
            if known != (lev, cross):
                try:
                    apply(plan.hl_coin, lev, cross)
                except Exception:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "leverage"}]}}}

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

        if getattr(plan, "trigger", None) in ("sl", "tp"):
            orders = [
                row(
                    plan.side == "buy",
                    plan.sz,
                    plan.limit_px,
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": _f(plan.limit_px),
                            "tpsl": plan.trigger,
                        }
                    },
                    True,
                    plan.cloid,
                )
            ]
            ex.set_expires_after(int(time.time() * 1000) + 5_000)
            return ex.bulk_orders(orders, grouping="na")

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

    send.apply = apply
    return send
