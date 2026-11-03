"""Standard-library tests for case modeling and SQLite persistence."""

import tempfile
import unittest
from pathlib import Path

from email_cases import CASE_STATUSES, CaseStore, create_case


CLASSIFICATION = {
    "issue": "Link down", "reason": "The email reports a complete loss of connectivity.",
    "why_ping_count": "Ten pings provide an initial reachability check.",
    "evidence": ["The link is down."], "proposed_investigation": "Run a 10-ping check.",
    "ping_count": 10,
}


class CaseModelStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = CaseStore(Path(self.temp_dir.name) / "cases.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_reopen_case(self):
        case = create_case("From: noc@example.test\nSubject: Branch outage\n\nThe link is down.", CLASSIFICATION,
                           site_identifiers=["BR-01"], l2_scope_status="Confirmed")
        self.store.save(case)
        reopened = CaseStore(self.store.path).get(case["case_id"])
        self.assertEqual(reopened, case)
        self.assertEqual(reopened["subject"], "Branch outage")
        self.assertEqual(reopened["sender"], "noc@example.test")

    def test_status_changes_are_persisted(self):
        case = create_case("The link is down.", CLASSIFICATION)
        self.store.save(case)
        self.store.update_status(case["case_id"], "Investigating")
        self.assertEqual(self.store.get(case["case_id"])["status"], "Investigating")

    def test_multiple_sites_remain_distinct_items(self):
        case = create_case("Multi-site outage", CLASSIFICATION,
                           site_identifiers=["SITE-A", "SITE-B", "SITE-A"])
        self.assertEqual(case["affected_sites"], ["SITE-A", "SITE-B"])
        self.assertEqual([row["site"] for row in case["site_items"]], ["SITE-A", "SITE-B"])

    def test_invalid_empty_input_and_status_are_rejected(self):
        with self.assertRaises(ValueError):
            create_case("  ", CLASSIFICATION)
        with self.assertRaises(ValueError):
            create_case("An email", {})
        case = create_case("An email", CLASSIFICATION)
        self.store.save(case)
        with self.assertRaises(ValueError):
            self.store.update_status(case["case_id"], "Deleted")
        self.assertEqual(CASE_STATUSES[0], "New")

    def test_store_operations_close_connections(self):
        import sqlite3
        from unittest.mock import patch
        connections = []
        connect = self.store._connect

        def track_connection():
            connection = connect()
            connections.append(connection)
            return connection

        case = create_case("The link is down.", CLASSIFICATION)
        with patch.object(self.store, "_connect", side_effect=track_connection):
            self.store.save(case)
            self.store.get(case["case_id"])
            self.store.list_cases()
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
