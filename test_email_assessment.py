"""Evidence-limited assessments and FieldBase draft selection."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

from email_assessment import assess_email_report, response_body, response_options
from email_proactiveness import _build_email_reply_draft
from ping_diagnostics import PingSummary


def ping(name='Bank router', target='192.0.2.3', sent=5, received=5, **kwargs):
    summary = PingSummary(sent, received, (100 * (sent - received) / sent) if sent else 0,
                          'PASS' if sent == received else 'FAIL' if not received else 'WARNING',
                          min_rtt='1ms', avg_rtt='2ms668us', max_rtt='4ms', **kwargs)
    return dict(target_name=name, target=target, summary=summary, raw='captured', result_rows=[])


def report(*pings):
    return dict(pings=list(pings or (ping('Gateway', '192.0.2.1'), ping())),
                connection_status='connected', captured_at='2026-11-01T10:00:00+00:00',
                interfaces='0 R ether1 ether 1500 00:11:22:33:44:55')


@pytest.mark.parametrize('evidence,status', [
    (report(), 'Reachable'),
    (report(ping(received=4)), 'Degraded'),
    (report(ping('Gateway','192.0.2.1'), ping(received=0)), 'Degraded'),
    (report(ping('Gateway','192.0.2.1',received=0), ping(received=0)), 'Unreachable'),
    (report(ping(received=0, execution_status='early_stop',stop_reasons=('packet_loss',))), 'Unreachable'),
    (report(ping(execution_status='early_stop',stop_reasons=('max_latency',))), 'Degraded'),
    (report(ping(sent=0, received=0, execution_status='timeout')), 'Inconclusive'),
    (report(ping(execution_status='ssh_error',error='SSH operation failed')), 'Diagnostic Error'),
    (report(ping(execution_status='command_error',error='Command failed')), 'Diagnostic Error'),
    ({'connection_status':'host_unreachable'}, 'Diagnostic Error'),
    ({}, 'Inconclusive'),
    ({'pings':[{'target':'192.0.2.3','summary':{'sent':'invalid'}}]}, 'Inconclusive'),
    ({'pings':[{'target':'192.0.2.3','summary':{'sent':5,'received':6,'packet_loss':0}}]}, 'Inconclusive'),
])
def test_assessment_scenarios_preserve_evidence(evidence, status):
    original = deepcopy(evidence)
    assessment = assess_email_report(evidence)
    assert assessment['status'] == status
    assert 1 <= len(assessment['actions']) <= 3
    assert 'production' in assessment['interpretation']
    assert evidence == original


def test_no_invented_latency_threshold_or_interface_state():
    evidence = report()
    evidence['pings'][1]['summary'] = replace(evidence['pings'][1]['summary'], avg_rtt='500ms')
    result = assess_email_report(evidence)
    assert result['status'] == 'Reachable'
    assert not result['baseline_available']
    assert [x['Interface'] for x in result['interfaces']] == ['ether1']
    assert result['interfaces'][0]['State'] == 'UP'
    compared = assess_email_report(evidence, baseline_ms={'192.0.2.3':20})
    assert compared['status'] == 'Degraded'
    assert compared['observations'][1]['elevated']


def test_no_reply_is_not_a_cable_diagnosis_and_ssh_failure_is_not_icmp_failure():
    result = assess_email_report(report(ping('Gateway','192.0.2.1'),ping(received=0)))
    assert 'does not prove a cable fault' in result['interpretation']
    result = assess_email_report({},diagnostic_error='Authentication failed',lab=False)
    assert result['status'] == 'Diagnostic Error'
    assert 'unreachable' not in response_options(result)[0]


def test_fieldbase_templates_require_evidence_roles_and_no_lab_extrapolation():
    demarc = assess_email_report(report(ping('Demarcation router','192.0.2.1'),ping(received=0)),lab=False)
    assert response_options(demarc)[1] == 'demarcation'
    assert 'may indicate' in response_body(demarc)[0]
    gateway = assess_email_report(report(ping('Gateway','192.0.2.1'),ping(received=0)),lab=False)
    assert 'demarcation' not in response_options(gateway)[0]
    missing_contact = assess_email_report(report(ping('FieldBase router',received=0)),lab=False)
    assert response_options(missing_contact)[1] == 'unreachable'
    assert response_options(missing_contact,dict(contact_person='Provided contact',phone_number='Provided phone'))[1] != 'unreachable'
    assert response_options(assess_email_report(report()))[1] == 'lab'
    for template in ('up','optimal','unreachable','demarcation','restored'):
        body,_ = response_body(assess_email_report(report()),template)
        assert 'live branch link status' in body
        assert 'has been restored' not in body
        assert 'appears optimal' not in body
        assert 'our demarcation router' not in body


def test_link_up_and_optimal_require_complete_evidence_and_documented_baseline():
    result = assess_email_report(report(),lab=False)
    assert response_options(result)[1] == 'up'
    assert 'optimal' not in response_options(result)[0]
    result = assess_email_report(report(),lab=False,baseline_ms={'192.0.2.1':5,'192.0.2.3':5})
    assert response_options(result)[1] == 'optimal'
    assert 'approximately 2.668 ms' in response_body(result)[0]
    missing = assess_email_report({},lab=False)
    body,supported = response_body(missing,'optimal')
    assert not supported and '[XX]' in body and 'appears optimal' not in body


def test_recovery_requires_matching_targets_and_chronological_failure_evidence():
    current=report()
    prior=report(ping('Gateway','192.0.2.1',received=0),ping(received=0))
    prior['captured_at']='2026-11-01T09:00:00+00:00'
    assert assess_email_report(current,lab=False,previous_report=prior)['recovered']
    result=assess_email_report(current,lab=False,previous_report=prior)
    assert response_options(result)[1]=='restored'
    assert 'has been restored' in response_body(result)[0]
    assert 'precise restoration time' in result['interpretation']
    assert not assess_email_report(current,previous_report={**prior,'captured_at':current['captured_at']})['recovered']
    prior['pings'][1]['target']='198.51.100.7'
    assert not assess_email_report(current,previous_report=prior)['recovered']
    assert 'has been restored' not in response_body(assess_email_report(current),'restored')[0]
    assert 'has been restored' in response_body(assess_email_report(current),'restored',engineer_restored=True)[0]


def test_customer_draft_is_concise_and_never_invents_recipient_or_live_lab_status():
    case=dict(subject='Original subject',confirmed_inventory_record={})
    subject,body=_build_email_reply_draft(case,report())
    assert subject=='Original subject'
    assert body.startswith('Dear @Recipient,') and body.endswith('Best regards,')
    assert 'LAB' in body and 'packet-loss=' not in body
    assert len(body.split()) < 80
    assert 'observed up' not in body
    simulated=assess_email_report({**report(),'simulated':True})
    assert 'simulated LAB' in response_body(simulated)[0]


def test_draft_ui_selection_edit_copy_and_explicit_save_only():
    script='''
import streamlit as st
from unittest.mock import MagicMock
from email_proactiveness import _render_reply_draft
if 'store' not in st.session_state: st.session_state['store']=MagicMock()
_render_reply_draft(st,st.session_state['store'],st.session_state['case'])
'''
    case=dict(case_id='UI',subject='Fixture',lab_diagnostics=report(),reply_draft_body='Preserved engineer draft')
    original=deepcopy(case)
    at=AppTest.from_string(script)
    at.session_state['case']=case
    at.run(timeout=20)
    assert not at.exception
    assert at.text_area[0].value=='Preserved engineer draft'
    assert at.session_state['case']==original
    assert not at.session_state['store'].save.called
    at.text_area[0].set_value('Edited response').run()
    assert at.code[0].value=='Edited response'
    at.selectbox[0].set_value('restored').run()
    assert 'has been restored' not in at.text_area[0].value
    at.checkbox[0].check().run()
    assert not at.exception
    assert 'has been restored' in at.text_area[0].value
    assert not at.session_state['store'].save.called
    at.button[0].click().run()
    assert at.session_state['store'].save.call_count==1
    assert at.session_state['case']['reply_draft_body']==at.text_area[0].value


def test_inventory_contact_phone_row_then_vlan_row_without_duplicate_phone():
    from email_proactiveness import APPROVED_LAB
    record=dict(company_id='UI',organisation='Fixture',branch='Branch',matched_by=['Company ID'],synthetic=True,
                diagnostic_environment='LAB',lab_diagnostic_enabled='YES',lab_router_host=APPROVED_LAB['router'],
                lab_gateway_target=APPROVED_LAB['gateway'],lab_bank_router_target=APPROVED_LAB['bank_router'],
                contact_person='Contact fixture',phone_number='Phone fixture',service_vlan='120',dr_vlan='220',
                ip_block='Service block fixture',dr_ip_block='DR block fixture',aliases=[],device_identifiers=[])
    script='''
import streamlit as st
from email_proactiveness import _render_site_inventory_lookup
_render_site_inventory_lookup(st,[], 'fixture')
'''
    at=AppTest.from_string(script)
    at.session_state['inventory_loaded']=dict(synthetic=True,records=[record],source='Fixture',warnings=[],error=None)
    at.session_state['inventory_lookup_email']='fixture'
    at.session_state['inventory_matches']=[deepcopy(record)]
    at.run(timeout=20)
    assert not at.exception
    assert len(at.columns)==4
    for column,label in zip(at.columns,('Contact person','Phone number','Service VLAN','DR VLAN')):
        assert label in column.markdown[0].value
    assert not any('Phone fixture' in item.value for item in at.expander[0].markdown)
    assert at.session_state['inventory_matches'][0]==record
    assert not at.expander[0].proto.expanded


def test_no_mandatory_l2_checkbox_reintroduced():
    import ast
    import inspect
    from email_proactiveness import render_email_proactiveness
    source=inspect.getsource(render_email_proactiveness)
    assert 'l2_scope_confirmed = True' in source
    assert not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                   and node.func.attr=='checkbox' for node in ast.walk(ast.parse(source)))


def test_recovery_never_crosses_scope_or_ignores_current_execution_error():
    current=report()
    previous=report(ping('Gateway','192.0.2.1',received=0),ping(received=0))
    previous['captured_at']='2026-11-01T09:00:00+00:00'
    for changes in ({'simulated':True},{'lab_authorization':'confirmed'},{'host':'other-management-host'},
                    {'connection_status':'host_unreachable'}):
        assert not assess_email_report(current,lab=False,previous_report={**previous,**changes})['recovered']
    result=assess_email_report(current,lab=False,previous_report=previous,diagnostic_error='SSH stream error')
    assert not result['recovered'] and 'restored' not in response_options(result)[0]


def test_icmp_silence_does_not_override_known_ssh_reachability_in_customer_draft():
    evidence=report(ping('FieldBase router','192.0.2.1',received=0))
    evidence['host']='192.0.2.1'
    result=assess_email_report(evidence,lab=False)
    assert 'unreachable' not in response_options(result)[0]
    evidence=report(ping('Demarcation router','192.0.2.1'),ping(received=0))
    evidence['host']='192.0.2.3'
    assert 'demarcation' not in response_options(assess_email_report(evidence,lab=False))[0]
