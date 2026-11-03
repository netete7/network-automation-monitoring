"""Run a read-only inventory query against one MikroTik RouterOS device."""

from getpass import getpass
import ipaddress
import re
import time

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)
from ping_diagnostics import (
    PingSummary,
    aggregate_ping_results,
    format_ping_summary_line,
    extract_ping_result_row,
    parse_packet_count,
    parse_ping_summary,
    ping_stop_reasons,
    rtt_milliseconds,
)


def discover_bank_router_candidates(connection) -> list[dict[str, str]]:
    """Read neighbor and ARP evidence without treating any result as verified."""
    candidates: dict[str, set[str]] = {}
    for source, command in (
        ("RouterOS ARP table", "/ip arp print detail"),
        ("RouterOS neighbor discovery", "/ip neighbor print detail"),
    ):
        try:
            output = connection.send_command(command)
        except Exception:
            # One RouterOS discovery source may be unavailable while the other
            # still provides useful evidence.
            continue
        for address in re.findall(r"\baddress[=:]\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b", output, re.I):
            try:
                normalized = str(ipaddress.IPv4Address(address))
            except ipaddress.AddressValueError:
                continue
            candidates.setdefault(normalized, set()).add(source)
    return [
        {"address": address, "evidence": "; ".join(sorted(sources))}
        for address, sources in sorted(candidates.items())
    ]


def prioritize_bank_router_candidates(
    candidates: list[dict[str, str]], router_host: str,
    configured_addresses: list[str] | set[str] | None = None,
) -> list[dict[str, str]]:
    """Put a discovered next-usable address first when it shares a known subnet."""
    if not candidates:
        return []
    subnet_addresses = list(configured_addresses or [])
    try:
        host_ip = str(ipaddress.IPv4Address(router_host))
    except ipaddress.AddressValueError:
        host_ip = None
    matching_host = [
        item for item in subnet_addresses
        if item.split("/", 1)[0] == host_ip
    ]
    subnet_addresses = matching_host + [item for item in subnet_addresses if item not in matching_host]
    if not subnet_addresses:
        subnet_addresses.append(f"{router_host}/24")
    preferred: str | None = None
    for item in subnet_addresses:
        try:
            interface = ipaddress.ip_interface(item if "/" in item else f"{item}/24")
        except ValueError:
            continue
        if not isinstance(interface, ipaddress.IPv4Interface):
            continue
        network = interface.network
        try:
            next_address = ipaddress.IPv4Address(int(interface.ip) + 1)
        except ipaddress.AddressValueError:
            continue
        if next_address in network and str(next_address) != str(network.broadcast_address):
            preferred = str(next_address)
            if any(candidate.get("address") == preferred for candidate in candidates):
                break
            preferred = None
    if preferred is None or not any(c.get("address") == preferred for c in candidates):
        return sorted(candidates, key=lambda candidate: candidate.get("address", ""))
    return sorted(candidates, key=lambda candidate: (candidate.get("address") != preferred, candidate.get("address", "")))


