"""Safe local checks for network_ssh failure handling."""

import contextlib
import io
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock
from unittest.mock import patch

# The production dependency is not needed for these local mocked checks.
if importlib.util.find_spec("netmiko") is None:
    netmiko = types.ModuleType("netmiko")
    exceptions = types.ModuleType("netmiko.exceptions")

    class NetmikoAuthenticationException(Exception):
        pass

    class NetmikoTimeoutException(Exception):
        pass

    netmiko.ConnectHandler = None
    exceptions.NetmikoAuthenticationException = NetmikoAuthenticationException
    exceptions.NetmikoTimeoutException = NetmikoTimeoutException
    sys.modules["netmiko"] = netmiko
    sys.modules["netmiko.exceptions"] = exceptions
else:
    from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

import network_ssh
from ping_diagnostics import _format_milliseconds, rtt_milliseconds


class FakePingChannel:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.pending = []
        self.writes = []

    def write_channel(self, data):
        self.writes.append(data)
        if data == "\x03":
            self.pending.append("[lab@router] > ")
        elif self.error:
            self.pending.append(self.error)
        elif self.output is not None:
            self.pending.extend(self.output)

    def read_channel(self):
        if self.pending:
            item = self.pending.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ""


def ping_output(rows, count, received=None):
    received = len(rows) if received is None else received
    loss = (count - received) * 100 / count
    summary = f"sent={count} received={received} packet-loss={loss:g}%"
    if received:
        rtts = [network_ssh._ping_observation_from_row(row).avg_rtt for row in rows
                if "timeout" not in row.lower()]
        values = [rtt_milliseconds(value) for value in rtts]
        avg = _format_milliseconds(sum(values) / len(values))
        summary += f" min-rtt={rtts[0]} avg-rtt={avg} max-rtt={rtts[-1]}"
    return ["SEQ HOST SIZE TTL TIME STATUS\n"] + [row + "\n" for row in rows] + [summary + "\n", "[admin@CHR] >"]


