"""Evidence interpretation and editable response templates for Email Proactiveness only.

This module never connects to devices, changes reports, or writes cases. Optional
latency baselines are milliseconds keyed by target IP; prior reports must be dated
and cover the same targets before recovery can be inferred.
"""
from __future__ import annotations

from datetime import datetime
import math

from diagnostic_report_ui import interface_rows_for_display
from ping_diagnostics import restore_ping_summary, rtt_milliseconds

RESPONSE_TEMPLATES = {
    'review': ('Investigation update', 'We are reviewing the reported connectivity issue at the subject location.\n\nKindly reconfirm current connectivity and provide a contact available at the branch.'),
    'lab': ('LAB reachability update', ''),
    'up': ('A · Link observed up', 'The link at the subject location is observed up.\n\nKindly reconfirm.'),
    'optimal': ('B · Optimization / stable performance', 'Please be informed that the link appears optimal from our end, with no drops observed and an average latency of approximately {latency} ms.\n\nKindly reconfirm from your end and revert.'),
    'unreachable': ('C · FieldBase router unreachable / LCON', 'Please be informed that we are unable to reach our router at the subject location.\n\nKindly provide a local contact person (LCON) at the branch to assist with further troubleshooting.'),
    'demarcation': ('D · Demarcation reachable, bank router unreachable', 'Please be informed that we can reach our demarcation router at the branch but are unable to reach your router.\n\nThis may indicate an issue between both routers.\n\nKindly provide a contact currently at the branch whom we can work with to troubleshoot further.'),
    'restored': ('E · Link restored', 'Please be informed that the link has been restored. We will continue to monitor the link to ensure stability.\n\nKindly reconfirm connectivity from your end.'),
}


