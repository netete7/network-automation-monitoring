import unittest

from ping_diagnostics import parse_ping_summary
from report_formatting import normalize_identity, recommendations_for, transcript_for


def sample_report(identity="C H R", pings=None):
    return {
        "captured_at": "2026-10-25T22:58:45+00:00",
        "router": identity,
        "host": "192.168.64.2",
        "interfaces": "0 R ether1 ether 1500 EE:30:13:07:0F:9F",
        "traffic": "",
        "counters": "",
        "addresses": "",
        "pings": pings or [],
    }


class ReportFormattingTests(unittest.TestCase):
    def test_normalizes_spaced_initials_without_changing_normal_names(self):
        self.assertEqual(normalize_identity("C H R"), "CHR")
        self.assertEqual(normalize_identity("Branch Router"), "Branch Router")

    def test_prompt_is_immediately_before_assessment_and_identity_is_normalized(self):
        summary = parse_ping_summary("sent=10 received=10 packet-loss=0%", 10)
        report = sample_report(pings=[{
            "target_name": "Gateway", "target": "192.168.64.1",
            "raw": "sent=10 received=10 packet-loss=0%", "summary": summary,
            "result_rows": ["0 192.168.64.1 56 64 1ms", "1 192.168.64.1 56 64 2ms"],
        }])

        transcript = transcript_for(report)
        self.assertIn("Router: CHR (192.168.64.2)", transcript)
        lines = transcript.splitlines()
        assessment_index = lines.index("Assessment: PASS | 10/10 replies | 0% loss")
        self.assertEqual(lines[assessment_index - 1], "NETE@CHR>")
        self.assertNotEqual(lines[-1], "NETE@CHR>")
        self.assertIn("ether2: NOT PRESENT / UNKNOWN", transcript)
        self.assertIn("SEQ HOST SIZE TTL TIME STATUS\n0 192.168.64.1 56 64 1ms", transcript)
        self.assertLess(transcript.index("1 192.168.64.1 56 64 2ms"), transcript.index("sent=10"))

    def test_gateway_pass_gives_next_diagnostic_step(self):
        summary = parse_ping_summary("sent=10 received=10 packet-loss=0%", 10)
        report = sample_report(pings=[{"target_name": "Gateway", "summary": summary}])

        recommendations = recommendations_for(report)
        self.assertIn("All gateway probes received replies", recommendations[0])
        self.assertIn("No bank-router candidate was found", recommendations[1])

    def test_unreachable_bank_router_focuses_on_local_path(self):
        summary = parse_ping_summary("sent=10 received=0 packet-loss=100%", 10)
        report = sample_report(pings=[{
            "target_name": "Bank router", "target": "192.0.2.2", "summary": summary,
        }])
        recommendation = " ".join(recommendations_for(report))
        self.assertIn("Verify the address and bank-facing cable/link", recommendation)
        self.assertIn("neighbor/ARP entry", recommendation)

    def test_verified_bank_router_pass_does_not_recommend_cable_checks(self):
        summary = parse_ping_summary(
            "sent=20 received=20 packet-loss=0% min-rtt=1ms711us "
            "avg-rtt=3ms739us max-rtt=4ms698us", 20,
        )
        report = sample_report(pings=[{
            "target_name": "Bank router", "target": "192.168.128.3",
            "summary": summary,
        }])
        report.update({"selected_bank_router": "192.168.128.3",
                       "bank_discovery_status": "candidate_selected",
                       "bank_candidates": [{"address": "192.168.128.3"}]})
        recommendations = " ".join(recommendations_for(report))
        self.assertIn("replied to ICMP", recommendations)
        self.assertNotIn("cable/link", recommendations)

    def test_unknown_bank_router_result_does_not_claim_unreachable(self):
        report = sample_report(pings=[{
            "target_name": "Bank router", "target": "192.0.2.2",
            "summary": None,
        }])
        recommendation = " ".join(recommendations_for(report))
        self.assertIn("could not be verified", recommendation)
        self.assertNotIn("cable/link", recommendation)

    def test_loss_and_unknown_gateway_results_suggest_checks(self):
        loss = parse_ping_summary("sent=10 received=8 packet-loss=20%", 10)
        unknown_report = sample_report(pings=[{"target_name": "Gateway", "summary": None}])
        loss_report = sample_report(pings=[{"target_name": "Gateway", "summary": loss}])

        for report in (loss_report, unknown_report):
            text = " ".join(recommendations_for(report))
            self.assertIn("interface/link", text)
            self.assertIn("gateway address and routing", text)
            self.assertIn("ICMP is filtered", text)


if __name__ == "__main__":
    unittest.main()