class DiagnosticFailureTests(unittest.TestCase):
    def test_discovery_merges_arp_and_neighbor_evidence_without_guessing(self):
        connection = Mock()
        connection.send_command.side_effect = [
            "0 address=192.0.2.20 mac-address=AA:BB interface=ether1\n1 address=192.0.2.20",
            "0 address=192.0.2.20 identity=bank-router interface=ether1\n1 address=not-an-ip",
        ]

        candidates = network_ssh.discover_bank_router_candidates(connection)

        self.assertEqual(candidates, [{
            "address": "192.0.2.20",
            "evidence": "RouterOS ARP table; RouterOS neighbor discovery",
        }])
        self.assertEqual(connection.send_command.call_count, 2)

    def test_likely_next_address_is_prioritized_only_when_discovered(self):
        candidates = [
            {"address": "192.168.128.9", "evidence": "RouterOS ARP table"},
            {"address": "192.168.128.3", "evidence": "RouterOS neighbor discovery"},
        ]
        ordered = network_ssh.prioritize_bank_router_candidates(
            candidates, "192.168.128.2", ["192.168.128.2/24"]
        )
        self.assertEqual(ordered[0]["address"], "192.168.128.3")
        self.assertEqual(ordered[0]["evidence"], "RouterOS neighbor discovery")

    def test_missing_preferred_address_does_not_invent_a_candidate(self):
        candidates = [{"address": "192.168.128.9", "evidence": "RouterOS ARP table"}]
        ordered = network_ssh.prioritize_bank_router_candidates(
            candidates, "192.168.128.2", ["192.168.128.2/24"]
        )
        self.assertEqual(ordered, candidates)

    def test_initial_ping_helper_runs_one_bounded_target(self):
        rows = [f"{i} 192.168.128.3 56 64 1ms" for i in range(5)]
        connection = FakePingChannel(ping_output(rows, 5))
        result = network_ssh.ping_router_target(connection, "192.168.128.3", 5)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["raw"].count("sent="), 1)
        self.assertTrue(result["raw"].startswith("sent=5 received=5 packet-loss=0%"))
        self.assertEqual(connection.writes, ["/ping address=192.168.128.3 count=5 interval=250ms\n"])

    def test_ping_rows_stream_without_repeating_aggregate_lines(self):
        rows = [f"{i} 192.168.128.3 56 64 2ms9us" for i in range(5)]
        connection = FakePingChannel(ping_output(rows, 5))
        progress = Mock()

        result = network_ssh.ping_router_target(
            connection, "192.168.128.3", 5, progress_callback=progress
        )

        expected_rows = [f"{i} 192.168.128.3 56 64 2ms9us" for i in range(5)]
        self.assertEqual(result["result_rows"], expected_rows)
        self.assertEqual(progress.call_count, 5)
        self.assertEqual([call.args[1] for call in progress.call_args_list], expected_rows)
        self.assertEqual(result["raw"].count("sent="), 1)
        self.assertNotIn("sent=1", "\n".join(result["result_rows"]))

    def test_routeros_summary_with_returned_prompt_is_verified_against_twenty_rows(self):
        rows = ["0 192.168.128.3 56 64 1ms711us"]
        rows.extend(f"{i} 192.168.128.3 56 64 3ms798us" for i in range(1, 18))
        rows.append("18 192.168.128.3 56 64 3ms805us")
        rows.append("19 192.168.128.3 56 64 4ms698us")
        output = ["SEQ HOST SIZE TTL TIME STATUS\n"]
        output.extend(row + "\n" for row in rows)
        output.append(
            "sent=20 received=20 packet-loss=0% min-rtt=1ms711us "
            "avg-rtt=3ms739us max-rtt=4ms698us\r\n[admin@CHR] >"
        )
        result = network_ssh.ping_router_target(
            FakePingChannel(output), "192.168.128.3", 20
        )
        summary = result["summary"]
        self.assertEqual(summary.execution_status, "completed")
        self.assertEqual(summary.status, "PASS")
        self.assertEqual((summary.sent, summary.received, summary.packet_loss), (20, 20, 0))
        self.assertEqual(summary.min_rtt, "1ms711us")
        self.assertEqual(summary.avg_rtt, "3ms739us")
        self.assertEqual(summary.max_rtt, "4ms698us")

    def test_intermediate_summaries_do_not_end_one_hundred_packet_stream(self):
        rows = [f"{i} 192.168.128.3 56 64 2ms" for i in range(100)]
        output = ["SEQ HOST SIZE TTL TIME STATUS\n"]
        output.extend(row + "\n" for row in rows)
        output.extend(["sent=20 received=20 packet-loss=0%\n",
                       "sent=40 received=40 packet-loss=0%\n",
                       "sent=100 received=100 packet-loss=0% min-rtt=2ms avg-rtt=2ms max-rtt=2ms\n",
                       "[admin@CHR] >"])
        connection = FakePingChannel(output)
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 100)["summary"]
        self.assertEqual(summary.execution_status, "completed")
        self.assertEqual((summary.sent, summary.received, summary.avg_rtt), (100, 100, "2ms"))
        self.assertEqual(len(connection.pending), 0)

    def test_mismatched_target_row_is_preserved_and_unknown(self):
        output = ping_output(["0 192.168.128.1 56 64 2ms"], 5)
        summary_result = network_ssh.ping_router_target(
            FakePingChannel(output), "192.168.128.3", 5
        )
        self.assertEqual(summary_result["summary"].execution_status, "unknown")
        self.assertIn("192.168.128.1", summary_result["result_rows"][0])

    def test_missing_prompt_taints_channel_and_prevents_next_ping(self):
        connection = FakePingChannel(ping_output(
            [f"{i} 192.168.128.3 56 64 2ms" for i in range(5)], 5
        )[:-1])
        with patch.object(network_ssh.time, "monotonic", side_effect=[0, 36]):
            first = network_ssh.ping_router_target(connection, "192.168.128.3", 5)
        second = network_ssh.ping_router_target(connection, "192.168.128.1", 5)
        self.assertEqual(first["summary"].execution_status, "unknown")
        self.assertEqual(second["summary"].execution_status, "unknown")
        self.assertEqual(connection.writes, [
            "/ping address=192.168.128.3 count=5 interval=250ms\n", "\x03"
        ])

    def test_consecutive_target_pings_keep_each_hosts_rows_isolated(self):
        class SequencedChannel(FakePingChannel):
            def __init__(self, outputs):
                super().__init__()
                self.outputs = iter(outputs)

            def write_channel(self, data):
                self.writes.append(data)
                if data != "\x03":
                    self.pending.extend(next(self.outputs))

        first_rows = [f"{i} 192.168.128.3 56 64 2ms" for i in range(5)]
        second_rows = [f"{i} 192.168.128.1 56 64 3ms" for i in range(5)]
        connection = SequencedChannel([ping_output(first_rows, 5), ping_output(second_rows, 5)])
        first = network_ssh.ping_router_target(connection, "192.168.128.3", 5)
        second = network_ssh.ping_router_target(connection, "192.168.128.1", 5)
        self.assertEqual(first["summary"].execution_status, "completed")
        self.assertTrue(all("192.168.128.3" in row for row in first["result_rows"]))
        self.assertEqual(second["summary"].execution_status, "completed")
        self.assertTrue(all("192.168.128.1" in row for row in second["result_rows"]))

    def test_summary_that_conflicts_with_individual_rows_is_not_pass(self):
        rows = [f"{i} 192.168.128.3 56 64 2ms" for i in range(19)]
        rows.append("19 192.168.128.3 timeout")
        output = ["SEQ HOST SIZE TTL TIME STATUS\n", *(row + "\n" for row in rows),
                  "sent=20 received=20 packet-loss=0% min-rtt=2ms avg-rtt=2ms max-rtt=2ms\n",
                  "[admin@CHR] >"]
        summary = network_ssh.ping_router_target(
            FakePingChannel(output), "192.168.128.3", 20
        )["summary"]
        self.assertEqual(summary.execution_status, "unknown")
        self.assertNotEqual(summary.status, "PASS")

    def test_prompt_with_incomplete_rows_remains_unknown(self):
        output = ["SEQ HOST SIZE TTL TIME STATUS\n",
                  "0 192.168.128.3 56 64 2ms\n",
                  "1 192.168.128.3 56 64 2ms\n",
                  "[admin@CHR] >"]
        summary = network_ssh.ping_router_target(
            FakePingChannel(output), "192.168.128.3", 20
        )["summary"]
        self.assertEqual(summary.execution_status, "unknown")
        self.assertIn("before all requested", summary.error)

    def test_ping_helper_rejects_invalid_ip_and_bounds_transport_failure(self):
        connection = FakePingChannel(error=NetmikoTimeoutException("read timeout"))
        with self.assertRaises(ValueError):
            network_ssh.ping_router_target(connection, "not-an-ip", 5)
        result = network_ssh.ping_router_target(connection, "192.168.128.3", 100)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(connection.writes[0], "/ping address=192.168.128.3 count=100 interval=250ms\n")

    def test_packet_loss_stops_after_two_confirmed_timeouts(self):
        connection = FakePingChannel(["SEQ HOST SIZE TTL TIME STATUS\n",
                                      "0 192.168.128.3 timeout\n",
                                      "1 192.168.128.3 timeout\n"])
        result = network_ssh.ping_router_target(connection, "192.168.128.3", 100)
        summary = result["summary"]
        self.assertEqual(summary.execution_status, "early_stop")
        self.assertEqual(summary.stop_reasons, ("packet_loss",))
        self.assertEqual((summary.attempts_started, summary.sent, summary.received), (2, 2, 0))
        self.assertEqual(summary.sent - summary.received, 2)
        self.assertEqual(summary.packet_loss, 100)
        self.assertIn("\x03", connection.writes)

    def test_latency_threshold_stops_after_slow_successful_reply(self):
        connection = FakePingChannel(["SEQ HOST SIZE TTL TIME STATUS\n",
                                      "0 192.168.128.3 56 64 100ms\n"])
        progress = Mock()
        result = network_ssh.ping_router_target(
            connection, "192.168.128.3", 100, progress_callback=progress
        )
        summary = result["summary"]
        self.assertEqual(summary.execution_status, "early_stop")
        self.assertEqual(summary.stop_reasons, ("max_latency",))
        self.assertEqual(summary.attempts_started, 1)
        self.assertEqual(summary.max_rtt, "100ms")
        self.assertIn("\x03", connection.writes)
        self.assertEqual(progress.call_count, 1)
        self.assertEqual(progress.call_args.args[0].execution_status, "early_stop")

    def test_normal_completion_and_observed_loss_statistics(self):
        rows = [f"{i} 192.168.128.3 56 64 {i * 10}ms" for i in range(1, 6)]
        connection = FakePingChannel(ping_output(rows, 5))
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 5)["summary"]
        self.assertEqual(summary.execution_status, "completed")
        self.assertEqual((summary.attempts_started, summary.sent, summary.received), (5, 5, 5))
        self.assertEqual(summary.packet_loss, 0)
        self.assertEqual((summary.min_rtt, summary.avg_rtt, summary.max_rtt), ("10ms", "30ms", "50ms"))

    def test_packet_loss_percentage_uses_confirmed_completed_attempts(self):
        rows = [f"{i} 192.168.128.3 56 64 10ms" for i in range(4)]
        rows.append("4 192.168.128.3 timeout")
        connection = FakePingChannel(ping_output(rows, 5, received=4))
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 5)["summary"]
        self.assertEqual(summary.packet_loss, 20)
        self.assertEqual((summary.sent, summary.received), (5, 4))

    def test_timeout_is_not_counted_as_packet_loss(self):
        connection = FakePingChannel(output=["SEQ HOST SIZE TTL TIME STATUS\n",
                                             "0 192.168.128.3 56 64 1ms\n",
                                             NetmikoTimeoutException("SSH command timed out")])
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 5)["summary"]
        self.assertEqual(summary.execution_status, "unknown")
        self.assertEqual((summary.attempts_started, summary.sent, summary.received), (1, 1, 1))
        self.assertEqual(summary.sent - summary.received, 0)

    def test_routeros_command_rejection_is_distinct_from_timeout(self):
        connection = FakePingChannel(error="expected end of command (line 1 column 21)\n")
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 5)["summary"]
        self.assertEqual(summary.execution_status, "command_error")
        self.assertEqual(summary.sent, 0)

    def test_lost_ssh_channel_is_distinct_from_ping_timeout(self):
        connection = FakePingChannel(error=EOFError("channel closed"))
        summary = network_ssh.ping_router_target(connection, "192.168.128.3", 5)["summary"]
        self.assertEqual(summary.execution_status, "ssh_error")
        self.assertEqual(summary.sent, 0)

    def test_authentication_failure_does_not_print_password(self):
        output = io.StringIO()
        with (
            patch.object(network_ssh, "router_config", return_value={"password": "test-secret"}),
            patch.object(
                network_ssh,
                "ConnectHandler",
                side_effect=NetmikoAuthenticationException("login rejected"),
            ),
            contextlib.redirect_stdout(output),
        ):
            network_ssh.main()

        self.assertIn("Authentication failed", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())

    def test_missing_interfaces_are_reported_as_unavailable(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            network_ssh.print_expected_ethernet_ports({"ether1": "UP"})

        report = output.getvalue()
        self.assertIn("ether1: DETECTED | UP", report)
        for number in range(2, 6):
            self.assertIn(
                f"ether{number}: UNAVAILABLE — not present on this lab router",
                report,
            )


if __name__ == "__main__":
    unittest.main()
