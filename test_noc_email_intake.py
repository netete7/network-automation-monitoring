"""Realistic pasted NOC intake regressions; no network or inventory modifications."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from streamlit.testing.v1 import AppTest

from email_proactiveness import classify_email, parse_site_identifiers
from noc_email import parse_noc_email
from site_inventory import _read_workbook, match_inventory_from_email

EMAILS = {
 'A': ('RE: POOR FIELDBASE LINK AT GTBANK FRANCE ROAD', 'Dear Team,\n\nKindly move to optimize subject link urgently.\n\nWarm Regards,'),
 'B': ('LINK DOWN — FIDELITY JABI ABUJA', 'Dear Team,\n\nKindly note that Fidelity Jabi Abuja is down please restore ASAP.\n\nRegards,'),
 'C': ('LINK OPTIMIZATION — ZONE4', 'Dear @Janet Adegbenro,\n\nKindly optimize your link at ZONE4.\n\nRegards,'),
 'D': ('RE: FIELDBASE SLOW LINK AT SOL_0135 ===38, AJAYI AINA STREET, IFAKO GBAGADA, LAGOS', 'Dear FIELDBASE,\n\nYour link at the subject location is slow.\n\nUrgently optimize the link.\n\nRegards.'),
 'E': ('INCIDENT NOTIFICATION — LINK INTERMITTENCY', 'Dear FBASE,\n\nThe above issue has been opened against you.\n\nOperation Type: Incident\nService Outlet Name: Wema Bank Marina Service Branch\nService Outlet Code: WEM-MARINA-01\nFaulty Description: Link Intermittency\nRoot Cause: Transmission Failure\nFaulty Status: Open\n\nFault Message:\nPlease investigate and resolve.'),
}


@pytest.fixture(scope='module')
def inventory():
    records,_=_read_workbook(Path(__file__).with_name('FieldBase_Simulated_NOC_Inventory_600.xlsx').read_bytes())
    return records


@pytest.mark.parametrize('key,issue,count',[
 ('A','Link optimization requested',100),('B','Link down',10),('C','Link optimization requested',100),
 ('D','Degraded or unoptimized link',100),('E','Degraded or unoptimized link',100),
])
def test_five_requested_examples(key,issue,count,inventory):
    subject,body=EMAILS[key]
    email=f'Subject: {subject}\n\n{body}'
    result=classify_email(email)
    assert (result['issue'],result['ping_count'])==(issue,count)
    assert result['reported_subject']==subject
    before=deepcopy(inventory)
    matches=match_inventory_from_email(email,inventory)
    assert inventory==before and len(inventory)==600
    if key=='E':
        assert [m['company_id'] for m in matches]==['SIM-WEM-001']
        assert result['reported_root_cause']=='Transmission Failure'
        assert result['root_cause_verified'] is False
    else:
        # These real customer branches are absent from the simulated fixture.
        assert matches==[]
        assert result['reported_site_references']


@pytest.mark.parametrize('placement', ['before','after','bare-before','bare-after','split-label'])
def test_subject_location_before_after_body_and_reply_prefixes(placement):
    subject,body=EMAILS['D']
    email={'before':f'Subject: {subject}\n\n{body}', 'after':f'{body}\n\nSubject: {subject}',
           'bare-before':f'{subject}\n\n{body}', 'bare-after':f'{body}\n\n{subject}',
           'split-label':f'Subject:\n\n{subject}\n\nBody:\n{body}'}[placement]
    result=classify_email(email)
    assert result['ping_count']==100 and result['reported_subject']==subject
    assert 'SOL_0135' in result['site_identifiers']
    assert any('AJAYI AINA' in x for x in result['reported_site_references'])


@pytest.mark.parametrize('text',[
 'Operation Type: Incident\nFaulty Description: Link Intermittency\nRoot Cause: Transmission Failure\nFaulty Status: Open',
 '| Field | Value |\n|---|---|\n| Operation Type | Incident |\n| Faulty Description | Link Intermittency |\n| Root Cause | Transmission Failure |\n| Faulty Status | Open |',
 'Operation Type\tIncident\nFaulty Description\tLink Intermittency\nRoot Cause\tTransmission Failure\nFaulty Status\tOpen',
 'Operation Type\nIncident\nFaulty Description\nLink Intermittency\nRoot Cause\nTransmission Failure\nFaulty Status\nOpen',
 '<table><tr><td>Operation Type</td><td>Incident</td></tr><tr><td>Faulty Description</td><td>Link Intermittency</td></tr><tr><td>Root Cause</td><td>Transmission Failure</td></tr><tr><td>Faulty Status</td><td>Open</td></tr></table>',
])
def test_structured_intermittency_without_descriptive_fault_message(text):
    result=classify_email(text)
    assert result['ping_count']==100
    assert result['reported_fields']['fault_description']=='Link Intermittency'
    assert result['reported_root_cause']=='Transmission Failure' and not result['root_cause_verified']


def test_all_requested_structured_fields_and_unverified_root_cause():
    text='\n'.join(['Operation ID: INC-100','Operation Type: Incident','Service Provider: FBASE',
                    'Service Outlet Code: WEM-MARINA-01','Sol ID: 0135','Service Outlet Name: Marina',
                    'Faulty Description: Packet Loss','Faulty Status: Open','Root Cause: Transmission Failure',
                    'Fault Message: Please investigate.','Associated Service ID: SERVICE-001'])
    fields=classify_email(text)['reported_fields']
    assert len(fields)==11 and fields['operation_id']=='INC-100' and fields['sol_id']=='0135'
    assert 'WEM-MARINA-01' in parse_site_identifiers(text)
    assert classify_email('Root Cause: Link Down\nPlease investigate')['ping_count'] is None


@pytest.mark.parametrize('phrase', ['link intermittency','intermittent connection','link flapping','packet drops','poor link','high latency','slow response'])
def test_noc_performance_language_retains_100_packet_policy(phrase):
    result=classify_email(f'Please investigate {phrase} at the branch.')
    assert result['ping_count']==100 and result['issue']!='Link down'


@pytest.mark.parametrize('phrase',['Confirm router reachable','Please check device status','Kindly verify the router reachability'])
def test_device_reachability_is_a_short_check_not_a_declared_outage(phrase):
    result=classify_email(phrase)
    assert (result['issue'],result['ping_count'])==('Device reachability check',5)


def test_conflicting_subject_body_and_clear_latest_updates():
    assert classify_email('Subject: Link down at Wema Marina\n\nThe link is slow and intermittent.')['ping_count'] is None
    assert classify_email('Subject: Slow link at Wema Marina\n\nThe link is down.')['ping_count'] is None
    assert classify_email('Subject: Link down at Wema Marina\n\nLatest update: the link is slow, not down.')['ping_count']==100
    assert classify_email('Subject: Slow link at Wema Marina\n\nLatest update: the link is down.')['ping_count']==10
    assert classify_email('Faulty Description: Link Down\nFaulty Status: Closed')['ping_count'] is None
    assert classify_email('Faulty Description: Link Intermittency\nFaulty Status: Open\nFaulty Status: Closed')['ping_count'] is None


def test_restore_requests_are_not_restoration_evidence():
    assert classify_email('Please restore ASAP.')['ping_count']==10
    for text in ('The link has been restored.','The link is restored; kindly reconfirm.'):
        assert classify_email(text)['ping_count'] is None
    result=classify_email('Subject: Link down at Wema Marina\n\nThe link has now been restored.')
    assert result['ping_count'] is None


def test_quoted_history_and_person_names_do_not_supply_sites_or_incidents(inventory):
    text='Dear @Janet Adegbenro,\nPlease check the issue.\nRegards,\nWema Bank Marina Service Branch\n\nOn Monday someone wrote:\nRE: LINK DOWN AT WEMA MARINA'
    assert classify_email(text)['ping_count'] is None
    assert parse_noc_email(text)['subject'] is None
    assert match_inventory_from_email(text,inventory)==[]
    assert match_inventory_from_email('Dear Wema Bank Marina Service Branch,\nPlease advise.',inventory)==[]
    assert match_inventory_from_email('Latest update: the site is slow.\n> Subject: Link down at Wema Marina',inventory)==[]


def test_case_and_whitespace_and_forward_prefixes(inventory):
    text='  Subject: fWd: LiNk InTeRmItTeNcY AT WEM-MARINA-01\n\n FaUlTy DeScRiPtIoN :   LiNk InTeRmItTeNcY   \n FaUlTy StAtUs : Open '
    assert classify_email(text)['ping_count']==100
    assert [x['company_id'] for x in match_inventory_from_email(text,inventory)]==['SIM-WEM-001']


def test_matching_explicit_abbreviation_disambiguation_and_unmatched_codes(inventory):
    assert [x['company_id'] for x in match_inventory_from_email('Subject: Poor GTB link at Marina',inventory)]==['SIM-GTB-028']
    assert match_inventory_from_email('GTBank reports a link down; please check the router on the road.',inventory)==[]
    assert match_inventory_from_email('Subject: Link down at SOL_9999 Abuja',inventory)==[]
    assert match_inventory_from_email('Subject: Link down at Fidelity Marina Lagos',inventory)==[]


def test_zone4_ambiguity_and_org_context_never_uses_recipient_name():
    records=[dict(company_id='A',organisation='Wema',branch='Zone 4',aliases=['ZONE4']),
             dict(company_id='B',organisation='GTBank',branch='Zone4',aliases=[])]
    email='Subject: LINK OPTIMIZATION — ZONE4\n\nDear @Janet Adegbenro,\nKindly optimize your link at ZONE4.'
    assert {x['company_id'] for x in match_inventory_from_email(email,records)}=={'A','B'}
    assert [x['company_id'] for x in match_inventory_from_email('GTB\n'+email,records)]==['B']


def test_sol_outlet_codes_and_addresses_are_used_only_when_present_in_record():
    records=[dict(company_id='A',organisation='Wema',branch='Exact address fixture',aliases=[],raw_fields={'Sol ID':'0135','Branch Address':'38 Ajayi Aina Street Ifako Gbagada Lagos'}),
             dict(company_id='B',organisation='GTBank',branch='Ajayi fixture',aliases=[],raw_fields={'Sol ID':'9999'})]
    subject,body=EMAILS['D']
    matches=match_inventory_from_email(f'Subject: {subject}\n{body}',records)
    assert [x['company_id'] for x in matches]==['A']
    assert 'Site code' in matches[0]['matched_by']
    assert match_inventory_from_email('Subject: Link down at SOL_8888 Ajayi',records)==[]


def test_no_attempt_is_not_displayed_as_an_execution_error():
    script='''
import streamlit as st
from email_proactiveness import _render_email_assessment
_render_email_assessment(st,{},st.session_state['case'])
'''
    at=AppTest.from_string(script)
    at.session_state['case']=dict(detected_issue='Needs review',diagnostic_status='Blocked',diagnostic_error='No approved diagnostic packet count is available.')
    at.run(timeout=20)
    assert not at.exception
    assert any('Needs Review' in x.value for x in at.markdown)
    assert not any('Diagnostic Error' in x.value for x in at.markdown)
    at.session_state['case']=dict(diagnostic_status='Failed',diagnostic_error='SSH timeout')
    at.run(timeout=20)
    assert any('Diagnostic Error' in x.value for x in at.markdown)


def test_headerless_table_is_not_mistaken_for_a_subject():
    result=classify_email('Faulty Description | Link Intermittency\nFaulty Status | Open')
    assert result['ping_count']==100 and result['reported_subject'] is None
    assert result['reported_fields']['fault_description']=='Link Intermittency'


def test_historical_site_identifiers_do_not_reenter_through_reviewed_site_merge():
    text='Please review the current slow link.\n\n-----Original Message-----\nSubject: Link down FBN-ABA-MAIN-101-RTR'
    assert parse_site_identifiers(text)==[]
    assert classify_email(text)['ping_count']==100


def test_bare_subject_with_current_header_block_is_retained():
    text='RE: POOR LINK AT WEM-MARINA-01\nFrom: noc@example.invalid\nSent: Today\nTo: FieldBase\n\nKindly optimize the subject link.'
    result=classify_email(text)
    assert result['ping_count']==100 and result['reported_subject']=='RE: POOR LINK AT WEM-MARINA-01'


def test_explicit_restoration_verification_uses_existing_ten_packet_policy():
    result=classify_email('Please verify the restored link at WEM-MARINA-01.')
    assert (result['issue'],result['ping_count'])==('Restoration verification',10)
    assert 'not independently verified' in result['reason']


def test_negated_packet_loss_does_not_negate_a_separate_latency_complaint():
    assert classify_email('The link has no packet loss.')['ping_count'] is None
    assert classify_email('The link has high latency but no packet loss.')['ping_count']==100


def test_manual_sol_lookup_reads_existing_fields_without_changing_inventory():
    from site_inventory import match_inventory
    record=dict(company_id='A',organisation='GTBank',branch='Branch fixture',raw_fields={'Sol ID':'135'})
    before=deepcopy(record)
    assert [x['company_id'] for x in match_inventory('SOL_0135',[record],organisation='GTB')]==['A']
    assert record==before