def ping_router_target(
    connection, target: str, count: int = 10, progress_callback=None
) -> dict[str, object]:
    """Read one RouterOS ping command incrementally from Netmiko's SSH channel."""
    target = str(ipaddress.ip_address(target))
    if count not in (5, 10, 20, 100):
        raise ValueError("Ping count must be 5, 10, 20, or 100.")
    if getattr(connection, "_routeros_channel_ready", True) is False:
        summary = aggregate_ping_results(
            [], count, execution_status="unknown",
            error="The previous RouterOS command did not return to a confirmed prompt; the SSH channel cannot be reused.",
        )
        return {"raw": "", "summary": summary, "status": "unknown", "result_rows": []}
    observations = []
    result_rows = []
    summary = aggregate_ping_results(observations, count)
    deadline = time.monotonic() + 35
    if not all(hasattr(connection, name) for name in ("write_channel", "read_channel")):
        raise TypeError("The SSH connection does not support incremental channel reads.")

    channel_buffer = ""
    command_started = False
    early_stop_reasons: tuple[str, ...] = ()
    execution_status = "completed"
    error = None
    command_complete = False
    router_summary = None
    prompt_returned = False
    mismatched_rows = []
    try:
        connection.write_channel(f"/ping address={target} count={count} interval=250ms\n")
        command_started = True
        while time.monotonic() < deadline:
            chunk = connection.read_channel()
            if not chunk:
                time.sleep(0.04)
                continue
            channel_buffer += chunk.replace("\r", "\n")
            lines = channel_buffer.split("\n")
            channel_buffer = lines.pop()
            for line in lines:
                stripped = line.strip()
                lower_line = stripped.lower()
                if any(marker in lower_line for marker in (
                    "bad command name", "syntax error", "expected end of command", "failure:"
                )):
                    execution_status = "command_error"
                    error = "RouterOS rejected the ping command."
                    break
                result_row = extract_ping_result_row(stripped)
                if result_row:
                    row_parts = result_row.split()
                    if len(row_parts) < 2 or row_parts[1] != target:
                        mismatched_rows.append(result_row)
                        result_rows.append(result_row)
                        continue
                    observation = _ping_observation_from_row(result_row)
                    if observation is not None:
                        observations.append(observation)
                        result_rows.append(result_row)
                        summary = aggregate_ping_results(
                            observations, count, attempts_started=len(observations)
                        )
                        early_stop_reasons = ping_stop_reasons(summary)
                        if early_stop_reasons:
                            summary = aggregate_ping_results(
                                observations, count, execution_status="early_stop",
                                stop_reasons=early_stop_reasons,
                            )
                        if progress_callback:
                            progress_callback(summary, result_row)
                        if early_stop_reasons:
                            execution_status = "early_stop"
                            connection.write_channel("\x03")
                            break
                if "sent=" in lower_line and "received=" in lower_line:
                    router_summary = parse_ping_summary(stripped, count)
                    # Progress summaries are common on long RouterOS pings.
                    # Keep draining through the prompt before channel reuse.
                    continue
                if re.search(r"(?:\]|\))\s*>\s*$|^[\w.-]+>\s*$", stripped):
                    prompt_returned = True
                    command_complete = True
                    break
            if not command_complete and channel_buffer:
                # RouterOS/SSH may leave the final statistics or prompt in the
                # last unterminated channel fragment rather than ending it with CR/LF.
                buffered_summary = parse_ping_summary(channel_buffer, count)
                if buffered_summary is not None:
                    router_summary = buffered_summary
                    channel_buffer = ""
                elif re.search(r"(?:\]|\))\s*>\s*$|^[\w.-]+>\s*$", channel_buffer.strip()):
                    prompt_returned = True
                    command_complete = True
                    channel_buffer = ""
            if execution_status in {"command_error", "early_stop"}:
                break
            if command_complete:
                break
        else:
            execution_status = "timeout"
            error = "RouterOS did not finish the ping command before the 35-second diagnostic limit."
    except NetmikoTimeoutException:
        execution_status = "timeout"
        error = "The SSH channel timed out while collecting RouterOS ping output."
    except (NetmikoAuthenticationException, EOFError, OSError):
        execution_status = "ssh_error"
        error = "The SSH session failed while collecting ping output."
    except Exception:
        execution_status = "command_error"
        error = "The RouterOS ping command could not be executed."
    finally:
        # If an error/timeout interrupted an active RouterOS command, request
        # cancellation so the shell is returned to a clean prompt before reuse.
        if command_started and execution_status in {"timeout", "ssh_error", "command_error"}:
            try:
                connection.write_channel("\x03")
            except Exception:
                pass

    if command_started and execution_status == "timeout" and not prompt_returned:
        execution_status = "unknown"
        error = "RouterOS did not return to a confirmed command prompt; the SSH channel cannot be reused."

    if command_started and execution_status == "early_stop":
        drain_deadline = time.monotonic() + 2
        drain_buffer = ""
        drain_prompt_returned = False
        while time.monotonic() < drain_deadline:
            try:
                chunk = connection.read_channel()
            except Exception:
                break
            if chunk:
                drain_buffer = (drain_buffer + chunk)[-1000:]
                if re.search(r"(?:\]|\))\s*>\s*$", drain_buffer):
                    drain_prompt_returned = True
                    break
            else:
                time.sleep(0.04)
        connection._routeros_channel_ready = drain_prompt_returned
    elif command_started:
        connection._routeros_channel_ready = prompt_returned

    if execution_status == "completed":
        observed = aggregate_ping_results(observations, count, attempts_started=len(observations))
        if mismatched_rows:
            execution_status = "unknown"
            error = "RouterOS returned ping rows for an address other than the requested target."
        elif not prompt_returned:
            execution_status = "unknown"
            error = "RouterOS did not return to a confirmed command prompt; the SSH channel cannot be reused."
        elif router_summary is not None:
            mismatch = _ping_summaries_conflict(observed, router_summary, count)
            if mismatch:
                execution_status = "unknown"
                error = mismatch
            else:
                summary = PingSummary(
                    **{**router_summary.__dict__, "attempts_started": len(observations)}
                )
        elif len(observations) == count and (prompt_returned or command_complete):
            # Complete per-packet rows are sufficient when a terminal/prompt
            # boundary is present but RouterOS's summary line was truncated.
            summary = observed
        else:
            execution_status = "unknown"
            error = "RouterOS ended the ping stream before all requested packet results were verified."
    if execution_status != "completed":
        summary = aggregate_ping_results(
            observations, count, execution_status=execution_status,
            stop_reasons=early_stop_reasons, error=error,
            attempts_started=len(observations),
        )
    if progress_callback and execution_status not in {"completed", "early_stop"}:
        progress_callback(summary, None)

    return {
        "raw": format_ping_summary_line(summary), "summary": summary,
        "status": summary.execution_status, "result_rows": result_rows,
    }


