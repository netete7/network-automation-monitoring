"""The known stale LAB fixture endpoint is corrected without expanding scope."""
from io import BytesIO
from unittest.mock import patch

import pytest
from openpyxl import Workbook
from site_inventory import _read_workbook
from email_proactiveness import run_confirmed_lab_diagnostics
from test_app_diagnostics import fake_connection


def inventory_record(**overrides):
    fields = {
        'Company ID': 'SIM-WEM-001', 'Branch': 'Wema Bank Marina Service Branch',
        'Diagnostic Environment': 'LAB', 'Lab Diagnostic Enabled': 'YES',
        'Lab Router Host': '192.168.128.2', 'Lab Gateway Target': '192.168.128.1',
        'Lab Bank Router Target': '192.168.128.3',
    }
    fields.update(overrides)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Wema'
    sheet.append(list(fields))
    sheet.append(list(fields.values()))
    content = BytesIO()
    workbook.save(content)
    records, warnings = _read_workbook(content.getvalue())
    assert not warnings
    return records[0]


def test_stale_lab_source_maps_only_host_side_endpoint_and_keeps_source_evidence():
    record = inventory_record()
    assert record['lab_gateway_target'] == '192.168.128.254'
    assert record['raw_fields']['Lab Gateway Target'] == '192.168.128.1'
    assert record['lab_router_host'] == '192.168.128.2'
    assert record['lab_bank_router_target'] == '192.168.128.3'


@pytest.mark.parametrize('overrides', [
    {'Company ID': 'SIM-WEM-002'},
    {'Diagnostic Environment': 'PRODUCTION'},
    {'Lab Diagnostic Enabled': 'NO'},
    {'Lab Router Host': '192.168.128.9'},
    {'Lab Bank Router Target': '192.168.128.4'},
    {'Lab Gateway Target': '192.168.128.99'},
    {'Lab Gateway Target': ''},
    {'Lab Gateway Target': '192.168.128.254'},
])
def test_other_inventory_configurations_are_preserved(overrides):
    expected = overrides.get('Lab Gateway Target', '192.168.128.1')
    assert inventory_record(**overrides)['lab_gateway_target'] == expected


def test_fresh_inventory_diagnostic_uses_host_side_and_bank_targets_without_discovery():
    record = inventory_record()
    connection = fake_connection([])
    with patch('diagnostics_service.ConnectHandler', return_value=connection) as connect, \
         patch('diagnostics_service.discover_bank_router_candidates') as discover, \
         patch('diagnostics_service.ping_router_target', wraps=__import__('diagnostics_service').ping_router_target) as ping:
        report = run_confirmed_lab_diagnostics(record, 'test-user', 'test-secret', 5)
    assert connect.call_args.kwargs['host'] == '192.168.128.2'
    assert [(call.args[1], call.args[2]) for call in ping.call_args_list] == [
        ('192.168.128.254', 5), ('192.168.128.3', 5)]
    assert [(p['target_name'], p['target']) for p in report['pings']] == [
        ('Gateway', '192.168.128.254'), ('Bank router', '192.168.128.3')]
    assert report['lab_authorization'] == 'confirmed'
    discover.assert_not_called()
    connection.disconnect.assert_called_once()
