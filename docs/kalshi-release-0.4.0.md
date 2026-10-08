# Kalshi trader 0.4.0 delivery evidence

Scope: [issue #16](https://github.com/0xinsider/potd-trader/issues/16),
[trader PR #17](https://github.com/0xinsider/potd-trader/pull/17), and
[companion docs PR #436](https://github.com/0xinsider/docs.0xinsider.com/pull/436).
Trevor authorized shipping the Kalshi trader and preparing his own demo run on October 8, 2026.

## Verified source and runtime

Permitted source checks: locked sync, Ruff source lint/format, strict Mypy (15 modules),
source compilation, CLI help/version and wheel/source packaging. The legacy pre-merge helper
invokes unittest and was not run under the current no-tests instruction. Its permitted legs
were run directly. No tests or test-only infrastructure were changed or run.

Independent source review found and resolved outcome/price/fee reconciliation gaps, stale
source timing, allocation ordering, guaranteed pre-POST refusal recovery, per-pick mapping
failures, and linked contract document drift. Existing Polymarket behavior remains in its
original commands; the optional live-enable callback defaults to the old behavior.

A new stopped local runtime folder verified init, live status/off, empty ledger and public
demo market discovery. An inherited LIVE=yes did not enable the file-only Kalshi setup.
A dry run with the blank API key stopped with the named missing credential. Existing private
settings, ledgers and wallets were not read or changed. No order or live-enable command ran.

Public read evidence captured at 2026-10-08T11:50:10.659224+00:00:

```json
[
  {
    "environment": "demo",
    "ticker": "KXNFLGAME-26OCT15SEADEN-SEA",
    "exchange_index": 0,
    "fingerprint": "a4edc2dfd6b2c1815ccffeb6c2ffce57db1a58da3b75e1be6a9542990b6ac2bd",
    "fee_per_contract": "1.0176",
    "tradable": true,
    "yes_levels": 3,
    "no_levels": 4,
    "yes_best": "0.5700",
    "no_best": "0.4400"
  },
  {
    "environment": "production",
    "ticker": "KXNFLGAME-26OCT19WASSF-WAS",
    "exchange_index": 0,
    "fingerprint": "fab08d17b9940e3f0976843b899c5d61f4bb0a38574c6c377e18eee2779e6e4b",
    "fee_per_contract": "1.0176",
    "tradable": true,
    "yes_levels": 47,
    "no_levels": 20,
    "yes_best": "0.2200",
    "no_best": "0.8000"
  }
]
```

Both provider bundles validated official inline rules, event/series settlement sources,
byte hashes of both bounded official contract PDFs, shard status and YES/NO book formats.
These are read-path witnesses at the recorded time, not executed fills or matching-slate coverage.

## Supported boundary and remaining execution evidence

The first version requires a locally reviewed exact source token/condition to destination
ticker/outcome mapping. It skips unavailable or changed contracts, missing metadata, combinations,
explicit demo fixtures, and unsupported settlement or fee models. Source analytics remain
Polymarket-derived. Source kickoff is the timing boundary; a Kalshi close/occurrence time never
substitutes for kickoff. No Kalshi expected return is inferred from a source published return.

Kalshi stake/cap include worst-case exchange fees: per contract, 0.0175 times the maximum
current/scheduled quadratic multiplier plus 1.0001 USD for fractional fill rounding. This
conservatively reduces purchasable quantity and reserves more than expected fees. Unknown and
filled orders retain the full reserve; only proven zero-fill terminal orders release it.
External broker/FCM commissions are unsupported.

Use a dedicated account/default subaccount 0 without concurrent manual or other-tool trading.
REST exposure/balance projections are delayed; no atomic flat-position precondition exists.
All instances of this tool for an account share one local configuration and ledger. Uncertain
submissions remain blocked, never automatically reposted. A bounded missing order read does
not release an intent. A terminal zero-fill acknowledgement is held until complete provider
readback; known failures before POST release safely.

Authenticated POTD planning, account auth/balance/exposure and demo execution were not observed:
no user-entered Pro/Max feed key or Kalshi demo key/PEM was available in the newly prepared folder.
The concrete setup is at /Users/trevor.lasn/Work/kalshi-potd-demo, stopped with blank key fields.
The user enters keys locally and reviews a genuinely equivalent current contract before enabling
his demo orders. Demo availability or a demo fill cannot prove production equivalence or liquidity.

## Re-check commands

```bash
uv sync --locked
uv run --locked ruff check src
uv run --locked ruff format --check src
uv run --locked mypy src
uv run --locked python -m compileall -q src
uv run --locked potd-trader --help
uv run --locked potd-trader kalshi --help
uv build
```

After the user fills a stopped configuration, run `potd-trader kalshi status`, `picks`, `markets`,
`map` and `run --dry-run` in that folder. Only the user runs `live on` and types its environment's
phrase. The release page binds artifacts/checksums to the exact merged source and contains the
post-merge package-install receipt.

Official contracts: [V2 orders](https://docs.kalshi.com/api-reference/orders/create-order-v2),
[authentication](https://docs.kalshi.com/getting_started/api_keys),
[direction](https://docs.kalshi.com/getting_started/order_direction),
[fee rounding](https://docs.kalshi.com/getting_started/fee_rounding),
[fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf),
[demo](https://docs.kalshi.com/getting_started/demo_env),
[market](https://docs.kalshi.com/api-reference/market/get-market),
[series](https://docs.kalshi.com/api-reference/market/get-series),
[order read](https://docs.kalshi.com/api-reference/orders/get-order), and
[positions](https://docs.kalshi.com/api-reference/portfolio/get-positions).
