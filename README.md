# Local Hyperliquid Tradebot

A Hyperliquid bot that runs on your computer. You manage it in the browser: coins, strategies, risk, the account. The page is only on this machine (`127.0.0.1`), so the setup does not leave the box.

![desk after login](desk.png)

Paper mode is the default. It reads the real market, but the fills stay inside the program. Nothing is sent to the exchange until you connect an agent wallet and press Arm. For a live account you also need an empty file named `ARM` in the project folder. That file is not a password. The program only checks that it exists.

The main wallet key does not belong here. An agent key can sign orders. It cannot withdraw.

## Install

Python 3.12 or newer.

```
py -3 -m venv .venv
.venv\Scripts\activate
pip install -e .
```

On Linux or Mac, activate with `source .venv/bin/activate`. Use `python3` if you don't have `py`.

Copy the example so you can edit your own file:

```
copy config.example.toml config.toml
```

(`cp config.example.toml config.toml` on Linux or Mac.)

## Run

```
py -3 -m hlbot run --config config.toml
```

The terminal prints the address and a token, something like:

```
markets 929
paper http://127.0.0.1:8787
token 3f9c...
```

Open the address and paste the token. If you want a fixed token, set it before start.

PowerShell:

```
$env:HLBOT_TOKEN="desk-token"
py -3 -m hlbot run --config config.toml
```

cmd.exe is `set HLBOT_TOKEN=desk-token`. Bash is `export HLBOT_TOKEN=desk-token`.

The page listens on `127.0.0.1` only. Another computer on the network cannot open it. `--expose` removes that limit. Don't use it on a machine other people can reach. The token is the only login.

Check the file without starting the bot:

```
py -3 -m hlbot check --config config.toml
```

## On the page

The buttons, and when to press them, are in [admin.html](admin.html). Open that file in a browser.

BTC and ETH are already on. The other names are off. Leave it like that until the page looks right. A thin book can be refused. The button will ask before it turns a thin name on anyway.

Five strategies come with the example config. EMA 9/21 is the one that is enabled. RSI, MACD, Bollinger and Donchian are there, but off. Press Use to switch. Only one stays on.

You can add your own from the form (EMA, RSI, MACD, Bollinger, Donchian). Advanced is the raw JSON, if the form is not enough. A new strategy is saved off.

Hover a field when you don't remember what it does.

Arm means new entries are allowed. In paper they still fill on this computer. Disarm stops new entries. Positions that are already open stay until you flatten, or a stop fills.

Halt also stops new entries. Exits still go. Resume turns entries back on.

Flatten closes what is open, reduce-only.

The numbers at the top (equity, day pnl, quote age, loop) are the paper account until you connect a real one. "live" next to the quote means the price is fresh. It does not mean the account is live.

## Risk

Values in `config.toml` can only get tighter. A looser number is rejected and the old limits stay.

The program will not go past this, even if the file says so:

- each coin starts at 1x. a perp can be raised up to that coin's own exchange maximum, from the coin pane. the maximum shows a warning
- one order, 3x equity, and only as far as the leverage allows
- one coin, 3x
- gross exposure, 3x equity
- daily loss, 10%
- slippage, 8 bps

The example file is already inside those lines. Leverage starts at 1, one order 10%, daily loss 3%, day volume at least 5M, spread at most 8 bps.

1x keeps the size inside equity. A higher number on a coin borrows, and the exchange is set to that number before an entry. The slider stops at the maximum that coin has on the exchange, and that top setting is marked because the liquidation sits close to the price. Cross is the default. Isolated is a switch on the risk tab. A coin that only allows isolated stays isolated either way. If a position already open is closer to its liquidation than the liq distance, a new entry is skipped. A position that is already open is updated on the next loop.

Opens under about $10 are skipped. Closes are not. Spot cannot be shorted.

If the quote is older than `stale_ms` (1500 in the example), a new entry is refused. Closes are still allowed.

## A real account

Do the testnet first.

On Hyperliquid, create an API agent wallet. Keep the agent private key. You also need the main account address (`0x` and 40 hex characters). Leave the main private key out of this bot, and out of the chat, and out of the config file.

Then in the page, Account:

- mode: Testnet or Live
- account: that `0x` address
- vault: only if you use a vault
- agent key
- Connect

The key stays in memory for this run. It is not written to the config, the log, or the page. After a restart you connect again.

Connect does not arm anything. For live, make the empty file before you press Arm:

```
New-Item ARM -ItemType File
```

On Linux or Mac, `touch ARM`. Testnet does not need the file. Live without the file will not arm.

There is a second way. Put this in `config.toml`:

```
mode = "live"
account = "0xYOUR_ADDRESS"
```

and start with the agent key in the environment:

```
$env:HL_AGENT_KEY="0xYOUR_AGENT_KEY"
py -3 -m hlbot run --config config.toml
```

You still create `ARM` before Arm. The Account tab is easier, because the key does not stay in the shell history.

`ARM`, `state/` and `*.db` are in `.gitignore`. Don't commit them. Don't commit a config that has a key in it. The example config does not.

## Strategy example

Blank `coins` means every name you enabled on the page.

```toml
[[strategy]]
name = "ema"
kind = "ema"
enabled = true
coins = []
fast = 9
slow = 21
atr_len = 14
stop_atr = 1.8
take_r = 3
risk_frac = 0.004
max_hold_bars = 16
funding_block = 0.0003
```

`risk_frac` cannot be higher than 0.02. The name `guard` is already used by the bot.

A Donchian rule list, left off:

```toml
[[strategy]]
name = "btc_break"
kind = "rules"
enabled = false
coins = ["BTC"]
risk_frac = 0.004
stop_atr = 1.8
take_r = 3
rules = [
  { side = "buy", when = "close_gt_donchian_high", donchian = 20 },
  { side = "sell", when = "close_lt_donchian_low", donchian = 20 },
]
```

The bar size starts at 15 minutes. On Strategies there is a dropdown if you want 1m, 5m, 1h, 4h, 1d, or the others in that list. Switching throws away the bars already loaded. The strategy still only asks. Size, spread, a stale book and the daily loss limit can still drop the order.

## Other

`state/journal.db` is a small local log. `state/heartbeat` is the last time the loop ran.

```
py -3 -m hlbot guard --heartbeat state/heartbeat
```

This only prints ok or stale. It does not close positions.

Tests, if you installed with the dev extra (`pip install -e ".[dev]"`):

```
py -3 -m pytest
```
