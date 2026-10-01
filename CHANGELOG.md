# Changelog

## 0.3.6

- Recognize Pro or Max from the authenticated feed response, never a local plan setting.
  Pro allows five daily picks including the designated free selection; Max allows up to fifteen.
  Missing or unknown included quotas stop trading. Historical wire ranks through twenty stay readable.
- Atomically reserve each pick against the account's New York product-day allowance alongside
  the existing UTC principal budget. Accepted and unresolved entries count; known rejections release
  their slot. Existing ledger files remain readable.
- Discard a cached slate when the authenticated allowance changes, including on a 304 response.
  Report the current plan, reserved picks and the budget needed for that plan.
- Preserve configured spending caps, price, authorization, kickoff, duplicate and live-stop guards.
  Stop old watchers and preserve the shared ledger before upgrading.

## 0.3.5

- Accept Pro and Max slates with ranks through 20. Pro opens 5 daily picks in total,
  including the free pick; Max opens every available published pick up to 20.
  A day can publish fewer picks.
- Show identity-free locked-rank metadata with a Max upgrade link. Locked rows never become
  trade candidates; a successful empty slate clears the watcher's previous slate.
- Preserve configured spending caps, the 25 pUSD settings default, the init suggestion of 10
  stakes, and all price, timing, authorization, duplicate, ledger and live-mode guards.
- Use matching 0.3.5 project, runtime and lock metadata without changing dependencies.
  Stop old watchers and preserve the shared ledger before upgrading.

## 0.3.4

- Run natively on Windows with x64 Python 3.12.4+, uv and PowerShell. WSL is optional; Windows,
  macOS and Linux use the same CLI, settings and ledger format.
- Use cross-platform process locks and flushed atomic writes. Windows uses Win32 locks that
  keep waiting during a long submission; only macOS/Linux flush the parent directory too.
- Include timezone data for New York pick-date checks on Windows. Setup explains Windows
  folder permissions instead of claiming POSIX file modes.
- Check locked installation, lint, formatting, typing, compilation, packaging, local stop
  commands, ledger access and timezone loading on native Windows, macOS and Linux.
- Add PowerShell setup and upgrade instructions. Stop all old watchers before upgrading and
  preserve the shared ledger. Price, budget, timing, duplicate and live-mode guards are unchanged.

## 0.3.3

- Remove `MIN_RANKS` and `MAX_RANKS`: old values are ignored, and no released pick is skipped
  because of its slot number. Pick and ledger labels no longer display a rank. The API's slot is
  still used internally to reject duplicate or changed-slot orders until its rank-free identity
  contract is delivered in 0xinsider/0xinsider#19968.
- `size` removes obsolete rank lines from `.env`. `status`, `run`, and `watch` warn when they find
  those lines or inherited variables. Price, market, kickoff, live-control, duplicate, and daily
  spending-cap guards are unchanged; a 25 pUSD cap still funds five 5 pUSD picks.

## 0.3.2

- All 10 published slots are eligible for the same `STAKE_USD` by default. Existing explicit
  `MIN_RANKS` and `MAX_RANKS` settings remain in force until their owner removes them.
- When the cap cannot cover every eligible pick, buy in release-time order, using token ID to
  break ties; log each cap skip. Rank identifies a pick but does not set its stake or priority.
- `init` asks for unit size and daily cap, showing the cost of 10 picks. `size` lets an existing
  stopped setup change both interactively. `status`, `run`, and `watch` report budget capacity.
- The default `DAILY_CAP_USD=25` remains unchanged, so an existing configuration does not gain
  spending authority. At a 5 pUSD unit, it covers five picks; choose 50 pUSD to cover ten.

## 0.3.1

- Accept slates of up to 10 picks. 0xinsider raised the daily maximum from 6 to 10 on
  2026-09-24; a slate with rank 7 or higher failed validation as a whole, so the trader bought
  nothing on those days. `MIN_RANKS` and `MAX_RANKS` now accept 1 to 10. The default still buys
  ranks 1 to 6; set `MAX_RANKS=10` to buy every pick. `DAILY_CAP_USD` bounds the day either way.

## 0.3.0

- `live off` stops running watchers sharing the configuration folder and waits for in-flight
  submission to finish. Adds `live status`, `run --dry-run`, and `watch --dry-run`.
- The setup preview stays dry even with inherited `LIVE=yes`.
- Durable locked reservations prevent duplicate orders and budget races between processes.
  Uncertain orders stay blocked and reserved after a crash or midnight.
- `DAILY_CAP_USD` uses local UTC submission timestamps and must be positive. It bounds order
  principal; provider fees remain additional. Product dates use America/New_York.
- Reject duplicate/inconsistent slates, incomplete market data, stale or mismatched entry
  authorizations, and expired quotes. Recheck immediately before posting.
- Restrict API credentials to the expected HTTPS origin and refuse redirects.
- Follow proof-pending retry times, close read clients, and back off visible read-network failures.
- Add deterministic safety tests and CI coverage, complete SDK network disclosure, private state
  ignores, and a versioned checkout installed with the checked-in dependency lock.

Stop every 0.2.0 process before upgrading; its live switch cannot stop a running watcher.
Existing JSON ledgers are preserved. Set a positive daily cap and mount an active `.env` for
live deployments; alternate API origins and missing safety fields no longer permit trading.
