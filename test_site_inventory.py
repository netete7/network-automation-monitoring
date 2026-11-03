"""Tests for the simulated inventory loader and site matcher."""

from pathlib import Path
import unittest
from unittest.mock import patch

import site_inventory


WORKBOOK = Path(__file__).with_name(
    "FieldBase_Simulated_NOC_Inventory_600.xlsx"
)


class InventoryReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records, cls.warnings = site_inventory._read_workbook(
            WORKBOOK.read_bytes()
        )

    def test_reads_all_six_hundred_inventory_records(self):
        self.assertEqual(len(self.records), 600)

    def test_discovers_all_twelve_organisations(self):
        organisations = {
            record["organisation"] for record in self.records
        }
        self.assertEqual(len(organisations), 12)

    def test_company_ids_are_unique(self):
        ids = [record["company_id"] for record in self.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_records_are_marked_synthetic(self):
        self.assertTrue(all(r["synthetic"] for r in self.records))

    def test_wema_alias_matches_company_id(self):
        matches = site_inventory.match_inventory(
            "WEM Marina", self.records
        )
        self.assertTrue(matches)
        self.assertEqual(matches[0]["company_id"], "SIM-WEM-001")
        self.assertIn("Alias", matches[0]["matched_by"])

    def test_company_id_matches_exact_record(self):
        matches = site_inventory.match_inventory(
            "SIM-WEM-001", self.records
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["organisation"], "Wema")

    def test_marina_is_ambiguous_across_organisations(self):
        matches = site_inventory.match_inventory(
            "Marina", self.records
        )
        organisations = {r["organisation"] for r in matches}
        self.assertGreater(len(matches), 1)
        self.assertGreater(len(organisations), 1)

    def test_organisation_context_narrows_marina(self):
        matches = site_inventory.match_inventory(
            "Marina", self.records, organisation="Wema"
        )
        self.assertTrue(matches)
        self.assertTrue(all(
            r["organisation"] == "Wema" for r in matches
        ))

    def test_blank_search_returns_no_matches(self):
        self.assertEqual(
            site_inventory.match_inventory(" ", self.records), []
        )

    def test_unknown_search_returns_no_matches(self):
        self.assertEqual(
            site_inventory.match_inventory(
                "NO-SUCH-BRANCH-999", self.records
            ),
            [],
        )


class InventoryLoadingTests(unittest.TestCase):
    def setUp(self):
        site_inventory.clear_inventory_cache()

    def tearDown(self):
        site_inventory.clear_inventory_cache()

    def test_google_inventory_loads_and_is_cached(self):
        content = WORKBOOK.read_bytes()

        with patch.object(
            site_inventory,
            "_download_google_sheet",
            return_value=content,
        ) as download:
            first = site_inventory.load_inventory()
            second = site_inventory.load_inventory()

        self.assertEqual(
            first["source"], "Google Sheets (simulated inventory)"
        )
        self.assertEqual(len(first["records"]), 600)
        self.assertEqual(second["refreshed_at"], first["refreshed_at"])
        download.assert_called_once()

    def test_manual_refresh_bypasses_cache(self):
        content = WORKBOOK.read_bytes()

        with patch.object(
            site_inventory,
            "_download_google_sheet",
            return_value=content,
        ) as download:
            site_inventory.load_inventory()
            refreshed = site_inventory.load_inventory(
                force_refresh=True
            )

        self.assertEqual(len(refreshed["records"]), 600)
        self.assertEqual(download.call_count, 2)

    def test_google_failure_uses_excel_fallback(self):
        with patch.object(
            site_inventory,
            "_download_google_sheet",
            side_effect=OSError("Simulated connection failure"),
        ):
            result = site_inventory.load_inventory()

        self.assertIn("Local Excel fallback", result["source"])
        self.assertEqual(len(result["records"]), 600)
        self.assertIsNotNone(result["error"])

    def test_summary_reports_inventory_size(self):
        content = WORKBOOK.read_bytes()

        with patch.object(
            site_inventory,
            "_download_google_sheet",
            return_value=content,
        ):
            inventory = site_inventory.load_inventory()

        summary = site_inventory.inventory_summary(inventory)
        self.assertEqual(summary["record_count"], 600)
        self.assertEqual(summary["organisation_count"], 12)
        self.assertEqual(summary["duplicate_company_ids"], [])


class EmailInventoryMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records, cls.warnings = site_inventory._read_workbook(
            WORKBOOK.read_bytes()
        )

    def test_wema_marina_email_returns_only_marina_branch(self):
        email = """
        Dear FieldBase NOC Team,
        We are reporting a complete loss of connectivity at
        Wema Bank Marina Service Branch.
        The branch has confirmed that its network connection
        is currently unavailable.
        Regards,
        IT Operations Team
        Wema Bank
        """

        matches = site_inventory.match_inventory_from_email(
            email, self.records
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["company_id"], "SIM-WEM-001")
        self.assertEqual(
            matches[0]["branch"],
            "Wema Bank Marina Service Branch",
        )
        self.assertIn("Organisation context", matches[0]["matched_by"])

    def test_organisation_name_without_branch_returns_no_matches(self):
        email = """
        Dear FieldBase NOC Team,
        Wema Bank is reporting a connectivity issue.
        Please investigate.
        """

        matches = site_inventory.match_inventory_from_email(
            email, self.records
        )

        self.assertEqual(matches, [])

    def test_selected_branch_retains_contact_information(self):
        email = """
        Wema Bank Marina Service Branch is currently offline.
        """

        matches = site_inventory.match_inventory_from_email(
            email, self.records
        )

        self.assertEqual(len(matches), 1)
        self.assertIn("contact_person", matches[0])
        self.assertIn("phone_number", matches[0])

if __name__ == "__main__":
    unittest.main()
