"""Private, read-only demo telemetry. No models, broker login or order endpoints."""

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_BODY = 2_000_000
TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
ERROR_CODES = {"terminal_unavailable", "snapshot_rejected", "collector_failure"}


class TelemetryError(ValueError):
    """Safe, fixed error messages only: never include input or credentials."""


def number(value, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TelemetryError("invalid_number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise TelemetryError("invalid_number") from exc
    if not finite or (minimum is not None and value < minimum):
        raise TelemetryError("invalid_number")
    return value


def integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise TelemetryError("invalid_integer")
    return value


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise TelemetryError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise TelemetryError("invalid_timestamp") from exc


def fields(value, required):
    if not isinstance(value, dict) or set(value) != set(required.split()):
        raise TelemetryError("invalid_fields")


def short_text(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+ -]{1,100}", value):
        raise TelemetryError("invalid_text")
    return value


def load_json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TelemetryError("duplicate_field")
            result[key] = value
        return result

    try:
        return json.loads(content, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TelemetryError("invalid_json") from exc


def read_token(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise TelemetryError("token_file_required")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise TelemetryError("token_file_must_be_private")
    token = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", token):
        raise TelemetryError("token_must_be_32_random_bytes_as_hex")
    return token


@dataclass(frozen=True)
class Policy:
    login: int
    server: str
    symbol: str
    timeframe: str = "H1"
    count: int = 200
    max_age_seconds: int = 120

    def __post_init__(self):
        integer(self.login, minimum=1)
        short_text(self.server)
        short_text(self.symbol)
        if self.symbol != self.symbol.strip() or self.timeframe not in TIMEFRAMES:
            raise TelemetryError("invalid_instrument")
        integer(self.count, minimum=2)
        integer(self.max_age_seconds, minimum=1)
        if self.count > 10000 or self.max_age_seconds > 120:
            raise TelemetryError("invalid_policy")


def validate_snapshot(data, policy, now):
    """Validate an authenticated collector assertion, not broker attestation."""
    fields(
        data,
        "schema_version source mode captured_at_utc account broker_symbol timeframe quote contract bars limitations",
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise TelemetryError("unsupported_schema")
    if data["source"] != "MT5 broker terminal" or data["mode"] != "demo-read-only":
        raise TelemetryError("demo_only")
    account = data["account"]
    fields(account, "login server trade_mode currency leverage balance equity margin_free")
    integer(account["login"], minimum=1)
    if (account["login"], account["server"], account["trade_mode"]) != (
        policy.login,
        policy.server,
        "demo",
    ):
        raise TelemetryError("account_not_allowed")
    short_text(account["currency"])
    integer(account["leverage"], minimum=1)
    for key in ("balance", "equity", "margin_free"):
        number(account[key])
    if data["broker_symbol"] != policy.symbol or data["timeframe"] != policy.timeframe:
        raise TelemetryError("instrument_not_allowed")
    captured = timestamp(data["captured_at_utc"])
    quote = data["quote"]
    fields(quote, "time_utc bid ask spread_points age_seconds")
    quoted = timestamp(quote["time_utc"])
    for value in (captured, quoted):
        if not -5 <= (now - value).total_seconds() <= policy.max_age_seconds:
            raise TelemetryError("stale_or_future_snapshot")
    if (quoted - captured).total_seconds() > 5:
        raise TelemetryError("quote_after_capture")
    bid, ask = number(quote["bid"], minimum=1e-15), number(quote["ask"], minimum=1e-15)
    if ask < bid:
        raise TelemetryError("inverted_quote")
    number(quote["age_seconds"], minimum=-5)
    number(quote["spread_points"], minimum=0)
    contract = data["contract"]
    fields(
        contract,
        "digits point tick_size tick_value_profit tick_value_loss contract_size volume_min volume_max volume_step stops_level_points freeze_level_points swap_long swap_short swap_mode currency_base currency_profit currency_margin",
    )
    for key in (
        "point",
        "tick_size",
        "tick_value_profit",
        "tick_value_loss",
        "contract_size",
        "volume_min",
        "volume_max",
        "volume_step",
    ):
        number(contract[key], minimum=1e-15)
    for key in ("digits", "stops_level_points", "freeze_level_points", "swap_mode"):
        integer(contract[key])
    for key in ("swap_long", "swap_short"):
        number(contract[key])
    for key in ("currency_base", "currency_profit", "currency_margin"):
        short_text(contract[key])
    if contract["digits"] > 12 or contract["volume_min"] > contract["volume_max"]:
        raise TelemetryError("invalid_contract")
    if not math.isclose(
        quote["spread_points"], (ask - bid) / contract["point"], rel_tol=1e-6, abs_tol=1e-6
    ):
        raise TelemetryError("inconsistent_spread")
    bars = data["bars"]
    if not isinstance(bars, list) or len(bars) != policy.count:
        raise TelemetryError("incomplete_history")
    previous = None
    for bar in bars:
        fields(bar, "time_utc open high low close tick_volume spread_points real_volume")
        opened = timestamp(bar["time_utc"])
        if (previous is not None and opened <= previous) or (
            quoted - opened
        ).total_seconds() < TIMEFRAMES[policy.timeframe]:
            raise TelemetryError("unordered_or_unclosed_bars")
        for key in ("open", "high", "low", "close"):
            number(bar[key], minimum=1e-15)
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(
            bar["open"], bar["close"]
        ):
            raise TelemetryError("invalid_ohlc")
        for key in ("tick_volume", "spread_points", "real_volume"):
            integer(bar[key])
        previous = opened
    # Do not persist arbitrary prose supplied over the network.
    if not isinstance(data["limitations"], list) or data["limitations"] != [
        "No orders or position sizing",
        "No commission or fill/slippage model",
        "Snapshot is not a continuous feed",
        "Bars are not complete tick history",
    ]:
        raise TelemetryError("invalid_limitations")
    return captured, quoted


class Store:
    """Private SQLite history, bounded to 100 snapshots and 1000 audit events."""

    def __init__(self, path, policy):
        self.path, self.policy = Path(path), policy
        if not self.path.is_absolute() or self.path.is_symlink():
            raise TelemetryError("absolute_private_database_required")
        parent = self.path.parent
        if not parent.is_dir() or (os.name == "posix" and parent.stat().st_mode & 0o077):
            raise TelemetryError("private_database_directory_required")
        if self.path.exists() and os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise TelemetryError("database_must_be_private")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise TelemetryError("unsupported_database_version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, captured REAL NOT NULL, quoted REAL NOT NULL, received TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, code TEXT NOT NULL, snapshot_id TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL)"
            )
            identity = json.dumps(vars(policy), sort_keys=True)
            db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (identity,))
            if db.execute("SELECT value FROM policy WHERE id=1").fetchone()[0] != identity:
                raise TelemetryError("database_policy_mismatch")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, data, now):
        captured, quoted = validate_snapshot(data, self.policy, now)
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM snapshots WHERE id=?", (digest,)).fetchone():
                return digest, False
            previous = db.execute(
                "SELECT captured, quoted FROM snapshots ORDER BY captured DESC LIMIT 1"
            ).fetchone()
            if previous and (
                captured.timestamp() <= previous["captured"]
                or quoted.timestamp() < previous["quoted"]
            ):
                raise TelemetryError("out_of_order_snapshot")
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (digest, captured.timestamp(), quoted.timestamp(), now.isoformat(), payload),
            )
            db.execute(
                "INSERT INTO events (at, code, snapshot_id) VALUES (?, 'snapshot_received', ?)",
                (now.isoformat(), digest),
            )
            db.execute(
                "DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY captured DESC LIMIT 100)"
            )
            self.prune_events(db)
        return digest, True

    @staticmethod
    def prune_events(db):
        db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)"
        )

    def record_error(self, code, now):
        if not isinstance(code, str) or code not in ERROR_CODES:
            raise TelemetryError("invalid_error_code")
        with self.connection() as db:
            db.execute("INSERT INTO events (at, code) VALUES (?, ?)", (now.isoformat(), code))
            self.prune_events(db)

    def latest(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM snapshots ORDER BY captured DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def events(self):
        with self.connection() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")
            ]

    def status(self, now):
        with self.connection() as db:
            row = db.execute(
                "SELECT id, captured, quoted, received FROM snapshots ORDER BY captured DESC LIMIT 1"
            ).fetchone()
            event = db.execute("SELECT code, at FROM events ORDER BY id DESC LIMIT 1").fetchone()
        state = "awaiting_snapshot"
        age = None
        if row:
            age = now.timestamp() - row["quoted"]
            capture_age = now.timestamp() - row["captured"]
            state = (
                "fresh"
                if -5 <= age <= self.policy.max_age_seconds
                and -5 <= capture_age <= self.policy.max_age_seconds
                else "stale"
            )
        if event and event["code"] != "snapshot_received":
            state = "collector_error"
        return {
            "service": "healthy",
            "mode": "demo-read-only",
            "broker_data": state,
            "broker_symbol": self.policy.symbol,
            "timeframe": self.policy.timeframe,
            "checked_at_utc": now.isoformat(),
            "quote_age_seconds": age,
            "last_received_at_utc": row["received"] if row else None,
            "snapshot_id": row["id"] if row else None,
            "last_collector_error": dict(event)
            if event and event["code"] != "snapshot_received"
            else None,
            "model": "not_configured",
            "execution": "disabled",
            "evidence": "Authenticated collector snapshot; not independent broker attestation",
        }


