import json
import urllib.request


def _isolated(row) -> bool:
    # strict isolated is the same lock, sometimes without the older flag
    if row.get("onlyIsolated"):
        return True
    return row.get("marginMode") == "strictIsolated"


def parse_catalog(perp_meta, spot_meta, dex_metas=None) -> dict:
    from hlbot.models import Coin

    coins = {}
    for i, row in enumerate(perp_meta.get("universe", [])):
        name = row["name"]
        coins[name] = Coin(
            name=name,
            market="perp",
            asset=i,
            sz_decimals=int(row["szDecimals"]),
            max_leverage=int(row.get("maxLeverage") or 1),
            label=name,
            api=name,
            delisted=bool(row.get("isDelisted")),
            isolated_only=_isolated(row),
            dex="",
        )

    # perpDexs[0] is null. first builder is 110000, then +10000 per dex
    for di, (dex_name, meta) in enumerate(dex_metas or []):
        offset = 110_000 + di * 10_000
        for i, row in enumerate(meta.get("universe", [])):
            name = row["name"]
            coins[name] = Coin(
                name=name,
                market="perp",
                asset=offset + i,
                sz_decimals=int(row["szDecimals"]),
                max_leverage=int(row.get("maxLeverage") or 1),
                label=name,
                api=name,
                delisted=bool(row.get("isDelisted")),
                isolated_only=_isolated(row),
                hip3=True,
                dex=dex_name,
            )

    tokens = {t["index"]: t for t in spot_meta.get("tokens", [])}
    for pair in spot_meta.get("universe", []):
        base = tokens[pair["tokens"][0]]
        quote = tokens[pair["tokens"][1]]
        raw = pair["name"]
        label = f'{base["name"]}/{quote["name"]}'
        key = raw
        if key in coins:
            key = label if label not in coins else f"spot:{raw}"
        coins[key] = Coin(
            name=key,
            market="spot",
            asset=10_000 + int(pair["index"]),
            sz_decimals=int(base["szDecimals"]),
            max_leverage=1,
            label=label,
            api=raw,
            base=base["name"],
            quote=quote["name"],
        )
    return coins


def fetch_catalog(post) -> dict:
    perp = post({"type": "meta"})
    spot = post({"type": "spotMeta"})
    dexs = post({"type": "perpDexs"})
    extras = []
    for row in dexs[1:]:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        extras.append((row["name"], post({"type": "meta", "dex": row["name"]})))
    return parse_catalog(perp, spot, extras)


def post_info(base_url: str, payload: dict, timeout: float = 20):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())
