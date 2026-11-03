"""Presentation-only checks for captured RouterOS ping text."""
from copy import deepcopy
from html.parser import HTMLParser
from unittest.mock import MagicMock
import pytest

from diagnostic_report_ui import render_ping_terminal, ping_text_for_display


class TerminalParser(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.in_pre = False
        self.text = ''
        self.green = []
        self.in_green = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == 'pre':
            self.in_pre = True
        if tag == 'span':
            self.in_green = dict(attrs).get('style') == 'color:#087f23'

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.in_pre = False
        if tag == 'span':
            self.in_green = False

    def handle_data(self, data):
        if self.in_pre:
            self.text += data
        if self.in_green:
            self.green.append(data)


HEADER = '  SEQ HOST                  SIZE TTL TIME       STATUS\n'
SUMMARY = 'sent=20 received=20 packet-loss=0% min-rtt=0ms avg-rtt=0ms max-rtt=0ms\n'


@pytest.mark.parametrize('text,green_count', [
    (HEADER + '    0 172.16.160.67           56 255 0ms\n' + SUMMARY, 1),
    (HEADER + '    0 192.168.128.254                             timeout\n'
     + 'sent=5 received=0 packet-loss=100%\n', 1),
    (HEADER + '   19 172.16.160.67           56 255 0ms\n' + SUMMARY
     + HEADER + '   20 172.16.160.67           56 255 0ms\n'
     + 'sent=40 received=39 packet-loss=2.5% min-rtt=0ms avg-rtt=1ms max-rtt=2ms\n', 2),
    (HEADER + '    7 192.168.128.3            56 64 2ms668us\n', 0),
    ('<script>alert("test")</script> & <CHR>\n', 0),
])
def test_render_preserves_every_character_and_colors_only_existing_summaries(text, green_count):
    ui = MagicMock()
    render_ping_terminal(ui, text)
    markup = ui.html.call_args.args[0]
    parsed = TerminalParser(markup)
    assert parsed.text == text
    assert len(parsed.green) == green_count
    assert all('sent=' in line and 'packet-loss=' in line for line in parsed.green)
    assert 'background:#fff;color:#111' in markup
    assert 'overflow-x:auto' in markup
    assert 'white-space:pre' in markup
    assert '<script>' not in markup
    assert ui.html.call_args.kwargs == {}


def test_raw_intermediate_evidence_is_preserved_without_mutating_report():
    raw = HEADER + '   19 172.16.160.67 56 255 0ms\n' + SUMMARY + HEADER + '   20 172.16.160.67 56 255 0ms\n'
    ping = dict(raw=raw, result_rows=['19 original', '20 original'], summary={'sent': 20})
    before = deepcopy(ping)
    assert ping_text_for_display(ping, 'existing formatted view') == raw
    assert ping == before


def test_summary_only_and_missing_raw_keep_existing_display():
    assert ping_text_for_display({'raw': SUMMARY}, 'original') == 'original'
    assert ping_text_for_display({}, 'original') == 'original'


@pytest.mark.parametrize('email_workflow', [False, True])
def test_full_raw_transcript_renders_in_both_workflows_without_altering_evidence(email_workflow):
    from streamlit.testing.v1 import AppTest
    from test_shared_report_ui import confirmed_report
    report = confirmed_report()
    raw = HEADER + '   19 192.168.128.3            56 64 0ms\n' + SUMMARY + HEADER + '   20 192.168.128.3            56 64 0ms\n'
    report['pings'][1]['raw'] = raw
    before = deepcopy(report)
    script = '''
import streamlit as st
from diagnostic_report_ui import render_diagnostic_report
from email_proactiveness import _render_diagnostic_report
if st.session_state['email_workflow']:
    _render_diagnostic_report(st, st.session_state['report'])
else:
    from ping_diagnostics import restore_diagnostic_report
    render_diagnostic_report(st, restore_diagnostic_report(st.session_state['report']))
'''
    at = AppTest.from_string(script)
    at.session_state['email_workflow'] = email_workflow
    at.session_state['report'] = report
    at.run(timeout=20)
    assert not at.exception
    terminals = [x.value for x in at.get('html') if 'class="winbox-ping-terminal"' in x.proto.body]
    assert any(TerminalParser(markup).text == raw for markup in terminals)
    assert at.session_state['report'] == before
    assert [x.label for x in at.get('download_button')] == ['Export transcript (.txt)', 'Export evidence (.json)']
