"""Confirmed-site lifecycle and shared diagnostic integration regressions."""
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from email_cases import CaseStore
from email_proactiveness import _process_confirmed_case, classify_email
from test_app_diagnostics import fake_connection
from report_formatting import transcript_for


class ConfirmedSiteWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CaseStore(Path(self.temp.name) / 'cases.sqlite3')
        self.ui = MagicMock()
        self.ui.spinner.return_value = nullcontext()
        self.ui.button.return_value = False
        self.record = dict(synthetic=True, company_id='LAB-1', diagnostic_environment='LAB',
                           lab_diagnostic_enabled='YES', lab_router_host='192.168.128.2',
                           lab_gateway_target='192.168.128.254', lab_bank_router_target='192.168.128.3')
        self.email = 'The link is down.'
        self.result = classify_email(self.email)

    def process(self, **overrides):
        args = dict(st=self.ui, store=self.store, email_text=self.email,
                    result=self.result, site_identifiers=['LAB-1'], l2_scope_confirmed=True,
                    inventory_record=self.record, username='lab-user', password='session-secret')
        args.update(overrides)
        return _process_confirmed_case(**args)

    def test_confirmation_runs_complete_service_without_discovery_and_reuses_report(self):
        connection = fake_connection([])
        with patch('diagnostics_service.ConnectHandler', return_value=connection) as connect, \
             patch('diagnostics_service.discover_bank_router_candidates') as discover:
            case = self.process()
            self.assertEqual(case['diagnostic_status'], 'Completed')
            report = case['lab_diagnostics']
            self.assertEqual(report['lab_authorization'], 'confirmed')
            self.assertEqual({p['target'] for p in report['pings']}, {'192.168.128.254', '192.168.128.3'})
            for key in ('interfaces', 'traffic', 'counters', 'addresses', 'routes'):
                self.assertIn(key, report)
            discover.assert_not_called()
            commands = [call.args[0] for call in connection.send_command.call_args_list]
            self.assertFalse(any('neighbor' in c or '/ip arp' in c for c in commands))
            self.assertEqual(self.process()['case_id'], case['case_id'])
            connect.assert_called_once()
            connection.disconnect.assert_called_once()
            text = transcript_for(report)
            self.assertIn('neighbor discovery skipped', text)
            self.assertNotIn('No candidates found', text)
        saved = self.store.get(case['case_id'])
        self.assertNotIn('session-secret', str(saved))
        self.assertNotIn('lab-user', str(saved))

    def test_credentials_wait_then_run_and_failures_can_retry(self):
        with patch('email_proactiveness.run_confirmed_lab_diagnostics', side_effect=RuntimeError('SSH timed out')) as runner:
            self.assertEqual(self.process(password='')['diagnostic_status'], 'Not started')
            runner.assert_not_called()
            self.assertEqual(self.process()['diagnostic_status'], 'Failed')
            self.process()
            self.assertEqual(runner.call_count, 1)
            case = self.process()
            self.process(retry=True, existing_case_id=case['case_id'])
            self.assertEqual(runner.call_count, 2)

    def test_interrupted_legacy_run_requires_explicit_retry(self):
        case = self.process(password='')
        case['diagnostic_status'] = 'Running'
        self.store.save(case)
        with patch('email_proactiveness.run_confirmed_lab_diagnostics') as runner:
            recovered = self.process()
            self.assertEqual(recovered['diagnostic_status'], 'Running')
            runner.assert_not_called()

    def test_active_run_is_not_duplicated(self):
        case = self.process(password='')
        case.update(diagnostic_status='Running', diagnostic_started_at=datetime.now(timezone.utc).isoformat())
        self.store.save(case)
        with patch('email_proactiveness.run_confirmed_lab_diagnostics') as runner:
            self.assertEqual(self.process()['diagnostic_status'], 'Running')
            runner.assert_not_called()

    def test_scope_and_lab_approval_block_connections(self):
        with patch('email_proactiveness.run_confirmed_lab_diagnostics') as runner:
            self.assertEqual(self.process(l2_scope_confirmed=False)['diagnostic_status'], 'Blocked')
            self.record['diagnostic_environment'] = 'PRODUCTION'
            self.assertEqual(self.process()['diagnostic_status'], 'Blocked')
            runner.assert_not_called()
