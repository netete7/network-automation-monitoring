"""Deterministic, offline-only scenarios for the router diagnostics dashboard."""

from __future__ import annotations

import ipaddress

from ping_diagnostics import parse_ping_summary


SCENARIOS = (
    "Candidate discovered and reachable",
    "Discovered but unreachable (timeouts)",
    "Management host unreachable",
    "No candidate; manual IP",
    "Multiple candidates; select one",
    "Target equals diagnostic router IP",
)


def simulated_report(scenario: str, manual_ip: str, selected_candidate: str) -> dict[str, object]:
    """Build a fixed mock report; this function has no network dependencies."""
    if scenario not in SCENARIOS:
        raise ValueError("Choose one of the listed simulation scenarios.")
    host = "192.0.2.10"
    candidates: list[dict[str, str]] = []
    target: str | None = None
    discovery_status = "no_candidate"
    connection_status = "connected"
    own_ips = [host, "192.0.2.1"]

    if scenario in {"Candidate discovered and reachable", "Discovered but unreachable (timeouts)"}:
        target = "192.0.2.20"
        candidates = [{"address": target, "evidence": "RouterOS ARP table; RouterOS neighbor discovery"}]
        discovery_status = "candidate_selected"
    elif scenario == "Multiple candidates; select one":
        candidates = [
            {"address": "192.0.2.21", "evidence": "RouterOS ARP table"},
            {"address": "192.0.2.22", "evidence": "RouterOS neighbor discovery"},
        ]
        try:
            target = str(ipaddress.IPv4Address(selected_candidate))
        except ipaddress.AddressValueError as exc:
            raise ValueError("Select one of the simulated candidate IPs.") from exc
        if target not in {item["address"] for item in candidates}:
            raise ValueError("Select one of the simulated candidate IPs.")
        discovery_status = "candidate_selected"
    elif scenario == "No candidate; manual IP":
        try:
            target = str(ipaddress.ip_address(manual_ip.strip()))
        except ValueError as exc:
            raise ValueError("Enter a valid manual bank-router IP for this scenario.") from exc
        discovery_status = "manual_ip"
    elif scenario == "Target equals diagnostic router IP":
        target = host
        discovery_status = "candidate_selected"
        candidates = [{"address": host, "evidence": "RouterOS ARP table"}]

    if scenario == "Management host unreachable":
        connection_status = "host_unreachable"

    pings = []
    if target and connection_status == "connected" and target not in own_ips:
        if scenario == "Discovered but unreachable (timeouts)":
            raw = ";;; request timed out\nsent=10 received=0 packet-loss=100%"
        else:
            raw = "sent=10 received=10 packet-loss=0% min-rtt=1ms avg-rtt=2ms max-rtt=3ms"
        pings.append({
            "target_name": "Bank router", "target": target,
            "raw": raw, "summary": parse_ping_summary(raw, 10),
        })

    warnings = []
    if target in own_ips:
        warnings.append("Bank-router IP matches an address configured on the diagnostic router; ping skipped.")

    return {
        "router": "NETE-SIMULATED", "host": host,
        "interfaces": "0 R ether1 ether 1500\n1 ether2 ether 1500",
        "traffic": "ether1 rx=125000 tx=98000",
        "counters": "ether1,0,0,0,0", "addresses": "192.0.2.10/24 on ether1",
        "routes": "0.0.0.0/0 gateway=192.0.2.1",
        "bank_candidates": candidates, "bank_discovery_status": discovery_status,
        "selected_bank_router": target, "router_addresses": own_ips,
        "warnings": warnings, "pings": pings,
        "connection_status": connection_status, "simulated": True,
        "captured_at": "2026-10-26T00:00:00+00:00",
    }