def _milliseconds(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return rtt_milliseconds(value) if isinstance(value, str) else None


def _target_observations(report, baseline_ms):
    observations = []
    for ping in report.get('pings', []) or []:
        if not isinstance(ping, dict):
            continue
        try:
            summary = restore_ping_summary(ping.get('summary'))
        except (TypeError, ValueError, OverflowError):
            summary = None
        target = str(ping.get('target') or 'Unknown')
        name = str(ping.get('target_name') or 'Target')
        if name == 'Gateway' and report.get('_email_lab_scope'):
            name = 'Host-side endpoint'
        item = dict(name=name, target=target, status='Inconclusive', summary=summary,
                    elevated=False, baseline_ms=None, explanation='No complete, consistent ping result is available.')
        if summary:
            execution = summary.execution_status
            valid = (summary.sent > 0 and 0 <= summary.received <= summary.sent
                     and math.isfinite(summary.packet_loss)
                     and abs(summary.packet_loss - 100 * (summary.sent - summary.received) / summary.sent) < 0.11)
            if summary.error or execution in {'ssh_error', 'command_error', 'authentication_error'}:
                item.update(status='Diagnostic Error', explanation=summary.error or f'Diagnostic execution ended with {execution}.')
            elif execution not in {'completed', 'early_stop'} or not valid:
                item['explanation'] = f'Execution state: {execution}; reachability is not established.'
            elif execution == 'early_stop' and not summary.stop_reasons:
                item['explanation'] = 'The test stopped before completion; the stopping reason is unavailable.'
            elif summary.received == 0:
                item.update(status='Unreachable', explanation='No ICMP replies were observed in the completed probes; this does not establish a physical disconnection or device failure.')
            elif summary.packet_loss > 0 or execution == 'early_stop':
                item.update(status='Degraded', explanation='Replies were observed, but the test had packet loss or stopped early. Review the observed counts and stopping reason.')
            else:
                item.update(status='Reachable', explanation='All completed ICMP probes received replies; application service and sustained link health remain unverified.')
            baseline = _milliseconds(baseline_ms.get(target))
            average = _milliseconds(summary.avg_rtt)
            if baseline is not None and average is not None and valid:
                item['baseline_ms'] = baseline
                if average > baseline and item['status'] == 'Reachable':
                    item.update(status='Degraded', elevated=True,
                                explanation=f'Average RTT {average:g} ms exceeds the supplied baseline of {baseline:g} ms; investigate this measured difference.')
        observations.append(item)
    return observations


def _documented_recovery(report, observations, previous_report):
    if not isinstance(previous_report, dict) or not observations or any(x['status'] != 'Reachable' for x in observations):
        return False
    if (bool(previous_report.get('simulated')) != bool(report.get('simulated'))
            or bool(previous_report.get('lab_authorization') == 'confirmed') != bool(report.get('lab_authorization') == 'confirmed')
            or previous_report.get('host') != report.get('host')
            or previous_report.get('connection_status') in {'host_unreachable', 'failed', 'ssh_error', 'authentication_error', 'timeout'}):
        return False
    try:
        before = datetime.fromisoformat(previous_report['captured_at'])
        after = datetime.fromisoformat(report['captured_at'])
        if not before < after:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    previous = _target_observations(previous_report, {})
    return (set(x['target'] for x in previous) == set(x['target'] for x in observations)
            and any(x['status'] == 'Unreachable' for x in previous)
            and all(x['status'] in {'Reachable', 'Degraded', 'Unreachable'} for x in previous))


def assess_email_report(report=None, *, diagnostic_error=None, lab=True,
                        baseline_ms=None, previous_report=None):
    """Interpret available evidence conservatively without mutating the source."""
    report = report if isinstance(report, dict) else {}
    lab = bool(lab or report.get('simulated') or report.get('lab_authorization') == 'confirmed')
    baseline_ms = baseline_ms if baseline_ms is not None else report.get('latency_baselines_ms', {})
    baseline_ms = baseline_ms if isinstance(baseline_ms, dict) else {}
    observations = _target_observations({**report, '_email_lab_scope': lab}, baseline_ms)
    states = [x['status'] for x in observations]
    connection = report.get('connection_status')
    if diagnostic_error or connection in {'host_unreachable', 'failed', 'ssh_error', 'authentication_error', 'timeout'} or 'Diagnostic Error' in states:
        status = 'Diagnostic Error'
    elif not states or 'Inconclusive' in states:
        status = 'Inconclusive'
    elif all(x == 'Unreachable' for x in states):
        status = 'Unreachable'
    elif any(x in {'Unreachable', 'Degraded'} for x in states):
        status = 'Degraded'
    else:
        status = 'Reachable'
    interpretations = {
        'Reachable': 'The tested targets replied to ICMP. This establishes reachability during this check; it does not establish full service availability or sustained stability.',
        'Degraded': 'The evidence shows partial reachability, loss, an early stop, or a measured difference from a supplied latency baseline. The cause remains unverified.',
        'Unreachable': 'No tested target returned ICMP replies in the completed probes. Filtering, addressing, routing or device availability may explain silence; physical disconnection is unverified.',
        'Inconclusive': 'The available evidence does not establish a complete reachability result. Missing or incomplete checks must not be treated as confirmed failures.',
        'Diagnostic Error': 'A diagnostic operation failed or could not complete. An SSH/authentication/timeout error alone does not establish that a router or link is down.',
    }
    interpretation = interpretations[status]
    if lab:
        interpretation += ' These are LAB checks; production backhaul and live branch service remain unverified.'
    if report.get('simulated'):
        interpretation += ' This report contains simulated evidence, not live device observations.'
    recovered = status == 'Reachable' and _documented_recovery(report, observations, previous_report or report.get('previous_report'))
    if recovered:
        interpretation += ' The same targets now reply after a dated, documented no-reply result; the precise restoration time and continued stability are not established.'
    bank = next((x for x in observations if x['name'].casefold() == 'bank router'), None)
    upstream = next((x for x in observations if x['name'].casefold() in {'host-side endpoint', 'gateway', 'fieldbase router', 'demarcation router'}), None)
    if status in {'Inconclusive', 'Diagnostic Error'}:
        actions = ['Review the captured execution error or incomplete evidence and confirm management access and credentials.',
                   'After correcting the execution issue, use the existing approved diagnostic workflow to collect a complete result.',
                   'Obtain live branch confirmation before reporting a production outage or restoration.']
    elif upstream and upstream['status'] == 'Reachable' and bank and bank['status'] == 'Unreachable':
        interpretation += ' The host-side/upstream test replied while the bank-router test did not. This narrows the observed difference but does not prove a cable fault.'
        actions = ['Verify the bank-router address, its route and ICMP policy using the captured evidence.',
                   'Ask a contact at the branch to check the bank-facing equipment and local link.',
                   'Repeat the approved checks after verification and reconfirm the affected service.']
    elif status == 'Unreachable':
        actions = ['Review the captured interface, addressing and route evidence for the tested targets.',
                   'Confirm target availability and ICMP policy with a local contact; do not assume a cable fault.',
                   'Repeat approved checks after verification and confirm service separately.']
    elif status == 'Degraded':
        actions = ['Review the lost probes, early-stop reason and any supplied latency baseline.',
                   'Check captured interface error/drop counters and routing; cumulative counters alone do not establish current loss.',
                   'Repeat the approved check and compare service behavior with a documented baseline.']
    else:
        actions = ['Ask the branch to reconfirm the reported application or connectivity issue.',
                   'Compare further service checks with an available baseline before claiming optimal performance.',
                   'If symptoms continue, review the relevant service path with the responsible network contact.']
    interfaces = [x for x in interface_rows_for_display(str(report.get('interfaces') or '')) if x['State'] != 'Unavailable']
    return dict(status=status, observations=observations, interpretation=interpretation, actions=actions[:3],
                management_host=report.get('host'), management_connected=connection == 'connected',
                lab=lab, recovered=recovered, error=diagnostic_error, interfaces=interfaces,
                baseline_available=any(x['baseline_ms'] is not None for x in observations), simulated=bool(report.get('simulated')))


def response_options(assessment, record=None):
    """Return evidence-supported template choices; unknown device roles stay unknown."""
    record = record or {}
    eligible = {'review'}
    recommended = 'review'
    observations = assessment['observations']
    if assessment['lab']:
        eligible.add('lab')
        return eligible, 'lab' if observations else 'review'
    bank = next((x for x in observations if x['name'].casefold() == 'bank router'), None)
    demarc = next((x for x in observations if x['name'].casefold() == 'demarcation router'), None)
    fieldbase = next((x for x in observations if x['name'].casefold() in {'fieldbase router', 'demarcation router'}), None)
    if (assessment['status'] == 'Reachable' and bank
            and any(x['name'].casefold() in {'gateway', 'fieldbase router', 'demarcation router'} for x in observations)
            and len({x['target'] for x in observations}) >= 2):
        eligible.add('up'); recommended = 'up'
        if all(x['baseline_ms'] is not None and x['summary'] and _milliseconds(x['summary'].avg_rtt) is not None for x in observations):
            eligible.add('optimal'); recommended = 'optimal'
    fieldbase_ssh_reachable = bool(fieldbase and assessment['management_connected'] and fieldbase['target'] == assessment['management_host'])
    bank_ssh_reachable = bool(bank and assessment['management_connected'] and bank['target'] == assessment['management_host'])
    if fieldbase and fieldbase['status'] == 'Unreachable' and not fieldbase_ssh_reachable:
        eligible.add('unreachable')
        usable_contact = all(str(record.get(k) or '').strip().casefold() not in {'', 'n/a', 'none', 'unknown', 'not provided', 'none available'} for k in ('contact_person', 'phone_number'))
        if not usable_contact:
            recommended = 'unreachable'
    if demarc and demarc['status'] == 'Reachable' and bank and bank['status'] == 'Unreachable' and not bank_ssh_reachable:
        eligible.add('demarcation'); recommended = 'demarcation'
    if assessment['recovered']:
        eligible.add('restored'); recommended = 'restored'
    return eligible, recommended


def response_body(assessment, template=None, *, record=None, engineer_restored=False):
    """Build one concise draft, guarding unsupported conclusions even after selection."""
    eligible, recommended = response_options(assessment, record)
    template = template or recommended
    supported = template in eligible or (template == 'restored' and engineer_restored)
    if template == 'restored' and engineer_restored:
        message = RESPONSE_TEMPLATES['restored'][1]
    elif assessment['lab'] and template != 'review':
        findings = '; '.join(f"{x['name']}: {x['status'].lower()}" for x in assessment['observations'])
        prefix = 'The simulated LAB result indicates ' if assessment['simulated'] else 'The controlled LAB checks show '
        message = (prefix + findings + '.' if findings else 'Diagnostic verification is not yet complete.')
        if assessment['status'] == 'Diagnostic Error':
            message = 'The controlled LAB diagnostics could not complete. The live branch status remains unverified.'
        message += '\n\nThese checks do not establish the live branch link status.\n\nKindly reconfirm connectivity at the subject location.'
        if template == 'optimal' and not any(x['summary'] and _milliseconds(x['summary'].avg_rtt) is not None for x in assessment['observations']):
            message += '\n\nAverage latency: [XX] ms (pending verification).'
    elif not supported:
        message = RESPONSE_TEMPLATES['review'][1]
        if template == 'optimal':
            message += '\n\nAverage latency: [XX] ms (pending verification).'
    else:
        message = RESPONSE_TEMPLATES[template][1]
        if template == 'optimal':
            bank = next(x for x in assessment['observations'] if x['name'].casefold() == 'bank router')
            average = _milliseconds(bank['summary'].avg_rtt)
            message = message.format(latency=f'{average:g}' if average is not None else '[XX]')
    return 'Dear @Recipient,\n\n' + message + '\n\nBest regards,', supported
