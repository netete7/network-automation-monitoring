"""Shared presentation of captured router diagnostics; performs no network operations."""
from __future__ import annotations
import json
from html import escape
import math
import re
from ping_diagnostics import PingSummary, format_ping_summary_line
from report_formatting import recommendations_for, transcript_for

def render_ping_terminal(st, text: str) -> None:
    """Style escaped ping evidence without changing its whitespace or order."""
    lines = []
    for line in text.splitlines(keepends=True):
        content = escape(line)
        if re.match(r"^\s*sent=\d+\s+received=\d+\s+packet-loss=", line):
            content = f'<span style="color:#087f23">{content}</span>'
        lines.append(content)
    # Bypass Markdown processing so raw terminal newlines reach the browser.
    st.html(
        '<div class="winbox-ping-terminal" role="region" aria-label="RouterOS ping terminal" '
        'tabindex="0" style="background:#fff;color:#111;overflow-x:auto;'
        'border:1px solid #aaa;border-radius:4px;padding:16px;">'
        '<pre style="margin:0;background:#fff;color:#111;white-space:pre;'
        'overflow-wrap:normal;word-break:normal;font-family:ui-monospace,SFMono-Regular,'
        'Menlo,Consolas,monospace;font-size:14px;line-height:1.5;">'
        + ''.join(lines) + '</pre></div>',
    )


def ping_text_for_display(ping: dict, fallback: str) -> str:
    """Prefer a captured full transcript; keep the existing view for summary-only evidence."""
    raw = str(ping.get("raw") or "")
    if re.search(r"^\s*(?:SEQ\s+HOST\s+SIZE\s+TTL\s+TIME|\d+\s+\S+)", raw, re.MULTILINE):
        return raw
    return fallback


def ping_rows_for_display(rows: list[str]) -> list[dict[str, str]]:
    """Convert collected RouterOS response rows into readable table columns."""
    displayed = []
    for row in rows:
        parts = str(row).split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if len(parts) >= 5:
            sequence, host, size, ttl, response_time = parts[:5]
            status = " ".join(parts[5:]) or "Reply"
        else:
            sequence, host = parts[:2]
            size = ttl = response_time = "—"
            status = " ".join(parts[2:]) or "No response details"
        displayed.append({
            "Sequence": sequence, "Host": host, "Size": size,
            "TTL": ttl, "Time": response_time, "Status": status,
        })
    return displayed


def interface_rows_for_display(output: str) -> list[dict[str, str]]:
    """Show expected Ethernet ports plus actual interfaces returned by RouterOS."""
    actual: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        flags = ""
        name_index = 1
        if re.fullmatch(r"[A-Z*]+", parts[1]):
            flags = parts[1]
            name_index = 2
        if name_index >= len(parts):
            continue
        name = parts[name_index]
        actual[name] = {
            "Interface": name,
            "State": "DISABLED" if "X" in flags else (
                "UP" if "R" in flags else "DOWN / NOT RUNNING"
            ),
            "Type": parts[name_index + 1] if name_index + 1 < len(parts) else "—",
            "Actual MTU": parts[name_index + 2] if name_index + 2 < len(parts) else "—",
            "MAC address": parts[name_index + 3] if name_index + 3 < len(parts) else "—",
        }

    displayed = []
    for number in range(1, 6):
        name = f"ether{number}"
        displayed.append(actual.pop(name, {
            "Interface": name, "State": "Unavailable", "Type": "—",
            "Actual MTU": "—", "MAC address": "—",
        }))
    displayed.extend(actual.values())
    return displayed


