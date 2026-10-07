# Origin read-only demo telemetry

## Verified scope and limits

This service receives snapshots from `tradingagents.brokers.mt5_collector`, which
uses the existing read-only MT5 adapter on the terminal host. It has no broker
login, model, strategy, sizing, order or execution endpoint. It does not install
MT5, choose a Windows/Wine runtime, or authenticate Exness.

The Linux HTTP service runs separately from the terminal. Only the collector
needs a verified MT5-compatible Python runtime. A working Linux HTTP service
**does not establish a working MT5 integration**. Do not install the Windows MT5
package into native Linux Python and claim otherwise.

The service validates the configured exact account/server, explicit demo mode,
exact symbol/timeframe, current capture and quote timestamps, finite prices,
contract fields and the requested number of ordered closed bars. It accepts no
Yahoo aliases. Capture and quote age are checked again at receipt and on every
status request (120 seconds maximum, five seconds future tolerance). Weekends or
closed markets may correctly report stale data. It never reports an independently
verified terminal connection: `fresh` means a recent authenticated collector
snapshot. Anyone holding the ingest secret can forge that assertion.

Account balance, equity and free margin now come from `account_info()` in the
adapter. They are observations, not risk limits or permissions to trade. The
telemetry boundary requires these fields and explicit `account.trade_mode=demo`;
old PR #2 JSON without them is rejected. Run the matching collector revision.

No real MT5, Exness, model call, VPS deployment or demo order was verified while
implementing this component. Automated tests use mocked terminal data. Local HTTP
smoke checks are service checks, not broker integration checks.

Reproduce the isolated HTTP check after installing the minimal service requirements:
`python -m deploy.origin.smoke`. It creates temporary credentials and a database,
starts Waitress on loopback, passes an explicitly synthetic terminal through the
collector, checks authentication/readiness/rejection, restarts to check persistence,
and cleans up. CI runs this separately from the mocked contract test suite.

## API and storage

All responses use `Cache-Control: no-store`. The service binds only to
`127.0.0.1:8765`; deploy behind an authenticated private HTTPS reverse proxy for
remote use. No permissive browser CORS configuration is supplied.

| Method and route | Credential | Meaning |
| --- | --- | --- |
| `GET /healthz` | None | Process and database readable; no broker claim |
| `GET /readyz` | Read token | 200 only when latest broker data is fresh; otherwise 503 |
| `GET /v1/status` | Read token | Status, source commit, timestamps, fixed collector error, execution disabled, model not configured |
| `GET /v1/snapshot` | Read token | Full private account/quote/contract/bars; 503 when not fresh |
| `GET /v1/events` | Read token | Latest 50 timestamped audit events |
| `POST /v1/snapshots` | Ingest token | Validate then persist; 201 new, 200 identical retry, 422 rejected |
| `POST /v1/collector-error` | Ingest token | Fixed error code only; immediately marks data unsuitable |

Use `Authorization: Bearer TOKEN` from protected server-side storage. Read and
ingest secrets must differ and must each contain 32 random bytes encoded as 64 hex
characters. Never place either in a frontend bundle, URL, screenshot, log or Git.
Do not paste them into chat or command arguments. Rotate by replacing protected
files and restarting the relevant service/collector. Do not put the read secret
in browser storage to connect the Origin Site.

The body limit is 2 MB. Unknown fields, duplicate JSON keys, account mismatches,
nonfinite numbers, incomplete/unclosed bars and stale/future data are rejected.
The ingest API accepts only a narrowly defined snapshot schema, not arbitrary
research/model output. Identical retries do not refresh timestamps or clear an
error. Older captures or regressing quote timestamps are rejected. SQLite binds
the database to its original account/instrument policy; changing that policy
requires a separate database. This prevents relabeling old data as another feed.

SQLite retains the most recent 100 snapshots and 1,000 events. This is a bounded
engineering diagnostic buffer, **not a complete trading audit or backtest store**.
Use SQLite's backup API for consistent backups. The database and parent directory
must be private (0600/0700 on POSIX). Windows requires explicit directory ACLs;
POSIX mode bits are not a substitute. Collector logs rotate at 1 MB with three
backups and contain only fixed codes. They never log broker exception strings,
account IDs, request headers or response bodies.

## Build a known release

Use Python 3.11+ (validated locally with Python 3.12). The service requires only
`requirements-origin-service.txt`, not the entire research dependency set. It
pins Waitress and python-dotenv. Broker runtime dependencies remain deliberately
unselected until a real terminal is tested.

From a trusted checkout, select the full reviewed commit SHA:

```bash
deploy/origin/package.sh FULL_40_CHARACTER_COMMIT /absolute/new/release-output
```

The output contains a source archive and SHA256SUMS. The archive contains only
the broker/service modules, minimal requirements, deployment examples and a
`REVISION` file; it contains no working-tree files, `.env`, tokens or runtime data.
Verify the SHA256SUMS after transferring through the trusted access route.

## Host preparation after access and inventory

Before changing the target, verify its host key via a trusted route and inventory
OS, architecture, CPU/RAM/disk, listening ports, current systemd units, reverse
proxy, deployment directories and existing backups. Preserve unrelated services.
Do not run these instructions blindly against an unaudited host.

On an audited Linux/systemd host:

1. Extract the verified artifact into `/opt/origin-demo/releases/FULL_SHA` and
   create its `.venv` using the audited Python installation. Install only
   `requirements-origin-service.txt` into that venv. Keep release directories
   owned by root and readable but not writable by the runtime user. Do not add a
   `.env` to the release or its ancestors.
