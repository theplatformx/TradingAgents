"""Mocked terminal and in-process HTTP contract tests; no external integrations."""

import copy
import io
import json
import logging
from datetime import timedelta
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from tests.test_mt5_demo_snapshot import NOW, broker as broker, export
from tradingagents.brokers.demo_telemetry import (
    Application,
    Policy,
    Store,
    TelemetryError,
    load_json,
    read_token,
    validate_snapshot,
)
from tradingagents.brokers.mt5_collector import NoRedirect, Publisher, collect_once

READ = "a" * 64
INGEST = "b" * 64
POLICY = Policy(login=111, server="Example-Demo", symbol="EURUSD", count=2)


@pytest.fixture
def snapshot(broker):
    return export(broker)


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    return Store(tmp_path / "telemetry.sqlite", POLICY)


def request(app, path, *, method="GET", token=READ, data=None, raw=None, **headers):
    raw = raw if raw is not None else json.dumps(data).encode() if data is not None else b""
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "HTTP_AUTHORIZATION": "Bearer " + token,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        **headers,
    }
    response = []
    body = b"".join(app(env, lambda status, hs: response.append((status, dict(hs)))))
    assert response[0][1]["Cache-Control"] == "no-store"
    return int(response[0][0].split()[0]), json.loads(body)


def test_empty_fresh_stale_error_and_recovery(store, snapshot):
    assert store.status(NOW)["broker_data"] == "awaiting_snapshot"
    digest, created = store.accept(snapshot, NOW)
    assert created
    fresh = store.status(NOW)
    assert fresh["broker_data"] == "fresh"
    assert fresh["execution"] == "disabled"
    assert fresh["model"] == "not_configured"
    assert fresh["snapshot_id"] == digest
    assert "login" not in json.dumps(fresh) and "Example-Demo" not in json.dumps(fresh)
    assert store.status(NOW + timedelta(seconds=121))["broker_data"] == "stale"
    assert store.status(NOW - timedelta(seconds=6))["broker_data"] == "stale"
    store.record_error("terminal_unavailable", NOW)
    assert store.status(NOW)["broker_data"] == "collector_error"
    snapshot["captured_at_utc"] = (NOW + timedelta(seconds=1)).isoformat()
    store.accept(snapshot, NOW + timedelta(seconds=1))
    assert store.status(NOW + timedelta(seconds=1))["broker_data"] == "fresh"


def test_replay_does_not_refresh_or_clear_error_and_older_is_rejected(store, snapshot):
    store.accept(snapshot, NOW)
    store.record_error("snapshot_rejected", NOW)
    assert store.accept(snapshot, NOW + timedelta(seconds=10))[1] is False
    assert store.status(NOW)["broker_data"] == "collector_error"
    assert store.status(NOW)["last_received_at_utc"] == NOW.isoformat()
    snapshot["captured_at_utc"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(TelemetryError, match="out_of_order"):
        store.accept(snapshot, NOW)
    assert len(store.events()) == 2


def test_restart_persists_and_cannot_relabel_database_policy(store, snapshot):
    store.accept(snapshot, NOW)
    reopened = Store(store.path, POLICY)
    assert reopened.latest() == snapshot
    assert reopened.status(NOW)["broker_data"] == "fresh"
    with pytest.raises(TelemetryError, match="policy_mismatch"):
        Store(store.path, Policy(login=222, server="Another-Demo", symbol="XAUUSD", count=2))


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("account", "login", 222),
        ("account", "login", True),
        ("account", "server", "Example-Real"),
        ("account", "trade_mode", "real"),
        ("account", "balance", float("nan")),
        ("account", "equity", float("inf")),
        ("quote", "bid", 0),
        ("quote", "ask", 1.0),
        ("quote", "bid", 10**400),
        ("quote", "spread_points", 9000),
        ("quote", "bid", True),
        ("contract", "volume_max", 0.001),
        ("contract", "tick_size", 0),
        ("contract", "digits", 1.5),
        ("contract", "currency_profit", "secret\ntext"),
        (None, "mode", "real"),
        (None, "broker_symbol", "EURUSD=X"),
        (None, "timeframe", "M1"),
        (None, "schema_version", True),
        (None, "source", "fixture"),
        (None, "limitations", ["untrusted prose"]),
        (None, "captured_at_utc", "2026-10-06T21:00:00"),
        (None, "captured_at_utc", "2026-10-06T21:00:06+00:00"),
        (None, "captured_at_utc", "2026-10-06T20:57:59+00:00"),
        ("quote", "time_utc", "2026-10-06T20:57:59+00:00"),
    ],
)
def test_untrusted_snapshots_never_enter_storage(store, snapshot, section, key, value):
    (snapshot[section] if section else snapshot)[key] = value
    with pytest.raises(TelemetryError):
        store.accept(snapshot, NOW)
    assert store.latest() is None
    assert store.status(NOW)["broker_data"] == "awaiting_snapshot"


