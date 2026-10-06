# Origin TradingAgents research baseline

## Pinned source and verification

Upstream: TauricResearch/TradingAgents, release **v0.6.0**.
Exact tag commit: `1394a3f72aa4393e1a98f51b382434c4b4c2d972`.
The branch also named v0.6.0 points to a different commit with the same source
tree; use the tag commit above to reproduce the baseline.

GitHub compare confirmed the existing theplatformx fork main is 292 commits
behind this release with no unique commits. This draft includes that upstream
update plus Origin changes; main has not been changed.

Validated on 2026-10-06, Linux x86_64, Python 3.12.14:

- Upstream baseline: 1,262 tests passed, one optional Bedrock-provider test skipped,
  one test deselected by upstream defaults, 99 subtests passed.
- With Origin forex mode: 1,279 tests passed, the same skip/deselection and
  99 subtests passed. Network integration tests are excluded by upstream defaults.
- `ruff check .`, package/CLI imports and CLI help passed.
- The first run had 31 environment failures: the required SOCKS proxy needed
  `socksio`. Installing version 1.0.0 resolved them without bypassing the proxy.
- No paid model calls, live market integration checks, broker logins or orders
  were used. These results do not establish profitability or live reliability.

`requirements-origin-tested.txt` records exact installed package versions. It is
a snapshot of this Python environment, not a cross-platform lock or security
audit. Dependency and upstream updates require fresh verification.

## Reproduce locally

From a checkout of this branch:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-origin-tested.txt
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/tradingagents --help
```

Windows uses `.venv\Scripts\python.exe` and the corresponding executables.
The Linux/Python 3.12 environment above is the one validated here.

## Forex research mode

The normal CLI detects supported currency pairs, including EURUSD, USDJPY and
Yahoo EURUSD=X. It selects market/news analysts and rejects company fundamentals
and the existing company-oriented social analyst.

```bash
.venv/bin/tradingagents --ticker EURUSD --analysts market,news
```

This is an interactive research run. Select a supported provider and model through
the prompts. A real run needs valid provider access and working data sources;
live providers were not tested in this baseline.

Python callers explicitly select the analysts and mode:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph

graph = TradingAgentsGraph(selected_analysts=["market", "news"], config=your_config)
state, decision = graph.propagate("EURUSD=X", analysis_date, asset_type="forex")
```

The caller supplies `your_config` and `analysis_date`. Forex graph entry rejects
incompatible analysts. All agents receive base/quote direction, macro focus and
research-only limitations. Bull/bear debate uses relative currency policy rather
than company revenue claims.

## Remaining limitations

- Yahoo rates are research data, not the broker's executable bid/ask, tick history,
  spread, slippage, commissions or swaps. Live availability is unverified.
- Upstream XAUUSD/XAUUSD+ maps to **GC=F gold futures**, not broker spot gold.
  Forex mode rejects it. Do not validate a gold CFD EA against that alias.
- Broker suffixes such as EURUSD.a are not mapped. The adapter will need explicit
  broker-symbol mapping rather than guessing.
- Market tools still use stock-named interfaces. Currency-pair price support does
  not imply all stock metrics have sensible forex meanings.
- News coverage is not a complete point-in-time forex calendar. Macro sources
  can need access such as FRED_API_KEY. Historical vintage and timezone handling
  need review before evaluating strategy returns.
- Upstream backtest/reflection scores directional price outcomes, not MT5 fills,
  leverage, margin, account equity or prop-firm loss limits. Unmatched benchmarks
  default to SPY. Choose a justified benchmark explicitly and do not describe
  these figures as forex trading profit.
- Prompt instructions are not deterministic validators. Models can hallucinate,
  disagree and change across versions.
- The repo has API/local-model clients. A ChatGPT/Codex subscription does not
  establish API entitlement or supported OAuth in this repo. No subscription
  relay or token reuse has been implemented. Validate the intended model route
  before a real run; do not silently switch to a paid API.
- No MT5 execution bridge, deterministic risk gate or live account integration
  is implemented in these Origin changes.

## Next implementation before test accounts

1. Start with one instrument (EURUSD for the first currency-pair test), timeframe
   and MT5 demo account. Record broker/server, exact symbol, digits, tick size/value,
   contract size, volume rules, account currency, sessions and timezone. Keep
   credentials on the chosen host or its secret store; never commit secrets or
   paste account passwords in chat.
2. Add a read-only MT5 data adapter and reproducible broker-data snapshots. Compare
   research prices with that feed; missing/stale data must fail the strategy gate.
3. Convert a research hypothesis into explicit EA rules and risk limits. The LLM
   proposes; an independently checked risk gate governs orders. Pin prompt, model
   and strategy versions and log inputs and decisions.
4. Validate the EA in MT5 Strategy Tester with broker ticks, trading costs, holdout
   periods and walk-forward checks. Define acceptable drawdown, cost sensitivity
   and rule compliance before looking at results.
5. Run demo forward tests with reconciled trades, daily/maximum-loss gates,
   exposure caps and a kill switch. A live rollout is a separate reviewed step;
   this baseline performs no live trading.
6. Feed verified results into the existing Origin account/replay screens. Keep
   illustrative growth projections separate from actual trades/payouts. No
   Origin Site deployment is included in this branch.
