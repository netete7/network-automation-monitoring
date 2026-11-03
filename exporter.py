"""Expose MikroTik RouterOS interface metrics for Prometheus."""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
from dotenv import load_dotenv

load_dotenv()


from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from prometheus_client import Gauge, start_http_server

load_dotenv()
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
                    LOG.warning("Traffic collection failed for interface %s", name)
        INTERFACES_UP.set(running)
    except (NetmikoAuthenticationException, NetmikoTimeoutException, OSError, ValueError):
        DEVICE_UP.set(0)
        LOG.error("Collection failed; device marked down (connection or response error)")
    except Exception:
        LOG.error("Unexpected collection error; connection status remains reachable")
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                LOG.warning("Could not close RouterOS session cleanly")


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




# # # from prometheus_client import start_http_server, Gauge
# # # import time

# # # device_up = Gauge("network_device_up", "Whether the network device is reachable")

# # # device_up.set(1)

# # # start_http_server(8000)

# # # while True:
# # #     time.sleep(10)



# # from netmiko import ConnectHandler
# # from netmiko.exceptions import NetmikoAuthenticationException
# # from netmiko.exceptions import NetmikoTimeoutException
# # from prometheus_client import start_http_server, Gauge
# # import time

# # device = {
# #     "device_type": "mikrotik_routeros",
# #     "host": "192.0.2.10",
# #     "username": "monitoring-user",
# #     "password": "",
# # }

# # device_up = Gauge(
# #     "network_device_up",
# #     "Whether the network device is reachable"
# #     "Number of running network interfaces"
# # )

# # start_http_server(8000)

# # while True:
# #     try:
# #         connection = ConnectHandler(**device)

# #         device_up.set(1)

# #     interfaces = connection.send_command("/interface print")

# #     interface_lines = interfaces.splitlines()

# #     running_interfaces = 0

# #     for line in interface_lines:
# #         if line.strip().startswith("0 R") or line.strip().startswith("1 R"):
# #             running_interfaces += 1

# #     interfaces_up.set(running_interfaces)   

# #         connection.disconnect()

# #     except (NetmikoAuthenticationException, NetmikoTimeoutException):
# #         device_up.set(0)

# #     except Exception:
# #         device_up.set(0)

# #     time.sleep(10)


# from netmiko import ConnectHandler
# from netmiko.exceptions import NetmikoAuthenticationException
# from netmiko.exceptions import NetmikoTimeoutException
# from prometheus_client import start_http_server, Gauge
# import time

# device = {
#     "device_type": "mikrotik_routeros",
#     "host": "192.0.2.10",
#     "username": "monitoring-user",
#     "password": "replace-with-a-new-secret",
# }

# device_up = Gauge(
#     "network_device_up",
#     "Whether the network device is reachable"
# )

# interface_up = Gauge(
#     "network_interface_up",
#     "Whether a network interface is running",
#     ["interface"]
# )
# interface_rx = Gauge(
#     "network_interface_rx_bps",
#     "Received bits per second on a network interface",
#     ["interface"]
# )
# interface_tx = Gauge(
#     "network_interface_tx_bps",
#     "Transmitted bits per second on a network interface",
#     ["interface"]
# )
# interface_rx_errors = Gauge(
#     "network_interface_rx_errors_per_second",
#     "Receive errors per second on a network interface",
#     ["interface"]
# )
# interfaces_up = Gauge(
#     "network_interfaces_up",
#     "Number of running network interfaces"
# )

# start_http_server(8000)


# while True:
#     try:
#         connection = ConnectHandler(**device)

#         device_up.set(1)
#         interfaces = connection.send_command("/interface print detail")

#         interface_traffic = connection.send_command(
#             "/interface monitor-traffic ether1 once"
#         )
#         print(interface_traffic)

#         running_interfaces = 0

#         for line in interfaces.splitlines():
#             parts = line.strip().split()

#             # Skip headings and continuation lines.
#             if not parts or not parts[0].isdigit():
#                 continue

#             # Find the interface name, e.g. name="ether1".
#             name_part = next(
#                 (part for part in parts if part.startswith('name="')),
#                 None
#             )

#             if name_part is None:
#                 continue

#             interface_name = name_part.split('"')[1]
#             name_index = parts.index(name_part)
#             flags = parts[1:name_index]

#             # Record both running (1) and non-running (0) states.
#             is_running = "R" in flags

#             interface_up.labels(
#                 interface=interface_name
#             ).set(1 if is_running else 0)

#             if is_running:
#                 running_interfaces += 1

#         interfaces_up.set(running_interfaces)
#         connection.disconnect()

#     except (NetmikoAuthenticationException, NetmikoTimeoutException):
#         device_up.set(0)

#     except Exception:
#         device_up.set(0)

