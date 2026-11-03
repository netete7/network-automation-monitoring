"""Release regressions use only fixture evidence, temporary databases and mocked SSH."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from streamlit.testing.v1 import AppTest

import test_email_diagnostic_workflow as workflow
from email_cases import CaseStore, create_case
from email_proactiveness import classify_email
from simulation import simulated_report
from test_app_diagnostics import fake_connection
from test_shared_report_ui import confirmed_report


def live_fixture():
    return {**simulated_report('Candidate discovered and reachable', '', '192.0.2.21'),
            'simulated': False, 'bank_candidates': []}


def run_dashboard_script(body):
    # Existing parser tests import app.py in bare mode. Use a fresh process to
    # isolate Streamlit's global layout context, as test_app_diagnostics does.
    import subprocess
    import sys
    setup = """
from streamlit.testing.v1 import AppTest
from unittest.mock import patch
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from simulation import simulated_report
app = AppTest.from_file('app.py').run()
report = {**simulated_report('Candidate discovered and reachable', '', '192.0.2.21'),
          'simulated': False, 'bank_candidates': []}
app.session_state['report'] = report
"""
    completed = subprocess.run([sys.executable, '-c', setup + body],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize('action', ['main', 'custom_empty', 'custom_candidates', 'candidate'])
@pytest.mark.parametrize('failure', ['NetmikoAuthenticationException', 'NetmikoTimeoutException'])
def test_failed_attempt_clears_previous_report(action, failure):
    body = f"action = {action!r}\nfailure = {failure}\n" + """
app.session_state['router_host'] = '192.0.2.10'
app.session_state['router_username'] = 'test-user'
app.session_state['router_password'] = 'wrong-test-secret'
if action in ('custom_candidates', 'candidate'):
    app.session_state['bank_candidates'] = [{'address': '192.0.2.20', 'evidence': 'fixture'}]
app.run()
assert not app.exception
app.text_input(key='router_host').set_value('192.0.2.10')
app.text_input(key='router_username').set_value('test-user')
app.text_input(key='router_password').set_value('wrong-test-secret')
if action == 'main':
    key = 'FormSubmitter:diagnostics-Run read-only checks'
elif action == 'candidate':
    key = 'FormSubmitter:test_other_candidate-Test Selected Candidate'
else:
    field = 'custom_bank_ip_empty' if action == 'custom_empty' else 'custom_bank_ip'
    form = 'ping_custom_ip_no_candidates' if action == 'custom_empty' else 'ping_custom_ip'
    app.text_input(key=field).set_value('198.51.100.20')
    key = f'FormSubmitter:{form}-Ping Custom IP'
with patch('diagnostics_service.ConnectHandler', side_effect=failure('test failure')) as connect:
    app.button(key=key).click().run()
assert not app.exception
connect.assert_called_once()
assert app.error
assert 'report' not in app.session_state
assert not app.get('download_button')
assert not any('succeeded' in item.value for item in app.success)
"""
    run_dashboard_script(body)


def test_invalid_submission_cannot_keep_stale_success():
    run_dashboard_script("""
app.button(key='FormSubmitter:diagnostics-Run read-only checks').click().run()
assert not app.exception
assert app.error
assert 'report' not in app.session_state
""")


def test_distinct_cases_with_identical_evidence_have_stable_unique_download_keys():
    app = AppTest.from_string('''
import streamlit as st
from email_proactiveness import _render_diagnostic_report
for case_id in ('CASE-A', 'CASE-B'):
    _render_diagnostic_report(st, st.session_state['report'], case={'case_id': case_id})
''')
    app.session_state['report'] = confirmed_report()
    app.run()
    assert not app.exception
    keys = [button.proto.id for button in app.get('download_button')]
    assert len(keys) == len(set(keys)) == 4
    app.run()
    assert not app.exception
    assert keys == [button.proto.id for button in app.get('download_button')]


def test_opening_active_case_does_not_duplicate_report_or_draft_widgets():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'cases.sqlite3'
        store = CaseStore(path)
        email = 'The link is down.'
        record = dict(synthetic=True, company_id='LAB-TEST', diagnostic_environment='LAB',
                      lab_diagnostic_enabled='YES')
        from email_proactiveness import _automation_key
        case = create_case(email, classify_email(email))
        case.update(automation_key=_automation_key(email, record) + ':test-scan',
                    confirmed_inventory_record=record, diagnostic_packet_count=10,
                    diagnostic_status='Completed', lab_diagnostics=confirmed_report())
        store.save(case)
        app = AppTest.from_string('from email_proactiveness import render_email_proactiveness\nrender_email_proactiveness()')
        with patch.dict('os.environ', {'FIELDBASE_CASE_DB': str(path)}), \
             patch('email_proactiveness._render_site_inventory_lookup', return_value=record), \
             patch('email_proactiveness.run_confirmed_lab_diagnostics') as runner:
            app.session_state['proactiveness_scan'] = classify_email(email)
            app.session_state['proactiveness_scan_email'] = email
            app.session_state['proactiveness_investigation_id'] = 'test-scan'
            app.session_state['proactiveness_open_case'] = f"{case['case_id']} · New · {case['detected_issue']}"
            app.run()
            assert not app.exception
            assert len(app.get('download_button')) == 3
            assert len([x for x in app.text_area if x.label == 'Reply body']) == 1
            runner.assert_not_called()
        assert store.get(case['case_id']) == case


class CredentialRecoveryTests(unittest.TestCase):
    setUp = workflow.ConfirmedSiteWorkflowTests.setUp
    process = workflow.ConfirmedSiteWorkflowTests.process

    def test_explicit_retry_uses_corrected_credentials_and_preserves_draft(self):
        with patch('diagnostics_service.ConnectHandler', side_effect=NetmikoAuthenticationException('test failure')):
            failed = self.process(password='wrong-test-secret', investigation_id='recovery')
        failed['reply_draft_body'] = 'Keep my edited response'
        self.store.save(failed)
        with patch('diagnostics_service.ConnectHandler', return_value=fake_connection([])) as connect:
            completed = self.process(retry=True, existing_case_id=failed['case_id'], password='corrected-test-secret')
        assert connect.call_args.kwargs['password'] == 'corrected-test-secret'
        assert completed['case_id'] == failed['case_id']
        assert completed['diagnostic_status'] == 'Completed'
        assert completed['reply_draft_body'] == 'Keep my edited response'
        assert completed['diagnostic_error'] is None
        assert 'corrected-test-secret' not in json.dumps(self.store.get(failed['case_id']))

    def test_exception_cannot_persist_session_credentials(self):
        username, password = ' test-private-user ', 'test-private-password'
        with patch('email_proactiveness.run_confirmed_lab_diagnostics',
                   side_effect=RuntimeError(f'Failed using {username.strip()} and {password}')):
            failed = self.process(username=username, password=password)
        for secret in (username.strip(), password):
            assert secret not in json.dumps(self.store.get(failed['case_id']))
            assert secret not in str(self.ui.error.call_args_list)


def test_exporter_error_logs_do_not_include_exception_credentials(caplog):
    import exporter
    secret = 'test-log-secret'
    with patch('exporter.ConnectHandler', side_effect=NetmikoAuthenticationException(secret)):
        exporter.collect_once({'host': '192.0.2.10', 'username': 'test-user', 'password': secret})
    assert secret not in caplog.text
    assert 'device marked down' in caplog.text