def format_ping_terminal(identity: str, username: str, target: str,
                          rows: list[str], summary: PingSummary | None,
                          *, finished: bool = True) -> str:
    """Format only collected RouterOS ping data as a fixed-width terminal view."""
    parsed = ping_rows_for_display(rows)
    host_width = max(40, len("HOST"), *(len(row["Host"]) for row in parsed))
    lines = [f"[{username}@{identity}] > ping {target}"]
    lines.append(
        f"{'SEQ':>5} {'HOST':<{host_width}} {'SIZE':>4} {'TTL':>3} {'TIME':<10} STATUS"
    )
    for row in parsed:
        lines.append(
            f"{row['Sequence']:>5} {row['Host']:<{host_width}} "
            f"{row['Size']:>4} {row['TTL']:>3} {row['Time']:<10} {row['Status']}"
        )

    if finished:
        summary_line = format_ping_summary_line(summary) if summary else ""
        if summary_line:
            lines.append(f"    {summary_line}")
        if summary and summary.execution_status == "early_stop":
            stop_labels = {
                "packet_loss": "PACKET LOSS DETECTED — TEST STOPPED EARLY",
                "max_latency": "MAX LATENCY THRESHOLD EXCEEDED — TEST STOPPED EARLY",
            }
            lines.extend(f";;; {stop_labels[reason]}" for reason in summary.stop_reasons)
        elif summary and summary.execution_status != "completed":
            lines.append(
                f";;; Ping execution: {summary.execution_status.upper()}"
                + (f" — {summary.error}" if summary.error else "")
            )
        lines.append(f"[{username}@{identity}] >")
    return "\n".join(lines)


def counter_rows_for_display(output: str) -> list[dict[str, str]]:
    """Format the compact per-interface error/drop counters for the overview."""
    labels = ("Interface", "RX errors", "TX errors", "RX drops", "TX drops")
    displayed = []
    for line in output.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) == len(labels):
            displayed.append(dict(zip(labels, values)))
    return displayed


def address_rows_for_display(output: str) -> list[dict[str, str]]:
    """Show configured addresses without copying the full RouterOS table."""
    return [
        {"Address": address}
        for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b", output)
    ]


def route_rows_for_display(output: str) -> list[dict[str, str]]:
    """Summarize captured routes using only destination and gateway fields."""
    displayed = []
    for line in output.splitlines():
        destination = re.search(r"\bdst-address=([^\s]+)", line)
        gateway = re.search(r"\bgateway=([^\s,]+)", line)
        if destination or gateway:
            displayed.append({
                "Destination": destination.group(1) if destination else "—",
                "Gateway": gateway.group(1) if gateway else "—",
            })
    return displayed



def format_report_rtt(value) -> str:
    """Display numeric milliseconds or preserve RouterOS duration units."""
    if value is None or isinstance(value, bool):
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:g} ms" if math.isfinite(value) and value >= 0 else "N/A"
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|us|ns|d|h|m|s))+", value):
            return value
        try:
            numeric = float(value)
        except ValueError:
            return "N/A"
        return f"{numeric:g} ms" if math.isfinite(numeric) and numeric >= 0 else "N/A"
    return "N/A"