#     time.sleep(10)







 
# from netmiko import ConnectHandler
# from netmiko.exceptions import NetmikoAuthenticationException
# from netmiko.exceptions import NetmikoTimeoutException
# from prometheus_client import start_http_server, Gauge
# import time


# # MikroTik device
# device = {
#     "device_type": "mikrotik_routeros",
#     "host": "192.0.2.10",
#     "username": "monitoring-user",
#     "password": "replace-with-a-new-secret",
# }


# # -----------------------------
# # Prometheus metrics
# # -----------------------------

# device_up = Gauge(
#     "network_device_up",
#     "Whether the network device is reachable"
# )

# interface_up = Gauge(
#     "network_interface_up",
#     "Whether a network interface is running",
#     ["interface"]
# )

# interfaces_up = Gauge(
#     "network_interfaces_up",
#     "Number of running network interfaces"
# )

# interface_rx = Gauge(
#     "network_interface_rx_bps",
#     "Received bits per second on a network interface",
#     ["interface"]
# )

# interface_tx = Gauge(
#     "network_interface_tx_bps",
#     "Transmitted bits per second on a network interface",
#     ["interface"]
# )

# interface_rx_errors = Gauge(
#     "network_interface_rx_errors_per_second",
#     "Receive errors per second on a network interface",
#     ["interface"]
# )


# # Start Prometheus exporter
# start_http_server(8000)


# # -----------------------------
# # Monitoring loop
# # -----------------------------

# while True:

#     try:
#         connection = ConnectHandler(**device)

#         # Device is reachable
#         device_up.set(1)


#         # -----------------------------
#         # Interface status
#         # -----------------------------

#         interfaces = connection.send_command(
#             "/interface print"
#         )

#         interface_lines = interfaces.splitlines()

#         running_interfaces = 0

#         for line in interface_lines:

#             if line.strip().startswith("0 R") or line.strip().startswith("1 R"):

#                 parts = line.split()

#                 interface_name = parts[2]

#                 interface_up.labels(
#                     interface=interface_name
#                 ).set(1)

#                 running_interfaces += 1


#         interfaces_up.set(running_interfaces)


#         # -----------------------------
#         # Interface traffic
#         # -----------------------------

#         traffic = connection.send_command(
#             "/interface monitor-traffic ether1 once"
#         )

#         traffic_lines = traffic.splitlines()

#         current_interface = None


#         for line in traffic_lines:

#             line = line.strip()


#             # Interface name
#             if line.startswith("name:"):

#                 current_interface = line.split(
#                     ":", 1
#                 )[1].strip()


#             # RX traffic
#             elif line.startswith("rx-bits-per-second:"):

#                 rx_value = line.split(
#                     ":", 1
#                 )[1].strip()


#                 if rx_value.endswith("kbps"):

#                     rx_bps = (
#                         float(
#                             rx_value.replace("kbps", "")
#                         ) * 1000
#                     )


#                 elif rx_value.endswith("Mbps"):

#                     rx_bps = (
#                         float(
#                             rx_value.replace("Mbps", "")
#                         ) * 1_000_000
#                     )


#                 elif rx_value.endswith("bps"):

#                     rx_bps = float(
#                         rx_value.replace("bps", "")
#                     )


#                 else:

#                     rx_bps = 0


#                 if current_interface:

#                     interface_rx.labels(
#                         interface=current_interface
#                     ).set(rx_bps)


#             # RX errors
#             elif line.startswith("rx-errors-per-second:"):

#                 error_value = line.split(
#                     ":", 1
#                 )[1].strip()


#                 if current_interface:

#                     interface_rx_errors.labels(
#                         interface=current_interface
#                     ).set(float(error_value))


#             # TX traffic
#             elif line.startswith("tx-bits-per-second:"):

#                 tx_value = line.split(
#                     ":", 1
#                 )[1].strip()


#                 if tx_value.endswith("kbps"):

#                     tx_bps = (
#                         float(
#                             tx_value.replace("kbps", "")
#                         ) * 1000
#                     )


#                 elif tx_value.endswith("Mbps"):

#                     tx_bps = (
#                         float(
#                             tx_value.replace("Mbps", "")
#                         ) * 1_000_000
#                     )


#                 elif tx_value.endswith("bps"):

#                     tx_bps = float(
#                         tx_value.replace("bps", "")
#                     )


#                 else:

#                     tx_bps = 0


#                 if current_interface:

#                     interface_tx.labels(
#                         interface=current_interface
#                     ).set(tx_bps)


#         # Close SSH connection
#         connection.disconnect()


#     except (
#         NetmikoAuthenticationException,
#         NetmikoTimeoutException
#     ):

#         device_up.set(0)


#     except Exception:

#         device_up.set(0)


#     # Wait before next collection
#     time.sleep(10)