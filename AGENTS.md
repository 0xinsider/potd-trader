# potd-trader agent contract

This repository buys the 0xinsider Pick of the Day on a person's own Polymarket account. You
are working in it on behalf of that person. Real money is at stake and the two secrets in
`.env` control a wallet. Read this whole file before any command.

## What it is

Six files under `src/potd_trader/`:

| File | Owns |
| --- | --- |
| `oxinsider.py` | `GET /api/v1/pick-of-the-day`: the 200 slate, the 404 `retry_at` schedule, 304, 429/503 |
| `polymarket.py` | geoblock, market facts, the book quote, and the one write: a Fill-and-Kill market BUY |
| `trader.py` | the guards, top to bottom, then `execute()` |
| `ledger.py` | `data/ledger.json`: the entry is written BEFORE the post, so a pick is bought at most once |
| `config.py` | `.env` to `Settings`; both keys are `SecretStr` |
| `cli.py` | `status`, `setup`, `run`, `watch`, `ledger` |

Contracts to read before changing a call: the endpoint,
<https://docs.0xinsider.com/api-reference/endpoint/get-pick-of-the-day>; the SDK,
<https://docs.polymarket.com/getting-started/python> (installed pin: `polymarket-client>=0.10,<0.11`).
Read the installed package with `uv run python -c "import inspect, polymarket; ..."` rather than
recalling a signature.

## Rules that never bend

1. Never print, echo, log, cat, or paste `.env`, `POLYMARKET_PRIVATE_KEY`, `OXINSIDER_API_KEY`,
   or a relayer key. Not in a command, not in a message, not in a commit. If a command's output
   would contain one, do not run it.
2. Never ask the person to paste a key into the chat. Tell them which value goes on which line
   of `.env` and where it comes from; they type it into the file themselves.
3. Never set `LIVE=yes`. That edit is the person's, made by hand, in their editor. Never run
   `run` or `watch` while `.env` says `LIVE=yes` unless the person typed that instruction in this
   session.
4. Never commit `.env` or `data/`. Both are in `.gitignore`; keep them there.
5. Never edit `data/ledger.json` except when the person asks after checking Activity on
   polymarket.com. A ledger entry is the only thing standing between a crash and a double buy.
6. Never add a network destination. The program talks to `api.0xinsider.com`,
   `clob.polymarket.com`, `gamma-api.polymarket.com`, and `polymarket.com/api/geoblock`, and the
   README's "What leaves your machine" table names all four. A new one changes that promise.
7. Never widen a guard to make a buy happen. `MAX_PRICE`, `MAX_SLIPPAGE_PCT`, `DAILY_CAP_USD`,
   and `KICKOFF_BUFFER_MINUTES` are the person's settings; explain a skip, do not route around it.
8. No emojis anywhere. No tests: verification is the dry run against real routes.

## Setting it up for a person

The person wants it running in minutes. Do these in order and stop where it says stop.

1. `uv sync` (needs Python 3.12+ and `uv`; install `uv` from <https://docs.astral.sh/uv/> if
   missing).
2. `cp .env.example .env && chmod 600 .env`.
3. Tell them the 3 values to fill in, in one short message:
   - `OXINSIDER_API_KEY`: a live key (`oxi_sk_live_...`) from <https://0xinsider.com/developers>.
     The pick endpoint needs Pro.
   - `POLYMARKET_PRIVATE_KEY`: their signer key. Email or Google login: polymarket.com, Settings,
     Export private key (<https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key>).
     MetaMask or Rabby login: the key from that wallet app.
   - `POLYMARKET_WALLET_ADDRESS`: the address in their polymarket.com profile menu. It holds the
     pUSD. It is not the signer address.
   Then stop and wait for them to say the file is filled in.
4. `uv run potd-trader status`: report the geoblock result, wallet, wallet type, approvals, and
   balance in your own words. If approvals are MISSING, point at the Approvals section of the
   README (`potd-trader setup` with a Relayer API key) and stop.
5. `uv run potd-trader run` with `LIVE=no`: a dry run. Explain each `BUY` and `SKIP` line. A 404
   with `Earliest change:` is the schedule, not an error; say when the next pick can appear.
6. Stop. Say that `LIVE=yes` in `.env` is their edit, that `STAKE_USD` starts at 5, and that
   `uv run potd-trader watch` keeps it running. Do not make the edit and do not start `watch`.

## Verification

```bash
uv run ruff check . && uv run mypy src
uv run potd-trader run        # LIVE=no: exit 0 and a plan line per released pick
```

A change to `polymarket.py` is verified against the installed SDK's real signatures and a real
book (`PublicClient().estimate_market_price(...)` on a live token), never against a stub.

## Commits

Conventional Commit types (`feat:`, `fix:`, `docs:`, `chore:`). Say what the person sees, not
how it works inside. Keep `README.md` and the guide at
<https://docs.0xinsider.com/guides/auto-buy-the-pick> in step with any setting or command you add.
