"""Regression coverage for report RTT metrics and authorization labels."""
import unittest
from unittest.mock import MagicMock, patch

from email_proactiveness import _format_report_rtt, _render_diagnostic_report


class ReportDisplayTests(unittest.TestCase):
    def test_rtt_values(self):
        for value, expected in [
            (2, '2 ms'), (2.668, '2.668 ms'), (0, '0 ms'),
            ('2ms668us', '2ms668us'), ('4ms84us', '4ms84us'),
            ('500us', '500us'), ('0ms', '0ms'), (' 2.5 ', '2.5 ms'),
            (None, 'N/A'), ('', 'N/A'), ('N/A', 'N/A'),
            ('unknown', 'N/A'), (float('nan'), 'N/A'),
            (float('inf'), 'N/A'), (-1, 'N/A'), (True, 'N/A'),
        ]:
            with self.subTest(value=value):
                self.assertEqual(_format_report_rtt(value), expected)

    def test_render_serialized_and_numeric_rtts_and_missing_authorization(self):
        for values in [('2ms668us', '4ms84us', '5ms362us'), (0, 2.5, 5), (None, None, None)]:
            for authorization in [None, 'confirmed']:
                with self.subTest(values=values, authorization=authorization):
                    ui = MagicMock()
                    ui.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
                    report = dict(router='CHR', host='192.168.128.2', captured_at='2026-10-31',
                        interfaces='0 R ether1 ether 1500 00:11:22:33:44:55', traffic='', counters='',
                        addresses='192.168.128.2/24', routes='', bank_candidates=[],
                        selected_bank_router='192.168.128.3', bank_discovery_status='inventory_confirmed',
                        simulated=False, lab_authorization=authorization, pings=[
                        dict(target_name='Bank router', target='192.168.128.3', raw='', summary=dict(
                            sent=10, received=10, packet_loss=0, status='PASS',
                            min_rtt=values[0], avg_rtt=values[1], max_rtt=values[2]))])
                    with patch('report_formatting.recommendations_for', return_value=[]), \
                         patch('report_formatting.transcript_for', return_value='evidence'):
                        _render_diagnostic_report(ui, report)
                    self.assertTrue(any('actual router environment has not been independently verified' in str(c) for c in ui.caption.call_args_list))
                    # Inspect returned columns via a shared metric mock on a second render.
                    column = MagicMock()
                    ui.columns.side_effect = lambda n: [column] * n
                    _render_diagnostic_report(ui, report)
                    ui.metric.assert_any_call('LAB authorization', 'Confirmed' if authorization else 'Not recorded')
                    for label, value in zip(('Minimum RTT', 'Average RTT', 'Maximum RTT'), values):
                        column.metric.assert_any_call(label, _format_report_rtt(value))
