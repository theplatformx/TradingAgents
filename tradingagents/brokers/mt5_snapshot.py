"""Export broker data from an already logged-in MT5 demo terminal; never send orders.

Run on the terminal host with its compatible MetaTrader5 Python package. Linux
research workers consume the resulting JSON, not a guessed native MT5 import.
Account credentials are managed by the terminal, not this module or GitHub.
"""

import argparse
import importlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path


class SnapshotError(ValueError):
    """Missing, inconsistent or unsuitable broker data."""


def _number(value, field, *, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"Invalid {field}") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise SnapshotError(f"Invalid {field}")
    return result


def _demo_account(client, login, server):
    account = client.account_info()
    if account is None:
        raise SnapshotError("No logged-in MT5 account")
    if account.login != login or account.server != server:
        raise SnapshotError("Terminal account/server does not match the requested demo account")
    if account.trade_mode != client.ACCOUNT_TRADE_MODE_DEMO:
        raise SnapshotError("This adapter accepts demo accounts only")
    return account


def collect_snapshot(
    client, *, login, server, symbol, timeframe="H1", count=200, max_tick_age_seconds=120, now=None
):
    """Collect a single verified snapshot. Caller owns terminal initialize/shutdown.

    No symbol aliasing: XAUUSD means the broker's exact symbol, never GC=F.
    Bars start at position 1, excluding the current unfinished bar.
    """
    if not isinstance(login, int) or isinstance(login, bool) or login <= 0:
        raise SnapshotError("Expected a positive account login")
    if not isinstance(server, str) or not server.strip():
        raise SnapshotError("Expected a server name")
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        raise SnapshotError("Expected the exact broker symbol")
    if timeframe not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}:
        raise SnapshotError("Unsupported timeframe")
    if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 10000:
        raise SnapshotError("Bar count must be between 2 and 10000")
    age_limit = _number(max_tick_age_seconds, "tick age limit", positive=True)
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SnapshotError("Snapshot time must have a timezone")
    now = now.astimezone(UTC)
    account = _demo_account(client, login, server)
    terminal = client.terminal_info()
    if terminal is None or not terminal.connected:
        raise SnapshotError("MT5 terminal is disconnected")
    info = client.symbol_info(symbol)
    if info is None or info.name != symbol:
        raise SnapshotError("Exact broker symbol was not found")
    if not info.visible and not client.symbol_select(symbol, True):
        raise SnapshotError("Cannot select symbol in Market Watch")
    tick = client.symbol_info_tick(symbol)
    if tick is None:
        raise SnapshotError("No broker tick available")
    tick_ms = _number(tick.time_msc, "tick time", positive=True)
    tick_age = now.timestamp() - tick_ms / 1000
    if tick_age < -5 or tick_age > age_limit:
        raise SnapshotError("Broker tick is stale or timestamp is in the future")
    tick_time = datetime.fromtimestamp(tick_ms / 1000, UTC)
    bid = _number(tick.bid, "bid", positive=True)
    ask = _number(tick.ask, "ask", positive=True)
    if ask < bid:
        raise SnapshotError("Broker ask is below bid")
    point = _number(info.point, "point", positive=True)
    metadata = {
        "digits": int(info.digits),
        "point": point,
        "tick_size": _number(info.trade_tick_size, "tick size", positive=True),
        "tick_value_profit": _number(
            info.trade_tick_value_profit, "profit tick value", positive=True
        ),
        "tick_value_loss": _number(info.trade_tick_value_loss, "loss tick value", positive=True),
        "contract_size": _number(info.trade_contract_size, "contract size", positive=True),
        "volume_min": _number(info.volume_min, "minimum volume", positive=True),
        "volume_max": _number(info.volume_max, "maximum volume", positive=True),
        "volume_step": _number(info.volume_step, "volume step", positive=True),
        "stops_level_points": int(info.trade_stops_level),
        "freeze_level_points": int(info.trade_freeze_level),
        "swap_long": _number(info.swap_long, "long swap"),
        "swap_short": _number(info.swap_short, "short swap"),
        "swap_mode": int(info.swap_mode),
        "currency_base": info.currency_base,
        "currency_profit": info.currency_profit,
        "currency_margin": info.currency_margin,
    }
    if (
        not 0 <= metadata["digits"] <= 12
        or metadata["volume_max"] < metadata["volume_min"]
        or metadata["stops_level_points"] < 0
        or metadata["freeze_level_points"] < 0
    ):
        raise SnapshotError("Inconsistent broker contract metadata")
    rates = client.copy_rates_from_pos(symbol, getattr(client, f"TIMEFRAME_{timeframe}"), 1, count)
    if rates is None or len(rates) != count:
        raise SnapshotError("Requested closed-bar history is incomplete")
    bars = []
    previous_time = 0
    for rate in rates:
        timestamp = int(rate["time"])
        if timestamp <= previous_time or timestamp >= tick_time.timestamp():
            raise SnapshotError("Bar timestamps are unordered, duplicated or in the future")
        bar = {
            field: _number(rate[field], field, positive=True)
            for field in ("open", "high", "low", "close")
        }
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(
            bar["open"], bar["close"]
        ):
            raise SnapshotError("Invalid bar OHLC range")
        bar.update(
            time_utc=datetime.fromtimestamp(timestamp, UTC).isoformat(),
            tick_volume=int(rate["tick_volume"]),
            spread_points=int(rate["spread"]),
            real_volume=int(rate["real_volume"]),
        )
        if min(bar["tick_volume"], bar["spread_points"], bar["real_volume"]) < 0:
            raise SnapshotError("Negative bar volume or spread")
        bars.append(bar)
        previous_time = timestamp
    # Refuse a snapshot assembled across an account switch during collection.
    _demo_account(client, login, server)
    return {
        "schema_version": 1,
        "source": "MT5 broker terminal",
        "mode": "demo-read-only",
        "captured_at_utc": now.isoformat(),
        "account": {
            "login": login,
            "server": server,
            "currency": account.currency,
            "leverage": int(account.leverage),
        },
        "broker_symbol": symbol,
        "timeframe": timeframe,
        "quote": {
            "time_utc": tick_time.isoformat(),
            "bid": bid,
            "ask": ask,
            "spread_points": (ask - bid) / point,
            "age_seconds": tick_age,
        },
        "contract": metadata,
        "bars": bars,
        "limitations": [
            "No orders or position sizing",
            "No commission or fill/slippage model",
            "Snapshot is not a continuous feed",
            "Bars are not complete tick history",
        ],
    }


