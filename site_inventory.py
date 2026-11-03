"""Read-only inventory loader, cache and conservative site matcher.

The Google Sheet is a public synthetic fixture. Never use its records as
evidence of production equipment or as targets for live diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.request import Request, urlopen
import os
import re

from openpyxl import load_workbook
from noc_email import parse_noc_email


DEFAULT_SHEET_ID = "1B9oBqeg90hU_dQZIDIunjtAPiqAbd0rx"
DEFAULT_FALLBACK = Path(__file__).with_name(
    "FieldBase_Simulated_NOC_Inventory_600.xlsx"
)
CACHE_SECONDS = 600

_EXCLUDED_SHEETS = {
    "data dictionary",
    "site aliases",
    "test scenarios",
    "validation report",
}

_cache: dict[tuple[str, str], dict] = {}
_cache_times: dict[tuple[str, str], float] = {}
_cache_lock = Lock()


def _normalise(value: object) -> str:
    """Lowercase text and remove punctuation for conservative comparisons."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _cell_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _column_index(headers: list[str], name: str) -> int | None:
    wanted = _normalise(name)
    for index, header in enumerate(headers):
        if _normalise(header) == wanted:
            return index
    return None


def _get_cell(row: tuple, headers: list[str], name: str) -> str:
    index = _column_index(headers, name)
    if index is None or index >= len(row):
        return ""
    return _cell_text(row[index])


def _read_workbook(file_content: bytes) -> tuple[list[dict], list[str]]:
    """Read organisation worksheets and the separate Site Aliases worksheet."""
    workbook = load_workbook(
        BytesIO(file_content),
        read_only=False,
        data_only=True,
    )

    records: list[dict] = []
    warnings: list[str] = []
    records_by_id: dict[str, dict] = {}

    for sheet in workbook.worksheets:
        if _normalise(sheet.title) in {
            _normalise(name) for name in _EXCLUDED_SHEETS
        }:
            continue

        rows = sheet.iter_rows(values_only=True)
        headers_row = next(rows, None)
        if not headers_row:
            continue

        headers = [_cell_text(value) for value in headers_row]
        company_index = _column_index(headers, "Company ID")
        branch_index = _column_index(headers, "Branch")

        if company_index is None or branch_index is None:
            warnings.append(
                f"Worksheet '{sheet.title}' was skipped: required columns "
                "Company ID and Branch were not found."
            )
            continue

        for row_number, row in enumerate(rows, start=2):
            if not row or not any(value is not None for value in row):
                continue

            company_id = _get_cell(row, headers, "Company ID")
            branch = _get_cell(row, headers, "Branch")

            if not company_id or not branch:
                warnings.append(
                    f"Worksheet '{sheet.title}', row {row_number}: "
                    "missing Company ID or Branch."
                )
                continue

            raw_values = {
                header: _cell_text(row[index]) if index < len(row) else ""
                for index, header in enumerate(headers)
                if header
            }

            device_values = []
            for header, value in raw_values.items():
                if not value:
                    continue
                if _normalise(header) in {
                    "company id",
                    "service vlan",
                    "ip block",
                    "dr vlan",
                    "dr ip block",
                    "branch",
                    "contact person",
                    "phone number",
                }:
                    continue
                device_values.append(value)

            record = {
                "company_id": company_id,
                "organisation": sheet.title,
                "branch": branch,
                "service_vlan": _get_cell(row, headers, "Service VLAN"),
                "ip_block": _get_cell(row, headers, "IP BLOCK"),
                "dr_vlan": _get_cell(row, headers, "DR VLAN"),
                "dr_ip_block": _get_cell(row, headers, "DR IP BLOCK"),
                "contact_person": _get_cell(row, headers, "Contact Person"),
                "phone_number": _get_cell(row, headers, "Phone Number"),
                "device_identifiers": list(dict.fromkeys(device_values)),
                "aliases": [],

                # Lab diagnostic configuration from the inventory sheet
                "diagnostic_environment": _get_cell(
                    row, headers, "Diagnostic Environment"
                ).strip().upper(),

                "lab_router_host": _get_cell(
                    row, headers, "Lab Router Host"
                ),

                "lab_gateway_target": _get_cell(
                    row, headers, "Lab Gateway Target"
                ),

                "lab_bank_router_target": _get_cell(
                    row, headers, "Lab Bank Router Target"
                ),

                "lab_diagnostic_enabled": _get_cell(
                    row, headers, "Lab Diagnostic Enabled"
                ).strip().upper(),

                "raw_fields": raw_values,
                "synthetic": True,
            }

            # Correct only the known stale simulated CHR host-side endpoint.
            # Preserve raw_fields as source evidence; this is not an internet gateway.
            if (
                company_id == "SIM-WEM-001"
                and record["diagnostic_environment"] == "LAB"
                and record["lab_diagnostic_enabled"] == "YES"
                and record["lab_router_host"] == "192.168.128.2"
                and record["lab_gateway_target"] == "192.168.128.1"
                and record["lab_bank_router_target"] == "192.168.128.3"
            ):
                record["lab_gateway_target"] = "192.168.128.254"

            if company_id in records_by_id:
                warnings.append(
                    f"Duplicate Company ID found: {company_id}."
                )
            else:
                records_by_id[company_id] = record
                records.append(record)

    # Alias records are matched to their Company ID, never to a guessed row.
    if "Site Aliases" in workbook.sheetnames:
        sheet = workbook["Site Aliases"]
        rows = sheet.iter_rows(values_only=True)
        headers_row = next(rows, None)

        if headers_row:
            headers = [_cell_text(value) for value in headers_row]
            for row in rows:
                company_id = _get_cell(row, headers, "Company ID")
                alias = _get_cell(row, headers, "Alias")

                if not company_id or not alias:
                    continue

                record = records_by_id.get(company_id)
                if record is None:
                    warnings.append(
                        f"Alias '{alias}' refers to unknown Company ID "
                        f"'{company_id}'."
                    )
                    continue

                record["aliases"].append(alias)

    workbook.close()

    if not records:
        raise ValueError(
            "No usable inventory records were found. Check the workbook "
            "and its required columns."
        )

    return records, warnings


