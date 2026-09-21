# potd-trader

Buys the [0xinsider Pick of the Day](https://0xinsider.com/pick-of-the-day) on your own
Polymarket account, one small market order per pick, at most once per pick.

It runs on your machine. Your Polymarket key signs orders locally and never leaves the process.
0xinsider only ever sees your 0xinsider API key, and only to hand you the pick.

The setup walk-through:
**[docs.0xinsider.com/guides/auto-buy-the-pick](https://docs.0xinsider.com/guides/auto-buy-the-pick)**.
API contract: [Get Pick of the Day](https://docs.0xinsider.com/api-reference/endpoint/get-pick-of-the-day).

## What leaves your machine

| Goes to | What | Why |
| --- | --- | --- |
| `api.0xinsider.com` | your 0xinsider API key | to read today's picks (Pro) |
| `clob.polymarket.com` | a signed order and the API credentials Polymarket's SDK derives from your key | to place the buy |
| `gamma-api.polymarket.com` | the pick's token id | to read the market's kickoff and status |
| `polymarket.com/api/geoblock` | nothing (your IP, as any request does) | Polymarket's own "may this region trade?" check |

Your private key is read from `~/.potd-trader/.env`, held in memory by the official
[`polymarket-client`](https://pypi.org/project/polymarket-client/) SDK, and used to sign. There is
no 0xinsider endpoint that accepts a wallet key. The whole program is 7 short files under
`src/potd_trader/`; read them.

## One command

```bash
uv tool install git+https://github.com/0xinsider/potd-trader && potd-trader init
```

`init` asks 4 questions in the terminal (the two keys are typed with echo off), writes
`~/.potd-trader/.env` at mode 600, then runs `status` and a dry run so you see what it would buy.
It ends in dry-run mode unless you type the phrase it asks for. No `uv` yet?
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).

Then:

```bash
potd-trader live on    # type "spend real money" to confirm; `live off` reverts
potd-trader watch      # keep it running: wake at each release, buy, sleep
```

## Set it up with an AI agent

Paste this into Claude Code, Codex, Cursor, or any agent that reads `AGENTS.md`:

```text
Clone https://github.com/0xinsider/potd-trader, read its AGENTS.md, and follow the
"Setting it up for a person" steps in order. Install it, create .env from .env.example,
then tell me which 3 values to fill in and where each comes from. Never ask me to paste
a key into this chat and never print one. When I say the file is filled in, run
`potd-trader status` and a dry `potd-trader run`, and explain what it would buy. Leave
LIVE=no; I will change it myself.
```

The agent stops before anything spends money. `AGENTS.md` holds the rules it works under;
`CLAUDE.md` points Claude Code at the same file.

## Setup by hand

The repository-checkout layout: a `.env` next to the code takes precedence over `~/.potd-trader/.env`.
You need Python 3.12+, [uv](https://docs.astral.sh/uv/), a 0xinsider Pro key, and a Polymarket
account holding some pUSD.

```bash
git clone https://github.com/0xinsider/potd-trader && cd potd-trader
uv sync
cp .env.example .env && chmod 600 .env
```

Fill in `.env`:

- `OXINSIDER_API_KEY`: a live key from [0xinsider.com/developers](https://0xinsider.com/developers).
- `POLYMARKET_PRIVATE_KEY`: your account's signer key. Email or Google login: polymarket.com,
  Settings, **Export private key** (Polymarket's
  [help article](https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key)).
  MetaMask or Rabby login: the key from that wallet app.
- `POLYMARKET_WALLET_ADDRESS`: the address in your polymarket.com profile menu. It holds the pUSD.

Then check everything without buying anything:

```bash
uv run potd-trader status   # region, wallet, approvals, pUSD balance
uv run potd-trader run      # dry run: reads the pick, prints what it WOULD buy
```

`run` is a dry run until `.env` says `LIVE=yes`. Every other value counts as no.

## Run it for real

```bash
potd-trader live on    # or set LIVE=yes in the file by hand
```

```bash
uv run potd-trader run     # once: buy today's released picks, then exit
uv run potd-trader watch   # keep running: wake at each release, buy, sleep
```

`watch` reads the schedule the API returns (`Retry-After`, `scheduled_picks[].release_at`) and
sleeps until the next instant a pick can change. It never hammers the API. Leave it in a terminal,
a `tmux` window, or the Docker image below.

```bash
docker build -t potd-trader .
docker run --rm --env-file ~/.potd-trader/.env -v "$HOME/.potd-trader:/app/data" potd-trader
```

## What it does, per pick

Top to bottom, in `src/potd_trader/trader.py`:

1. Skip if the pick is settled, has no CLOB token, or the game started.
2. Skip if the ledger already holds an order for this pick.
3. Skip if `DAILY_CAP_USD` would be exceeded.
4. Read the Polymarket market: skip if closed, or if kickoff is inside `KICKOFF_BUFFER_MINUTES`.
5. Quote the live book for `STAKE_USD`: skip if the price is above `MAX_PRICE` or more than
   `MAX_SLIPPAGE_PCT` above the price the pick was published at.
6. Otherwise: write the ledger entry, place one Fill-and-Kill market BUY with that quote as the
   ceiling, record the exchange's answer.

Before the first live order of a run it also checks Polymarket's geoblock, that your wallet's
trading approvals are set, and that the balance covers the stakes.

## The ledger

`~/.potd-trader/ledger.json` records every live order with the pick's date, rank, token, stake, and the
exchange's answer. A pick with an entry in `submitting`, `accepted` or `unknown` is never bought
again. `unknown` means the post threw before an answer arrived: check Polymarket, Activity, and
delete the entry by hand if no order exists.

```bash
uv run potd-trader ledger
```

## Approvals

`potd-trader status` reports whether the wallet's trading approvals are set. An account that has
traded on polymarket.com usually has them; a fresh wallet does not. `potd-trader setup` sets them
gaslessly through a Relayer API key (polymarket.com, Settings, API Keys). It never places an order.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIVE` | `no` | exactly `yes` spends money |
| `STAKE_USD` | `5` | pUSD per pick |
| `MAX_PRICE` | `0.925` | never buy above this probability |
| `MAX_SLIPPAGE_PCT` | `3` | skip when the book is this far above the pick's price |
| `DAILY_CAP_USD` | `25` | per product day, across picks; `0` disables |
| `KICKOFF_BUFFER_MINUTES` | `5` | no buys this close to kickoff |
| `MIN_RANKS` / `MAX_RANKS` | `1` / `6` | which ranked slots to buy |
| `LEDGER_PATH` | `~/.potd-trader/ledger.json` | where orders are recorded |
| `POTD_TRADER_HOME` | `~/.potd-trader` | where `.env` and the ledger live |
| `WATCH_IDLE_MINUTES` | `30` | `watch` cadence when nothing is scheduled |

## Read this before LIVE=yes

- This places real orders with real money and cannot be undone. A Fill-and-Kill order fills what
  the book offers up to the ceiling and cancels the rest; partial fills happen.
- The Pick of the Day is a published record, not a promise. Its hit rate and ROI are on the
  [track record](https://0xinsider.com/pick-of-the-day/verify), computed on a $100 stake at the
  published price; your fills will differ.
- Polymarket is not available in every region. The tool refuses to buy when Polymarket says so.
- Keep `~/.potd-trader/.env` private (`init` writes it at mode 600). Anyone with that file can trade from your account.
- MIT licensed, no warranty. 0xinsider does not operate, monitor, or custody anything here.

## Contributing

Issues and pull requests at [github.com/0xinsider/potd-trader](https://github.com/0xinsider/potd-trader).
`uv run ruff check . && uv run mypy src` before you push.
