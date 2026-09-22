# Changelog

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