@pytest.mark.parametrize("case", ["missing", "unordered", "unclosed", "ohlc", "volume", "unknown"])
def test_invalid_history_and_unknown_fields(snapshot, case):
    if case == "missing":
        snapshot["bars"].pop()
    elif case == "unordered":
        snapshot["bars"].reverse()
    elif case == "unclosed":
        snapshot["bars"][-1]["time_utc"] = (NOW - timedelta(seconds=30)).isoformat()
    elif case == "ohlc":
        snapshot["bars"][0]["low"] = 3
    elif case == "volume":
        snapshot["bars"][0]["tick_volume"] = -1
    else:
        snapshot["account"]["password"] = "MUST_NOT_PERSIST"
    with pytest.raises(TelemetryError):
        validate_snapshot(snapshot, POLICY, NOW)


def test_auth_scopes_health_and_no_execution_endpoint(store, snapshot):
    app = Application(store, READ, INGEST, clock=lambda: NOW, revision="c" * 40)
    assert request(app, "/healthz", token="")[1] == {"service": "healthy"}
    assert request(app, "/readyz")[0] == 503
    assert request(app, "/v1/status", token="")[0] == 401
    assert request(app, "/v1/status", token=INGEST)[0] == 401
    assert request(app, "/v1/snapshots", method="POST", data=snapshot)[0] == 401
    assert request(app, "/v1/snapshots", method="POST", token=INGEST, data=snapshot)[0] == 201
    assert request(app, "/v1/snapshots", method="POST", token=INGEST, data=snapshot)[0] == 200
    assert request(app, "/readyz")[0] == 200
    assert request(app, "/v1/status")[1]["source_commit"] == "c" * 40
    assert request(app, "/v1/snapshot")[1] == snapshot
    assert len(request(app, "/v1/events")[1]["events"]) == 1
    assert request(app, "/v1/orders", method="POST", data={"side": "buy"})[0] == 404
    assert request(app, "/v1/status", token=READ, HTTP_ORIGIN="https://example.com")[0] == 200
    later = Application(store, READ, INGEST, clock=lambda: NOW + timedelta(seconds=121))
    assert request(later, "/readyz")[0] == 503
    assert request(later, "/v1/snapshot")[0] == 503


def test_fixed_error_codes_and_private_error_responses(store):
    app = Application(store, READ, INGEST, clock=lambda: NOW)
    assert (
        request(
            app,
            "/v1/collector-error",
            method="POST",
            token=INGEST,
            data={"code": "snapshot_rejected"},
        )[0]
        == 202
    )
    assert request(
        app, "/v1/collector-error", method="POST", token=INGEST, data={"code": "password=secret"}
    ) == (422, {"error": "invalid_error_code"})
    assert "secret" not in json.dumps(store.events())
    assert request(app, "/v1/snapshots", method="POST", token=INGEST, raw=b'{"password":"SECRET"')[
        1
    ] == {"error": "invalid_json"}
    assert (
        request(
            app, "/v1/snapshots", method="POST", token=INGEST, raw=b"{}", CONTENT_LENGTH="2000001"
        )[0]
        == 413
    )
    assert (
        request(
            app, "/v1/snapshots", method="POST", token=INGEST, raw=b"{}", CONTENT_TYPE="text/plain"
        )[0]
        == 415
    )
    assert (
        request(app, "/v1/snapshots", method="POST", token=INGEST, raw=b"{}", CONTENT_LENGTH="")[0]
        == 411
    )
    with pytest.raises(TelemetryError):
        load_json('{"x": 1, "x": 2}')


