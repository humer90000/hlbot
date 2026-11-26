from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "max_leverage": 1,
    "max_order_frac": 0.10,
    "max_coin_frac": 0.25,
    "max_gross_frac": 1.0,
    "max_daily_loss_frac": 0.03,
    "max_slippage_bps": 8,
    "min_liq_distance": 0.15,
    "stale_ms": 1500,
    "max_entry_deviation": 0.01,
    "book_participation": 0.10,
    "max_spread_bps": 8,
    "min_day_volume_usd": 5_000_000,
}

# resolve_limits refuses anything past these. the desk can only go tighter.
_CAP = {
    # leftover desk-wide knob. the page sets leverage per coin, starting at 1.
    "max_leverage": ("max", 3),
    # 1x still clamps notional to equity. the fraction ceilings stay here.
    "max_order_frac": ("max", 3),
    "max_coin_frac": ("max", 3),
    "max_gross_frac": ("max", 3),
    "max_daily_loss_frac": ("max", 0.10),
    "max_slippage_bps": ("max", 8),
    "min_liq_distance": ("min", 0.15),
    "stale_ms": ("max", 1500),
    "max_entry_deviation": ("max", 0.01),
    "book_participation": ("max", 0.10),
    "max_spread_bps": ("max", 12),
    "min_day_volume_usd": ("min", 1_000_000),
}

_TOP = {"mode", "host", "port", "equity", "arm_file", "account", "vault", "interval", "risk", "strategy"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_leverage: float
    max_order_frac: float
    max_coin_frac: float
    max_gross_frac: float
    max_daily_loss_frac: float
    max_slippage_bps: float
    min_liq_distance: float
    stale_ms: int
    max_entry_deviation: float
    book_participation: float
    max_spread_bps: float
    min_day_volume_usd: float


@dataclass
class AppConfig:
    mode: str
    host: str
    port: int
    equity: float
    arm_file: str
    account: str
    vault: str
    limits: Limits
    strategies: list
    info_url: str
    interval: str = "15m"


def ceilings() -> dict:
    return {key: bound for key, (_how, bound) in _CAP.items()}


def limits_dict(limits: Limits) -> dict:
    return {
        "max_leverage": limits.max_leverage,
        "max_order_frac": limits.max_order_frac,
        "max_coin_frac": limits.max_coin_frac,
        "max_gross_frac": limits.max_gross_frac,
        "max_daily_loss_frac": limits.max_daily_loss_frac,
        "max_slippage_bps": limits.max_slippage_bps,
        "min_liq_distance": limits.min_liq_distance,
        "stale_ms": limits.stale_ms,
        "max_entry_deviation": limits.max_entry_deviation,
        "book_participation": limits.book_participation,
        "max_spread_bps": limits.max_spread_bps,
        "min_day_volume_usd": limits.min_day_volume_usd,
    }


def resolve_limits(raw) -> Limits:
    raw = dict(raw or {})
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown risk field: " + ", ".join(sorted(unknown)))
    merged = dict(DEFAULTS)
    merged.update(raw)
    for key, value in merged.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ConfigError(f"{key} not a number")
        if float(value) <= 0:
            raise ConfigError(f"{key} {value} below floor 0")
        how, bound = _CAP[key]
        if how == "max" and float(value) > bound:
            raise ConfigError(f"{key} {value} above ceiling {bound}")
        if how == "min" and float(value) < bound:
            raise ConfigError(f"{key} {value} below floor {bound}")
    if merged["max_leverage"] <= 0:
        raise ConfigError("max_leverage")
    if merged["stale_ms"] < 100:
        raise ConfigError("stale_ms below 100")
    if not (
        0 < merged["max_order_frac"] <= merged["max_coin_frac"] <= merged["max_gross_frac"]
    ):
        raise ConfigError("order/coin/gross fractions out of order")
    # mm gap is about 1/(2*lev). min_liq_distance has to sit inside that
    room = 1 / (2 * float(merged["max_leverage"]))
    if float(merged["min_liq_distance"]) > room + 1e-12:
        raise ConfigError("min_liq_distance leaves no room at this leverage")
    merged["stale_ms"] = int(merged["stale_ms"])
    merged["min_day_volume_usd"] = float(merged["min_day_volume_usd"])
    return Limits(**merged)


def check_bind(host: str, expose: bool) -> str:
    if host in ("127.0.0.1", "localhost", "::1"):
        return host
    if expose:
        return host
    raise ConfigError(f"refusing bind {host}")


def info_url(mode: str) -> str:
    if mode == "testnet":
        return "https://api.hyperliquid-testnet.xyz"
    return "https://api.hyperliquid.xyz"


def load_app(path) -> AppConfig:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(data) - _TOP
    if unknown:
        raise ConfigError("unknown field: " + ", ".join(sorted(unknown)))
    mode = data.get("mode", "paper")
    if mode not in ("paper", "backtest", "testnet", "live"):
        raise ConfigError("mode")
    host = data.get("host", "127.0.0.1")
    port = int(data.get("port", 8787))
    if not 1 <= port <= 65535:
        raise ConfigError("port")
    equity = float(data.get("equity", 10_000))
    if equity <= 0:
        raise ConfigError("equity")
    from hlbot.feed import INTERVALS

    interval = str(data.get("interval") or "15m")
    if interval not in INTERVALS:
        raise ConfigError("interval")
    from hlbot.strat import build_strategy

    rows = data.get("strategy", [])
    if not isinstance(rows, list):
        raise ConfigError("strategy")
    strategies = [build_strategy(row) for row in rows]
    return AppConfig(
        mode=mode,
        host=host,
        port=port,
        equity=equity,
        arm_file=str(data.get("arm_file", "ARM")),
        account=str(data.get("account", "")),
        vault=str(data.get("vault", "")),
        limits=resolve_limits(data.get("risk", {})),
        strategies=strategies,
        info_url=info_url(mode),
        interval=interval,
    )
