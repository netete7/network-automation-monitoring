"""Render the confirmed-site report in a real Streamlit test session."""
import json
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from email_proactiveness import run_confirmed_lab_diagnostics
from test_app_diagnostics import fake_connection
from ping_diagnostics import parse_ping_summary


def confirmed_report():
    record = dict(synthetic=True, diagnostic_environment='LAB', lab_diagnostic_enabled='YES',
                  lab_router_host='192.168.128.2', lab_gateway_target='192.168.128.254',
                  lab_bank_router_target='192.168.128.3')
    connection = fake_connection([])
    raw = 'sent=10 received=10 packet-loss=0% min-rtt=2ms668us avg-rtt=4ms84us max-rtt=5ms362us'
    def ping_result(connection, target, count, **kwargs):
        return dict(raw=raw, summary=parse_ping_summary(raw, count), status='PASS',
                    result_rows=[f'{i} {target} 56 64 2ms668us' for i in range(count)])
    with patch('diagnostics_service.ping_router_target', side_effect=ping_result), \
         patch('diagnostics_service.ConnectHandler' , return_value=connection), \
         patch('diagnostics_service.discover_bank_router_candidates') as discovery:
        report = run_confirmed_lab_diagnostics(record, 'lab-user', 'memory-only', 10)
    discovery.assert_not_called()
    report['routes'] = 'DAc dst-address=192.168.128.0/24 gateway=ether1'
    return json.loads(json.dumps(report, default=lambda obj: obj.__dict__))


def test_complete_confirmed_report_renders_and_exports():
    report = confirmed_report()
    script = '''
import streamlit as st
from email_proactiveness import _render_diagnostic_report
_render_diagnostic_report(st, st.session_state['fixture'])
'''
    at = AppTest.from_string(script)
    at.session_state['fixture'] = report
    at.run(timeout=20)
    assert not at.exception
    assert [x.value for x in at.subheader] == [
        'Router overview', 'Gateway Ping', 'Confirmed inventory targets',
        'Bank-router Ping', 'Diagnostic assessment and recommendations']
    ports = at.dataframe[0].value
    assert list(ports['Interface'])[:5] == ['ether1', 'ether2', 'ether3', 'ether4', 'ether5']
    assert list(ports['State'])[1:5] == ['Unavailable'] * 4
    assert len(at.dataframe) == 4
    assert [x.label for x in at.expander] == [
        'Captured Device Evidence', 'Gateway raw output', 'Bank-router raw output']
    terminals = [x.value for x in at.get('html') if 'class="winbox-ping-terminal"' in x.proto.body]
    assert len(terminals) == 4
    assert 'ping 192.168.128.254' in terminals[0]
    assert 'ping 192.168.128.3' in terminals[2]
    assert 'ROUTES' in at.code[0].value
    assert 'unverified candidate' not in str(at)
    metrics = {x.label: x.value for x in at.metric}
    assert metrics['Minimum RTT'] == '2ms668us'
    assert metrics['Confirmed sent'] == '10'
    downloads = at.get('download_button')
    assert [x.label for x in downloads] == ['Export transcript (.txt)', 'Export evidence (.json)']

