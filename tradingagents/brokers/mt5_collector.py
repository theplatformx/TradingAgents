"""Poll a pre-authenticated demo terminal and publish private, read-only telemetry."""

import argparse
import importlib
import json
import logging
import os
import time
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tradingagents.brokers.demo_telemetry import (
    Policy,
    TelemetryError,
    fields,
    load_json,
    read_token,
)
from tradingagents.brokers.mt5_snapshot import SnapshotError, collect_snapshot


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer token to a redirected origin.
        return None


class Publisher:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not parsed.hostname
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                )
            )
        ):
            raise TelemetryError("https_service_origin_required")
        self.url, self.token = url.rstrip("/"), token
        # Uses configured proxies and CA trust; never disable certificate verification.
        self.opener = build_opener(NoRedirect())

    def send(self, path, payload):
        request = Request(
            self.url + path,
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(request, timeout=15) as response:
            if response.status not in (200, 201, 202):
                raise TelemetryError("ingest_failed")
            response.read(4096)


def collect_once(client, terminal, policy, publisher, logger):
    """One bounded attempt; logs only fixed codes, never terminal/provider errors."""
    code = None
    try:
        if not client.initialize(str(terminal), timeout=10000):
            code = "terminal_unavailable"
        else:
            snapshot = collect_snapshot(
                client,
                login=policy.login,
                server=policy.server,
                symbol=policy.symbol,
                timeframe=policy.timeframe,
                count=policy.count,
                max_tick_age_seconds=policy.max_age_seconds,
            )
    except SnapshotError:
        code = "snapshot_rejected"
    except Exception:
        code = "collector_failure"
    finally:
        try:
            client.shutdown()
        except Exception:
            code = "collector_failure"
    try:
        if code:
            publisher.send("/v1/collector-error", {"code": code})
            logger.warning(code)
            return False
        publisher.send("/v1/snapshots", snapshot)
        logger.info("snapshot_delivered")
        return True
    except HTTPError as exc:
        # A server-side validation rejection must not leave an older good snapshot
        # looking current. Network failure instead ages out at the service.
        if exc.code == 422 and code is None:
            with suppress(OSError, ValueError):
                publisher.send("/v1/collector-error", {"code": "snapshot_rejected"})
        logger.warning("delivery_failed")
        return False
    except (OSError, URLError, ValueError):
        logger.warning("delivery_failed")
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--once", action="store_true", help="Exit nonzero unless one snapshot is delivered"
    )
    args = parser.parse_args(argv)
    try:
        config = load_json(args.config.read_bytes())
        fields(
            config,
            "login server symbol timeframe count terminal service_url ingest_token_file log_file interval_seconds",
        )
        policy = Policy(
            **{key: config[key] for key in ("login", "server", "symbol", "timeframe", "count")}
        )
        interval = config["interval_seconds"]
        if type(interval) is not int or not 10 <= interval <= 60:
            raise TelemetryError("interval_must_be_10_to_60_seconds")
        terminal = Path(config["terminal"])
        if not terminal.is_absolute() or not terminal.is_file():
            raise TelemetryError("absolute_terminal_path_required")
        publisher = Publisher(config["service_url"], read_token(config["ingest_token_file"]))
        client = importlib.import_module("MetaTrader5")
        log_path = Path(config["log_file"])
        if not log_path.is_absolute() or not log_path.parent.is_dir():
            raise TelemetryError("private_log_directory_required")
        if log_path.is_symlink() or (os.name == "posix" and log_path.parent.stat().st_mode & 0o077):
            raise TelemetryError("private_log_directory_required")
        if os.name == "posix":
            os.umask(0o077)
            if log_path.exists() and log_path.stat().st_mode & 0o077:
                raise TelemetryError("log_must_be_private")
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
        handler.formatter.converter = time.gmtime
        logger = logging.getLogger("origin.mt5.collector")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    except (OSError, ValueError, TypeError, ImportError):
        parser.exit(
            1, "Collector startup failed; check private config and the compatible MT5 runtime.\n"
        )
    try:
        while True:
            success = collect_once(client, terminal, policy, publisher, logger)
            if args.once:
                return 0 if success else 1
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