def write_snapshot(snapshot, path):
    """Write a new file; refuse overwrites/symlinks. Parent must already exist.

    POSIX uses owner-only permissions; Windows operators must secure directory ACLs.
    Keep files in a private runtime directory outside the git checkout.
    """
    content = json.dumps(snapshot, indent=2, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--terminal", type=Path, required=True, help="Absolute MT5 terminal EXE path"
    )
    parser.add_argument(
        "--login", type=int, required=True, help="Expected demo login already in terminal"
    )
    parser.add_argument("--server", required=True, help="Expected demo server already in terminal")
    parser.add_argument(
        "--symbol", required=True, help="Exact broker symbol; aliases are not applied"
    )
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument(
        "--output", type=Path, required=True, help="New private JSON file outside git"
    )
    args = parser.parse_args(argv)
    if not args.terminal.is_absolute() or not args.terminal.is_file():
        parser.error("An existing absolute terminal EXE path is required")
    try:
        client = importlib.import_module("MetaTrader5")
    except ImportError:
        parser.error("Run on the MT5 host with a compatible MetaTrader5 Python installation")
    try:
        if not client.initialize(str(args.terminal), timeout=10000):
            raise SnapshotError("Cannot initialize the specified MT5 terminal")
        snapshot = collect_snapshot(
            client,
            login=args.login,
            server=args.server,
            symbol=args.symbol,
            timeframe=args.timeframe,
            count=args.count,
        )
        write_snapshot(snapshot, args.output)
    except (SnapshotError, OSError) as exc:
        parser.exit(1, f"Snapshot failed: {exc}\n")
    finally:
        client.shutdown()
    print("Demo broker snapshot exported; no orders sent.")


if __name__ == "__main__":
    main()
