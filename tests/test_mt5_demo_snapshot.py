"""Contract tests for broker export; the suite never reaches MT5 or sends orders."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tradingagents.brokers.mt5_snapshot import SnapshotError, collect_snapshot, write_snapshot

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)


@pytest.fixture
def broker():
    info = SimpleNamespace(
        name="EURUSD",
        visible=True,
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0,
        trade_contract_size=100000,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=10,
        trade_freeze_level=0,
        swap_long=-1.0,
        swap_short=0.1,
        swap_mode=1,
        currency_base="EUR",
        currency_profit="USD",
        currency_margin="EUR",
    )
    rates = [
        {
            "time": int(NOW.timestamp()) - offset,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.11,
            "tick_volume": 100,
            "spread": 10,
            "real_volume": 0,
        }
        for offset in (7200, 3600)
    ]
    client = SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0,
        TIMEFRAME_H1=16385,
        account_info=Mock(
            return_value=SimpleNamespace(
                login=111, server="Example-Demo", trade_mode=0, currency="USD", leverage=100
            )
        ),
        terminal_info=Mock(return_value=SimpleNamespace(connected=True)),
        symbol_info=Mock(return_value=info),
        symbol_select=Mock(return_value=True),
        symbol_info_tick=Mock(
            return_value=SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=1.1, ask=1.1001)
        ),
        copy_rates_from_pos=Mock(return_value=rates),
        order_send=Mock(side_effect=AssertionError("Read-only adapter attempted an order")),
        login=Mock(side_effect=AssertionError("Adapter must not handle credentials")),
    )
    return client


def export(client, **overrides):
    args = {"login": 111, "server": "Example-Demo", "symbol": "EURUSD", "count": 2, "now": NOW}
    args.update(overrides)
    return collect_snapshot(client, **args)


def test_demo_snapshot_has_exact_symbol_utc_and_closed_bars(broker):
    result = export(broker)
    assert result["mode"] == "demo-read-only"
    assert result["broker_symbol"] == "EURUSD"
    assert result["captured_at_utc"].endswith("+00:00")
    assert result["quote"]["spread_points"] == pytest.approx(10)
    assert result["contract"]["volume_min"] == 0.01
    assert len(result["bars"]) == 2
    broker.copy_rates_from_pos.assert_called_once_with("EURUSD", broker.TIMEFRAME_H1, 1, 2)
    broker.order_send.assert_not_called()
    broker.login.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("trade_mode", 2), ("login", 222), ("server", "Example-Real")]
)
def test_real_or_wrong_account_fails_before_reading_prices(broker, field, value):
    setattr(broker.account_info.return_value, field, value)
    with pytest.raises(SnapshotError):
        export(broker)
    broker.symbol_info_tick.assert_not_called()


def test_account_switch_during_collection_is_rejected(broker):
    correct = broker.account_info.return_value
    broker.account_info.side_effect = [correct, SimpleNamespace(login=222, server="Example-Real")]
    with pytest.raises(SnapshotError, match="does not match"):
        export(broker)


@pytest.mark.parametrize("age", [121, -6, -1e20])
def test_stale_or_future_quotes_are_rejected(broker, age):
    broker.symbol_info_tick.return_value.time_msc = (NOW.timestamp() - age) * 1000
    with pytest.raises(SnapshotError, match="stale or timestamp"):
        export(broker)


@pytest.mark.parametrize("value", [0, float("nan"), float("inf"), -1])
def test_invalid_prices_are_rejected(broker, value):
    broker.symbol_info_tick.return_value.bid = value
    with pytest.raises(SnapshotError, match="Invalid bid"):
        export(broker)


def test_inverted_quotes_are_rejected(broker):
    broker.symbol_info_tick.return_value.ask = 1.0
    with pytest.raises(SnapshotError, match="ask is below"):
        export(broker)


@pytest.mark.parametrize("case", ["missing", "duplicate", "bad_ohlc", "future"])
def test_incomplete_or_corrupt_bars_are_rejected(broker, case):
    rates = broker.copy_rates_from_pos.return_value
    if case == "missing":
        rates.pop()
    elif case == "duplicate":
        rates[1]["time"] = rates[0]["time"]
    elif case == "bad_ohlc":
        rates[1]["low"] = 2
    else:
        rates[1]["time"] = int(NOW.timestamp()) + 3600
    with pytest.raises(SnapshotError):
        export(broker)


def test_symbol_aliases_are_not_applied(broker):
    with pytest.raises(SnapshotError, match="Exact broker symbol"):
        export(broker, symbol="XAUUSD")
    broker.symbol_info.assert_called_once_with("XAUUSD")
    broker.copy_rates_from_pos.assert_not_called()


def test_numpy_terminal_bars_and_exact_gold_symbol_are_exported(broker):
    import numpy as np

    rates = broker.copy_rates_from_pos.return_value
    names = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
    broker.copy_rates_from_pos.return_value = np.array(
        [tuple(row[name] for name in names) for row in rates],
        dtype=[
            (name, "f8" if name in ("open", "high", "low", "close") else "i8") for name in names
        ],
    )
    broker.symbol_info.return_value.name = "XAUUSD"
    result = export(broker, symbol="XAUUSD")
    assert result["broker_symbol"] == "XAUUSD"
    broker.copy_rates_from_pos.assert_called_once_with("XAUUSD", broker.TIMEFRAME_H1, 1, 2)
    assert json.loads(json.dumps(result, allow_nan=False))["bars"][0]["tick_volume"] == 100


@pytest.mark.parametrize("case", ["account", "terminal", "symbol", "tick"])
def test_missing_or_disconnected_source_is_rejected(broker, case):
    if case == "account":
        broker.account_info.return_value = None
    elif case == "terminal":
        broker.terminal_info.return_value.connected = False
    elif case == "symbol":
        broker.symbol_info.return_value = None
    else:
        broker.symbol_info_tick.return_value = None
    with pytest.raises(SnapshotError):
        export(broker)


def test_private_snapshot_file_and_no_overwrite(broker, tmp_path):
    path = tmp_path / "broker.json"
    write_snapshot(export(broker), path)
    assert json.loads(path.read_text())["schema_version"] == 1
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_snapshot(export(broker), path)
