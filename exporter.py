"""Expose MikroTik RouterOS interface metrics for Prometheus."""

from __future__ import annotations

import logging
import os
import re
import signal
import threading

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from prometheus_client import Gauge, start_http_server

LOG = logging.getLogger("mikrotik_exporter")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "10"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))
STOP = threading.Event()

DEVICE_UP = Gauge("network_device_up", "Whether the network device is reachable")
INTERFACE_UP = Gauge("network_interface_up", "Whether a network interface is running", ["interface"])
INTERFACE_RX = Gauge("network_interface_rx_bps", "Received bits per second on a network interface", ["interface"])
INTERFACE_TX = Gauge("network_interface_tx_bps", "Transmitted bits per second on a network interface", ["interface"])
INTERFACES_UP = Gauge("network_interfaces_up", "Number of running network interfaces")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Required environment variable {name} is not set")
    return value


def parse_interfaces(output: str) -> dict[str, float]:
    """Return interface running state from RouterOS detail rows."""
    states: dict[str, float] = {}
    for line in output.splitlines():
        row = re.match(r'^\s*\d+\s+(.*?)\s+name="([^"]+)"', line)
        if row:
            flags, name = row.groups()
            states[name] = 1.0 if "R" in flags.split() else 0.0
    return states


def parse_traffic(output: str) -> tuple[float | None, float | None]:
    """Read RouterOS monitor-traffic rx/tx bit rates, accepting comma separators."""
    values: dict[str, float] = {}
    for line in output.splitlines():
        match = re.match(r"\s*(rx-bits-per-second|tx-bits-per-second)\s*:\s*([\d,]+)", line, re.I)
        if match:
            values[match.group(1).lower()] = float(match.group(2).replace(",", ""))
    return values.get("rx-bits-per-second"), values.get("tx-bits-per-second")


def collect_once(device: dict[str, object]) -> None:
    connection = None
    try:
        connection = ConnectHandler(**device)
        DEVICE_UP.set(1)
        state_output = connection.send_command("/interface print detail")
        states = parse_interfaces(state_output)
        if not states:
            raise ValueError("No interface rows parsed from RouterOS response")

        running = 0
        for name, is_up in states.items():
            INTERFACE_UP.labels(interface=name).set(is_up)
            running += int(is_up)
            if is_up:
                safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
                try:
                    traffic_output = connection.send_command(
                        f'/interface monitor-traffic "{safe_name}" once'
                    )
                    rx_bps, tx_bps = parse_traffic(traffic_output)
                    if rx_bps is not None:
                        INTERFACE_RX.labels(interface=name).set(rx_bps)
                    if tx_bps is not None:
                        INTERFACE_TX.labels(interface=name).set(tx_bps)
                except Exception:
                    LOG.warning("Traffic collection failed for interface %s", name, exc_info=True)
        INTERFACES_UP.set(running)
    except (NetmikoAuthenticationException, NetmikoTimeoutException, OSError, ValueError):
        DEVICE_UP.set(0)
        LOG.exception("Collection failed; device marked down")
    except Exception:
        LOG.exception("Unexpected collection error; connection status remains reachable")
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                LOG.warning("Could not close RouterOS session cleanly", exc_info=True)


def stop(_signum: int, _frame: object) -> None:
    STOP.set()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    device: dict[str, object] = {
        "device_type": "mikrotik_routeros",
        "host": required_env("ROUTER_HOST"),
        "username": required_env("ROUTER_USERNAME"),
        "password": required_env("ROUTER_PASSWORD"),
        "timeout": 10,
    }
    start_http_server(METRICS_PORT)
    LOG.info("Prometheus metrics listening on port %d", METRICS_PORT)
    while not STOP.is_set():
        collect_once(device)
        STOP.wait(POLL_INTERVAL)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    main()
