from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from hlbot.guard import flatten_intents, heartbeat_due
from hlbot.models import Account, Reject
from hlbot.px import D, fmt_num
from hlbot.risk import Gate, decide
from hlbot.sim import SimBroker
from hlbot.wire import build_action


class Snap:
    def __init__(self, now_ms, books, positions, spot, pos_bars, coins, enabled):
        self.now_ms = now_ms
        self.books = books
        self.positions = positions
        self.spot = spot
        self.pos_bars = pos_bars
        self.coins = coins
        self.enabled = enabled


class Session:
    def __init__(
        self,
        limits,
        coins,
        strategies,
        mode="paper",
        equity=10_000,
        info_url="https://api.hyperliquid.xyz",
        arm_path=None,
        state_dir=None,
        journal_path=None,
        transport=None,
        frozen=False,
        beat_max_ms=10_000,
        interval="15m",
    ):
        self.limits = limits
        self.coins = dict(coins)
        self.strategies = list(strategies)
        self.mode = mode
        self.info_url = info_url
        self.arm_path = arm_path
        self.state_dir = state_dir
        self.transport = transport
        self.frozen = frozen
        self.beat_max_ms = beat_max_ms
        self.paper_equity = D(equity)
        self.broker = SimBroker(equity)
        self.day_start = D(equity)
        self.live_equity = None
        self.account_address = ""
        self.vault_address = ""
        self.agent_address = ""
        self._agent_secret = ""
        self.mid_ms = 0
        self.feed = "idle"
        self.enabled = set()
        self.forced = set()
        self.books = {}
        self.liq = {}
        self.sent = set()
        self.armed = False
        self.halted = False
        self.synced = True
        self.now_ms = 0
        self.beat_ms = 0
        self.pos_bars = {}
        self.bar_seen = {}
        self.log = deque(maxlen=80)
        self.lock = threading.Lock()
        self.journal = Journal(journal_path) if journal_path else None
        self.last_error = ""
        from hlbot.feed import INTERVALS

        self.interval = interval if interval in INTERVALS else "15m"

    def gate(self) -> Gate:
        return Gate(self.limits, set(self.enabled), set(self.forced))

    def note(self, text):
        self.log.appendleft(f"{self.now_ms} {text}")

    def mids(self):
        out = {}
        for name, book in self.books.items():
            if book.bid > 0 and book.ask >= book.bid:
                out[name] = (D(book.bid) + D(book.ask)) / 2
        return out

    def _stamp(self):
        if not self.frozen:
            self.now_ms = int(time.time() * 1000)

    def _positions(self):
        out = {}
        for name, (szi, _entry) in self.broker.pos.items():
            if szi != 0:
                out[name] = szi
        return out

    def _account(self) -> Account:
        mids = self.mids()
        return Account(
            equity=self._equity(),
            day_start=self.day_start,
            synced=self.synced,
            halted=self.halted,
            armed=self.armed,
            positions=self._positions(),
            spot=dict(self.broker.spot),
            usdc=self.broker.cash,
            sent=self.sent,
            liq={k: D(v) for k, v in self.liq.items()},
        )

    def _equity(self):
        if self.mode in ("live", "testnet") and self.live_equity is not None:
            return self.live_equity
        return self.broker.equity(self.mids())

    def _loss_hit(self) -> bool:
        eq = self._equity()
        if self.day_start <= 0:
            return True
        return (self.day_start - eq) / self.day_start >= D(self.limits.max_daily_loss_frac)

    def _roll_holds(self):
        for name, coin in self.coins.items():
            book = self.books.get(name)
            if book is None:
                continue
            exp = self.broker.exposure(name, coin)
            if exp == 0:
                self.pos_bars[name] = 0
                self.bar_seen.pop(name, None)
                continue
            if book.bar_ts <= 0:
                continue
            seen = self.bar_seen.get(name)
            if seen is None:
                self.bar_seen[name] = book.bar_ts
                self.pos_bars[name] = 0
            elif seen != book.bar_ts:
                self.pos_bars[name] = self.pos_bars.get(name, 0) + 1
                self.bar_seen[name] = book.bar_ts

    def snapshot(self) -> Snap:
        return Snap(
            self.now_ms,
            self.books,
            self._positions(),
            dict(self.broker.spot),
            dict(self.pos_bars),
            self.coins,
            set(self.enabled),
        )

    def step(self):
        with self.lock:
            return self._step()

    def _step(self):
        self._stamp()
        loss = self._loss_hit()
        flatten_now = heartbeat_due(self.now_ms, self.beat_ms, self.beat_max_ms, loss)
        self.beat_ms = self.now_ms
        self._write_state()
        self._roll_holds()
        if flatten_now:
            intents = flatten_intents(self._positions(), "guard", self.now_ms)
        else:
            intents = []
            snap = self.snapshot()
            for strat in self.strategies:
                if not strat.enabled:
                    continue
                intents.extend(strat.generate(snap))
        return self._apply(intents)

    def flatten(self):
        with self.lock:
            self._stamp()
            intents = flatten_intents(self._positions(), "manual", self.now_ms)
            return self._apply(intents)

    def _apply(self, intents):
        intents = sorted(intents, key=lambda i: (0 if i.purpose == "close" else 1, i.coin))
        seen = set()
        out = []
        for intent in intents:
            if intent.coin in seen:
                rej = Reject(intent.coin, "one coin")
                out.append(rej)
                self.note(f"{intent.coin} one coin")
                continue
            seen.add(intent.coin)
            coin = self.coins.get(intent.coin)
            book = self.books.get(intent.coin)
            acct = self._account()
            result = decide(intent, book, coin, acct, self.gate(), self.now_ms, self.mids())
            out.append(result)
            if isinstance(result, Reject):
                self.note(f"{intent.coin} {intent.purpose} {result.reason}")
                self._journal("reject", intent.coin, result.reason)
                continue
            self.sent.add(result.cloid)
            acct.sent.add(result.cloid)
            self.note(
                f"{result.side} {result.coin} {fmt_num(result.sz)}"
                + (" clipped" if result.clipped else "")
            )
            self._journal("order", result.coin, result.cloid)
            fill = self._execute(result, book, coin)
            if self.mode in ("paper", "backtest"):
                if fill is None:
                    self.note(f"miss {result.coin}")
                else:
                    self.note(f"fill {result.coin} {fmt_num(fill.sz)} @ {fmt_num(fill.px)}")
                    self._journal("fill", result.coin, f"{fill.sz}@{fill.px}")
        return out

    def _execute(self, plan, book, coin):
        action = build_action(plan)
        if self.mode in ("paper", "backtest"):
            return self.broker.submit(plan, book, coin)
        if self.transport is None:
            self.note(f"no transport {plan.coin}")
            return None
        try:
            self.transport(plan, action)
        except Exception as exc:
            self.note(f"send failed {plan.coin} {exc.__class__.__name__}")
            return None
        return None

    def _journal(self, kind, coin, body):
        if self.journal is None:
            return
        self.journal.write(self.now_ms, kind, coin, str(body))

    def _write_state(self):
        if not self.state_dir:
            return
        path = Path(self.state_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "heartbeat").write_text(str(self.beat_ms))
        rows = {k: str(v) for k, v in self._positions().items()}
        (path / "positions.json").write_text(json.dumps(rows))

    def arm(self):
        with self.lock:
            if self.mode in ("live", "testnet"):
                if self.transport is None:
                    return "no transport"
            if self.mode == "live":
                if not self.arm_path or not Path(self.arm_path).is_file():
                    return "no ARM file"
            self.armed = True
            self.note("armed")
            return None

    def disarm(self):
        with self.lock:
            self.armed = False
            self.note("disarmed")

    def halt(self):
        with self.lock:
            self.halted = True
            self.note("halt")

    def resume(self):
        with self.lock:
            self.halted = False
            self.note("resume")

    def set_interval(self, value):
        from hlbot.feed import INTERVALS

        if value not in INTERVALS:
            return "interval"
        with self.lock:
            if value == self.interval:
                return None
            self.interval = value
            for book in self.books.values():
                book.highs = ()
                book.lows = ()
                book.closes = ()
                book.bar_ts = 0
            self.bar_seen.clear()
            self.pos_bars.clear()
            self.note("interval " + value)
        return None

    def seed_coins(self, names=("BTC", "ETH")):
        for name in names:
            coin = self.coins.get(name)
            if coin is None or coin.delisted:
                continue
            err = self.set_coin(name, True, False)
            if err:
                self.set_coin(name, True, True)

    def select_strategy(self, name) -> bool:
        with self.lock:
            if not any(s.name == name for s in self.strategies):
                return False
            for strat in self.strategies:
                strat.enabled = strat.name == name
            self.note("strategy " + name)
            return True

    def connect_account(self, mode, account, vault, agent_key, fetch=None):
        mode = (mode or "paper").strip()
        account = (account or "").strip()
        vault = (vault or "").strip()
        agent_key = (agent_key or "").strip()
        if mode not in ("paper", "testnet", "live"):
            raise ValueError("mode")
        if mode == "paper":
            with self.lock:
                self._set_paper()
            return {"mode": "paper"}
        if not _addr_ok(account):
            raise ValueError("account")
        if vault and not _addr_ok(vault):
            raise ValueError("vault")
        key = agent_key or self._agent_secret
        if not key:
            raise ValueError("agent key")
        try:
            import eth_account

            wallet = eth_account.Account.from_key(key)
        except Exception:
            raise ValueError("agent key")
        from hlbot.config import info_url
        from hlbot.live import make_sender

        url = info_url(mode)
        if fetch is None:
            from hlbot.catalog import post_info

            def fetch(payload, _url=url):
                return post_info(_url, payload)

        try:
            state = fetch({"type": "clearinghouseState", "user": account})
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(exc.__class__.__name__)
        if not isinstance(state, dict) or "marginSummary" not in state:
            raise ValueError("account state")
        transport = make_sender(url, account, vault, key)
        with self.lock:
            self.mode = mode
            self.info_url = url
            self.account_address = account
            self.vault_address = vault
            self.agent_address = wallet.address
            self._agent_secret = key
            self.transport = transport
            self.armed = False
            self._install_exchange(state)
            self.note("account " + mode)
        return {"mode": mode, "agent": wallet.address}

    def _set_paper(self):
        self.mode = "paper"
        self.info_url = "https://api.hyperliquid.xyz"
        self.transport = None
        self.account_address = ""
        self.vault_address = ""
        self.agent_address = ""
        self._agent_secret = ""
        self.live_equity = None
        self.armed = False
        self.liq = {}
        self.broker = SimBroker(self.paper_equity)
        self.day_start = self.paper_equity
        self.sent = set()
        self.note("paper")

    def _install_exchange(self, state):
        summary = state.get("marginSummary") or {}
        equity = D(str(summary.get("accountValue") or "0"))
        pos = {}
        liq = {}
        for row in state.get("assetPositions") or []:
            item = row.get("position") or {}
            coin = item.get("coin")
            if not coin:
                continue
            sz = D(str(item.get("szi") or "0"))
            if sz == 0:
                continue
            entry = D(str(item.get("entryPx") or "0"))
            pos[coin] = (sz, entry)
            raw_liq = item.get("liquidationPx")
            if raw_liq not in (None, ""):
                liq[coin] = float(raw_liq)
        self.broker.pos = pos
        self.liq = liq
        self.live_equity = equity
        self.day_start = equity
        self.synced = True

    def set_coin(self, name, on, force=False):
        with self.lock:
            coin = self.coins.get(name)
            if coin is None:
                return "unknown"
            if not on:
                self.enabled.discard(name)
                self.forced.discard(name)
                return None
            if coin.delisted:
                return "delisted"
            liquid = self._liquid(name)
            if not liquid and not force:
                return "illiquid"
            self.enabled.add(name)
            if force and not liquid:
                self.forced.add(name)
            else:
                self.forced.discard(name)
            return None

    def enable_liquid(self):
        with self.lock:
            n = 0
            for name, coin in self.coins.items():
                if coin.delisted or not self._liquid(name):
                    continue
                if name not in self.enabled:
                    n += 1
                self.enabled.add(name)
                self.forced.discard(name)
            return n

    def _liquid(self, name) -> bool:
        coin = self.coins.get(name)
        book = self.books.get(name)
        if coin is None or coin.delisted or book is None:
            return False
        return (
            book.spread_bps <= self.limits.max_spread_bps
            and book.day_ntl >= self.limits.min_day_volume_usd
        )

    def upsert_strategy(self, raw):
        from hlbot.strat import build_strategy

        strat = build_strategy(raw)
        with self.lock:
            for i, cur in enumerate(self.strategies):
                if cur.name == strat.name:
                    self.strategies[i] = strat
                    if strat.enabled:
                        self._only(strat.name)
                    return strat
            self.strategies.append(strat)
            if strat.enabled:
                self._only(strat.name)
            return strat

    def _only(self, name):
        for strat in self.strategies:
            strat.enabled = strat.name == name

    def drop_strategy(self, name) -> bool:
        with self.lock:
            before = len(self.strategies)
            self.strategies = [s for s in self.strategies if s.name != name]
            return len(self.strategies) != before

    def enabled_names(self):
        with self.lock:
            return sorted(self.enabled)

    def ingest_books(self, books: dict):
        with self.lock:
            by_api = {}
            for name, coin in self.coins.items():
                by_api.setdefault(name, name)
                if coin.api:
                    by_api.setdefault(coin.api, name)
                if coin.label:
                    by_api.setdefault(coin.label, name)
            for key, book in books.items():
                name = by_api.get(key)
                if name is None:
                    continue
                prev = self.books.get(name)
                if prev and prev.highs:
                    book.highs = prev.highs
                    book.lows = prev.lows
                    book.closes = prev.closes
                    book.bar_ts = prev.bar_ts
                self.books[name] = book

    def touch_mids(self, mids: dict, now_ms: int) -> int:
        # payload is px only. shift the last impact so the spread stays and book_ts moves
        with self.lock:
            by_api = {}
            for name, coin in self.coins.items():
                by_api.setdefault(name, name)
                if coin.api:
                    by_api.setdefault(coin.api, name)
                if coin.label:
                    by_api.setdefault(coin.label, name)
            n = 0
            for key, raw in mids.items():
                name = by_api.get(key)
                book = self.books.get(name) if name else None
                if book is None:
                    continue
                try:
                    mid = float(raw)
                except (TypeError, ValueError):
                    continue
                if mid <= 0:
                    continue
                old = book.mid
                if old > 0:
                    delta = mid - old
                    bid = book.bid + delta
                    ask = book.ask + delta
                else:
                    bid = mid
                    ask = mid
                if bid <= 0 or ask < bid:
                    continue
                book.bid = bid
                book.ask = ask
                book.mark = mid
                book.book_ts = now_ms
                self.mid_ms = now_ms
                n += 1
            return n

    def touch_top(self, key, levels, now_ms) -> bool:
        if not isinstance(levels, (list, tuple)) or len(levels) < 2:
            return False
        bid_row, ask_row = levels[0], levels[1]
        if not isinstance(bid_row, dict) or not isinstance(ask_row, dict):
            return False
        with self.lock:
            name = self._name_for(key)
            book = self.books.get(name) if name else None
            if book is None:
                return False
            try:
                bid = float(bid_row["px"])
                ask = float(ask_row["px"])
                bid_sz = float(bid_row.get("sz") or 0)
                ask_sz = float(ask_row.get("sz") or 0)
            except (KeyError, TypeError, ValueError):
                return False
            if bid <= 0 or ask < bid:
                return False
            book.bid = bid
            book.ask = ask
            if bid_sz > 0:
                book.bid_sz = bid_sz
            if ask_sz > 0:
                book.ask_sz = ask_sz
            book.mark = (bid + ask) / 2
            book.book_ts = now_ms
            self.mid_ms = now_ms
            return True

    def ingest_candles(self, name: str, rows) -> bool:
        from hlbot.feed import apply_candles

        with self.lock:
            book = self.books.get(name)
            if book is None or not rows:
                return False
            apply_candles(book, rows, self.interval)
            return True

    def push_candle(self, key, row) -> bool:
        with self.lock:
            name = self._name_for(key)
            book = self.books.get(name) if name else None
            if book is None or not isinstance(row, dict):
                return False
            mark = row.get("i")
            if mark and mark != self.interval:
                return False
            try:
                t = int(row["t"])
                high = float(row["h"])
                low = float(row["l"])
                close = float(row["c"])
            except (KeyError, TypeError, ValueError):
                return False
            if book.closes and book.bar_ts == t:
                book.highs = book.highs[:-1] + (high,)
                book.lows = book.lows[:-1] + (low,)
                book.closes = book.closes[:-1] + (close,)
                return True
            if book.bar_ts and t < book.bar_ts:
                return False
            book.highs = (book.highs + (high,))[-240:]
            book.lows = (book.lows + (low,))[-240:]
            book.closes = (book.closes + (close,))[-240:]
            book.bar_ts = t
            return True

    def _name_for(self, key):
        if key in self.coins:
            return key
        for name, coin in self.coins.items():
            if key == coin.api or key == coin.label:
                return name
        return None

    def quote_age_ms(self, now_ms=None):
        if self.mid_ms <= 0:
            return None
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return now - self.mid_ms

    def replace_catalog(self, coins):
        fresh = dict(coins)
        with self.lock:
            for name, old in self.coins.items():
                if name not in fresh:
                    fresh[name] = replace(old, delisted=True)
            self.coins = fresh
            alive = {n for n, c in fresh.items() if not c.delisted}
            self.enabled &= alive
            self.forced &= self.enabled

    def public_state(self):
        with self.lock:
            eq = self._equity()
            positions = []
            for name, (szi, entry) in self.broker.pos.items():
                if szi == 0:
                    continue
                positions.append(
                    {"coin": name, "szi": float(szi), "entry": float(entry)}
                )
            now = self.now_ms if self.frozen else int(time.time() * 1000)
            age = None if self.beat_ms <= 0 else now - self.beat_ms
            quote_age = None if self.mid_ms <= 0 else now - self.mid_ms
            arm_ready = bool(self.arm_path and Path(self.arm_path).is_file())
            return {
                "mode": self.mode,
                "armed": self.armed,
                "halted": self.halted,
                "synced": self.synced,
                "equity": float(eq),
                "day_start": float(self.day_start),
                "day_pnl": float(eq - self.day_start),
                "now_ms": now,
                "beat_age_ms": age,
                "quote_age_ms": quote_age,
                "feed": self.feed,
                "interval": self.interval,
                "coins": len(self.coins),
                "enabled": len(self.enabled),
                "positions": positions,
                "log": list(self.log),
                "risk": _risk_public(self.limits),
                "ceilings": _ceilings(),
                "strategies": [s.as_dict() for s in self.strategies],
                "last_error": self.last_error,
                "link": {
                    "address": self.account_address,
                    "vault": self.vault_address,
                    "agent": self.agent_address,
                    "key_loaded": bool(self._agent_secret),
                    "arm_file": arm_ready,
                },
            }

    def public_coins(self):
        with self.lock:
            rows = []
            for name in sorted(self.coins, key=lambda n: (self.coins[n].market, n)):
                coin = self.coins[name]
                book = self.books.get(name)
                pos = self.broker.exposure(name, coin)
                rows.append(
                    {
                        "name": name,
                        "label": coin.label,
                        "market": coin.market,
                        "dex": coin.dex,
                        "hip3": coin.hip3,
                        "delisted": coin.delisted,
                        "max_leverage": coin.max_leverage,
                        "sz_decimals": coin.sz_decimals,
                        "enabled": name in self.enabled,
                        "forced": name in self.forced,
                        "liquid": self._liquid(name),
                        "bid": book.bid if book else None,
                        "ask": book.ask if book else None,
                        "spread_bps": round(book.spread_bps, 2) if book else None,
                        "funding": book.funding if book else None,
                        "day_ntl": book.day_ntl if book else None,
                        "position": float(pos),
                        "bars": len(book.closes) if book else 0,
                        "book_age_ms": (self.now_ms - book.book_ts) if book and self.now_ms else None,
                    }
                )
            return rows


class Journal:
    def __init__(self, path):
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p, check_same_thread=False)
        self.db.execute(
            "create table if not exists events ("
            "id integer primary key, ts integer, kind text, coin text, body text)"
        )
        self.db.commit()

    def write(self, ts, kind, coin, body: str):
        self.db.execute(
            "insert into events (ts, kind, coin, body) values (?, ?, ?, ?)",
            (ts, kind, coin, body),
        )
        self.db.commit()

    def recent(self, n=20):
        cur = self.db.execute(
            "select ts, kind, coin, body from events order by id desc limit ?",
            (n,),
        )
        return cur.fetchall()

    def close(self):
        self.db.close()


def _risk_public(limits):
    from hlbot.config import limits_dict

    return limits_dict(limits)


def _ceilings():
    from hlbot.config import ceilings

    return ceilings()


def _addr_ok(value) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith(("0x", "0X")):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True
