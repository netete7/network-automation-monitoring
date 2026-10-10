"""Run a read-only inventory query against one MikroTik RouterOS device."""

import os

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Set {name} in the process environment before running this script")
    return value


device = {
    "device_type": "mikrotik_routeros",
    "host": required_env("ROUTER_HOST"),
    "username": required_env("ROUTER_USERNAME"),
    "password": required_env("ROUTER_PASSWORD"),
}
connection = None

try:
    connection = ConnectHandler(**device)
    identity = connection.send_command("/system identity print")
    router_name = identity.split("name:", 1)[-1].strip()
    interfaces = connection.send_command("/interface print detail")
    addresses = connection.send_command("/ip address print")

    print("Router identity:", router_name)
    print("Interfaces:")
    for line in interfaces.splitlines():
        if 'name="' in line and 'type="' in line:
            name = line.split('name="', 1)[1].split('"', 1)[0]
            interface_type = line.split('type="', 1)[1].split('"', 1)[0]
            print(f"  {name} | {interface_type}")
    print("IP addresses:")
    for line in addresses.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "/" in parts[2]:
            print(" ", parts[2])
except NetmikoAuthenticationException:
    print("Authentication failed; check the dedicated monitoring account.")
except NetmikoTimeoutException:
    print("Connection timed out; check the host and network path.")
finally:
    if connection is not None:
        connection.disconnect()
