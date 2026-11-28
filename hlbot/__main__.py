import argparse
import os
import secrets
import threading
import time

from hlbot.catalog import fetch_catalog, post_info
from hlbot.config import check_bind, load_app
from hlbot.feed import books_from_perp, books_from_spot, candle_body
from hlbot.guard import heartbeat_due
from hlbot.session import Session


def _session(cfg, state_dir, transport):
    journal = os.path.join(state_dir, "journal.db")
    return Session(
        limits=cfg.limits,
        coins={},
        strategies=cfg.strategies,
        mode=cfg.mode,
        equity=cfg.equity,
        info_url=cfg.info_url,
        arm_path=cfg.arm_file,
        state_dir=state_dir,
        journal_path=journal,
        transport=transport,
        beat_max_ms=10_000,
        interval=cfg.interval,
    )


def _transport(cfg):
    if cfg.mode not in ("testnet", "live"):
        return None
    key = os.environ.get("HL_AGENT_KEY", "")
    if not key or not cfg.account:
        print("no HL_AGENT_KEY or account, orders will not leave the box", flush=True)
        return None
    from hlbot.live import make_sender

    return make_sender(cfg.info_url, cfg.account, cfg.vault, key)


def _load_markets(session):
    def post(payload):
        return post_info(session.info_url, payload)

    coins = fetch_catalog(post)
    session.replace_catalog(coins)
    session.catalog_ms = int(time.time() * 1000)
    print(f"markets {len(coins)}", flush=True)
    _refresh_prices(session, post)
    session.seed_coins()


def _sync_catalog(session, post):
    from hlbot.catalog import reload_due

    now = int(time.time() * 1000)
    if not reload_due(getattr(session, "catalog_ms", 0), now):
        return
    try:
        coins = fetch_catalog(post)
    except Exception as exc:
        session.last_error = exc.__class__.__name__
        return
    before = sum(1 for c in session.coins.values() if not c.delisted)
    session.replace_catalog(coins)
    session.catalog_ms = now
    after = sum(1 for c in session.coins.values() if not c.delisted)
    if before and after != before:
        session.note(f"markets {after}")
        print(f"markets {after}", flush=True)


def _refresh_prices(session, post=None):
    if post is None:
        def post(payload):
            return post_info(session.info_url, payload)

    _sync_catalog(session, post)
    now = int(time.time() * 1000)
    books = {}
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    books.update(books_from_perp(meta, ctxs, now))
    dexs = post({"type": "perpDexs"})
    for row in dexs[1:]:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        try:
            dex_meta, dex_ctx = post({"type": "metaAndAssetCtxs", "dex": row["name"]})
        except Exception:
            continue
        books.update(books_from_perp(dex_meta, dex_ctx, now))
    spot = post({"type": "spotMetaAndAssetCtxs"})
    books.update(books_from_spot(spot[1], now))
    session.ingest_books(books)
    return len(books)


def _refresh_mids(session):
    raw = post_info(session.info_url, {"type": "allMids"})
    if isinstance(raw, dict) and "mids" in raw and isinstance(raw["mids"], dict):
        raw = raw["mids"]
    if not isinstance(raw, dict):
        return 0
    return session.touch_mids(raw, int(time.time() * 1000))


def _refresh_candles(session):
    names = session.enabled_names()
    if not names:
        return 0
    cursor = getattr(session, "candle_cursor", 0)
    batch = [names[(cursor + i) % len(names)] for i in range(min(4, len(names)))]
    session.candle_cursor = (cursor + len(batch)) % len(names)
    now = int(time.time() * 1000)
    got = 0
    for name in batch:
        coin = session.coins.get(name)
        if coin is None:
            continue
        rows = post_info(session.info_url, candle_body(coin.api or name, now, session.interval))
        if session.ingest_candles(name, rows):
            got += 1
    return got


def cmd_run(cfg, expose, state_dir):
    host = check_bind(cfg.host, expose)
    session = _session(cfg, state_dir, _transport(cfg))
    try:
        _load_markets(session)
    except Exception as exc:
        print(f"market load failed ({exc.__class__.__name__}), console is up anyway", flush=True)
    token = os.environ.get("HLBOT_TOKEN") or secrets.token_hex(16)
    from hlbot.web import create_app

    app = create_app(session, token)
    stop = threading.Event()
    from hlbot.stream import start

    start(session, stop, _refresh_prices, _refresh_mids, _refresh_candles)
    print(f"{cfg.mode} http://{host}:{cfg.port}", flush=True)
    print(f"token {token}", flush=True)
    import uvicorn

    uvicorn.run(app, host=host, port=cfg.port, log_level="warning")


def cmd_guard(path, max_age_ms, now_ms):
    beat = int(open(path, encoding="utf-8").read().strip())
    if heartbeat_due(now_ms, beat, max_age_ms, False):
        print("stale", now_ms - beat)
        return 2
    print("ok", now_ms - beat)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hlbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--expose", action="store_true")
    run.add_argument("--state", default="state")

    chk = sub.add_parser("check")
    chk.add_argument("--config", required=True)

    grd = sub.add_parser("guard")
    grd.add_argument("--heartbeat", required=True)
    grd.add_argument("--max-age-ms", type=int, default=10_000)

    args = p.parse_args(argv)
    if args.cmd == "check":
        cfg = load_app(args.config)
        print(f"ok {cfg.mode} strategies={len(cfg.strategies)} leverage<={cfg.limits.max_leverage}")
        return 0
    if args.cmd == "guard":
        import time

        return cmd_guard(args.heartbeat, args.max_age_ms, int(time.time() * 1000))
    if args.cmd == "run":
        cfg = load_app(args.config)
        cmd_run(cfg, args.expose, args.state)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
