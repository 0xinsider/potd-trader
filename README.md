# potd-trader

Buy the [0xinsider Pick of the Day](https://0xinsider.com/pick-of-the-day) on your own
Polymarket account, with a small stake and explicit spending limits. Dry-run is the default.
Your wallet key signs locally through the official Polymarket SDK.

[Setup guide](https://docs.0xinsider.com/guides/auto-buy-the-pick) ·
[API contract](https://docs.0xinsider.com/api-reference/endpoint/get-pick-of-the-day) ·
[Release notes](CHANGELOG.md)

## Install a version with locked dependencies

Python 3.12.4+ and [uv](https://docs.astral.sh/uv/) are required. Supported systems: Windows
with x64 Python, macOS and Linux, including WSL. Keep the configuration and ledger on a local
filesystem, outside OneDrive, other synced folders and network drives.

```bash
git clone --branch v0.3.4 --depth 1 https://github.com/0xinsider/potd-trader potd-trader-src
cd potd-trader-src
uv sync --locked
uv run --locked potd-trader init
```

On Windows, install Git and uv from PowerShell, then reopen your terminal:

```powershell
winget install --id Git.Git --exact
winget install --id astral-sh.uv --exact
```

In the new PowerShell window, use a local folder under your Windows profile:

```powershell
Set-Location $env:USERPROFILE
git clone --branch v0.3.4 --depth 1 https://github.com/0xinsider/potd-trader potd-trader-src
Set-Location potd-trader-src
uv sync --locked --python 3.12
uv run --locked potd-trader init
```

uv installs Python if needed. WSL is optional; native Windows uses the same commands and ledger
format. Use x64 Python on Windows; native ARM64 Python is not covered by this release.

The [v0.3.4 release](https://github.com/0xinsider/potd-trader/releases/tag/v0.3.4) identifies the
merged commit and includes SHA-256 checksums for its package artifacts. For an immutable source
pin, check out that full commit instead of a moving branch. `uv sync --locked` installs the
versions and artifact hashes in the checked-in `uv.lock` and refuses a stale lockfile. Installing
an unversioned Git URL or a package without its lockfile does not reproduce that environment.

`init` creates a new `potd-trader/` folder inside the checkout. It asks five questions, hides both
keys as you type, creates a private folder, and runs account checks and a forced dry run.
On macOS/Linux the folder is mode 700 and `.env` is mode 600. On Windows, the new folder limits
access to your user and administrators; files inherit its permissions. An inherited `LIVE=yes`
cannot make this setup run buy. Only the final confirmation
can enable subsequent live commands. An existing folder is never overwritten. The last two
questions set your unit size per pick and daily cap; the cap prompt shows the cost of all 10.

From that new folder, `uv` finds the project in its parent directory:

```bash
cd potd-trader
uv run --locked potd-trader run --dry-run
uv run --locked potd-trader live on     # type "spend real money" yourself
uv run --locked potd-trader watch
```

## What leaves your machine

| Goes to | What | Why |
| --- | --- | --- |
| `api.0xinsider.com` | your 0xinsider API key | read today's Pro picks |
| `clob.polymarket.com` | signed authentication messages, derived API credentials, signed orders, and token IDs | authenticate, check balance, quote, and buy |
| `gamma-api.polymarket.com` | token IDs | read market identity, kickoff, tick size, and status |
| `polymarket.com/api/geoblock` | your IP, as with any request | check trading eligibility for your location |
| `polygon.drpc.org` | public wallet, token, and contract addresses in RPC calls | SDK wallet and approval checks |
| `relayer-v2.polymarket.com` | wallet addresses; relayer credentials and signed approval requests when setup is requested | SDK wallet and relayer operations |

These are runtime destinations, not package-installation hosts. The reviewed SDK is pinned to
`polymarket-client==0.10.0`. The private key remains inside the signing process; there is no
0xinsider endpoint accepting it. `OXINSIDER_API_BASE` only accepts `https://api.0xinsider.com`;
redirects are refused. The optional builder code stays optional. No withdrawal command exists.

Keeping the key local does not make a recommendation feed infallible. A compromised feed could
recommend unwanted purchases within your limits. Use a dedicated wallet and a small budget.

## Set up by hand or with an agent

Inside the versioned checkout:

```bash
cp .env.example .env
chmod 600 .env
```

On Windows, prefer `init` so it creates the private folder. If setting up by hand, use
`Copy-Item .env.example .env` in a folder whose Windows Security permissions allow only your
account and administrators. `chmod` does not set Windows access permissions.

Fill in these values locally; never paste keys into a chat:

- `OXINSIDER_API_KEY`: a live Pro key from [0xinsider.com/developers](https://0xinsider.com/developers).
- `POLYMARKET_PRIVATE_KEY`: the signer key. Email/Google login: Polymarket Settings, Export
  private key ([official help](https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key)).
  Wallet login: export from that wallet app.
- `POLYMARKET_WALLET_ADDRESS`: the account address in your Polymarket profile menu, which holds
  the pUSD. This can differ from the signer address.

```bash
uv run --locked potd-trader status
uv run --locked potd-trader run --dry-run
```

For an existing setup, stop its watcher, run `potd-trader live off`, then run
`uv run --locked potd-trader size` inside the configuration folder. Type your unit size and daily
cap; the command preserves keys and other settings and removes obsolete rank settings. Run
`status` and a dry run, then restart the watcher.
Only you can turn live trading back on. A one-shot `run` sees picks already released; `watch`
wakes for later releases.

An agent must read [AGENTS.md](AGENTS.md), keep secrets out of output, run only these read/dry-run
checks, and leave enabling live orders to you. `status` reports region, account, approvals,
balance, reserved UTC budget, and unresolved orders. Missing approvals require `setup` with a
Relayer API key configured in `.env`; that command submits approval transactions, not orders.

## Stop and resume

```bash
uv run --locked potd-trader live off
uv run --locked potd-trader live status
```

`live off` writes a persistent `HALT` file beside the active `.env`, then waits for any submission
already in progress before acknowledging the stop. Running watchers using that folder cannot
start another submission after that acknowledgment. An order already sent may fill; this is not
an order-cancellation command. If a network call is stuck, the acknowledgment can wait for it.
Creating `HALT` yourself signals the stop before the next post too.

`live on` requires your confirmation, writes `LIVE=yes`, and clears `HALT`. A watcher that started
with live control enabled can resume on its next cycle. One started in dry-run stays dry until
restarted. `run --dry-run` and `watch --dry-run` always prohibit orders. Live mode requires both
process settings and the active file to say exactly `yes`, with no `HALT`; an inherited variable
cannot override a file saying `no`, and a missing control file fails closed. Restart after changing
other settings. A local `.env` is used alone; the home file is a fallback, not merged into it.

## What happens before an order

1. Validate current New York product dates and unique pick slots/tokens. Refuse a duplicate or
   inconsistent slate. Skip settled, unreleased, started, out-of-rank, or incomplete picks.
2. Require a positive published price and a current entry authorization for the selected token.
3. Check the independent Polymarket market identity and backed outcome, an explicitly open
   market, known kickoff, tick size, and minimum order size. Keep the kickoff buffer.
4. Quote the book for your stake. Enforce `MAX_PRICE`, `MAX_SLIPPAGE_PCT`, and the authorization's
   `max_entry_price` together. The authorization is a drift limit, not a promise of profit.
5. Recheck the market and book before signing. After signing, recheck live control and the
   quote deadline (30 seconds at most, shorter near kickoff/authorization expiry).
6. Under an exclusive file lock, reload the ledger, reject a repeated pick/slot/token, and reserve
   the stake against the local UTC submission day. Persist and sync the reservation before posting.
7. Post one Fill-and-Kill BUY through the SDK's separate `post_order`. Record its result without
   automatic approval transactions or an application retry of an ambiguous submission.

Live preflight also checks geoblocking, wallet approvals, and the available balance. FAK orders
can fill partially; their full requested principal remains reserved for that UTC day. Exchange
fees are additional: `STAKE_USD` and `DAILY_CAP_USD` bound order principal, not fee-inclusive debits.

`watch` honors `Retry-After`, release times, and `proof_pending_picks[].retry_at`. Read transport
failures receive bounded backoff with a visible warning; an ambiguous order does not get retried.

Every released, otherwise eligible pick uses the same unit size. There is no rank-based selection,
stake, or priority in this trader. If the remaining cap funds only some picks, the
trader considers the earliest released eligible picks first, breaking simultaneous-release ties
by token ID. It prints each cap skip. The default 25 pUSD cap funds five 5 pUSD picks, not all 10;
set 50 pUSD explicitly if you want capacity for 10. `MIN_RANKS` and `MAX_RANKS` from older setups
are ignored; `status`, `run`, and `watch` warn about them and about capacity below 10. The current
API still supplies a slot number for durable duplicate protection. Removing that contract across
the product and historical proofs is tracked in
[0xinsider/0xinsider#19968](https://github.com/0xinsider/0xinsider/issues/19968).

## Ledger and recovery

`ledger.json` beside `.env` records local submission timestamps, pick identities, reserved stakes,
and exchange answers. The adjacent `.lock` file coordinates concurrent processes. All instances
for one wallet must share this ledger and configuration folder on the same local filesystem;
separate copies or machines do not coordinate. Never delete the ledger or lock files while running.

A `submitting`, `accepted`, or `unknown` entry blocks the pick, the same date/rank slot even if its
token changes, and the token even if the feed relabels its date. Known rejections release their
reservation. `submitting` and `unknown` entries continue consuming budget across midnight until
reconciled. Corrupt state fails closed. Existing v1 ledgers remain readable without a migration.

```bash
uv run --locked potd-trader ledger
```

For an unresolved order: stop all instances, inspect Polymarket Activity and the order/fill state,
and preserve a backup. Only after proving no order can still execute should you remove its local
entry. If the order exists, preserve its identity and record the confirmed result instead. Never
clear an entry just to make the bot try again. There is no automatic unknown-order reconciliation.

## Upgrade from 0.2.0

Stop every old `watch` process first: the old `live off` command cannot stop an already-running
old binary. Back up your private configuration folder and ledger, install the new release in a
separate checkout, and use the same ledger path. Run `--dry-run` before restarting live.

Behavior changes: `DAILY_CAP_USD=0` is rejected; set a positive cap. Dates are checked against New
York, but spending is capped by locally recorded UTC submissions. Missing safety data now skips
trading. Alternate API origins are refused. Environment-only live deployments must mount an
active `.env` control file containing `LIVE=yes`; keys may still come from the environment.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIVE` | `no` | exact `yes` in process settings and active `.env`, with no `HALT` |
| `STAKE_USD` | `5` | order principal per pick; `0` means no buys |
| `MAX_PRICE` | `0.925` | maximum purchase probability |
| `MAX_SLIPPAGE_PCT` | `3` | maximum increase over the published backed price |
| `DAILY_CAP_USD` | `25` | positive ceiling on UTC order principal plus unresolved prior intents |
| `KICKOFF_BUFFER_MINUTES` | `5` | stop buying this far before kickoff or authorization expiry |
| `LEDGER_PATH` | `ledger.json` beside `.env` | shared durable order state |
| `POTD_TRADER_HOME` | `~/.potd-trader` | fallback configuration folder |
| `WATCH_IDLE_MINUTES` | `30` | idle recheck cadence |

## Docker

Build from the release checkout. Mount the configuration folder so `live off` on the host and
container see the same control file and ledger. Never bake secrets into the image.

```bash
docker build -t potd-trader:0.3.4 .
docker run --rm -v "$PWD/potd-trader:/app/data" potd-trader:0.3.4
```

## Verification and limits

The Check workflow installs locked dependencies, runs lint, formatting and strict source typing,
compiles the source, builds packages, and exercises CLI help, version, local setup, `live off`,
`live status`, an empty ledger and New York timezone loading on Windows, macOS and Linux.
These checks use no wallet credentials and submit no orders. Historical fake-exchange cases
remain in the repository; they are not run by this workflow or claimed as Windows evidence.

Atomic writes flush file content before replacement. macOS/Linux also flush the parent directory;
Windows does not, so sudden power loss can lose a recent replacement. After a power failure,
stop trading and compare the ledger with Polymarket Activity before resuming. Never run the same
wallet from Windows and WSL, separate machines, containers or separate ledger copies at once.

Stop every old watcher before upgrading, including any WSL or container process. Preserve the
configuration folder and ledger, then check `live status` and `run --dry-run` with the new version.
Only you enable live orders. Platform checks do not prove future feed integrity or profit.

Orders spend real money and cannot be undone. Polymarket regional restrictions apply. This tool
is MIT licensed, with no warranty; 0xinsider does not operate or custody your wallet.
