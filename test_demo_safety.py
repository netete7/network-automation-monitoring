"""Demo regressions: temporary SQLite stores and mocked SSH only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from email_cases import CaseStore, create_case
from email_proactiveness import classify_email
import test_email_diagnostic_workflow as workflow


class FreshInvestigationTests(unittest.TestCase):
    setUp = workflow.ConfirmedSiteWorkflowTests.setUp
    process = workflow.ConfirmedSiteWorkflowTests.process

    def test_identical_email_new_scan_uses_current_credentials_and_cannot_reuse_success(self):
        old_report = {'old_success': True}
        with patch('email_proactiveness.run_confirmed_lab_diagnostics', return_value=old_report):
            old = self.process(investigation_id='first')
        with patch('email_proactiveness.run_confirmed_lab_diagnostics',
                   side_effect=RuntimeError('SSH authentication failed')) as runner:
            new = self.process(investigation_id='second', username='current-user', password='wrong')
        runner.assert_called_once_with(self.record, 'current-user', 'wrong', self.result['ping_count'])
        self.assertNotEqual(old['case_id'], new['case_id'])
        self.assertEqual(new['diagnostic_status'], 'Failed')
        self.assertIsNone(new['lab_diagnostics'])
        self.ui.error.assert_called_with('LAB diagnostics failed: SSH authentication failed')
        self.assertEqual(self.store.get(old['case_id'])['lab_diagnostics'], old_report)
        self.assertEqual(len(self.store.list_cases()), 2)


class DeleteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cases.sqlite3'
        self.store = CaseStore(self.path)
        self.lab = create_case('Link down', classify_email('Link down'))
        self.lab['confirmed_inventory_record'] = dict(synthetic=True, diagnostic_environment='LAB')
        self.other = create_case('Another case', classify_email('Link down'))
        for case in (self.lab, self.other):
            self.store.save(case)
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE unrelated (value TEXT)')
            conn.execute("INSERT INTO unrelated VALUES ('keep')")
            self.other_row = conn.execute('SELECT * FROM cases WHERE case_id=?', (self.other['case_id'],)).fetchone()

    def test_delete_requires_confirmation_and_preserves_other_rows(self):
        with self.assertRaises(ValueError):
            self.store.delete_lab_case(self.lab['case_id'])
        with self.assertRaises(ValueError):
            self.store.delete_lab_case(self.other['case_id'], confirmed=True)
        self.assertTrue(self.store.delete_lab_case(self.lab['case_id'], confirmed=True))
        self.assertIsNone(self.store.get(self.lab['case_id']))
        self.assertFalse(self.store.delete_lab_case("' OR 1=1 --", confirmed=True))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT * FROM cases').fetchall(), [self.other_row])
            self.assertEqual(conn.execute('SELECT * FROM unrelated').fetchall(), [('keep',)])

    def delete_app(self):
        return AppTest.from_string('''
import streamlit as st
from email_cases import CaseStore
from email_proactiveness import _render_delete_case
store = CaseStore(st.session_state['db'])
case = store.get(st.session_state['case_id'])
if case:
    _render_delete_case(st, store, case)
''')

    def test_visible_delete_cancel_and_confirm(self):
        app = self.delete_app()
        app.session_state['db'] = str(self.path)
        app.session_state['case_id'] = self.lab['case_id']
        app.run()
        self.assertFalse(app.exception)
        app.button(key=f"delete_case_{self.lab['case_id']}").click().run()
        self.assertIsNotNone(self.store.get(self.lab['case_id']))
        app.button(key=f"cancel_delete_case_{self.lab['case_id']}").click().run()
        self.assertIsNotNone(self.store.get(self.lab['case_id']))
        app.button(key=f"delete_case_{self.lab['case_id']}").click().run()
        app.button(key=f"confirm_delete_case_{self.lab['case_id']}").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(self.store.get(self.lab['case_id']))

    def test_scan_clears_old_displays_and_confirmation_even_for_identical_email(self):
        app = AppTest.from_string('from email_proactiveness import render_email_proactiveness\nrender_email_proactiveness()')
        email = 'The link is down.'
        with patch.dict('os.environ', {'FIELDBASE_CASE_DB': str(self.path)}), \
             patch('email_proactiveness._render_site_inventory_lookup', return_value=None), \
             patch('email_proactiveness.run_confirmed_lab_diagnostics') as runner:
            app.run()
            app.text_area(key='proactiveness_email_text').set_value(email).run()
            for _ in range(2):
                app.session_state['inventory_confirmed_records'] = {'OLD': {'synthetic': True}}
                app.session_state['inventory_lookup_email'] = email
                app.session_state['proactiveness_open_case'] = next(
                    option for option in app.selectbox(key='proactiveness_open_case').options
                    if option.startswith(self.lab['case_id']))
                app.button(key='scan_proactiveness_email').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state['proactiveness_scan'], classify_email(email))
                self.assertEqual(app.selectbox(key='proactiveness_open_case').value, '— Select a case —')
                self.assertNotIn('inventory_confirmed_records', app.session_state)
                fresh_id = app.session_state['proactiveness_investigation_id']
                if _:
                    self.assertNotEqual(previous_id, fresh_id)
                previous_id = fresh_id
            runner.assert_not_called()
        self.assertEqual(len(self.store.list_cases()), 2)

    def test_scan_then_explicit_site_confirmation_uses_current_credentials(self):
        from test_lab_inventory_target import inventory_record
        from netmiko.exceptions import NetmikoAuthenticationException
        record = inventory_record()
        record['synthetic'] = True
        record['matched_by'] = ['test fixture']
        inventory = dict(synthetic=True, records=[record], warnings=[])
        app = AppTest.from_string('from email_proactiveness import render_email_proactiveness\nrender_email_proactiveness()')
        with patch.dict('os.environ', {'FIELDBASE_CASE_DB': str(self.path)}), \
             patch('site_inventory.load_inventory', return_value=inventory), \
             patch('site_inventory.inventory_summary', return_value=dict(source='test', record_count=1, organisation_count=1, refreshed_at='')), \
             patch('site_inventory.match_inventory_from_email', return_value=[record]), \
             patch('diagnostics_service.ConnectHandler', side_effect=NetmikoAuthenticationException('Authentication failed')) as connect:
            app.run()
            app.text_input(key='lab_router_username').set_value('current-test-user').run()
            app.text_input(key='lab_router_password').set_value('wrong-test-password').run()
            app.text_area(key='proactiveness_email_text').set_value('The link is down.').run()
            app.button(key='scan_proactiveness_email').click().run()
            self.assertFalse(app.exception)
            connect.assert_not_called()
            app.button(key=f"confirm_inventory_{record['company_id']}").click().run()
            self.assertFalse(app.exception)
            connect.assert_called_once()
            self.assertEqual(connect.call_args.kwargs['username'], 'current-test-user')
            self.assertEqual(connect.call_args.kwargs['password'], 'wrong-test-password')
            self.assertTrue(any('failed' in e.value.lower() for e in app.error))
            failed = [c for c in self.store.list_cases() if c['diagnostic_status'] == 'Failed']
            self.assertEqual(len(failed), 1)
            self.assertIsNone(failed[0]['lab_diagnostics'])
            app.button(key='scan_proactiveness_email').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(connect.call_count, 1)
            self.assertFalse(app.error)
