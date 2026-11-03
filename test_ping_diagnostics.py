import unittest

from ping_diagnostics import (
    PingSummary, aggregate_ping_results, format_ping_summary_line,
    parse_packet_count, parse_ping_summary, ping_stop_reasons,
)


class PacketCountTests(unittest.TestCase):
    def test_blank_uses_default(self):
        self.assertEqual(parse_packet_count("  "), 10)

    def test_accepts_small_configurable_counts_and_legacy_hundred(self):
        self.assertEqual(parse_packet_count("5"), 5)
        self.assertEqual(parse_packet_count("10"), 10)
        self.assertEqual(parse_packet_count("20"), 20)
        self.assertEqual(parse_packet_count("100"), 100)

    def test_rejects_other_values(self):
        for value in ("0", "1", "1000", "ten", "10.0"):
            with self.subTest(value=value):
                self.assertIsNone(parse_packet_count(value))


class PingSummaryTests(unittest.TestCase):
    def test_pass_with_rtt_fields(self):
        result = parse_ping_summary(
            "sent=10 received=10 packet-loss=0% "
            "min-rtt=1ms2us avg-rtt=1ms777us max-rtt=2ms382us",
            10,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "PASS")
        self.assertEqual((result.sent, result.received, result.packet_loss), (10, 10, 0))
        self.assertEqual(result.avg_rtt, "1ms777us")

    def test_single_packet_command_summary_parses(self):
        result = parse_ping_summary("sent=1 received=1 packet-loss=0% avg-rtt=25ms", 1)
        self.assertEqual(result.status, "PASS")

    def test_final_summary_is_rendered_as_one_compact_routeros_line(self):
        observation = parse_ping_summary(
            "sent=1 received=1 packet-loss=0% min-rtt=1ms981us "
            "avg-rtt=1ms981us max-rtt=1ms981us", 1,
        )
        summary = aggregate_ping_results([observation], 1)
        self.assertEqual(
            format_ping_summary_line(summary),
            "sent=1 received=1 packet-loss=0% min-rtt=1ms981us "
            "avg-rtt=1ms981us max-rtt=1ms981us",
        )

    def test_one_hundred_packet_count_is_parsed(self):
        result = parse_ping_summary("sent=100 received=100 packet-loss=0%", 100)
        self.assertEqual(result.status, "PASS")

    def test_stop_reasons_report_both_observed_thresholds(self):
        summary = PingSummary(
            sent=3, received=1, packet_loss=200 / 3, status="WARNING",
            min_rtt="100ms", avg_rtt="100ms", max_rtt="100ms",
        )
        self.assertEqual(ping_stop_reasons(summary), ("packet_loss", "max_latency"))

    def test_latency_must_exceed_99_ms(self):
        summary = PingSummary(
            sent=1, received=1, packet_loss=0, status="PASS",
            min_rtt="99ms", avg_rtt="99ms", max_rtt="99ms",
        )
        self.assertEqual(ping_stop_reasons(summary), ())

    def test_partial_loss_is_warning(self):
        result = parse_ping_summary("sent=10 received=9 packet-loss=10%", 10)
        self.assertEqual(result.status, "WARNING")

    def test_one_hundred_packet_run(self):
        result = parse_ping_summary(
            "sent=100 received=99 packet-loss=1% avg-rtt=800us", 100
        )
        self.assertEqual(result.status, "WARNING")
        self.assertEqual(result.avg_rtt, "800us")

    def test_uses_final_summary_after_routeros_progress_summaries(self):
        output = "\n".join(
            [
                "sent=20 received=20 packet-loss=0% avg-rtt=1ms973us",
                "sent=40 received=40 packet-loss=0% avg-rtt=1ms981us",
                "sent=60 received=60 packet-loss=0% avg-rtt=2ms1us",
                "sent=80 received=80 packet-loss=0% avg-rtt=1ms953us",
                (
                    "sent=100 received=100 packet-loss=0% "
                    "min-rtt=1ms178us avg-rtt=1ms959us max-rtt=2ms870us"
                ),
            ]
        )
        result = parse_ping_summary(output, 100)
        self.assertEqual(result.status, "PASS")
        self.assertEqual((result.sent, result.received, result.packet_loss), (100, 100, 0))
        self.assertEqual(result.min_rtt, "1ms178us")
        self.assertEqual(result.avg_rtt, "1ms959us")
        self.assertEqual(result.max_rtt, "2ms870us")

    def test_zero_replies_is_fail(self):
        result = parse_ping_summary("sent=10 received=0 packet-loss=100%", 10)
        self.assertEqual(result.status, "FAIL")

    def test_missing_or_incomplete_summary_is_unknown(self):
        self.assertIsNone(parse_ping_summary("ping timed out", 10))
        self.assertIsNone(parse_ping_summary(None, 10))
        self.assertIsNone(
            parse_ping_summary("sent=3 received=3 packet-loss=0%", 10)
        )

    def test_inconsistent_or_malformed_summary_is_unknown(self):
        self.assertIsNone(
            parse_ping_summary("sent=10 received=10 packet-loss=50%", 10)
        )
        self.assertIsNone(
            parse_ping_summary("sent=ten received=10 packet-loss=0%", 10)
        )

    def test_unrecognized_rtt_is_omitted_without_losing_valid_status(self):
        result = parse_ping_summary(
            "sent=10 received=10 packet-loss=0% avg-rtt=unknown", 10
        )
        self.assertEqual(result.status, "PASS")
        self.assertIsNone(result.avg_rtt)


if __name__ == "__main__":
    unittest.main()
