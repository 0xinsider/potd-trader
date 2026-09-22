# Safety release 0.3.0 evidence

Scope: [issue #1](https://github.com/0xinsider/potd-trader/issues/1), source-review findings about
live control, setup, duplicate submissions, local budgets, missing guard inputs, credential
routing, state ignores, and reproducible installation. Companion guide and CLI changelog:
[docs PR #145](https://github.com/0xinsider/docs.0xinsider.com/pull/145).

Verified September 23, 2026 in Europe/Tallinn (September 22 UTC). No real wallet order, approval
transaction, live switch, or existing ledger was changed for verification.

- `./scripts/pre-merge-check.sh`: 36 deterministic fake-exchange cases, including separate
  competing processes, crash-after-reservation, unknown submission, stop during signing,
  in-flight stop acknowledgment, duplicate slates, UTC/New York rollover, corrupt storage,
  malformed guard fields, SDK prepare/post separation, and credential destination checks.
  Ruff check/format, strict mypy on 10 source modules, and CLI help/version pass.
- `uv build --out-dir /tmp/potd-release-0.3.0`: wheel and source distribution build successfully.
  Final artifact checksums and the exact merged SHA are attached to the versioned release.
- Installed SDK inspected with `inspect.signature` and `inspect.getsource`:
  `polymarket-client==0.10.0`; `PublicClient.list_markets`, `estimate_market_price`,
  `SecureClient.create_market_order`, and `SecureClient.post_order`. The first secure method
  prepares/signs without posting; the second posts a supplied signed order. The combined
  helper's automatic allowance recovery is not used by the trader.
- Read-only SDK witness at 2026-09-22T21:25Z: bounded public market page returned 100 markets,
  including 13 with a future sports kickoff. `PublicReads.market_for_token` returned an open,
  accepting binary market with a 0.01 tick and five-share minimum. Its SDK FAK estimate for
  5 pUSD was 0.52. Token:
  `45449095624845083925429639167157282791258305996635407140970056627935819175173`.
  This proves read-path compatibility at that instant, not future liquidity or an executed fill.
- Authenticated `run --dry-run` was unavailable: the existing checkout has no usable API
  configuration. The fake feed cases establish application behavior; they are not a live
  authenticated wallet or complete dependency-tree audit.

Official contracts consulted:

- [POTD response, New York product day, entry authorization, proof retries](https://docs.0xinsider.com/api-reference/endpoint/get-pick-of-the-day).
- [Official Polymarket Python SDK](https://docs.polymarket.com/getting-started/python), plus the installed 0.10.0 source.
- [Python 3.12 file locks](https://docs.python.org/3.12/library/fcntl.html).
- [Pydantic settings precedence](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).
- [uv locked project sync](https://docs.astral.sh/uv/concepts/projects/sync/).

The safety boundary is one local shared configuration folder and ledger. Old releases, separate
ledger copies, other trading programs, and other machines do not coordinate with these locks.
The cap reserves principal, excluding provider fees. Unknown orders remain blocked for manual
exchange reconciliation; there is no claim of automatic exactly-once delivery across the network.
