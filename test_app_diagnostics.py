
"""Focused tests for the dashboard's one-target discovery workflow."""

import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import app
import diagnostics_service
from ping_diagnostics import parse_ping_summary
from simulation import simulated_report


def fake_connection(candidates):
    connection = Mock()
    connection.ping_commands = []

    def send(command, **_kwargs):
        if command == "/system identity print":
            return "name: NETE-LAB"
        if command == "/interface print":
            return "0 R ether1"
        if command == "/interface print stats":
            return ""
        if command.startswith(":foreach"):
            return "ether1,0,0,0,0"
        if command == "/ip address print":
            return "0 192.168.128.2/24 interface=ether1"
        if command == "/ip route print detail":
            return ""
        if command == "/ip arp print detail":
            return "\n".join(
                f"address={item['address']}" for item in candidates
            )
        if command == "/ip neighbor print detail":
            return ""
        if command.startswith("/ping "):
            return (
                "sent=1 received=1 packet-loss=0% "
                "min-rtt=1ms avg-rtt=1ms max-rtt=1ms"
            )
        raise AssertionError(f"Unexpected RouterOS command: {command}")

    connection.send_command.side_effect = send

    def write_channel(data):
        if data.startswith("/ping "):
            connection.ping_commands.append(data.strip())
            count = int(data.split("count=", 1)[1].split()[0])
            target = data.split("address=", 1)[1].split()[0]
            rows = [
                f"{i} {target} 56 64 1ms\n"
                for i in range(count)
            ]
            connection.read_channel.side_effect = [
                "SEQ HOST SIZE TTL TIME STATUS\n",
                *rows,
                (
                    f"sent={count} received={count} packet-loss=0% "
                    "min-rtt=1ms avg-rtt=1ms max-rtt=1ms\n"
                ),
            ]
        elif data == "\x03":
            connection.read_channel.side_effect = [
                "[lab@router] > "
            ]

    connection.write_channel.side_effect = write_channel
    connection.read_channel.side_effect = [""]
    return connection


class DashboardDiscoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {
                "address": "192.168.128.9",
                "evidence": "RouterOS ARP table",
            },
            {
                "address": "192.168.128.3",
                "evidence": "RouterOS ARP table",
            },
        ]

    def run_with_candidates(self, candidates, target=None):
        connection = fake_connection(candidates)

        with patch.object(
            diagnostics_service,
            "ConnectHandler",
            return_value=connection,
        ):
            report = app.run_diagnostics(
                "192.168.128.2",
                "lab-user",
                "in-memory-secret",
                None,
                target,
                10,
            )

        ping_commands = connection.ping_commands
        return report, ping_commands

    def test_initial_run_pings_only_highest_priority_discovered_candidate(self):
        report, commands = self.run_with_candidates(self.candidates)

        self.assertEqual(
            commands,
            ["/ping address=192.168.128.3 count=10 interval=250ms"],
        )
        self.assertEqual(
            report["selected_bank_router"],
            "192.168.128.3",
        )
        self.assertEqual(
            report["selected_candidate_source"],
            "RouterOS ARP table",
        )
        self.assertEqual(
            len(report["pings"][0]["result_rows"]),
            10,
        )

    def test_engineer_can_test_another_discovered_candidate_on_demand(self):
        report, commands = self.run_with_candidates(
            self.candidates,
            "192.168.128.9",
        )

        self.assertEqual(
            commands,
            ["/ping address=192.168.128.9 count=10 interval=250ms"],
        )
        self.assertEqual(
            report["selected_bank_router"],
            "192.168.128.9",
        )

    def test_manual_custom_ip_is_tested_when_discovery_is_empty(self):
        report, commands = self.run_with_candidates(
            [],
            "198.51.100.20",
        )

        self.assertEqual(
            commands,
            ["/ping address=198.51.100.20 count=10 interval=250ms"],
        )
        self.assertEqual(
            report["bank_discovery_status"],
            "manual_ip",
        )
        self.assertEqual(
            report["selected_candidate_source"],
            "Manual IP",
        )

    def test_empty_discovery_without_manual_target_does_not_ping_a_candidate(self):
        report, commands = self.run_with_candidates([])

        self.assertEqual(commands, [])
        self.assertIsNone(report["selected_bank_router"])