def _ping_summaries_conflict(observed, final, requested: int) -> str | None:
    """Reject RouterOS totals/statistics that disagree with collected packet rows."""
    if observed.sent != requested or final.sent != observed.sent:
        return "RouterOS packet totals do not match the individually collected ping rows."
    if final.received != observed.received or abs(final.packet_loss - observed.packet_loss) > 0.01:
        return "RouterOS received/loss totals conflict with the individually collected ping rows."
    for field in ("min_rtt", "avg_rtt", "max_rtt"):
        reported = getattr(final, field)
        measured = getattr(observed, field)
        if reported is None or measured is None:
            continue
        reported_ms = rtt_milliseconds(reported)
        measured_ms = rtt_milliseconds(measured)
        tolerance = max(0.01, abs(measured_ms or 0) * 0.01)
        if reported_ms is None or measured_ms is None or abs(reported_ms - measured_ms) > tolerance:
            return f"RouterOS {field.replace('_', '-')} conflicts with the individual ping response times."
    return None


def _ping_observation_from_row(row: str):
    """Convert one actual RouterOS output row into a verified one-packet result."""
    parts = row.split()
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    from ping_diagnostics import rtt_milliseconds

    lowered = row.lower()
    if "timeout" in lowered:
        return PingSummary(sent=1, received=0, packet_loss=100.0, status="FAIL")
    times = re.findall(r"\d+(?:\.\d+)?(?:ms|us|ns|d|h|m|s)(?:\d+(?:\.\d+)?(?:ms|us|ns|d|h|m|s))*", row)
    valid_times = [value for value in times if rtt_milliseconds(value) is not None]
    rtt = valid_times[-1] if valid_times else None
    if rtt is None:
        return None
    return PingSummary(
        sent=1, received=1, packet_loss=0.0, status="PASS",
        min_rtt=rtt, avg_rtt=rtt, max_rtt=rtt,
    )

def router_config() -> dict[str, object]:
    host = input("MikroTik management IP: ").strip()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        raise SystemExit("Management host must be a valid IPv4 or IPv6 address.")
    username = input("SSH username: ").strip()
    password = getpass("SSH password: ")

    if not host or not username or not password:
        raise SystemExit(
            "Management IP, username, and password are required."
        )

    return {
        "device_type": "mikrotik_routeros",
        "host": host,
        "username": username,
        "password": password,
        "timeout": 10,
    }

def prompt_packet_count() -> int | None:
    count = parse_packet_count(input("Packet count [5, 10, 20, or 100, Enter for 10]: "))
    if count is None:
        print("Ping skipped: packet count must be 5, 10, 20, or 100.")
    return count