def test_bounded_retention(store, snapshot):
    for i in range(105):
        now = NOW + timedelta(seconds=i)
        row = copy.deepcopy(snapshot)
        row["captured_at_utc"] = now.isoformat()
        store.accept(row, now)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 100
    for _ in range(1001):
        store.record_error("terminal_unavailable", NOW)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1000
    assert len(store.events()) == 50


def test_private_token_and_database_files(tmp_path, store):
    import os

    token_file = tmp_path / "token"
    token_file.write_text(READ)
    token_file.chmod(0o600)
    assert read_token(token_file) == READ
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600
        token_file.chmod(0o644)
        with pytest.raises(TelemetryError, match="private"):
            read_token(token_file)
        token_file.chmod(0o600)
        token_file.write_text("short")
        with pytest.raises(TelemetryError):
            read_token(token_file)
    with pytest.raises(TelemetryError, match="separate"):
        Application(store, READ, READ)


def test_collector_sends_snapshot_without_login_or_order(broker, monkeypatch):
    broker.initialize = Mock(return_value=True)
    broker.shutdown = Mock()
    publisher = Mock()
    # Freeze the terminal input age, not an actual network or MT5 integration.
    monkeypatch.setattr(
        "tradingagents.brokers.mt5_collector.collect_snapshot",
        lambda client, **kwargs: export(client),
    )
    assert collect_once(broker, "/terminal.exe", POLICY, publisher, logging.getLogger("test"))
    assert publisher.send.call_args.args[0] == "/v1/snapshots"
    broker.shutdown.assert_called_once()
    broker.login.assert_not_called()
    broker.order_send.assert_not_called()


@pytest.mark.parametrize(
    "case,code",
    [
        ("init", "terminal_unavailable"),
        ("account", "snapshot_rejected"),
        ("exception", "collector_failure"),
    ],
)
def test_collector_reports_safe_failures(broker, monkeypatch, case, code, caplog):
    broker.initialize = Mock(return_value=case != "init")
    broker.shutdown = Mock()
    if case == "account":
        broker.account_info.return_value.trade_mode = 2
    if case == "exception":
        broker.initialize.side_effect = RuntimeError("SECRET")
    monkeypatch.setattr(
        "tradingagents.brokers.mt5_collector.collect_snapshot",
        lambda client, **kwargs: export(client),
    )
    publisher = Mock()
    assert not collect_once(broker, "/terminal.exe", POLICY, publisher, logging.getLogger("test"))
    publisher.send.assert_called_once_with("/v1/collector-error", {"code": code})
    assert "SECRET" not in caplog.text
    broker.shutdown.assert_called_once()
    broker.order_send.assert_not_called()


@pytest.mark.parametrize(
    "error", [URLError("SECRET"), HTTPError("https://example", 422, "SECRET", {}, None)]
)
def test_collector_delivery_errors_are_redacted(broker, monkeypatch, error, caplog):
    broker.initialize = Mock(return_value=True)
    broker.shutdown = Mock()
    monkeypatch.setattr(
        "tradingagents.brokers.mt5_collector.collect_snapshot",
        lambda client, **kwargs: export(client),
    )
    publisher = Mock()
    publisher.send.side_effect = error
    assert not collect_once(broker, "/terminal.exe", POLICY, publisher, logging.getLogger("test"))
    assert "SECRET" not in caplog.text
    assert "delivery_failed" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com?token=x",
        "https://example.com/path",
        "file:///tmp/service",
    ],
)
def test_collector_refuses_insecure_or_credential_urls(url):
    with pytest.raises(TelemetryError):
        Publisher(url, INGEST)


def test_publisher_never_follows_redirects():
    assert (
        NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None
    )
