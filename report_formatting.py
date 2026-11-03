"""Format local RouterOS evidence and evidence-based next steps."""

from __future__ import annotations


def normalize_identity(value: str) -> str:
    """Join a RouterOS identity printed as spaced single characters."""
    parts = value.split()
    if len(parts) > 1 and all(len(part) == 1 for part in parts):
        return "".join(parts)
    return value.strip()


def recommendations_for(report: dict[str, object]) -> list[str]:
    """Return next steps limited to the evidence captured in this report."""
    recommendations = []
    if report.get("connection_status") == "host_unreachable":
        return [
            "The diagnostic router could not be reached at the management IP. Confirm the management address, power and link status, then check the route or VLAN between this workstation and the router before retrying."
        ]

    bank_target = report.get("selected_bank_router") or next(
        (ping.get("target") for ping in report.get("pings", [])
         if ping.get("target_name") == "Bank router"), None
    )
    if bank_target and bank_target in report.get("router_addresses", []):
        recommendations.append(
            "The selected bank-router IP matches an address configured on the diagnostic router, so its ping was skipped. Verify the bank router's actual IP from the site addressing plan or device owner, then enter that address and rerun."
        )
        return recommendations

    pings = report.get("pings", [])
    if not isinstance(pings, list):
        pings = []
    gateway_ping = next(
        (ping for ping in pings if ping.get("target_name") == "Gateway"), None
    )

    if gateway_ping is not None:
        summary = gateway_ping.get("summary")
        if summary and summary.status == "PASS":
            recommendations.append(
                "All gateway probes received replies. Continue with the bank-router check if that is the issue being investigated; if users still lack service, test an approved external destination and DNS separately."
            )
        elif summary and summary.status in {"FAIL", "WARNING"}:
            recommendations.append(
                "Gateway replies were missing or lost; check the interface/link, gateway address and routing, and whether ICMP is filtered."
            )
        else:
            recommendations.append(
                "Gateway reachability is unknown because the ping summary was unavailable or inconsistent; check the interface/link, gateway address and routing, and whether ICMP is filtered."
            )

    bank_ping = next(
        (ping for ping in pings if ping.get("target_name") == "Bank router"), None
    )
    if bank_ping is not None:
        summary = bank_ping.get("summary")
        if summary is None or summary.execution_status in {
            "unknown", "timeout", "ssh_error", "command_error"
        }:
            recommendations.append(
                "The ping result could not be verified from the collected RouterOS/SSH output. Review the captured evidence and rerun the test; this result alone does not establish that the bank router is unreachable."
            )
        elif summary.status == "PASS":
            if report.get("bank_discovery_status") == "manual_ip":
                recommendations.append(
                    "No ARP or neighbor candidate was available; the manually entered bank-router address replied. Confirm that address with the bank network contact, then check the bank-facing application or route that is failing."
                )
            elif len(report.get("bank_candidates", [])) > 1:
                recommendations.append(
                    f"The selected candidate {bank_target} replied to ICMP. Confirm that this candidate is the bank router with the bank network contact, then check the failing bank-facing service or route; this ping alone does not verify application service."
                )
            else:
                recommendations.append(
                    "The selected bank-router address replied to ICMP. Confirm that it is the intended device, then check the bank-facing service or route that is failing; this ping alone does not verify application service."
                )
        else:
            recommendations.append(
                "The selected bank-router address did not return a complete set of replies. Verify the address and bank-facing cable/link, then inspect the MikroTik interface, route and neighbor/ARP entry for that target; ask the bank network contact whether ICMP is filtered before treating silence as device failure."
            )
    else:
        candidates = report.get("bank_candidates", [])
        discovery_status = report.get("bank_discovery_status")
        if discovery_status == "manual_ip":
            recommendations.append(
                "No ARP or neighbor candidate was available; the manually entered bank-router address replied. Confirm that address with the bank network contact, then check the bank-facing application or route that is failing."
            )
        elif discovery_status == "candidate_selected" and candidates:
            recommendations.append(
                f"The selected candidate {bank_target} is the target for this check. Discovery only identifies an observed neighbor; confirm its identity with the bank network contact before relying on the result."
            )
        elif candidates:
            recommendations.append(
                f"Discovery found {len(candidates)} unverified candidate(s). Confirm the correct address with the bank network contact, select it, and rerun the reachability check."
            )
        else:
            recommendations.append(
                "No bank-router candidate was found in ARP or neighbor discovery. Confirm the bank router's IP with the bank network contact, enter it manually, and check the local link and subnet route if it remains unreachable."
            )
    return recommendations


