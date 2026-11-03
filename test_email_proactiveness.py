"""Standard-library tests for realistic email classification; no Streamlit dependency."""
import unittest

from email_proactiveness import classify_email, detect_l2_recipient, parse_site_identifiers, site_investigation_plan


# Anonymized, ticket-style examples exercise whole email snippets rather than
# isolated keywords. These contain no real customer or network identifiers.
REALISTIC_EMAILS = {
    "fieldbase_slow": "Your link at the subject location is slow.",
    "fieldbase_fluctuating": (
        "EscType: NETWORK\nVendor: Fieldbase\nCategory: Fluctuating Link\n"
        "Subject: Fluctuating link at BRANCH 014\nEscItem: BRANCH 014\nStatus: Pending\n\n"
        "Kindly investigate the reported link issue."
    ),
    "fieldbase_restore_request": "Kindly assist to restore the subject link urgently.",
    "outage": (
        "Subject: Branch 014 unreachable\n\n"
        "Hi NOC, since 09:18 the branch has had no connectivity. The edge link is down "
        "and staff cannot reach the hosted service. Please investigate."
    ),
    "degraded": (
        "Subject: Slow service at branch 027\n\n"
        "Users report high latency and intermittent access to the hosted application. "
        "The connection is still up, but response times have been poor since this morning."
    ),
    "restore_request": (
        "Subject: Restore service for branch 031\n\n"
        "The branch link dropped during planned work. Please restore the link and let us "
        "know when it is back. We have not confirmed connectivity yet."
    ),
    "link_up_poor_performance": (
        "Subject: Performance remains poor - branch 042\n\n"
        "The link is up and users can connect, but the service is still slow with packet "
        "loss. Please check the path and advise on next steps."
    ),
    "older_degraded_then_current_outage": (
        "Subject: Update to branch 055 incident\n\n"
        "Yesterday users reported high latency. Update from the site at 14:32 today: "
        "the link is currently down and no one can reach the service. Please treat this "
        "as the active issue."
    ),
    "ambiguous_timing": (
        "Subject: Branch 061 connectivity\n\n"
        "The link is down and users also report high latency. We are not sure whether "
        "these reports refer to the same time. Please advise."
    ),
    "conflicting_updates": (
        "Subject: Branch 073 status\n\n"
        "One user says the link is down; another says it is up but very slow. We have "
        "not confirmed which report is current."
    ),
    "resolved_outage": (
        "Subject: Branch 086 outage update\n\n"
        "The link was down earlier, but the outage has now been resolved and users "
        "confirm service is restored. No further issue is reported."
    ),
    "quoted_old_complaint": (
        "Subject: Branch 091 link status\n\nThe latest update is that service is available; please review.\n\n"
        "-----Original Message-----\n> The link is slow and has packet loss."
    ),
}