def render_diagnostic_report(st, report, *, inventory_confirmed=False, username="user",
                             assessment_renderer=None, widget_key_prefix="network_report"):
    def captured_transcript(captured):
        text = transcript_for(captured)
        if inventory_confirmed:
            text += "\n\nROUTES\n" + str(captured.get("routes") or "No route output returned.")
        return text

    st.subheader("Router overview")
    overview = st.columns(3)
    overview[0].metric("Router", str(report.get("router") or "Unknown"))
    overview[1].metric("Management IP", str(report.get("host") or "Unknown"))
    connection_state = "Simulation" if report.get("simulated") else (
        "Unreachable" if report.get("connection_status") == "host_unreachable" else "Connected"
    )
    overview[2].metric("Connection", connection_state)

    interfaces = interface_rows_for_display(str(report.get("interfaces", "")))
    if interfaces:
        st.markdown("**Interfaces**")
        st.dataframe(interfaces, hide_index=True, width="stretch")
    counters = counter_rows_for_display(str(report.get("counters", "")))
    if counters:
        st.markdown("**Interface error and drop counters**")
        st.dataframe(counters, hide_index=True, width="stretch")
    addresses = address_rows_for_display(str(report.get("addresses", "")))
    routes = route_rows_for_display(str(report.get("routes", "")))
    address_column, route_column = st.columns(2)
    with address_column:
        st.markdown("**Configured IP addresses**")
        if addresses:
            st.dataframe(addresses, hide_index=True, width="stretch")
        else:
            st.caption("No configured address was parsed from the captured output.")
    with route_column:
        st.markdown("**Routes**")
        if routes:
            st.dataframe(routes, hide_index=True, width="stretch")
        else:
            st.caption("Route output is available in captured evidence.")
    with st.expander("Captured Device Evidence", expanded=False):
        # Keep router command evidence available here while the ping details
        # appear once, in the lower structured results section.
        evidence_report = {**report, "pings": []}
        st.code(captured_transcript(evidence_report), language="shell", wrap_lines=True)

    gateway_ping = next(
        (item for item in report.get("pings", []) if item.get("target_name") == "Gateway"),
        None,
    )
    all_recommendations = recommendations_for(report)
    gateway_recommendations = [
        recommendation for recommendation in all_recommendations
        if gateway_ping is not None and "gateway" in recommendation.lower()
    ]
    if gateway_ping is not None:
        st.subheader("Gateway Ping")
        render_ping_terminal(st,
            ping_text_for_display(gateway_ping, format_ping_terminal(
                str(report.get("router") or report.get("host") or "Router"),
                "user" if report.get("simulated") else (
                    username or "user"
                ),
                str(gateway_ping.get("target", "")),
                gateway_ping.get("result_rows", []),
                gateway_ping.get("summary"),
            )),
        )
        if inventory_confirmed:
            st.caption(f"Gateway target: **{gateway_ping.get('target', 'Unknown')}** · Source: **Confirmed inventory record**")
            with st.expander("Gateway raw output"):
                render_ping_terminal(st, str(gateway_ping.get("raw") or "No raw output returned."))
        if assessment_renderer is None:
            for recommendation in gateway_recommendations:
                st.caption(f"Gateway assessment: {recommendation}")

    if inventory_confirmed:
        st.subheader("Confirmed inventory targets")
        st.write(f"Gateway target: **{gateway_ping.get('target') if gateway_ping else 'Not returned'}** · Source: **Confirmed inventory record**")
        st.write(f"Bank-router target: **{report.get('selected_bank_router') or 'Not returned'}** · Source: **Confirmed inventory record**")
    else:
        st.subheader("Bank-router IP Discovery")
        st.caption("Candidates are unverified RouterOS observations; confirm the actual bank-router address before testing it.")
        if report["bank_candidates"]:
            for candidate in report["bank_candidates"]:
                is_selected = candidate["address"] == report.get("selected_bank_router")
                st.write(f"{'Selected unverified candidate' if is_selected else 'Other unverified candidate'}: **{candidate['address']}** · Source: {candidate['evidence']}")
        else:
            st.info("No bank-router IP candidates were found in the ARP or neighbor tables. Enter the actual address manually in the sidebar.")
        st.caption("Routes and configured addresses are also captured as context; they are not automatically treated as bank-router candidates.")
    chosen = report.get("selected_bank_router")
    chosen_ping = next((item for item in report.get("pings", []) if item.get("target_name") == "Bank router"), None)
    transcript = captured_transcript(report)

    st.subheader("Bank-router Ping")
    if chosen:
        st.write(f"Target: **{chosen}** · Source: **{report.get('selected_candidate_source') or 'Manual IP'}**")
        if chosen_ping is None:
            st.warning("Ping was skipped because the target matches an address configured on the diagnostic router.")
        else:
            summary = chosen_ping.get("summary")
            if summary is None:
                st.info("Ping result: UNKNOWN — the collected output did not establish a complete result.")
            else:
                st.caption(f"Execution state: **{summary.execution_status.replace('_', ' ').title()}**")
                if summary.execution_status == "early_stop":
                    labels = {
                        "packet_loss": "PACKET LOSS DETECTED — TEST STOPPED EARLY",
                        "max_latency": "MAX LATENCY THRESHOLD EXCEEDED — TEST STOPPED EARLY",
                    }
                    for reason in summary.stop_reasons:
                        st.warning(labels.get(reason, reason))
                elif summary.execution_status != "completed":
                    st.error(f"Ping execution: **{summary.execution_status.upper()}**")
                    if summary.error:
                        st.caption(summary.error)
                else:
                    st.caption(f"Final assessment: **{summary.status}**")
                statistic_columns = st.columns(5)
                statistic_columns[0].metric("Packets attempted", summary.attempts_started)
                statistic_columns[1].metric("Confirmed sent", summary.sent)
                statistic_columns[2].metric("Received", summary.received)
                statistic_columns[3].metric("Lost", max(summary.sent - summary.received, 0))
                statistic_columns[4].metric("Packet loss", f"{summary.packet_loss:g}%" if summary.sent else "—")
                rtt_columns = st.columns(3)
                rtt_columns[0].metric("Minimum RTT", (format_report_rtt(summary.min_rtt) if inventory_confirmed else summary.min_rtt or "—"))
                rtt_columns[1].metric("Average RTT", (format_report_rtt(summary.avg_rtt) if inventory_confirmed else summary.avg_rtt or "—"))
                rtt_columns[2].metric("Maximum RTT", (format_report_rtt(summary.max_rtt) if inventory_confirmed else summary.max_rtt or "—"))
            render_ping_terminal(st,
                ping_text_for_display(chosen_ping, format_ping_terminal(
                    str(report.get("router") or report.get("host") or "Router"),
                    "user" if report.get("simulated") else (
                        username or "user"
                    ),
                    str(chosen), chosen_ping.get("result_rows", []), summary,
                )),
            )
        if inventory_confirmed and chosen_ping:
            with st.expander("Bank-router raw output"):
                render_ping_terminal(st, str(chosen_ping.get("raw") or "No raw output returned."))
        st.caption("An ICMP reply confirms reachability only; it does not verify that this address belongs to the bank router or that its application service is functioning.")
    else:
        st.info("No bank-router target selected. Candidate discovery and manual-IP testing remain available in the sidebar.")

    st.subheader("Diagnostic assessment and recommendations")
    if assessment_renderer is not None:
        assessment_renderer(st, report)
    else:
        if report.get("connection_status") == "host_unreachable":
            st.error("Management router connection failed; no router diagnostic evidence was collected.")
        else:
            st.success("Management router connection succeeded.")
        if chosen is None:
            st.info("No bank-router target has been selected or tested.")
        elif chosen_ping is None:
            st.info(f"Target {chosen} was not pinged because it matches an address configured on the diagnostic router.")
        else:
            assessment = chosen_ping.get("summary")
            if assessment is None:
                st.info("Bank-router ping assessment is unknown; the collected output did not establish a valid result.")
            elif assessment.execution_status == "completed":
                state_message = {
                    "PASS": "Selected target responded to ICMP and the test completed.",
                    "WARNING": "The completed ping test observed packet loss.",
                    "FAIL": "The completed ping test received no replies.",
                }.get(assessment.status, f"Ping test completed with assessment {assessment.status}.")
                st.write(state_message)
            elif assessment.execution_status == "early_stop":
                st.warning("Ping test deliberately stopped early after reaching a configured threshold.")
            else:
                st.warning(f"Ping test state: {assessment.execution_status}. {assessment.error or ''}".strip())
        for warning in report.get("warnings", []):
            st.warning(warning)
        for recommendation in all_recommendations:
            if recommendation not in gateway_recommendations:
                st.write(f"- {recommendation}")

    left, right = st.columns(2)
    with left:
        st.download_button("Export transcript (.txt)", transcript,
                           file_name="router-diagnostics.txt", mime="text/plain",
                           key=f"{widget_key_prefix}_transcript",
                           use_container_width=True)
    with right:
        export_report = {
            **{key: value for key, value in report.items() if key != "pings"},
            "pings": [
                {**{key: value for key, value in ping.items() if key != "summary"},
                 "summary": ping["summary"].__dict__ if ping["summary"] else None}
                for ping in report["pings"]
            ],
            "transcript": transcript,
        }
        st.download_button("Export evidence (.json)", json.dumps(export_report, indent=2),
                           file_name="router-diagnostics.json", mime="application/json",
                           key=f"{widget_key_prefix}_evidence",
                           use_container_width=True)
