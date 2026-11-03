import unittest

from report_formatting import recommendations_for
from simulation import simulated_report


class OfflineSimulationTests(unittest.TestCase):
    def run_case(self, scenario, manual="198.51.100.20", candidate="192.0.2.21"):
        return simulated_report(scenario, manual, candidate)

    def test_discovered_candidate_and_reachable(self):
        report = self.run_case("Candidate discovered and reachable")
        self.assertTrue(report["simulated"])
        self.assertEqual(report["bank_candidates"][0]["address"], "192.0.2.20")
        self.assertEqual(report["pings"][0]["summary"].status, "PASS")
        self.assertIn("replied to ICMP", " ".join(recommendations_for(report)))

    def test_discovered_candidate_times_out(self):
        report = self.run_case("Discovered but unreachable (timeouts)")
        self.assertEqual(report["pings"][0]["summary"].status, "FAIL")
        advice = " ".join(recommendations_for(report))
        self.assertIn("Verify the address", advice)
        self.assertIn("neighbor/ARP entry", advice)

    def test_management_host_unreachable(self):
        report = self.run_case("Management host unreachable")
        self.assertEqual(report["connection_status"], "host_unreachable")
        self.assertEqual(report["pings"], [])
        self.assertIn("management IP", recommendations_for(report)[0])

    def test_no_candidate_and_manual_ip(self):
        report = self.run_case("No candidate; manual IP")
        self.assertEqual(report["bank_candidates"], [])
        self.assertEqual(report["pings"][0]["target"], "198.51.100.20")
        self.assertIn("manually entered", " ".join(recommendations_for(report)))

    def test_multiple_candidates_and_selected_candidate(self):
        report = self.run_case("Multiple candidates; select one", candidate="192.0.2.22")
        self.assertEqual(len(report["bank_candidates"]), 2)
        self.assertEqual(report["pings"][0]["target"], "192.0.2.22")
        self.assertIn("Confirm that this candidate is the bank router", " ".join(recommendations_for(report)))

    def test_target_matching_diagnostic_router_is_not_pinged(self):
        report = self.run_case("Target equals diagnostic router IP")
        self.assertEqual(report["pings"], [])
        self.assertTrue(report["warnings"])
        self.assertIn("matches an address configured", recommendations_for(report)[0])

    def test_each_scenario_has_distinct_evidence_based_recommendations(self):
        reports = [self.run_case(scenario) for scenario in (
            "Candidate discovered and reachable",
            "Discovered but unreachable (timeouts)",
            "Management host unreachable",
            "No candidate; manual IP",
            "Multiple candidates; select one",
            "Target equals diagnostic router IP",
        )]
        advice = [" ".join(recommendations_for(report)) for report in reports]
        self.assertEqual(len(set(advice)), len(advice))
        for report, recommendation in zip(reports, advice):
            self.assertTrue(recommendation)
            self.assertFalse(report["pings"] == [] and "gateway" in recommendation.lower())


if __name__ == "__main__":
    unittest.main()