class EmailClassificationTests(unittest.TestCase):
    def assert_result_fields(self, result):
        for field in ("issue", "evidence", "reason", "proposed_investigation", "why_ping_count", "ping_count"):
            self.assertIn(field, result)

    def test_realistic_outage_email_proposes_ten_pings(self):
        result = classify_email(REALISTIC_EMAILS["outage"])
        self.assert_result_fields(result)
        self.assertEqual((result["issue"], result["ping_count"]), ("Link down", 10))
        self.assertTrue(any("no connectivity" in sentence.lower() for sentence in result["evidence"]))

    def test_fieldbase_slow_wording_proposes_one_hundred_pings(self):
        result = classify_email(REALISTIC_EMAILS["fieldbase_slow"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))

    def test_structured_fluctuating_escalation_proposes_one_hundred_pings(self):
        result = classify_email(REALISTIC_EMAILS["fieldbase_fluctuating"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))
        self.assertTrue(any("Fluctuating Link" in sentence for sentence in result["evidence"]))

    def test_short_fieldbase_restoration_request_proposes_ten_pings(self):
        result = classify_email(REALISTIC_EMAILS["fieldbase_restore_request"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Link restoration requested", 10))
        self.assertNotIn("restored", result["reason"].casefold())

    def test_incomplete_escalation_fields_without_symptom_need_review(self):
        result = classify_email("EscType: NETWORK\nVendor: Fieldbase\nStatus: Pending")
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])

    def test_conflicting_current_symptoms_need_review(self):
        result = classify_email("The link is down but users say it is up and very slow; status is unconfirmed.")
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])

    def test_quoted_old_complaint_does_not_override_latest_message(self):
        result = classify_email(REALISTIC_EMAILS["quoted_old_complaint"])
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])
        self.assertFalse(any("slow" in sentence.casefold() for sentence in result["evidence"]))

    def test_restoration_request_and_outgoing_confirmation_are_distinguished(self):
        requested = classify_email("Please restore the link.")
        restored_confirm = classify_email("The link has been restored. Kindly reconfirm.")
        restored_variant = classify_email("Link restored. Kindly confirm")
        connectivity_variant = classify_email("Connectivity has been restored. Please check and confirm")
        tested = classify_email("The link has been restored and tested successfully.")
        self.assertEqual((requested["issue"], requested["ping_count"]), ("Link restoration requested", 10))
        for result in (restored_confirm, restored_variant, connectivity_variant):
            self.assertEqual(result["issue"], "Outgoing restoration notification")
            self.assertIsNone(result["ping_count"])
            self.assertIn("No new diagnostic", result["proposed_investigation"])
        self.assertEqual(tested["issue"], "Direction uncertain")
        self.assertIsNone(tested["ping_count"])

    def test_restoration_text_without_direction_metadata_stays_uncertain(self):
        result = classify_email("The link has been restored.")
        self.assertEqual(result["issue"], "Direction uncertain")
        self.assertIsNone(result["ping_count"])
        self.assertIn("does not establish", result["reason"])

    def test_explicit_direction_is_respected(self):
        outgoing = classify_email("The link has been restored.", direction="outgoing")
        incoming = classify_email("The link has been restored.", direction="incoming")
        self.assertEqual(outgoing["issue"], "Outgoing restoration notification")
        self.assertEqual(incoming["issue"], "Needs review")
        self.assertIsNone(outgoing["ping_count"])
        self.assertIsNone(incoming["ping_count"])

    def test_realistic_degraded_link_email_proposes_one_hundred(self):
        result = classify_email(REALISTIC_EMAILS["degraded"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))
        self.assertTrue(any("high latency" in sentence.lower() for sentence in result["evidence"]))

    def test_realistic_restore_request_proposes_verification_without_assuming_restored(self):
        result = classify_email(REALISTIC_EMAILS["restore_request"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Link restoration requested", 10))
        self.assertIn("does not establish", result["reason"])
        self.assertIn("After restoration", result["proposed_investigation"])

    def test_realistic_link_up_poor_performance_includes_additional_checks(self):
        result = classify_email(REALISTIC_EMAILS["link_up_poor_performance"])
        self.assertEqual(result["ping_count"], 100)
        self.assertTrue(any("link is up" in sentence.lower() for sentence in result["evidence"]))
        for check in ("errors/discards", "congestion", "service path", "gateway", "endpoint"):
            self.assertIn(check, result["proposed_investigation"])

    def test_clearly_current_outage_takes_priority_over_older_degraded_report(self):
        result = classify_email(REALISTIC_EMAILS["older_degraded_then_current_outage"])
        self.assertEqual((result["issue"], result["ping_count"]), ("Link down", 10))
        self.assertTrue(any("currently down" in sentence.lower() for sentence in result["evidence"]))

    def test_ambiguous_timing_requires_human_review(self):
        result = classify_email(REALISTIC_EMAILS["ambiguous_timing"])
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])
        self.assertIn("without clear timing", result["reason"])

    def test_conflicting_unconfirmed_updates_require_human_review(self):
        result = classify_email(REALISTIC_EMAILS["conflicting_updates"])
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])

    def test_resolved_outage_requires_human_review_without_ping_count(self):
        result = classify_email(REALISTIC_EMAILS["resolved_outage"])
        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])
        self.assertIn("already be resolved", result["reason"])

    def test_starlink_optimization_request_includes_limitations_and_checks(self):
        result = classify_email("Subject: Q4 STARLINK LINK OPTIMIZATION - FCMB BRANCH - IPAJA\n\nKindly optimize your link at IPAJA.")
        self.assertEqual((result["issue"], result["ping_count"]), ("Link optimization requested", 100))
        self.assertIn("cannot independently establish", result["reason"])
        self.assertIn("errors/discards", result["proposed_investigation"])

    def test_unstable_link_ping_sample_is_evidence_not_recommendation(self):
        text = ("Subject: Re: UNSTABLE FIELDBASE LINK AT TAJBANK MARINA LAGOS\n\n"
                "Kindly optimize the link. Sending 5, 100-byte ICMP Echos ... Success rate is 40 percent (2/5), "
                "round-trip min/avg/max = 95/139/184 ms. Kindly note that this link is not serving the branch as expected.")
        result = classify_email(text)
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))
        self.assertTrue(any("40 percent (2/5)" in item and "95/139/184 ms" in item for item in result["evidence"]))
        self.assertIn("does not establish the root cause", result["reason"])
        self.assertIn("existing five-ping result is evidence", result["why_ping_count"])

    def test_structured_bank_noc_incident_extracts_available_fields(self):
        text = ("Subject: UBA NOC ISSUE LOG ON incident\nService Provider: NGNG029_FBASE\nOperation type: Incident\n"
                "Service Outlet: 05370100_Abubakar Burga Str. Keffi\nLink Technology: 02_MPLS\n"
                "Faulty Description: Link Down\nFault Message: link is down\nFaulty Status: Open")
        result = classify_email(text)
        self.assertEqual((result["issue"], result["ping_count"]), ("Link down", 10))
        self.assertEqual(result["metadata"], {
            "service_outlet": "05370100_Abubakar Burga Str. Keffi",
            "link_technology": "02_MPLS",
            "incident_status": "Open",
            "fault_description": "Link Down",
        })

    def test_direct_link_down_with_restore_request_proposes_ten(self):
        result = classify_email("The link at this location is down. Kindly assist restore the link.")
        self.assertIn(result["issue"], ("Link down", "Link restoration requested"))
        self.assertEqual(result["ping_count"], 10)

    def test_slow_link_optimization_uses_degraded_class_and_extra_checks(self):
        result = classify_email("Subject: Re: FIELDBASE SLOW LINK AT SOL_0614, ABUJA\n\nYour link at the subject location is slow. Urgently optimize the link.")
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))
        self.assertIn("errors/discards", result["proposed_investigation"])

    def test_vague_optimization_body_uses_subject_context(self):
        result = classify_email("Subject: Poor FIELDBASE link @ GTBANK NLNG_BONNY\n\nKindly help optimize subject link urgently.")
        self.assertEqual((result["issue"], result["ping_count"]), ("Link optimization requested", 100))
        self.assertIn("congestion", result["proposed_investigation"])

    def test_multi_site_outage_preserves_distinct_sites_and_per_site_scope(self):
        sites = ("FBN-MAIDUGURI-250-RTR", "FBN-OPOPOGBORO-655-RTR", "FBN-ABA-MAIN-101-RTR",
                 "FBN-CALABAR2-519-RTR", "FBN-WARRIMAIN-350-RTR.firstbanknigeria.com",
                 "FBN-BODIJA2-685-RTR", "FBN-OGBOMOSHOQSP-270-RTR.firstbanknigeria.com",
                 "KONGO_HOTEL (ZARIA) GATEWAY_137")
        text = "Subject: SERVICE OUTAGE AT FIRST BANK BRANCH LOCATIONS || FIELDBASE\nPlease investigate and restore: " + ", ".join(sites)
        result = classify_email(text)
        self.assertEqual(result["issue"], "Multi-site outage/restoration request")
        self.assertEqual(result["site_identifiers"], list(sites))
        self.assertEqual(result["pings_per_site"], 10)
        self.assertIn("does not validate the others", result["proposed_investigation"])

    def test_packet_loss_wording_recommends_degraded_sample(self):
        result = classify_email("Subject: Re: FBASE LINK UNSTABLE AT SOL 0211\n\nThe link at this BO is dropping packet. Urgently optimize the link.")
        self.assertEqual((result["issue"], result["ping_count"]), ("Degraded or unoptimized link", 100))
        self.assertTrue(any("dropping packet" in sentence.casefold() for sentence in result["evidence"]))

    def test_missing_structured_fields_are_not_invented(self):
        result = classify_email("Service Outlet: Example Branch\nFaulty Description: Link Down")
        self.assertEqual(result["metadata"], {"service_outlet": "Example Branch", "fault_description": "Link Down"})

    def test_common_natural_language_outage_phrases_propose_ten_pings(self):
        outage_emails = [
            "We are reporting a complete loss of connectivity at Wema Bank Marina Service Branch.",
            "The branch network connection is currently unavailable.",
            "The site is offline and users cannot access the network.",
            "There is a total loss of service at the branch.",
            "The branch is unable to connect to the network.",
            "All services are unreachable at the affected location.",
            "The link is down and the branch cannot connect.",
        ]

        for email in outage_emails:
            with self.subTest(email=email):
                result = classify_email(email)
                self.assertEqual(result["issue"], "Link down")
                self.assertEqual(result["ping_count"], 10)
                self.assertTrue(result["evidence"])

    def test_wema_marina_example_is_classified_as_link_down(self):
        email = """
        Dear FieldBase NOC Team,

        We are reporting a complete loss of connectivity at
        Wema Bank Marina Service Branch.

        The branch has confirmed that its network connection
        is currently unavailable, affecting access to internal
        banking applications and other online services.

        Kindly investigate the service interruption and provide
        an update on the connection status and any findings.

        Please treat this as a priority.

        Regards,
        IT Operations Team Wema Bank
        """

        result = classify_email(email)

        self.assertEqual(result["issue"], "Link down")
        self.assertEqual(result["ping_count"], 10)
        self.assertTrue(
            any(
                "complete loss of connectivity" in item.casefold()
                for item in result["evidence"]
            )
        )

    def test_current_unavailable_report_takes_priority_over_older_degraded_report(self):
        email = """
        Subject: Branch connectivity update

        Yesterday users reported high latency and packet loss.

        Today's update: the branch network connection is
        currently unavailable. Please investigate the outage.
        """

        result = classify_email(email)

        self.assertEqual(result["issue"], "Link down")
        self.assertEqual(result["ping_count"], 10)

    def test_restored_or_negated_outage_does_not_trigger_active_link_down(self):
        emails = [
            "The site is no longer offline.",
            "The link is not down.",
            "The connection is no longer unavailable.",
        ]

        for email in emails:
            with self.subTest(email=email):
                result = classify_email(email)
                self.assertNotEqual(result["issue"], "Link down")
                self.assertIsNone(result["ping_count"])

    def test_routine_availability_enquiry_is_not_classified_as_outage(self):
        email = (
            "Please provide the service availability report "
            "for the Marina branch."
        )

        result = classify_email(email)

        self.assertEqual(result["issue"], "Needs review")
        self.assertIsNone(result["ping_count"])