def _google_export_url(sheet_id: str) -> str:
    return "https://" + "docs.google.com" + "/spreadsheets/d/" + sheet_id + "/export?format=xlsx"

def _download_google_sheet(url: str) -> bytes:
    request = Request(
        url,
        headers={"User-Agent": "FieldBase-NOC-Inventory/1.0"},
    )
    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "").casefold()
        content = response.read()

    if "spreadsheet" not in content_type and not content.startswith(b"PK"):
        raise ValueError(
            "Google Sheets did not return an Excel workbook. "
            "Check that the simulated sheet is publicly readable."
        )

    return content


def load_inventory(
    force_refresh: bool = False,
    fallback_path: str | Path | None = None,
) -> dict:
    """Load Google Sheets inventory, falling back to the local Excel fixture.

    Results are cached for ten minutes. Set force_refresh=True to bypass
    the cache. The returned source and timestamp describe the loaded copy.
    """
    sheet_id = os.getenv(
        "FIELDBASE_INVENTORY_SHEET_ID",
        DEFAULT_SHEET_ID,
    ).strip()

    export_url = _google_export_url(sheet_id)
    local_path = Path(fallback_path) if fallback_path else DEFAULT_FALLBACK
    cache_key = (export_url, str(local_path.resolve()))

    with _cache_lock:
        if not force_refresh and cache_key in _cache:
            age = monotonic() - _cache_times[cache_key]
            if age < CACHE_SECONDS:
                return dict(_cache[cache_key])

    google_error = None
    result = None

    try:
        content = _download_google_sheet(export_url)
        records, warnings = _read_workbook(content)
        result = {
            "records": records,
            "source": "Google Sheets (simulated inventory)",
            "refreshed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "warnings": warnings,
            "error": None,
            "synthetic": True,
        }
    except Exception as exc:
        google_error = f"Google Sheets unavailable: {exc}"

    if result is None:
        try:
            if not local_path.is_file():
                raise FileNotFoundError(
                    f"Fallback workbook not found: {local_path}"
                )

            records, warnings = _read_workbook(local_path.read_bytes())
            result = {
                "records": records,
                "source": "Local Excel fallback (simulated inventory)",
                "refreshed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "warnings": warnings,
                "error": google_error,
                "synthetic": True,
            }
        except Exception as exc:
            result = {
                "records": [],
                "source": "Unavailable",
                "refreshed_at": None,
                "warnings": [],
                "error": f"{google_error or 'Google Sheets unavailable.'} "
                         f"Local Excel fallback also failed: {exc}",
                "synthetic": True,
            }

    with _cache_lock:
        _cache[cache_key] = result
        _cache_times[cache_key] = monotonic()

    return dict(result)