class Application:
    def __init__(self, store, read_token_value, ingest_token_value, clock=None, revision=None):
        if read_token_value == ingest_token_value:
            raise TelemetryError("separate_read_and_ingest_tokens_required")
        self.store = store
        self.tokens = {"read": read_token_value, "ingest": ingest_token_value}
        self.clock = clock or (lambda: datetime.now(UTC))
        self.revision = revision

    def __call__(self, environ, start_response):
        try:
            status, result = self.dispatch(environ)
        except TelemetryError as exc:
            status, result = "422 Unprocessable Entity", {"error": str(exc)}
        except (OSError, sqlite3.Error):
            status, result = "503 Service Unavailable", {"error": "storage_unavailable"}
        content = json.dumps(result, allow_nan=False).encode()
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(content))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [content]

    def dispatch(self, env):
        method, path = env.get("REQUEST_METHOD"), env.get("PATH_INFO")
        if method == "GET" and path == "/healthz":
            with self.store.connection() as db:
                db.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            return "200 OK", {"service": "healthy"}
        write = method == "POST" and path in ("/v1/snapshots", "/v1/collector-error")
        expected = "Bearer " + self.tokens["ingest" if write else "read"]
        supplied = env.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return "401 Unauthorized", {"error": "unauthorized"}
        now = self.clock()
        if method == "GET":
            if path in ("/v1/status", "/readyz"):
                result = self.store.status(now)
                result["source_commit"] = self.revision
                return (
                    "503 Service Unavailable"
                    if path == "/readyz" and result["broker_data"] != "fresh"
                    else "200 OK"
                ), result
            if path == "/v1/snapshot":
                if self.store.status(now)["broker_data"] != "fresh":
                    return "503 Service Unavailable", {"error": "fresh_snapshot_required"}
                result = self.store.latest()
                return ("200 OK", result) if result else ("404 Not Found", {"error": "no_snapshot"})
            if path == "/v1/events":
                return "200 OK", {"events": self.store.events()}
        if write:
            if env.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                return "415 Unsupported Media Type", {"error": "json_required"}
            try:
                length = int(env.get("CONTENT_LENGTH", ""))
            except ValueError:
                return "411 Length Required", {"error": "length_required"}
            if not 0 < length <= MAX_BODY:
                return "413 Content Too Large", {"error": "body_size_limit"}
            raw = env["wsgi.input"].read(length)
            if len(raw) != length:
                raise TelemetryError("incomplete_body")
            data = load_json(raw)
            if path == "/v1/collector-error":
                fields(data, "code")
                self.store.record_error(data["code"], now)
                return "202 Accepted", {"accepted": True}
            digest, created = self.store.accept(data, now)
            return ("201 Created" if created else "200 OK"), {
                "snapshot_id": digest,
                "created": created,
            }
        return "404 Not Found", {"error": "not_found"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--revision-file", type=Path, default=Path("REVISION"))
    args = parser.parse_args(argv)
    try:
        config = load_json(args.config.read_bytes())
        revision = args.revision_file.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", revision) or not 1 <= args.port <= 65535:
            raise TelemetryError("invalid_release_configuration")
        fields(
            config, "login server symbol timeframe count database read_token_file ingest_token_file"
        )
        policy = Policy(
            **{key: config[key] for key in ("login", "server", "symbol", "timeframe", "count")}
        )
        app = Application(
            Store(config["database"], policy),
            read_token(config["read_token_file"]),
            read_token(config["ingest_token_file"]),
            revision=revision,
        )
        from waitress import serve

        serve(
            app,
            host="127.0.0.1",
            port=args.port,
            threads=4,
            max_request_body_size=MAX_BODY,
            expose_tracebacks=False,
        )
    except (OSError, ValueError, TypeError, ImportError, sqlite3.Error):
        parser.exit(
            1, "Telemetry startup failed; check private configuration, storage and dependencies.\n"
        )


if __name__ == "__main__":
    main()
