"""Streamlit dashboard for read-only RouterOS diagnostics."""

from __future__ import annotations

import ipaddress
import json
import re

import streamlit as st
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from report_formatting import recommendations_for, transcript_for
from ping_diagnostics import PingSummary, format_ping_summary_line
from diagnostics_service import run_diagnostics
from diagnostic_report_ui import (
    render_diagnostic_report, render_ping_terminal, ping_rows_for_display, interface_rows_for_display,
    format_ping_terminal, counter_rows_for_display, address_rows_for_display,
    route_rows_for_display,
)


st.set_page_config(page_title="NETE | Router Diagnostics", page_icon="⌘", layout="wide")
st.markdown(
    """
    <style>
    .stApp { background: #101820; color: #e8f1f2; }
    [data-testid="stHeader"] { background: rgba(16, 24, 32, .92); }
    .terminal-label { color: #69d5c0; font: 700 .78rem ui-monospace, monospace;
        letter-spacing: .12em; margin: .3rem 0 .6rem; }
    .hint { color: #a8b7bf; }
    div[data-testid="stCode"] { border: 1px solid #33515a; border-radius: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

workspace = st.sidebar.radio(
    "Workspace",
    ("Network Diagnosis", "Email Proactiveness"),
    index=0,
)
if workspace == "Email Proactiveness":
    from email_proactiveness import render_email_proactiveness

    render_email_proactiveness()
    st.stop()


def valid_ip(value: str, label: str, *, optional: bool = False) -> str | None:
    value = value.strip()
    if not value and optional:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid IPv4 or IPv6 address.") from exc


def run_live_with_progress(host: str, username: str, password: str,
                           gateway: str | None, bank_router: str | None,
                           packet_count: int) -> dict[str, object]:
    """Render progress from RouterOS rows as they are read from the SSH channel."""
    with st.status("Connecting and collecting router diagnostics…", expanded=True) as status:
        progress = st.progress(0)
        detail = st.empty()
        rows_by_target: dict[tuple[str, str], list[str]] = {}

        def show_result(label: str, target: str, summary: PingSummary,
                        result_row: str | None = None,
                        device_identity: str | None = None) -> None:
            fraction = min(summary.sent / packet_count, 1.0) if packet_count else 0.0
            progress.progress(fraction)
            key = (label, target)
            if result_row:
                rows_by_target.setdefault(key, []).append(result_row)
            finished = result_row is None
            with detail.container():
                loss = f"{summary.packet_loss:g}%" if summary.sent else "not available"
                st.caption(
                    f"{label} {target} · sent {summary.sent}/{packet_count} · "
                    f"received {summary.received} · lost {summary.sent - summary.received} "
                    f"({loss}) · RTT min/avg/max "
                    f"{summary.min_rtt or '—'} / {summary.avg_rtt or '—'} / {summary.max_rtt or '—'}"
                )
                render_ping_terminal(st,
                    format_ping_terminal(
                        device_identity or host, username.strip() or "user", target,
                        rows_by_target.get(key, []), summary if finished else None,
                        finished=finished,
                    ),
                )
                if summary.execution_status == "early_stop":
                    stop_state = "STOPPED EARLY" if finished else "STOPPING EARLY"
                    for reason, message in (
                        ("packet_loss", f"PACKET LOSS DETECTED — TEST {stop_state}"),
                        ("max_latency", f"MAX LATENCY THRESHOLD EXCEEDED — TEST {stop_state}"),
                    ):
                        if reason in summary.stop_reasons:
                            st.warning(message)
                elif finished and summary.execution_status != "completed":
                    st.warning(f"Ping stopped with {summary.execution_status}: {summary.error}")

        try:
            report = run_diagnostics(
                host, username, password, gateway, bank_router, packet_count,
                progress_callback=show_result,
            )
        except Exception:
            status.update(label="Diagnostics did not complete", state="error")
            raise
        status.update(label="Diagnostics complete", state="complete")
        # The final ping table is rendered in the lower results section below;
        # clear the temporary stream view so the completed run is not duplicated.
        detail.empty()
        return report


st.title("NETE · Router Diagnostics")
st.caption("Read-only MikroTik checks · terminal evidence")
run_mode = st.radio("Run mode", ("Live router", "Offline simulation"), horizontal=True)
st.caption(f"Current operating mode: **{run_mode}**")
if run_mode == "Offline simulation":
    st.info("SIMULATION MODE — deterministic mock data only. No SSH connection or network probe is made.")

with st.sidebar:
    st.subheader("Connection")
    previous_candidates = st.session_state.get("bank_candidates", [])
    candidate_options = [item["address"] for item in previous_candidates]
    if run_mode == "Offline simulation":
        with st.form("simulation", clear_on_submit=False):
            scenario = st.selectbox("Mock scenario", (
                "Candidate discovered and reachable",
                "Discovered but unreachable (timeouts)",
                "Management host unreachable",
                "No candidate; manual IP",
                "Multiple candidates; select one",
                "Target equals diagnostic router IP",
            ))
            sim_candidate = st.selectbox(
                "Select candidate (multiple-candidate scenario)",
                ("192.0.2.21", "192.0.2.22"),
            )
            sim_manual_ip = st.text_input("Manual bank-router IP", value="198.51.100.20")
            submitted = st.form_submit_button("Run simulation", type="primary", use_container_width=True)
    else:
        st.caption("Credentials stay in this active browser session and are never included in exports.")
        if st.button("Clear Credentials", use_container_width=True):
            st.session_state["router_password"] = ""
            st.session_state["router_username"] = ""
        with st.form("diagnostics", clear_on_submit=False):
            host_input = st.text_input("Management IP", placeholder="192.0.2.10", key="router_host")
            username = st.text_input("SSH username", key="router_username")
            password = st.text_input("SSH password", type="password", key="router_password")
            gateway_input = st.text_input("Gateway IP (optional)", key="gateway_input")
            selected_candidate = st.selectbox(
                "Unverified bank-router candidate (optional)", [""] + candidate_options,
                format_func=lambda value: "Select a candidate…" if value == "" else value,
            )
            bank_input = st.text_input("Actual bank-router IP (enter or override candidate)")
            packet_count = st.selectbox("Ping packet count", (5, 10, 20, 100), index=2, key="packet_count")
            submitted = st.form_submit_button("Run read-only checks", type="primary", use_container_width=True)

if submitted and run_mode == "Live router":
    st.session_state.pop("report", None)
    try:
        host = valid_ip(host_input, "Management IP")
        gateway = valid_ip(gateway_input, "Gateway IP", optional=True)
        bank_router = valid_ip(bank_input or selected_candidate, "Bank router IP", optional=True)
        if not username.strip() or not password:
            raise ValueError("SSH username and password are required.")
        st.session_state["report"] = run_live_with_progress(
            host, username.strip(), password, gateway, bank_router, packet_count
        )
        st.session_state["bank_candidates"] = st.session_state["report"]["bank_candidates"]
    except ValueError as exc:
        st.error(str(exc))
    except NetmikoAuthenticationException:
        st.error("Authentication failed. Check the SSH username and password.")
    except NetmikoTimeoutException:
        st.error("Connection timed out. Check the management IP and network path.")
    except Exception as exc:
        st.error("Diagnostics did not complete. Check the router connection and try again.")
elif submitted and run_mode == "Offline simulation":
    from simulation import simulated_report
    try:
        st.session_state["report"] = simulated_report(scenario, sim_manual_ip, sim_candidate)
        st.session_state["bank_candidates"] = st.session_state["report"]["bank_candidates"]
    except ValueError as exc:
        st.error(str(exc))

with st.sidebar:
    saved_candidates = st.session_state.get("bank_candidates", [])
    if run_mode == "Live router" and saved_candidates:
        with st.form("test_other_candidate"):
            candidate_ips = [item["address"] for item in saved_candidates]
            alternate_ip = st.selectbox("Choose another discovered candidate", candidate_ips)
            retest_count = st.selectbox("Candidate ping count", (5, 10, 20, 100), index=2, key="candidate_ping_count")
            test_candidate = st.form_submit_button("Test Selected Candidate", use_container_width=True)
        if test_candidate:
            st.session_state.pop("report", None)
            try:
                st.session_state["report"] = run_live_with_progress(
                    valid_ip(st.session_state.get("router_host", ""), "Management IP"),
                    st.session_state.get("router_username", "").strip(),
                    st.session_state.get("router_password", ""),
                    valid_ip(st.session_state.get("gateway_input", ""), "Gateway IP", optional=True),
                    valid_ip(alternate_ip, "Bank router IP"), retest_count,
                )
            except (NetmikoAuthenticationException, NetmikoTimeoutException):
                st.error("SSH connection failed. Check the management address and credentials.")
            except Exception:
                st.error("The candidate check did not complete. Check the entered values and try again.")
        with st.form("ping_custom_ip"):
            custom_ip = st.text_input("Manual custom IP", key="custom_bank_ip")
            custom_count = st.selectbox("Custom ping count", (5, 10, 20, 100), index=2, key="custom_ping_count")
            ping_custom = st.form_submit_button("Ping Custom IP", use_container_width=True)
        if ping_custom:
            st.session_state.pop("report", None)
            try:
                target = valid_ip(custom_ip, "Custom IP")
                st.session_state["report"] = run_live_with_progress(
                    valid_ip(st.session_state.get("router_host", ""), "Management IP"),
                    st.session_state.get("router_username", "").strip(),
                    st.session_state.get("router_password", ""),
                    valid_ip(st.session_state.get("gateway_input", ""), "Gateway IP", optional=True),
                    target, custom_count,
                )
            except (NetmikoAuthenticationException, NetmikoTimeoutException):
                st.error("SSH connection failed. Check the management address and credentials.")
            except Exception:
                st.error("The custom IP check did not complete. Check the entered values and try again.")
    elif run_mode == "Live router":
        with st.form("ping_custom_ip_no_candidates"):
            custom_ip = st.text_input("Manual custom IP", key="custom_bank_ip_empty")
            custom_count = st.selectbox("Custom ping count", (5, 10, 20, 100), index=2, key="custom_ping_count_empty")
            ping_custom = st.form_submit_button("Ping Custom IP", use_container_width=True)
        if ping_custom:
            st.session_state.pop("report", None)
            try:
                target = valid_ip(custom_ip, "Custom IP")
                st.session_state["report"] = run_live_with_progress(
                    valid_ip(st.session_state.get("router_host", ""), "Management IP"),
                    st.session_state.get("router_username", "").strip(),
                    st.session_state.get("router_password", ""),
                    valid_ip(st.session_state.get("gateway_input", ""), "Gateway IP", optional=True),
                    target, custom_count,
                )
            except (NetmikoAuthenticationException, NetmikoTimeoutException):
                st.error("SSH connection failed. Check the management address and credentials.")
            except Exception:
                st.error("The custom IP check did not complete. Check the entered values and try again.")

report = st.session_state.get("report")
if report and bool(report.get("simulated")) != (run_mode == "Offline simulation"):
    report = None
if report:
    if report.get("connection_status") == "host_unreachable":
        st.error("Diagnostic run finished with a management connection failure.")
    elif report.get("simulated"):
        st.info("Offline simulation result is ready.")
    else:
        st.success("Diagnostic run complete · management router connected.")
    if report.get("simulated"):
        st.warning("SIMULATED EVIDENCE — all addresses, discovery results and ping results below are deterministic mock data.")
    if report.get("connection_status") == "host_unreachable":
        st.error("Mock management host is unreachable; no router session or bank-router probe was made.")

    render_diagnostic_report(
        st, report, username=str(st.session_state.get("router_username", "")).strip(),
    )

else:
    st.info("Enter the router details in the sidebar. No connection is made until you submit the form.")