def clear_inventory_cache() -> None:
    """Clear cached inventory copies. Intended for tests and manual refresh."""
    with _cache_lock:
        _cache.clear()
        _cache_times.clear()


def match_inventory(
    query: str,
    records: list[dict],
    organisation: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return possible site matches without automatically selecting one.

    Matching is based on Company ID, branch, alias and device identifiers.
    An optional organisation context narrows candidates.
    """
    search = _match_normalise(query)
    if not search or not (set(search.split()) - _SITE_NOISE):
        return []

    organisation_search = _organisation_key(organisation) if organisation else None
    matches = []

    for record in records:
        if organisation_search and _organisation_key(record.get("organisation", "")) != organisation_search:
            continue
        fields = list(_record_site_fields(record))

        matched_by = []
        for match_type, value, _ in fields:
            candidate = _match_normalise(value)
            if not candidate:
                continue

            if search == candidate or search in candidate:
                matched_by.append(match_type)

        if matched_by:
            item = dict(record)
            item["matched_by"] = list(dict.fromkeys(matched_by))
            matches.append(item)

    matches.sort(
        key=lambda item: (
            0 if _normalise(item.get("company_id")) == search else 1,
            0 if _normalise(item.get("branch")) == search else 1,
            _normalise(item.get("organisation")),
            _normalise(item.get("branch")),
        )
    )

    return matches[:max(1, limit)]


# Explicit organisation aliases are context only, never sufficient site evidence.
_ORGANISATION_ALIASES = {
    "wema": ("wema", "wema bank", "wem"),
    "gtbank": ("gtbank", "gt bank", "gtb", "guaranty trust bank"),
    "zenith": ("zenith", "zenith bank", "zen"),
    "uba": ("uba", "united bank for africa"),
    "first bank": ("first bank", "firstbank", "first bank of nigeria", "fbn"),
    "cyberspace": ("cyberspace", "cyb"),
    "estream": ("estream", "e stream", "est"),
    "ecobank": ("ecobank", "eco bank", "eco"),
    "unity bank": ("unity bank", "unity", "uni"),
    "premium trust bank": ("premium trust bank", "premiumtrust", "ptb"),
    "tajbank": ("tajbank", "taj bank", "taj"),
    "fcmb": ("fcmb", "first city monument bank", "fcm"),
    "fidelity": ("fidelity", "fidelity bank"),
}
_SITE_NOISE = {
    "the", "and", "for", "from", "with", "site", "branch", "service", "outlet", "location",
    "network", "link", "bank", "limited", "ltd", "plc", "company", "fieldbase", "fbase",
    "down", "up", "poor", "slow", "optimize", "optimise", "optimization", "optimisation",
    "router", "incident", "transmission", "failure", "open", "closed", "yes", "no", "lab",
    "road", "street", "avenue", "way", "lagos", "abuja", "nigeria",
    "fluctuating", "intermittent", "intermittency", "flapping", "latency", "packet", "loss",
    "restore", "restoration", "request", "notification", "performance",
}


def _match_normalise(value):
    value = _normalise(value)
    value = re.sub(r"\bzone\s+(\d+)\b", r"zone\1", value)
    value = re.sub(r"\bsol\s*0*(\d+)\b", lambda m: "sol " + str(int(m.group(1))), value)
    value = re.sub(r"\brd\b", "road", value)
    return re.sub(r"\bstr\b", "street", value)


def _organisation_key(value):
    value = _normalise(value)
    return next((key for key, aliases in _ORGANISATION_ALIASES.items() if value in aliases), value)


def _record_site_fields(record):
    yield "Company ID", record.get("company_id", ""), True
    targets = {record.get(key) for key in ("lab_router_host", "lab_gateway_target", "lab_bank_router_target")}
    for value in record.get("device_identifiers", []):
        if value not in targets and _match_normalise(value) not in _SITE_NOISE:
            yield "Device identifier", value, True
    yield "Branch", record.get("branch", ""), False
    for value in record.get("aliases", []):
        yield "Alias", value, False
    for label, value in (record.get("raw_fields") or {}).items():
        field = _normalise(label)
        if field in {"sol id", "solid", "sol code", "service outlet code", "associated service id", "site id"}:
            yield "Site code", "SOL " + str(value) if field in {"sol id", "solid", "sol code"} else value, True
        elif field in {"address", "branch address", "site address", "service outlet address"}:
            yield "Address", value, False
    for key, label in (("sol_id", "SOL identifier"), ("service_outlet_code", "Service outlet code"), ("address", "Address")):
        if record.get(key):
            yield label, "SOL " + str(record[key]) if key == "sol_id" else record[key], key != "address"


def match_inventory_from_email(email_text: str, records: list[dict], limit: int = 50) -> list[dict]:
    """Return review candidates from current site clues, never from incident vocabulary.

    Unknown explicit codes do not fall back to loose token overlap. No records,
    authorisation fields, targets, or confirmation decisions are modified here.
    """
    parsed = parse_noc_email(email_text)
    email = _match_normalise(parsed["matching_text"])
    if not email:
        return []
    email_tokens = set(email.split())
    known = set()
    organisations = {_organisation_key(r.get("organisation", "")) for r in records}
    # Also recognize explicitly supported customer names absent from this fixture
    # (e.g. Fidelity) so another bank in the same city cannot be substituted.
    for key in organisations | set(_ORGANISATION_ALIASES):
        for alias in _ORGANISATION_ALIASES.get(key, (key,)):
            if alias and f" {_match_normalise(alias)} " in f" {email} ":
                known.add(key)
    has_code = bool(re.search(r"\bsol\s+\d+\b", email) or any(parsed["fields"].get(k) for k in ("sol_id", "service_outlet_code", "associated_service_id")))
    matches = []
    for record in records:
        organisation = _organisation_key(record.get("organisation", ""))
        if known and organisation not in known:
            continue
        org_tokens = set()
        for alias in _ORGANISATION_ALIASES.get(organisation, (organisation,)):
            org_tokens.update(_match_normalise(alias).split())
        matched_by = []
        score = 0
        for kind, value, strong in _record_site_fields(record):
            candidate = _match_normalise(value)
            specific = set(candidate.split()) - _SITE_NOISE - org_tokens
            if not candidate or not specific:
                continue
            if f" {candidate} " in f" {email} ":
                # A short status, city or organisation name is not a site identity.
                matched_by.append(kind)
                score = max(score, 4 if strong else 3)
            elif not strong and not has_code and kind in {"Branch", "Alias"}:
                # Require every meaningful location token, rather than any word
                # shared with a long incident email or a vaguely similar branch.
                if specific.issubset(email_tokens) and (organisation in known or len(specific) >= 2):
                    matched_by.append(kind + " context")
                    score = max(score, 2)
        if score:
            item = dict(record)
            item["matched_by"] = list(dict.fromkeys(matched_by))
            if organisation in known:
                item["matched_by"].append("Organisation context")
            item["_match_score"] = score
            matches.append(item)
    if matches:
        highest = max(item["_match_score"] for item in matches)
        matches = [item for item in matches if item["_match_score"] == highest]
    matches.sort(key=lambda item: (_normalise(item.get("organisation")), _normalise(item.get("branch"))))
    for item in matches:
        item.pop("_match_score", None)
    return matches[:max(1, limit)]


def inventory_summary(inventory: dict) -> dict:
    """Return basic inventory health and ambiguity information."""
    records = inventory.get("records", [])
    company_ids = [
        record.get("company_id", "")
        for record in records
        if record.get("company_id")
    ]

    return {
        "record_count": len(records),
        "organisation_count": len({
            record.get("organisation")
            for record in records
            if record.get("organisation")
        }),
        "duplicate_company_ids": sorted({
            value for value in company_ids
            if company_ids.count(value) > 1
        }),
        "warning_count": len(inventory.get("warnings", [])),
        "source": inventory.get("source", "Unavailable"),
        "refreshed_at": inventory.get("refreshed_at"),
    }
