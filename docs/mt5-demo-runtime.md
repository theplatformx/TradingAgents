# MT5 demo data adapter: deployment status and handoff

## What is ready

`tradingagents.brokers.mt5_snapshot` exports a JSON broker-data snapshot from an
already logged-in MT5 terminal. It checks the requested account/server and demo
mode before collecting prices and again afterward. It rejects disconnected
terminals, missing exact symbols, incomplete history, invalid quotes/contract
metadata, stale ticks and inconsistent bars. It reads completed bars (position 1
onward), with UTC timestamps. Broker symbol names are never converted to Yahoo
aliases, including metals.

The module neither accepts account passwords nor calls broker login/order APIs.
The terminal manages credentials. No account-specific settings, passwords or
server address have been committed. Its Linux tests use a simulated MT5 client;
these are contract tests, not proof of a real broker connection.

No orders, position sizing, EA strategy, continuous ingestion, commission model,
Strategy Tester run or broker-data override of TradingAgents research tools is
implemented here. The output is the first integration boundary to validate, not
a complete trading system.

## What still blocks deployment

On 2026-10-07 Asia/Vientiane, direct SSH from the ChatGPT workspace to the previously
supplied Contabo host failed with `Network is unreachable`, before authentication.
No host changes were made. No matching Contabo/Cloudflare server-control plugin
was found in the available directory search. A working authorized connection to
the VPS (for example the operator's own remote MCP) is needed for deployment.

Earlier records describe Ubuntu 24.04, but the current OS, architecture, disk/RAM,
available display/runtime, existing services and permissions have not been
re-verified. Do not change the host based solely on the earlier record.

## Runtime choice after access is restored

MetaQuotes documents MT5 on Linux through Wine:
https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux

This documents terminal support, not successful deployment of our Python bridge.
The Python adapter must execute in a runtime compatible with the MT5 Python
package and terminal. Native Linux Python imports are not assumed to work. If
using Linux/Wine, verify the terminal and Windows Python/MT5 package combination
in isolation before scheduling any service. Do not install/reboot/update all
host packages by copying the public installer command blindly.

Deploy from a reviewed git commit into an isolated runtime directory and service
user, preserving existing VPS services. Record exact terminal, Python, MT5 package
and Wine versions when actually tested. The research requirements snapshot does
not include or pin a broker runtime that has not yet been installed.

Cloudflare may protect a private operator/control endpoint. It is not the MT5
terminal host, and no public broker-command endpoint is needed for this exporter.
Do not open unauthenticated RPC, expose an RDP/VNC desktop publicly or commit
runtime account data. No Cloudflare changes were made as part of this adapter.

## First terminal-host run

Log the terminal into the designated demo account, with algorithmic/external
Python trading disabled during initial data validation. Read account/server and
the exact symbol list from that terminal. Use a private output directory outside
the checkout, secured by host permissions/ACLs.

Run the module using the Python executable compatible with that terminal:

```text
python -m tradingagents.brokers.mt5_snapshot --terminal ABSOLUTE_TERMINAL_EXE --login DEMO_LOGIN --server DEMO_SERVER --symbol EXACT_BROKER_SYMBOL --timeframe H1 --output PRIVATE_NEW_JSON_PATH
```

These are placeholders, not a command ready to run on an unaudited VPS. There is
no password argument. Existing output files are never overwritten; use a new
snapshot path each time. POSIX writes use owner-only permissions; Windows ACLs
must secure the parent directory. Snapshot data includes account identifiers
and belongs in private runtime storage.

The first verification is a real snapshot with matching login/server, fresh
bid/ask, correct contract/volume metadata and sufficient closed bars. Compare it
with the terminal screen before using it downstream. Closed markets can yield
stale ticks; this exporter will fail rather than label them current executable
prices. A demo quote still does not establish a fill or live-account performance.

Use $500 virtual funds for the first engineering test, followed by separate
$200/$100 feasibility runs. Risk/volume limits and stop-loss calculations must be
implemented before demo execution; high account leverage is not a sizing target.

## References

- Terminal initialization: https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
- Account metadata: https://www.mql5.com/en/docs/python_metatrader5/mt5accountinfo_py
- Symbol metadata: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py
- Tick quotes: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py
- Closed-bar selection: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py
