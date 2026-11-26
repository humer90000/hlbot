import json
import threading
import time

_SOCK = {}
_SUBBED = set()
_CANDLE = {}


def start(session, stop, prices, mids, candles):
    threading.Thread(target=_clock, args=(session, stop), name="hlbot-clock", daemon=True).start()
    threading.Thread(target=_socket, args=(session, stop), name="hlbot-ws", daemon=True).start()
    threading.Thread(target=_books, args=(session, stop, prices, candles), name="hlbot-books", daemon=True).start()
    threading.Thread(target=_mids, args=(session, stop, mids, candles), name="hlbot-mids", daemon=True).start()


def _clock(session, stop):
    nxt = time.monotonic()
    while not stop.is_set():
        nxt += 0.25
        delay = nxt - time.monotonic()
        if delay < 0:
            # don't catch up. a burst of steps stacks entries
            nxt = time.monotonic()
            delay = 0
        if stop.wait(delay):
            return
        try:
            session.step()
        except Exception as exc:
            session.last_error = exc.__class__.__name__


def _ws_url(info_url: str) -> str:
    if info_url.startswith("https://"):
        return "wss://" + info_url[len("https://") :] + "/ws"
    if info_url.startswith("http://"):
        return "ws://" + info_url[len("http://") :] + "/ws"
    return info_url


def _socket(session, stop):
    try:
        import websocket
    except Exception:
        session.feed = "rest"
        return
    holder = _SOCK

    def on_open(sock):
        holder["ws"] = sock
        _SUBBED.clear()
        _CANDLE.clear()
        sock.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        _subscribe_enabled(session, sock)

    def on_message(_sock, message):
        if not message or message[:1] != "{":
            return
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        channel = msg.get("channel")
        data = msg.get("data")
        now = int(time.time() * 1000)
        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids") if isinstance(data.get("mids"), dict) else data
            if isinstance(mids, dict):
                session.touch_mids(mids, now)
                session.feed = "ws"
            return
        if channel == "bbo" and isinstance(data, dict):
            session.touch_top(data.get("coin") or "", data.get("bbo"), now)
            session.feed = "ws"
            return
        if channel == "candle" and isinstance(data, dict):
            session.push_candle(data.get("s") or "", data)
            session.feed = "ws"

    while not stop.is_set():
        try:
            app = websocket.WebSocketApp(_ws_url(session.info_url), on_open=on_open, on_message=on_message)
            holder["ws"] = app
            app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:
            session.last_error = exc.__class__.__name__
            session.feed = "rest"
        if stop.wait(2):
            break
    sock = holder.get("ws")
    close = getattr(sock, "close", None)
    if close:
        try:
            close()
        except Exception:
            pass


def _books(session, stop, prices, candles):
    while not stop.is_set():
        try:
            prices(session)
        except Exception as exc:
            session.last_error = exc.__class__.__name__
        try:
            candles(session)
        except Exception as exc:
            session.last_error = exc.__class__.__name__
        _account(session)
        if stop.wait(30):
            return


def _subscribe_enabled(session, sock):
    want = session.interval
    enabled = set(session.enabled_names())
    for name, old in list(_CANDLE.items()):
        if name in enabled and old == want:
            continue
        coin = session.coins.get(name)
        api = (coin.api or name) if coin else name
        try:
            if name not in enabled:
                sock.send(json.dumps({"method": "unsubscribe", "subscription": {"type": "bbo", "coin": api}}))
            sock.send(
                json.dumps(
                    {"method": "unsubscribe", "subscription": {"type": "candle", "coin": api, "interval": old}}
                )
            )
        except Exception:
            return
        _CANDLE.pop(name, None)
        _SUBBED.discard(name)
    for name in enabled:
        if name in _SUBBED and _CANDLE.get(name) == want:
            continue
        coin = session.coins.get(name)
        if coin is None:
            continue
        api = coin.api or name
        try:
            sock.send(json.dumps({"method": "subscribe", "subscription": {"type": "bbo", "coin": api}}))
            sock.send(
                json.dumps(
                    {"method": "subscribe", "subscription": {"type": "candle", "coin": api, "interval": want}}
                )
            )
        except Exception:
            return
        _CANDLE[name] = want
        _SUBBED.add(name)


def _mids(session, stop, mids, candles):
    while not stop.is_set():
        sock = _SOCK.get("ws")
        if sock is not None:
            _subscribe_enabled(session, sock)
        if session.pull_candles:
            session.pull_candles = False
            try:
                candles(session)
            except Exception as exc:
                session.last_error = exc.__class__.__name__
        age = session.quote_age_ms()
        if session.feed != "ws" or age is None or age > 2000:
            try:
                mids(session)
                if session.feed != "ws":
                    session.feed = "rest"
            except Exception as exc:
                session.last_error = exc.__class__.__name__
        if stop.wait(1):
            return


def _account(session):
    if session.mode not in ("live", "testnet") or not session.account_address:
        return
    from hlbot.catalog import post_info

    try:
        state = post_info(
            session.info_url,
            {"type": "clearinghouseState", "user": session.account_address},
        )
    except Exception as exc:
        session.last_error = exc.__class__.__name__
        session.synced = False
        return
    if not isinstance(state, dict) or "marginSummary" not in state:
        session.synced = False
        return
    with session.lock:
        day = session.day_start
        session._install_exchange(state)
        session.day_start = day
        session.synced = True