def ping_target(
    connection, target: str, count: int | None = 10, *, router_name: str
) -> None:
    target = target.strip()
    if not target:
        print("Ping skipped: no target was entered.")
        return
    try:
        target = str(ipaddress.ip_address(target))
    except ValueError:
        print("Ping skipped: enter a valid IPv4 or IPv6 address.")
        return
    if count is None:
        count = prompt_packet_count()
        if count is None:
            return
    if count not in (5, 10, 20, 100):
        print("Ping skipped: packet count must be 5, 10, 20, or 100.")
        return

    print(f"\n--- Ping test: {target} (up to {count} packets) ---")
    execution = ping_router_target(connection, target, count)
    result = execution["summary"]
    print(execution["raw"])
    if result.execution_status == "early_stop":
        messages = {
            "packet_loss": "PACKET LOSS DETECTED — TEST STOPPED EARLY",
            "max_latency": "MAX LATENCY THRESHOLD EXCEEDED — TEST STOPPED EARLY",
        }
        for reason in result.stop_reasons:
            print(messages[reason])
    elif result.execution_status != "completed":
        print(f"Ping execution status: {result.execution_status.upper()} — {result.error}")
    else:
        print(f"Ping assessment: {result.status} ({result.received}/{result.sent} replies).")
    print(
        f"Packets attempted={result.attempts_started} confirmed={result.sent} "
        f"received={result.received} lost={result.sent - result.received} "
        f"observed-loss={result.packet_loss:g}%"
    )
    rtt = [f"{label}={value}" for label, value in (
        ("min-rtt", result.min_rtt), ("avg-rtt", result.avg_rtt),
        ("max-rtt", result.max_rtt),
    ) if value is not None]
    if rtt:
        print("RTT:", " ".join(rtt))
    print(f"\nNETE@{router_name}>")
    print("ICMP results can be affected by filtering, routing, or addressing.")

def print_expected_ethernet_ports(discovered_interfaces: dict[str, str]) -> None:
    print("\nExpected Ethernet ports (ether1-ether5):")
    for number in range(1, 6):
        name = f"ether{number}"
        if name in discovered_interfaces:
            print(f"  {name}: DETECTED | {discovered_interfaces[name]}")
        else:
            print(f"  {name}: UNAVAILABLE — not present on this lab router")


def main() -> None:
    connection = None

    try:
        device = router_config()
        connection = ConnectHandler(**device)
        identity = connection.send_command("/system identity print")
        router_name = " ".join(identity.split("name:", 1)[-1].split())
        interfaces = connection.send_command("/interface print")
        interface_traffic = connection.send_command("/interface print stats")
        interface_errors = connection.send_command(
            ':foreach i in=[/interface find] do={:put ([/interface get $i name] . "," . [/interface get $i rx-error] . "," . [/interface get $i tx-error] . "," . [/interface get $i rx-drop] . "," . [/interface get $i tx-drop])}'
        )
        addresses = connection.send_command("/ip address print")

        print("Router identity:", router_name)
        print("\nInterface traffic statistics:")
        print(interface_traffic)
        print("\nStructured interface counters:")
        print(interface_errors)
        print("\nNETE Automated interface assessment:")

        for line in interface_errors.splitlines():
            parts = line.strip().split(",")

            if len(parts) != 5:
                continue

            name = parts[0]
            rx_errors, tx_errors, rx_drops, tx_drops = map(int, parts[1:])

            if rx_errors > 0 or tx_errors > 0 or rx_drops > 0 or tx_drops > 0:
                print(f"  WARNING: {name} has non-zero error/drop counters.")
                print(
                    f"    RX errors: {rx_errors}, TX errors: {tx_errors}, "
                    f"RX drops: {rx_drops}, TX drops: {tx_drops}"
                )
            else:
                print(f"  OK: {name} reports zero errors and drops.")
        print("Interface status:")
        discovered_interfaces = {}

        for line in interfaces.splitlines():
            parts = line.split()

            if not parts or not parts[0].isdigit():
                continue

            name = next(
                (part for part in parts[1:]
                 if part.startswith("ether") or part == "lo"),
                None,
            )

            if name is None:
                continue

            name_index = parts.index(name)
            flags = parts[1:name_index]
            status = "UP" if "R" in flags else "DOWN"

            discovered_interfaces[name] = status
            print(f"  {name} | Status: {status}")

        print_expected_ethernet_ports(discovered_interfaces)


        print("IP addresses:")
        for line in addresses.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "/" in parts[2]:
                print(" ", parts[2])

        gateway = input("\nGateway IP to test (blank to skip): ").strip()

        if gateway:
            ping_target(connection, gateway, count=None, router_name=router_name)
        else:
            print("Gateway ping skipped.")

        bank_router = input(
            "\nBank router IP to test (blank to skip): "
        ).strip()

        if bank_router:
            ping_target(connection, bank_router, count=None, router_name=router_name)
        else:
            print("Bank-router ping skipped.")

    except NetmikoAuthenticationException:
        print("Authentication failed; check the dedicated monitoring account.")
    except NetmikoTimeoutException:
        print("Connection timed out; check the host and network path.")
    finally:
        if connection is not None:
            connection.disconnect()


if __name__ == "__main__":
    main()
