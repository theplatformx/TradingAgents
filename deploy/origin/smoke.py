"""Real loopback HTTP + persistence check with an explicitly SYNTHETIC MT5 client.

Run with: python -m deploy.origin.smoke
Uses no external network, real terminal, account, model, or order operation.
"""

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from tradingagents.brokers.demo_telemetry import Policy
from tradingagents.brokers.mt5_collector import NoRedirect, Publisher, collect_once


class SyntheticTerminal:
    ACCOUNT_TRADE_MODE_DEMO = 0
    TIMEFRAME_H1 = 16385

    def initialize(self, *args, **kwargs):
        return True

    def shutdown(self):
        pass

    def account_info(self):
        return SimpleNamespace(
            login=111,
            server="Synthetic-Demo",
            trade_mode=0,
            currency="USD",
            leverage=100,
            balance=500,
            equity=500,
            margin_free=500,
        )

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol,
            visible=True,
            digits=5,
            point=0.00001,
            trade_tick_size=0.00001,
            trade_tick_value_profit=1,
            trade_tick_value_loss=1,
            trade_contract_size=100000,
            volume_min=0.01,
            volume_max=100,
            volume_step=0.01,
            trade_stops_level=10,
            trade_freeze_level=0,
            swap_long=-1,
            swap_short=0.1,
            swap_mode=1,
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="EUR",
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time_msc=int(time.time() * 1000), bid=1.1, ask=1.1001)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        hour = int(time.time()) // 3600 * 3600
        return [
            {
                "time": hour - offset * 3600,
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.11,
                "tick_volume": 100,
                "spread": 10,
                "real_volume": 0,
            }
            for offset in (2, 1)
        ]


def main():
    root = Path(__file__).resolve().parents[2]
    revision_file = root / "REVISION"
    revision = (
        revision_file.read_text().strip()
        if revision_file.exists()
        else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    )
    with tempfile.TemporaryDirectory(prefix="origin-synthetic-smoke-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        tokens = {name: secrets.token_hex(32) for name in ("read", "ingest")}
        for name, value in tokens.items():
            fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(value)
        (private / "REVISION").write_text(revision)
        config = {
            "login": 111,
            "server": "Synthetic-Demo",
            "symbol": "EURUSD",
            "timeframe": "H1",
            "count": 2,
            "database": str(private / "telemetry.sqlite"),
            "read_token_file": str(private / "read"),
            "ingest_token_file": str(private / "ingest"),
        }
        (private / "config.json").write_text(json.dumps(config))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        # These requests target only loopback, never an external proxy/destination.
        opener = build_opener(ProxyHandler({}))

        def request(path, *, token=None, data=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = Request(
                origin + path,
                headers=headers,
                data=json.dumps(data).encode() if data is not None else None,
            )
            try:
                with opener.open(req, timeout=2) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)

        def start():
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tradingagents.brokers.demo_telemetry",
                    "--config",
                    str(private / "config.json"),
                    "--port",
                    str(port),
                    "--revision-file",
                    str(private / "REVISION"),
                ],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(100):
                try:
                    if request("/healthz")[0] == 200:
                        return process
                except (URLError, OSError):
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Synthetic smoke service failed to start")
                time.sleep(0.05)
            stop(process)
            raise RuntimeError("Synthetic smoke service health timeout")

        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        process = start()
        try:
            assert request("/v1/status")[0] == 401
            assert request("/readyz", token=tokens["read"])[0] == 503
            publisher = Publisher(origin, tokens["ingest"])
            publisher.opener = build_opener(ProxyHandler({}), NoRedirect())
            assert collect_once(
                SyntheticTerminal(),
                "/synthetic/terminal.exe",
                Policy(111, "Synthetic-Demo", "EURUSD", count=2),
                publisher,
                logging.getLogger("synthetic-smoke"),
            )
            assert request("/readyz", token=tokens["read"])[0] == 200
            status = request("/v1/status", token=tokens["read"])[1]
            assert status["execution"] == "disabled" and status["model"] == "not_configured"
            assert status["source_commit"] == revision
            snapshot = request("/v1/snapshot", token=tokens["read"])[1]
            assert request("/v1/snapshots", token=tokens["read"], data=snapshot)[0] == 401
            assert request("/v1/snapshots", token=tokens["ingest"], data=snapshot)[0] == 200
            snapshot["account"]["trade_mode"] = "real"
            assert request("/v1/snapshots", token=tokens["ingest"], data=snapshot)[0] == 422
            assert (
                request(
                    "/v1/collector-error",
                    token=tokens["ingest"],
                    data={"code": "terminal_unavailable"},
                )[0]
                == 202
            )
            assert request("/readyz", token=tokens["read"])[0] == 503
            stop(process)
            process = start()
            status = request("/v1/status", token=tokens["read"])[1]
            assert status["broker_data"] == "collector_error"
            assert len(request("/v1/events", token=tokens["read"])[1]["events"]) == 2
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "transport": "real loopback HTTP",
                        "terminal_data": "SYNTHETIC",
                        "restart_persistence": True,
                        "source_commit": revision,
                        "actual_mt5_connected": False,
                        "model_called": False,
                        "orders_sent": False,
                        "checked_at_utc": datetime.now(UTC).isoformat(),
                    }
                )
            )
        finally:
            stop(process)


if __name__ == "__main__":
    main()