class EmailIntakeHelperTests(unittest.TestCase):
    def test_l2_recipient_detection_matches_configured_address_case_insensitively(self):
        email = "To: NOC Team <L2-Support@example.com>\nCc: other@example.com\n\nThe link is down."
        self.assertIs(detect_l2_recipient(email, "l2-support@example.com"), True)

    def test_l2_recipient_detection_rejects_nonmatching_recipient(self):
        self.assertIs(detect_l2_recipient("To: noc@example.com\n\nLink is down", "l2@example.com"), False)

    def test_missing_l2_recipient_or_configuration_is_unknown(self):
        self.assertIsNone(detect_l2_recipient("Link is down", "l2@example.com"))
        self.assertIsNone(detect_l2_recipient("To: l2@example.com\n\nLink down", None))

    def test_site_list_parser_preserves_distinct_supported_identifiers(self):
        sites = parse_site_identifiers("FBN-ABA-MAIN-101-RTR, FBN-ABA-MAIN-101-RTR\nKONGO_HOTEL (ZARIA) GATEWAY_137")
        self.assertEqual(sites, ["FBN-ABA-MAIN-101-RTR", "KONGO_HOTEL (ZARIA) GATEWAY_137"])

    def test_per_site_plan_keeps_site_results_separate(self):
        plan = site_investigation_plan(["SITE-A", "SITE-A", "SITE-B"], 10)
        self.assertEqual([row["site"] for row in plan], ["SITE-A", "SITE-B"])
        self.assertTrue(all(row["ping_count"] == 10 and row["confirmation_required"] for row in plan))


class LabDiagnosticSafetyTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "synthetic": True,
            "diagnostic_environment": "LAB",
            "lab_diagnostic_enabled": "YES",
            "lab_router_host": "192.168.128.2",
            "lab_gateway_target": "192.168.128.254",
            "lab_bank_router_target": "192.168.128.3",
        }
        self.report = {
            "host": "192.168.128.2", "connection_status": "connected",
            "simulated": False,
            "bank_discovery_status": "inventory_confirmed",
            "selected_bank_router": "192.168.128.3",
            "pings": [{"target": "192.168.128.254"}, {"target": "192.168.128.3"}],
        }

    def run_lab(self, count=10):
        from email_proactiveness import run_confirmed_lab_diagnostics
        return run_confirmed_lab_diagnostics(self.record, " lab-user ", "secret", count)

    def test_approved_run_disables_discovery(self):
        from unittest.mock import patch
        with patch("email_proactiveness.run_diagnostics", return_value=self.report) as runner:
            self.assertEqual(self.run_lab(), self.report)
            self.assertIs(runner.call_args.kwargs["discover_candidates"], False)
            self.assertEqual(runner.call_args.kwargs["username"], "lab-user")
            self.assertEqual(runner.call_args.kwargs["gateway"], "192.168.128.254")

    def test_non_lab_and_unapproved_addresses_never_connect(self):
        from unittest.mock import patch
        for field, value in (
            ("diagnostic_environment", "PRODUCTION"),
            ("lab_diagnostic_enabled", "NO"),
            ("lab_router_host", "198.51.100.1"),
            ("lab_gateway_target", "198.51.100.2"),
            ("lab_gateway_target", "192.168.128.1"),
            ("lab_bank_router_target", "198.51.100.3"),
        ):
            with self.subTest(field=field), patch("email_proactiveness.run_diagnostics") as runner:
                original = self.record[field]
                self.record[field] = value
                with self.assertRaises(ValueError):
                    self.run_lab()
                runner.assert_not_called()
                self.record[field] = original

    def test_invalid_packet_count_never_connects(self):
        from unittest.mock import patch
        with patch("email_proactiveness.run_diagnostics") as runner:
            with self.assertRaises(ValueError):
                self.run_lab(1000)
            runner.assert_not_called()

    def test_invalid_reports_are_rejected(self):
        from unittest.mock import patch
        for changes in (
            {"host": "198.51.100.1"}, {"simulated": True},
            {"connection_status": "failed"}, {"pings": None},
            {"pings": []}, {"pings": [{"target": "192.168.128.254"}] * 2},
            {"pings": [{"target": "192.168.128.254"}, {"target": "198.51.100.1"}]},
        ):
            with self.subTest(changes=changes), patch(
                "email_proactiveness.run_diagnostics", return_value={**self.report, **changes}
            ):
                with self.assertRaises(ValueError):
                    self.run_lab()


if __name__ == "__main__":
    unittest.main()