2. Create `/etc/origin-demo` as root, mode 0700. Copy `service.example.json` to
   `service.json`, mode 0600, and replace the illustrative account/server/symbol
   with the verified allowlist. The current authorized account is recorded in the
   private project handoff; no account-specific settings belong in this repo.
   Keep the systemd credential paths and database path shown in the example.
3. Generate two independent secrets locally into `/etc/origin-demo/read-token`
   and `/etc/origin-demo/ingest-token`, with exclusive file creation and mode
   0600. For example, use `secrets.token_hex(32)` in a local Python script that
   writes directly to each file and prints nothing. Existing files must not be
   overwritten accidentally. Transfer only the ingest secret to the terminal
   host through a supported secure channel.
4. Point `/opt/origin-demo/current` at the release using an atomic symlink
   replacement. Record the previous target before replacing it. Install the
   included `origin-demo.service`, run `systemd-analyze verify` for this host,
   then `systemctl daemon-reload` and `systemctl enable --now origin-demo`.
   The unit uses a dynamic unprivileged user, protected credentials, private
   state, restart-on-failure and journald. Configure persistent, bounded journald
   retention under the host's existing logging policy; do not rewrite global
   logging configuration blindly.
5. Check `/healthz` locally and use a private client reading the read-token file
   to check `/v1/status`. Confirm the exact `source_commit`, `awaiting_snapshot`,
   `model=not_configured`, `execution=disabled`, and `/readyz` HTTP 503. A passing
   health check must never be presented as a connected broker.
6. Use the host's existing reverse proxy to expose a private HTTPS origin with
   valid TLS, a 2 MB body limit, bounded timeouts and access controls. Keep port
   8765 on loopback, restrict source access where possible, and redact request
   credentials from proxy logs. Do not add a public MT5/RDP/VNC or order endpoint.
   Verify TLS and read/ingest credential separation through the real route.

## First actual terminal/collector test

Follow `mt5-demo-runtime.md` to validate the installed terminal and its compatible
Python bridge. Record OS/Wine (if applicable), terminal, Python and MetaTrader5
package versions only after testing. Secure the terminal-host config and log
folder with ACLs before entering broker credentials in the terminal itself.
Keep algorithmic/external Python trading disabled during read-only verification.

Copy `collector.example.json` into that private directory and set the verified
demo allowlist, exact symbol, terminal path, private HTTPS service origin and
ingest-token path. No password is accepted. Use the matching source revision.

```text
COMPATIBLE_PYTHON -m tradingagents.brokers.mt5_collector --config PRIVATE_CONFIG_PATH --once
```

Success means one snapshot was accepted by the service; compare the snapshot's
account mode, balance/currency, quote, contract and closed bars with the terminal.
Check `/readyz` and `/v1/events`. Stop/close the terminal and verify a fixed error
or stale status, readiness 503 and no current snapshot delivery. Restart and
verify recovery. Keep the allowlist pointed at the designated demo account;
never switch to a real account to perform a negative test.

Only after these steps pass, run without `--once` under the terminal host's
supported session/task supervisor. The default example polls every 30 seconds,
reinitializes the selected terminal with a 10-second timeout, shuts the bridge
down after each read, and publishes over verified TLS with a 15-second timeout.
HTTP redirects are refused so bearer credentials cannot be redirected elsewhere.
Delivery failure ages out at the service; collector logs remain local.

## Existing Origin Site integration

Preserve the existing Site/project. Add a server-side adapter only after the
private HTTPS backend is reachable from its hosting environment. Store the read
secret as a Site runtime secret, never in client-side code. Return a sanitized
status projection (fresh/stale/error, timestamps, symbol, source revision,
execution disabled, model not configured). Treat fetch/auth failures explicitly
as unavailable. Do not fall back to fixture data or label saved snapshots live.
No Site source or publication was changed as part of this component.

## Rollback and remaining gates

Stop the collector first, stop the HTTP service, and take a consistent SQLite
backup. Atomically restore `current` to the recorded prior release and restart
only if that release supports schema version 1 and the same configured policy.
Unknown database versions fail startup. This first telemetry release has no
previous telemetry schema to migrate. On first-install rollback, stop/disable the
unit and remove the current symlink while preserving protected state for review.
Do not delete the database or unrelated services to make rollback succeed.

TradingAgents broker-data research routing, a real model integration, deterministic
risk limits, strategy rules, MT5 backtesting and safeguarded demo execution remain
separate work. No LLM text is accepted as an order, and `execution=disabled` cannot
be changed through configuration or this API.

## Access evidence from the handoff continuation

On 2026-10-07 UTC, the current GitHub branches and both successful CI runs matched
the handoff. Direct read-only SSH to the supplied VPS on port 22 returned
`Connection refused`, before authentication. The executor policy showed
`vpn_configured=false` and no TCP grants; the environment-status connector returned
`RPC UNAVAILABLE`. This does not establish the VPS's state. No host-key bypass,
server changes, broker passwords, paid services or model calls were attempted.
A supported, connected VPS/SSH MCP or configured private-network route is needed
before host inventory and deployment can continue.

Waitress 3.0.2 was confirmed as the current PyPI release. Deployment references:
[Waitress](https://docs.pylonsproject.org/projects/waitress/en/latest/) and
MetaQuotes [account_info](https://www.mql5.com/en/docs/python_metatrader5/mt5accountinfo_py).
The documentation sites could not be fetched through this session's HTTP route;
this session does not claim fresh verification of their contents. Balance/equity
fields still require verification against the actual terminal during integration.