def transcript_for(report: dict[str, object]) -> str:
    lines = [
        f"NETE ROUTER DIAGNOSTICS  |  {report['captured_at']}",
        f"Router: {normalize_identity(str(report['router']))} ({report['host']})",
        "=" * 72,
        "INTERFACE STATUS",
        str(report["interfaces"]).strip(),
        "",
        "EXPECTED ETHERNET PORTS (ether1–ether5)",
    ]
    interface_status: dict[str, str] = {}
    for line in str(report["interfaces"]).splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            name = next((part for part in parts[1:] if part.startswith("ether")), None)
            if name:
                index = parts.index(name)
                interface_status[name] = "UP" if "R" in parts[1:index] else "DOWN"
    for number in range(1, 6):
        name = f"ether{number}"
        lines.append(f"{name}: {interface_status.get(name, 'NOT PRESENT / UNKNOWN')}")
    lines += ["", "TRAFFIC COUNTERS (cumulative totals)", str(report["traffic"]).strip(),
              "", "ERROR / DROP COUNTERS", str(report["counters"]).strip(),
              "", "IP ADDRESSES", str(report["addresses"]).strip(), "", "PING CHECKS"]
    candidates = report.get("bank_candidates", [])
    lines += ["", "BANK-ROUTER TARGET"]
    if report.get("bank_discovery_status") == "inventory_confirmed":
        lines.append(f"Confirmed inventory target: {report.get('selected_bank_router')} (neighbor discovery skipped).")
    elif candidates:
        lines.extend(
            f"Candidate bank-router IP: {item['address']} | Source: {item['evidence']}"
            for item in candidates
        )
    else:
        lines.append("No candidates found in ARP or neighbor discovery.")
    pings = report["pings"]
    if not pings:
        lines.append("No ping target selected.")
    for ping in pings:
        lines.append(f"{ping['target_name']}: {ping['target']}")
        result_rows = ping.get("result_rows", [])
        if result_rows:
            lines.append("SEQ HOST SIZE TTL TIME STATUS")
            lines.extend(str(row) for row in result_rows)
        lines.append(str(ping["raw"]).strip())
        parsed = ping["summary"]
        lines.append(f"NETE@{normalize_identity(str(report['router']))}>")
        if parsed:
            if parsed.execution_status == "early_stop":
                messages = {
                    "packet_loss": "PACKET LOSS DETECTED — TEST STOPPED EARLY",
                    "max_latency": "MAX LATENCY THRESHOLD EXCEEDED — TEST STOPPED EARLY",
                }
                lines.extend(messages[reason] for reason in parsed.stop_reasons)
            elif parsed.execution_status == "completed":
                lines.append(
                    f"Assessment: {parsed.status} | {parsed.received}/{parsed.sent} replies | "
                    f"{parsed.packet_loss:g}% loss"
                )
            else:
                lines.append(f"Ping execution: {parsed.execution_status.upper()} | {parsed.error}")
            loss = f"{parsed.packet_loss:g}%" if parsed.sent else "not available"
            lines.append(
                f"Packets attempted={parsed.attempts_started} confirmed={parsed.sent} "
                f"received={parsed.received} lost={parsed.sent - parsed.received} "
                f"observed-loss={loss}"
            )
            if parsed.min_rtt or parsed.avg_rtt or parsed.max_rtt:
                lines.append(
                    f"RTT: min={parsed.min_rtt or '—'} avg={parsed.avg_rtt or '—'} "
                    f"max={parsed.max_rtt or '—'}"
                )
            if parsed.attempts_started > parsed.sent:
                lines.append("The final started probe has no complete result and was not counted as lost.")
        else:
            lines.append("Assessment: UNKNOWN | summary unavailable or inconsistent")
    lines += ["", "RECOMMENDATIONS / NEXT STEPS"]
    lines.extend(f"- {item}" for item in recommendations_for(report))
    lines += ["", "ICMP results can be affected by filtering, routing, or addressing."]
    return "\n".join(lines)
