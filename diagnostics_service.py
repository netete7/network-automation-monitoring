
"""Shared read-only MikroTik diagnostic service."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Callable

from netmiko import ConnectHandler

from report_formatting import normalize_identity
from network_ssh import (
    discover_bank_router_candidates,
    prioritize_bank_router_candidates,
    ping_router_target,
)
from ping_diagnostics import PingSummary


def run_diagnostics(
    host: str,
    username: str,
    password: str,
    gateway: str | None,
    bank_router: str | None,
    packet_count: int,
    progress_callback: Callable[
        [str, str, PingSummary, str | None, str], None
    ] | None = None,
    discover_candidates: bool = True,
) -> dict[str, object]:
    """Collect read-only RouterOS evidence and test supplied targets.

    Set discover_candidates=False when the bank-router IP is already
    confirmed by an inventory record. This prevents ARP/neighbor
    discovery from influencing the Email Proactiveness workflow.

    Network Diagnosis retains discovery by default.
    """

    device = {
        "device_type": "mikrotik_routeros",
        "host": host,
        "username": username,
        "password": password,
        "timeout": 10,
    }

    connection = None

    try:
        connection = ConnectHandler(**device)

        identity_output = connection.send_command(
            "/system identity print"
        )

        identity = (
            normalize_identity(
                identity_output.split("name:", 1)[-1]
            )
            or host
        )

        interfaces = connection.send_command("/interface print")
        traffic = connection.send_command("/interface print stats")

        counters = connection.send_command(
            ':foreach i in=[/interface find] do={:put '
            '([/interface get $i name] . "," . '
            '[/interface get $i rx-error] . "," . '
            '[/interface get $i tx-error] . "," . '
            '[/interface get $i rx-drop] . "," . '
            '[/interface get $i tx-drop])}'
        )

        addresses = connection.send_command("/ip address print")
        routes = connection.send_command("/ip route print detail")

        # Discovery is optional. Email Proactiveness disables it because
        # its bank-router address comes from the confirmed inventory row.
        candidates = (
            discover_bank_router_candidates(connection)
            if discover_candidates
            else []
        )

        own_ips = set()
        configured_addresses = []

        for address in addresses.splitlines():
            match = re.search(
                r"\b(\d{1,3}(?:\.\d{1,3}){3})/\d+",
                address,
            )

            if match:
                own_ips.add(match.group(1))
                configured_addresses.append(match.group(0))

        if discover_candidates:
            candidates = prioritize_bank_router_candidates(
                candidates,
                host,
                configured_addresses,
            )

        # An explicitly supplied IP always takes priority over discovery.
        selected = bank_router or (
            candidates[0]["address"] if candidates else None
        )

        selected_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate["address"] == selected
            ),
            None,
        )

        warnings = []

        if gateway in own_ips:
            warnings.append(
                "Gateway IP matches an address configured on "
                "the diagnostic router."
            )

        if selected in own_ips:
            warnings.append(
                "Bank-router IP matches an address configured on "
                "the diagnostic router."
            )

        if not discover_candidates and selected:
            selected_source = "Confirmed inventory record"
            discovery_status = "inventory_confirmed"
        else:
            selected_source = (
                selected_candidate["evidence"]
                if selected_candidate
                else "Manual IP" if selected else None
            )

            discovery_status = (
                "manual_ip"
                if selected and selected not in {
                    candidate["address"] for candidate in candidates
                }
                else "candidate_selected" if selected
                else "not_selected"
            )

        pings = []

        for label, target in (
            ("Gateway", gateway),
            ("Bank router", selected),
        ):
            if not target:
                continue

            if label == "Bank router" and target in own_ips:
                continue

            callback = (
                (
                    lambda summary, row=None, label=label,
                    target=target: progress_callback(
                        label,
                        target,
                        summary,
                        row,
                        identity,
                    )
                )
                if progress_callback
                else None
            )

            ping_result = ping_router_target(
                connection,
                target,
                packet_count,
                progress_callback=callback,
            )

            if progress_callback:
                progress_callback(
                    label,
                    target,
                    ping_result["summary"],
                    None,
                    identity,
                )

            pings.append({
                "target_name": label,
                "target": target,
                "raw": ping_result["raw"],
                "summary": ping_result["summary"],
                "status": ping_result["status"],
                "result_rows": ping_result.get("result_rows", []),
            })

        return {
            "router": identity,
            "host": host,
            "interfaces": interfaces,
            "traffic": traffic,
            "counters": counters,
            "addresses": addresses,
            "routes": routes,
            "bank_candidates": candidates,
            "selected_bank_router": selected,
            "selected_candidate_source": selected_source,
            "bank_discovery_status": discovery_status,
            "router_addresses": sorted(own_ips),
            "warnings": warnings,
            "connection_status": "connected",
            "simulated": False,
            "pings": pings,
            "captured_at": datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds"),
        }

    finally:
        if connection is not None:
            connection.disconnect()