class DashboardPresentationTests(unittest.TestCase):
    def test_ping_response_rows_are_split_into_readable_columns(self):
        rows = app.ping_rows_for_display([
            "0 192.168.128.3 56 64 2ms104us",
            "1 192.168.128.3 timeout",
            "2 192.168.128.3 host unreachable",
        ])

        self.assertEqual(rows[0], {
            "Sequence": "0",
            "Host": "192.168.128.3",
            "Size": "56",
            "TTL": "64",
            "Time": "2ms104us",
            "Status": "Reply",
        })
        self.assertEqual(rows[1]["Status"], "timeout")
        self.assertEqual(rows[1]["Time"], "—")
        self.assertEqual(rows[2]["Status"], "host unreachable")

    def test_router_overview_only_uses_captured_interface_and_counter_values(self):
        interfaces = app.interface_rows_for_display(
            "Flags: R - RUNNING\n# NAME TYPE MTU MAC\n"
            "0 R ether1 ether 1500 AA:BB:CC:DD:EE:01\n"
            "1 lo loopback 65536"
        )
        counters = app.counter_rows_for_display(
            "ether1,0,1,2,3\nlo,0,0,0,0"
        )

        self.assertEqual(
            [row["Interface"] for row in interfaces],
            [
                "ether1",
                "ether2",
                "ether3",
                "ether4",
                "ether5",
                "lo",
            ],
        )
        self.assertEqual(interfaces[0]["State"], "UP")
        self.assertEqual(interfaces[0]["Type"], "ether")
        self.assertEqual(
            interfaces[0]["MAC address"],
            "AA:BB:CC:DD:EE:01",
        )
        self.assertEqual(interfaces[1]["State"], "Unavailable")
        self.assertEqual(interfaces[-1]["Interface"], "lo")
        self.assertEqual(counters[0]["TX drops"], "3")

    def test_simulator_interfaces_are_preserved_and_missing_ports_are_unavailable(self):
        report = simulated_report(
            "Candidate discovered and reachable",
            "198.51.100.20",
            "192.0.2.21",
        )
        interfaces = app.interface_rows_for_display(
            report["interfaces"]
        )
        by_name = {
            row["Interface"]: row for row in interfaces
        }

        self.assertEqual(
            list(by_name)[:5],
            ["ether1", "ether2", "ether3", "ether4", "ether5"],
        )
        self.assertEqual(by_name["ether1"]["State"], "UP")
        self.assertEqual(by_name["ether1"]["Type"], "ether")
        self.assertEqual(
            by_name["ether2"]["State"],
            "DOWN / NOT RUNNING",
        )
        self.assertEqual(
            by_name["ether3"]["State"],
            "Unavailable",
        )
        self.assertEqual(by_name["ether4"]["Actual MTU"], "—")
        self.assertEqual(by_name["ether5"]["MAC address"], "—")

    def test_ping_terminal_has_aligned_routeros_prompt_rows_and_final_summary(self):
        summary = parse_ping_summary(
            "sent=2 received=2 packet-loss=0% "
            "min-rtt=2ms avg-rtt=3ms max-rtt=4ms",
            2,
        )
        terminal = app.format_ping_terminal(
            "NETE-LAB",
            "lab-user",
            "192.0.2.55",
            [
                "0 192.0.2.55 56 64 2ms",
                "1 192.0.2.55 56 64 4ms",
            ],
            summary,
        )
        lines = terminal.splitlines()

        self.assertEqual(
            lines[0],
            "[lab-user@NETE-LAB] > ping 192.0.2.55",
        )
        self.assertEqual(
            lines[1].index("SEQ") + len("SEQ") - 1,
            lines[2].index("0"),
        )
        self.assertEqual(
            lines[1].index("HOST"),
            lines[2].index("192.0.2.55"),
        )
        self.assertEqual(
            lines[1].index("SIZE") + len("SIZE") - 1,
            lines[2].index("56") + len("56") - 1,
        )
        self.assertEqual(
            lines[1].index("TTL") + len("TTL") - 1,
            lines[2].index("64") + len("64") - 1,
        )
        self.assertEqual(
            lines[1].index("TIME"),
            lines[2].index("2ms"),
        )
        self.assertEqual(
            lines[1].index("STATUS"),
            lines[2].index("Reply"),
        )
        self.assertIn("HOST", lines[1])
        self.assertIn("SIZE", lines[1])
        self.assertIn("TTL", lines[1])
        self.assertIn("TIME", lines[1])
        self.assertIn("STATUS", lines[1])
        self.assertIn("0 192.0.2.55", lines[2])
        self.assertIn(
            "sent=2 received=2 packet-loss=0%",
            lines[-2],
        )
        self.assertEqual(
            lines[-1],
            "[lab-user@NETE-LAB] >",
        )

    def test_live_ping_terminal_does_not_show_final_summary_or_closing_prompt_early(self):
        terminal = app.format_ping_terminal(
            "192.168.0.10",
            "user",
            "192.0.2.55",
            ["0 192.0.2.55 56 64 2ms"],
            None,
            finished=False,
        )

        self.assertTrue(
            terminal.startswith(
                "[user@192.168.0.10] > ping 192.0.2.55\n"
            )
        )
        self.assertNotIn("sent=", terminal)
        self.assertNotIn("[user@192.168.0.10] >\n", terminal)

    def test_gateway_ping_terminal_is_rendered_above_discovery_only_when_present(self):
        script = r'''
from streamlit.testing.v1 import AppTest
from simulation import simulated_report
from ping_diagnostics import parse_ping_summary

app_test = AppTest.from_file("app.py").run(timeout=20)
app_test.radio[0].set_value("Offline simulation")
report = simulated_report(
    "Candidate discovered and reachable",
    "198.51.100.20",
    "192.0.2.21",
)
app_test.session_state["report"] = report
app_test.run(timeout=20)
assert "Gateway Ping" not in [
    item.value for item in app_test.subheader
]

target = "192.168.0.254"
summary_text = (
    "sent=1 received=1 packet-loss=0% "
    "min-rtt=2ms avg-rtt=2ms max-rtt=2ms"
)
report["pings"].insert(0, {
    "target_name": "Gateway",
    "target": target,
    "raw": summary_text,
    "result_rows": [f"0 {target} 56 64 2ms"],
    "summary": parse_ping_summary(summary_text, 1),
})
app_test.session_state["report"] = report
app_test.run(timeout=20)

assert not [
    item.message for item in app_test.exception
]
headings = [
    item.value for item in app_test.subheader
]
assert (
    headings.index("Gateway Ping")
    < headings.index("Bank-router IP Discovery")
    < headings.index("Bank-router Ping")
)

terminal_blocks = [
    __import__("html").unescape(item.value) for item in app_test.get('html')
    if 'class="winbox-ping-terminal"' in item.proto.body
]
assert any(
    f"[user@NETE-SIMULATED] > ping {target}" in value
    and f"0 {target}" in value
    and summary_text in value
    and "[user@NETE-SIMULATED] ></pre>" in value
    for value in terminal_blocks
), terminal_blocks

buttons = [
    item.label for item in app_test.get("download_button")
]
assert (
    "Export transcript (.txt)" in buttons
    and "Export evidence (.json)" in buttons
)
'''

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()

