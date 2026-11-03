"""Email triage workspace with controlled LAB diagnostics."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from email.utils import getaddresses
import os
from pathlib import Path
import sqlite3
import uuid

import streamlit as st

from email_assessment import assess_email_report, response_body, response_options, RESPONSE_TEMPLATES
from noc_email import parse_noc_email
from diagnostics_service import run_diagnostics
from diagnostic_report_ui import render_diagnostic_report, format_report_rtt as _format_report_rtt
from email_cases import CASE_STATUSES, CaseStore, create_case
from ping_diagnostics import (
    format_ping_summary_line,
    restore_diagnostic_report,
)

APPROVED_LAB = {
    "router": "192.168.128.2",
    "gateway": "192.168.128.254",
    "bank_router": "192.168.128.3",
}


def run_confirmed_lab_diagnostics(
    record: dict,
    username: str,
    password: str,
    packet_count: int,
) -> dict:
    """Run diagnostics only against the explicitly approved virtual CHR."""

    if record.get("synthetic") is not True:
        raise ValueError(
            "Diagnostic blocked: the inventory record is not explicitly "
            "identified as synthetic."
        )

    if record.get("diagnostic_environment") != "LAB":
        raise ValueError("Diagnostic blocked: record is not marked LAB.")

    if record.get("lab_diagnostic_enabled") != "YES":
        raise ValueError("Diagnostic blocked: LAB diagnostics are not enabled.")

    host = str(record.get("lab_router_host", "")).strip()
    gateway = str(record.get("lab_gateway_target", "")).strip()
    bank_router = str(record.get("lab_bank_router_target", "")).strip()

    if (
        host != APPROVED_LAB["router"]
        or gateway != APPROVED_LAB["gateway"]
        or bank_router != APPROVED_LAB["bank_router"]
    ):
        raise ValueError(
            "Diagnostic blocked: inventory addresses do not match "
            "the approved LAB configuration."
        )

    for address in (host, gateway, bank_router):
        ipaddress.ip_address(address)

    if packet_count not in (5, 10, 20, 100):
        raise ValueError("Unsupported diagnostic packet count.")

    if not username.strip() or not password:
        raise ValueError("Enter the router username and password.")

    result = run_diagnostics(
        host=host,
        username=username.strip(),
        password=password,
        gateway=gateway,
        bank_router=bank_router,
        packet_count=packet_count,
        discover_candidates=False,
    )

    if not isinstance(result, dict):
        raise ValueError("The diagnostic runner returned an invalid report.")

    if (result.get("host") != host
            or result.get("connection_status") != "connected"
            or result.get("simulated") is not False):
        raise ValueError("Diagnostic report does not match the approved LAB connection.")

    returned_pings = result.get("pings", [])
    if not isinstance(returned_pings, list):
        raise ValueError("Diagnostic report has invalid ping results.")

    returned_targets = [
        str(ping.get("target", "")).strip()
        for ping in returned_pings
        if isinstance(ping, dict)
    ]

    if (
        len(returned_pings) != 2
        or len(returned_targets) != 2
        or set(returned_targets) != {gateway, bank_router}
    ):
        raise ValueError(
            "Diagnostic results do not contain exactly the two "
            "approved LAB ping targets. Results were not saved."
        )

    if result.get("bank_discovery_status") != "inventory_confirmed":
        raise ValueError(
            "Diagnostic blocked: the bank-router target was not "
            "reported as inventory-confirmed."
        )

    if result.get("selected_bank_router") != bank_router:
        raise ValueError(
            "Diagnostic blocked: the selected bank-router address "
            "does not match the confirmed inventory record."
        )

    result["lab_authorization"] = "confirmed"
    return result


ISSUE_RULES = (
    (
        "Link down",
        (
            "link down",
            "link is down",
            "outage",
            "offline",
            "disconnected",
            "no connectivity",
            "no connection",
            "complete loss of connectivity",
            "total loss of connectivity",
            "loss of connectivity",
            "loss of connection",
            "complete loss of service",
            "total loss of service",
            "network connection unavailable",
            "connection is currently unavailable",
            "connectivity unavailable",
            "currently unavailable",
            "service is unavailable",
            "site is down",
            "site down",
            "branch is down",
            "branch down",
            "site is offline",
            "site offline",
            "branch is offline",
            "unable to connect",
            "cannot connect",
            "can't connect",
            "all services unreachable",
            "services are unreachable",
        ),
        10,
        "The email reports a link outage or loss of connectivity.",
    ),
    (
        "Degraded or unoptimized link",
        (
            "degraded",
            "high latency",
            "intermittent",
            "fluctuating link",
            "packet loss",
            "dropping packet",
            "intermittency",
            "link flapping",
            "unstable link",
            "poor link",
            "poor fieldbase link",
            "packet drops",
            "high delay",
            "slow response",
            "slow link",
            "very slow",
            "slow",
            "not optimized",
            "unoptimized",
        ),
        100,
        "The email reports degraded performance or unstable connectivity.",
    ),
)

DOWN_PHRASES = ISSUE_RULES[0][1]
DEGRADED_PHRASES = ISSUE_RULES[1][1]

CURRENT_MARKERS = (
    "currently",
    "current issue",
    "right now",
    "now",
    "today",
    "latest update",
    "latest status",
)

NEGATION_PATTERNS = (
    r"\bno longer (?:down|offline|disconnected|unavailable)\b",
    r"\bnot (?:down|offline|disconnected|unavailable)\b",
    r"\bnot currently unavailable\b",
    r"\b(?:no|without) (?:longer )?(?:an? )?(?:outage|link down|connectivity issue)\b",
    r"\bresolved\b",
    r"\brestored\b",
)


def _active_evidence(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return phrase matches that are not explicitly negated or resolved."""
    matches = []

    for phrase in phrases:
        for match in re.finditer(re.escape(phrase), text):
            prefix = text[max(0, match.start() - 45):match.start()]
            suffix = text[match.end():match.end() + 30]
            negated = bool(
                re.search(r"\b(?:not(?: currently)?|no(?: longer| evidence of)?|without(?: any)?)\s*$", prefix)
                or re.match(r"\s+(?:is\s+)?(?:not observed|absent)\b", suffix)
            )
            if not negated:
                matches.append(phrase)
                break

    return matches


RESTORE_PATTERNS = (
    r"\brestore (?:the )?(?:subject )?(?:link|service)\b",
    r"\brestoration of (?:the )?(?:link|service)\b",
    r"\bbring (?:the )?(?:link|service) back\b",
    r"\bre-establish (?:the )?(?:link|connectivity)\b",
    r"\brestore (?:asap|urgently)\b",
)

RESOLVED_PATTERNS = (
    r"\b(?:outage|issue|link down|connectivity issue)\b.{0,35}\b(?:resolved|restored|fixed|cleared)\b",
    r"\b(?:resolved|restored|fixed|cleared)\b.{0,35}\b(?:outage|issue|link|connectivity)\b",
)

OUTGOING_RESTORATION_PATTERNS = (
    r"\b(?:the )?(?:link|connectivity|service) (?:has been |is )?restored\b.{0,60}\b(?:kindly|please) (?:(?:check and )?(?:re)?confirm|check)\b",
    r"\b(?:kindly|please) (?:re)?confirm\b.{0,60}\b(?:link|connectivity|service) (?:has been |is )?restored\b",
    r"\b(?:the )?(?:link|connectivity|service) restored\b.{0,60}\b(?:kindly|please) (?:check|confirm)\b",
)


def _latest_message(email_text: str) -> str:
    """Drop conventional quoted reply history while retaining current escalation headers."""
    retained = []
    in_quote = False

    for line in email_text.splitlines():
        stripped = line.strip()

        if (
            re.match(
                r"-{2,}\s*(?:original message|forwarded message)",
                stripped,
                re.I,
            )
            or re.match(r"On .+wrote:", stripped, re.I)
            or stripped.startswith(">")
        ):
            in_quote = True

        if not in_quote:
            retained.append(line)

    return "\n".join(retained)


def _evidence_sentences(
    email_text: str,
    phrases: list[str],
    extra: tuple[str, ...] = (),
) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", email_text.strip())
    needles = [value.casefold() for value in phrases] + list(extra)

    return list(dict.fromkeys(
        sentence.strip()
        for sentence in parts
        if sentence.strip()
        and any(n in sentence.casefold() for n in needles)
    ))


def _classification(
    issue: str,
    evidence: list[str],
    reason: str,
    investigation: str,
    ping_count: int | None,
    why_ping_count: str,
) -> dict[str, object]:
    return {
        "issue": issue,
        "evidence": evidence,
        "reason": reason,
        "proposed_investigation": investigation,
        "why_ping_count": why_ping_count,
        "ping_count": ping_count,
        "site_identifiers": [],
        "metadata": {},
    }


def _field_value(text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^\s*(?:{label_pattern})\s*:\s*(.+?)\s*$",
        text,
    )
    return match.group(1).strip() if match else None


def parse_site_identifiers(text: str) -> list[str]:
    """Extract distinct supported site identifiers from text (including reviewed OCR)."""
    patterns = (
        r"\bFBN-[A-Z0-9]+(?:-[A-Z0-9]+){1,}-RTR(?:\.firstbanknigeria\.com)?\b",
        r"\bKONGO_HOTEL\s*\([^\n)]*\)\s*GATEWAY_\d+\b",
        r"\bSOL[_ -]*\d+\b",
    )

    parsed = parse_noc_email(text)
    reference_text = parsed["matching_text"]
    found = []

    for pattern in patterns:
        found.extend(
            match.group(0).strip()
            for match in re.finditer(pattern, reference_text, re.I)
        )

    if parsed["fields"].get("sol_id"):
        sol = parsed["fields"]["sol_id"]
        found.append(sol if re.match(r"SOL[_ -]*", sol, re.I) else "SOL " + sol)
    for field in ("service_outlet_code", "associated_service_id"):
        if parsed["fields"].get(field):
            found.append(parsed["fields"][field])
    return list(dict.fromkeys(found))


def detect_l2_recipient(
    email_text: str,
    configured_address: str | None,
) -> bool | None:
    """Check whether To/Cc/Delivered-To headers name the configured L2 address."""
    if not configured_address or "@" not in configured_address:
        return None

    header_block = email_text.split("\n\n", 1)[0]
    values = []

    for line in header_block.splitlines():
        match = re.match(
            r"(?i)^\s*(to|cc|delivered-to|x-original-to)\s*:\s*(.*)$",
            line,
        )

        if match:
            values.append(match.group(2))

    recipients = {
        address.casefold()
        for _, address in getaddresses(values)
        if address
    }

    if not recipients:
        return None

    return configured_address.strip().casefold() in recipients


def site_investigation_plan(
    site_identifiers: list[str],
    ping_count: int | None,
) -> list[dict[str, object]]:
    """Create a separate, reviewable plan row per distinct site."""
    unique_sites = list(dict.fromkeys(
        site.strip()
        for site in site_identifiers
        if site.strip()
    ))

    return [
        {
            "site": site,
            "ping_count": ping_count,
            "confirmation_required": True,
        }
        for site in unique_sites
    ]
def _classify_email_text(
    email_text: str,
    direction: str | None = None,
) -> dict[str, object]:
    """Classify reported symptoms conservatively; this never performs network actions."""
    latest_text = _latest_message(email_text)
    normalized = re.sub(r"\s+", " ", latest_text.casefold()).strip()

    direction = direction.casefold().strip() if direction else None

    if direction not in (None, "incoming", "outgoing"):
        raise ValueError("direction must be 'incoming', 'outgoing', or None")

    outgoing_restoration = any(
        re.search(pattern, normalized)
        for pattern in OUTGOING_RESTORATION_PATTERNS
    )

    if direction == "outgoing" or (
        direction is None and outgoing_restoration
    ):
        if outgoing_restoration or re.search(
            r"\b(?:restored|back up|service available)\b",
            normalized,
        ):
            return _classification(
                "Outgoing restoration notification",
                _evidence_sentences(
                    latest_text,
                    [],
                    ("restored", "back up", "service available"),
                ) or [latest_text.strip()],
                "This is an outgoing operational restoration reply, not an incoming complaint.",
                "No new diagnostic recommendation is created. Await the recipient's reconfirmation or use the existing incident workflow.",
                None,
                "No ping count is selected because this message does not report a new incoming fault.",
            )

        return _classification(
            "Outgoing operational reply",
            [latest_text.strip()],
            "The message is explicitly marked as outgoing and is not classified as an incoming incident.",
            "No new diagnostic recommendation is created.",
            None,
            "No ping count is selected for an outgoing reply.",
        )

    if (
        direction is None
        and re.search(
            r"\b(?:restored|back up|service available)\b",
            normalized,
        )
        and not any(
            re.search(pattern, normalized)
            for pattern in RESOLVED_PATTERNS
        )
    ):
        return _classification(
            "Direction uncertain",
            _evidence_sentences(
                latest_text,
                [],
                ("restored", "back up", "service available"),
            ) or [latest_text.strip()],
            "The text describes restored service, but pasted text does not establish whether this is an incoming update or an outgoing operational reply.",
            "Confirm message direction and current incident status before choosing an investigation.",
            None,
            "No ping count is selected while message direction and current status are uncertain.",
        )

    down_matches = _active_evidence(normalized, DOWN_PHRASES)

    if re.search(
        r"\bfaulty description\s*:\s*link down\b|"
        r"\bfault message\s*:\s*link is down\b",
        normalized,
    ):
        down_matches.append("structured link-down incident")

    if re.search(
        r"\blink (?:at .{0,80} )?(?:is )?"
        r"(?:currently )?(?:completely )?down\b",
        normalized,
    ):
        down_matches.append("link down")

    degraded_matches = _active_evidence(normalized, DEGRADED_PHRASES)

    if (
        re.search(r"\b(?:optimi[sz]e|optimization|optimisation)\b", normalized)
        and not down_matches
    ):
        degraded_matches.append("optimization request")

    if re.search(r"\bnot serving .* as expected\b", normalized):
        degraded_matches.append("not serving as expected")

    evidence = _evidence_sentences(
        latest_text,
        list(dict.fromkeys(down_matches + degraded_matches)),
        (
            "currently down",
            "link is down",
            "complete loss of connectivity",
            "total loss of connectivity",
            "currently unavailable",
            "unable to connect",
            "cannot connect",
            "can't connect",
            "all services are unreachable",
            "services are unreachable",
        ),
    )

    ping_sample = re.search(
        r"success rate is\s+(\d+\s+percent\s*\(\d+/\d+\)),"
        r"\s*round-trip min/avg/max\s*=\s*([\d.]+/[\d.]+/[\d.]+\s*ms)",
        latest_text,
        re.I,
    )

    if ping_sample:
        evidence.append(
            f"Existing ping output: success rate {ping_sample.group(1)}; "
            f"round-trip min/avg/max {ping_sample.group(2)}"
        )

    if any(
        re.search(pattern, normalized)
        for pattern in RESOLVED_PATTERNS
    ):
        return _classification(
            "Needs review",
            evidence or ([latest_text.strip()] if latest_text.strip() else []),
            "The email indicates the issue may already be resolved or the link restored, so the current state needs human confirmation.",
            "Review the latest status and decide whether any verification is still needed.",
            None,
            "No ping count is selected because the issue may already be resolved.",
        )

    current_outage = False

    current_outage_phrases = (
        "outage",
        "currently down",
        "currently offline",
        "offline",
        "disconnected",
        "unavailable",
        "link down",
        "link is down",
        "no connectivity",
        "no connection",
        "complete loss of connectivity",
        "total loss of connectivity",
        "loss of connectivity",
        "loss of connection",
        "complete loss of service",
        "total loss of service",
        "unable to connect",
        "cannot connect",
        "can't connect",
        "all services unreachable",
        "services are unreachable",
    )

    for marker in CURRENT_MARKERS:
        marker_at = normalized.rfind(marker)

        if marker_at >= 0:
            context = normalized[marker_at:marker_at + 180]

            if any(
                phrase in context
                for phrase in current_outage_phrases
            ):
                current_outage = True
                break

    if current_outage and down_matches:
        current_evidence = _evidence_sentences(
            latest_text,
            list(dict.fromkeys(down_matches)),
            current_outage_phrases,
        )

        return _classification(
            "Link down",
            current_evidence or evidence,
            "The latest/current email context reports a complete outage, taking priority over earlier performance symptoms.",
            "Run a 10-ping reachability check against the intended endpoint.",
            10,
            "Ten pings provide a short confirmation of reachability during the reported current outage.",
        )

    sites = parse_site_identifiers(latest_text)

    if len(sites) > 1 and re.search(
        r"\b(?:outage|downtime|link down|restore|restoration)\b",
        normalized,
    ):
        result = _classification(
            "Multi-site outage/restoration request",
            evidence or [latest_text.strip()],
            "The email describes an outage or restoration request affecting multiple named sites.",
            f"Run an initial 10-ping reachability check separately for each affected site ({len(sites)} identified); one site's result does not validate the others.",
            10,
            "Ten pings are an initial reachability check per selected affected site.",
        )

        result["site_identifiers"] = sites
        result["pings_per_site"] = 10
        return result

    if any(
        re.search(pattern, normalized)
        for pattern in RESTORE_PATTERNS
    ):
        restore_evidence = _evidence_sentences(
            latest_text,
            [],
            ("restore", "restoration", "bring the link back", "re-establish"),
        )

        return _classification(
            "Link restoration requested",
            restore_evidence or ([latest_text.strip()] if latest_text.strip() else []),
            "The email requests or discusses restoring the link; this does not establish that restoration has already happened.",
            "After restoration is carried out, run a 10-ping reachability check to verify the link.",
            10,
            "Ten pings are a short reachability verification after restoration, not evidence that restoration is complete.",
        )

    if down_matches and degraded_matches:
        return _classification(
            "Needs review",
            evidence,
            "The email reports both an outage and degraded symptoms without clear timing to identify the current condition.",
            "Review the latest message/status and determine which symptom is current.",
            None,
            "No ping count is selected because the conflicting symptoms do not establish the appropriate test.",
        )

    if down_matches:
        result = _classification(
            "Link down",
            evidence,
            "The email reports a complete loss of connectivity.",
            "Run a 10-ping reachability check against the intended endpoint.",
            10,
            "Ten pings provide a short confirmation of reachability for a complete outage report.",
        )

        metadata = {
            "service_outlet": _field_value(latest_text, ("Service Outlet",)),
            "link_technology": _field_value(latest_text, ("Link Technology",)),
            "incident_status": _field_value(latest_text, ("Faulty Status", "Status")),
            "fault_description": _field_value(latest_text, ("Faulty Description", "Fault Message")),
        }

        result["metadata"] = {
            key: value for key, value in metadata.items() if value
        }

        return result

    if degraded_matches:
        link_up = bool(
            re.search(
                r"\b(?:link|connection|service)\s+(?:is\s+)?up\b|\bstill reachable\b",
                normalized,
            )
        )

        unoptimized = any(
            term in normalized
            for term in (
                "not optimized",
                "unoptimized",
                "optimize",
                "optimise",
                "optimization",
                "optimisation",
                "poor fieldbase link",
            )
        )

        issue = (
            "Link optimization requested"
            if unoptimized and not any(
                term in normalized
                for term in (
                    "slow",
                    "packet loss",
                    "dropping packet",
                    "high latency",
                    "fluctuating",
                    "unstable",
                )
            )
            else "Degraded or unoptimized link"
        )

        reason = (
            "The email requests link optimization; ping results assess reachability, latency, and packet loss but cannot independently establish whether the link is fully optimized."
            if issue == "Link optimization requested"
            else "The email reports degraded performance or unstable connectivity."
        )

        investigation = (
            "Run a 100-ping check and review the collected latency and packet-loss results."
        )

        if link_up or unoptimized:
            if issue != "Link optimization requested":
                reason += " Ping results alone cannot confirm every optimization issue."

            investigation += (
                " Also review interface errors/discards, utilization and congestion, "
                "the affected service path, and gateway and endpoint reachability."
            )

        if ping_sample:
            reason += (
                " The supplied sample suggests possible packet loss and elevated latency, "
                "but it does not establish the root cause."
            )

        result = _classification(
            issue,
            evidence,
            reason,
            investigation,
            100,
            "Run a 100-ping sample; an existing five-ping result is evidence, not the recommended sample size.",
        )

        metadata = {
            "service_outlet": _field_value(latest_text, ("Service Outlet",)),
            "link_technology": _field_value(latest_text, ("Link Technology",)),
            "incident_status": _field_value(latest_text, ("Faulty Status", "Status")),
            "fault_description": _field_value(latest_text, ("Faulty Description", "Fault Message")),
        }

        result["metadata"] = {
            key: value for key, value in metadata.items() if value
        }

        return result

    return _classification(
        "Needs review",
        [latest_text.strip()] if latest_text.strip() else [],
        "No clear active link-down, restoration, or degraded-link complaint was found.",
        "Review the email and select an investigation manually.",
        None,
        "No ping count is selected because the complaint is unclear.",
    )


def classify_email(email_text: str, direction: str | None = None) -> dict[str, object]:
    """Interpret current subject/body/incident fields; preserve the existing policies."""
    parsed = parse_noc_email(email_text)
    fields = parsed["fields"]
    diagnostic_text = parsed["analysis_text"]
    result = _classify_email_text(diagnostic_text, direction)
    body_text = "\n".join(x for x in (parsed["body"], fields.get("fault_description"), fields.get("fault_message")) if x)
    body_result = _classify_email_text(body_text, direction)
    subject_result = _classify_email_text(parsed["subject"] or "", direction)
    down = {"Link down", "Link restoration requested", "Multi-site outage/restoration request"}
    performance = {"Degraded or unoptimized link", "Link optimization requested"}
    active = down | performance
    # A clear current update wins over a stale subject; an unexplained conflict needs review.
    current_body = bool(re.search(r"\b(?:currently|right now|latest update|latest status|current status|today|now)\b", body_text, re.I))
    if current_body and body_result["issue"] in active:
        result = body_result
    elif ((subject_result["issue"] in down and body_result["issue"] in performance)
          or (subject_result["issue"] in performance and body_result["issue"] in down)):
        result = _classification(
            "Needs review", result["evidence"],
            "The subject and current body describe different outage/performance conditions without a clear latest status.",
            "Confirm the current symptom before selecting the existing approved investigation.", None,
            "No packet count is selected for contradictory subject/body reports.",
        )
    if (result["issue"] == "Link restoration requested"
            and _active_evidence(diagnostic_text.casefold(), DOWN_PHRASES)
            and not _active_evidence(diagnostic_text.casefold(), DEGRADED_PHRASES)):
        result = _classification(
            "Link down", result["evidence"],
            "The message reports an active outage and requests restoration; restoration has not been verified.",
            "Run a 10-ping reachability check against the intended endpoint.", 10,
            "Ten pings preserve the existing short outage investigation policy.",
        )
    status = fields.get("incident_status", "").casefold().strip()
    if status in {"closed", "resolved", "restored", "completed", "cancelled"}:
        result = _classification(
            "Needs review", result["evidence"],
            "The customer-provided incident status is closed or resolved; current service and any need for verification require review.",
            "Review the latest status; a customer status field is not verified restoration evidence.", None,
            "No packet count is selected for a closed/resolved report without current confirmation.",
        )
    elif "/" in fields.get("incident_status", ""):
        result = _classification("Needs review", result["evidence"], "Conflicting incident status fields require review.",
                                 "Confirm the latest incident status before diagnostics.", None,
                                 "No packet count is selected for conflicting status fields.")
    elif (result["issue"] in {"Needs review", "Direction uncertain"}
          and direction != "outgoing"
          and re.search(r"\b(?:restoration verification|verify (?:the )?restored (?:link|connectivity)|(?:please|kindly) (?:verify|check)\b.{0,45}\brestor(?:ed|ation))\b", diagnostic_text, re.I)):
        result = _classification(
            "Restoration verification", [body_text.strip() or diagnostic_text.strip()],
            "The message requests restoration verification; the reported restoration is not independently verified.",
            "Run the existing 10-ping restoration verification after confirming the site and approved LAB targets.", 10,
            "Ten packets preserve the existing restoration-verification policy; the request is not proof of restored service.",
        )
    elif result["issue"] == "Needs review" and not _active_evidence(diagnostic_text.casefold(), DOWN_PHRASES + DEGRADED_PHRASES):
        if re.search(r"\b(?:confirm|check|verify|test)\b.{0,45}\b(?:router|device)\b.{0,30}\b(?:reachable|reachability|status)\b", diagnostic_text, re.I):
            result = _classification(
                "Device reachability check", [body_text.strip() or diagnostic_text.strip()],
                "The message requests a device-status check, not a confirmed link outage.",
                "Run a 5-ping reachability check after confirming the site and approved LAB targets.", 5,
                "Five packets use an already approved count for a short device-reachability check; outage and performance policies remain 10 and 100.",
            )
    result["reported_subject"] = parsed["subject"]
    result["reported_site_references"] = parsed["site_references"]
    result["reported_fields"] = dict(fields)
    if fields.get("reported_root_cause"):
        result["reported_root_cause"] = fields["reported_root_cause"]
        result["root_cause_verified"] = False
    # Keep the established metadata keys for existing consumers and saved-case behavior.
    if result["ping_count"] is not None:
        legacy = {
            "service_outlet": fields.get("service_outlet") or fields.get("service_outlet_name"),
            "link_technology": fields.get("link_technology"),
            "incident_status": fields.get("incident_status"),
            "fault_description": fields.get("fault_description") or fields.get("fault_message"),
        }
        result["metadata"] = {key: value for key, value in legacy.items() if value}
        if not result.get("site_identifiers"):
            result["site_identifiers"] = parse_site_identifiers(parsed["latest_text"])
    return result


def _automation_key(email_text: str, record: dict) -> str:
    """Stable identifier to prevent duplicate cases for the same email/site."""
    source = (
        email_text.strip().casefold()
        + "|"
        + str(record.get("company_id", "")).strip()
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _render_email_assessment(st, report: dict, case: dict | None = None) -> None:
    """Present observed facts separately from interpretation and next actions."""
    case = case or {}
    error = case.get("diagnostic_error") if case.get("diagnostic_status") == "Failed" else None
    if not error and case.get("diagnostic_status") == "Failed":
        error = "The diagnostic run failed; further error details are unavailable."
    assessment = assess_email_report(report, diagnostic_error=error)
    if not report and case.get("diagnostic_status") != "Failed":
        assessment["status"] = "Needs Review" if str(case.get("detected_issue", "")).casefold() == "needs review" else "Not Attempted"
        assessment["interpretation"] = "No device diagnostic was attempted. Review the email and workflow status before choosing an approved investigation."
        assessment["actions"] = ["Review the reported symptom and any blocked/waiting workflow status.",
                                 "Confirm the inventory site and appropriate approved investigation.",
                                 "Use available LAB session credentials only after the issue and site are confirmed."]
        if case.get("diagnostic_error"):
            st.caption(f"Workflow status: {case['diagnostic_error']}")
    st.write(f"**Diagnostic status:** {assessment['status']}")
    st.markdown("**Observed results**")
    if assessment["observations"]:
        rows = []
        for item in assessment["observations"]:
            summary = item["summary"]
            rows.append({
                "Target": f"{item['name']} ({item['target']})",
                "Status": item["status"],
                "Sent / received": f"{summary.sent} / {summary.received}" if summary else "Not available",
                "Packet loss": f"{summary.packet_loss:g}%" if summary else "Not available",
                "RTT min / avg / max": " / ".join(_format_report_rtt(getattr(summary, field, None)) for field in ("min_rtt", "avg_rtt", "max_rtt")),
            })
        st.table(rows)
        for item in assessment["observations"]:
            st.caption(f"{item['name']}: {item['explanation']}")
    else:
        st.caption("No verified ping measurements are available.")
    if assessment["interfaces"]:
        st.caption("Captured interfaces: " + "; ".join(f"{row['Interface']}: {row['State']}" for row in assessment["interfaces"]))
    if assessment["error"]:
        st.warning(assessment["error"])
    st.markdown("**Interpretation**")
    st.write(assessment["interpretation"])
    if not assessment["baseline_available"]:
        st.caption("No latency baseline is available; these measurements do not establish optimal performance or abnormal latency.")
    for warning in report.get("warnings", []):
        st.warning(warning)
    st.markdown("**Recommended next actions**")
    for number, action in enumerate(assessment["actions"], 1):
        st.write(f"{number}. {action}")


def _render_diagnostic_report(st, report: dict, case: dict | None = None) -> None:
    """Render the original diagnosis presentation for a persisted LAB report."""
    if not report:
        return
    report = restore_diagnostic_report(report)
    st.markdown("### Diagnostic results")
    st.metric("LAB authorization", "Confirmed" if report.get("lab_authorization") == "confirmed" else "Not recorded")
    st.caption("LAB authorization records approved workflow scope; the actual router environment has not been independently verified.")
    st.caption(f"Captured: {report.get('captured_at') or 'Unknown'}")
    render_diagnostic_report(
        st, report, inventory_confirmed=True,
        widget_key_prefix=f"email_case_{case['case_id']}" if case else "email_report",
        username=str(st.session_state.get("lab_router_username", "")).strip() or "user",
        assessment_renderer=lambda ui, evidence: _render_email_assessment(ui, evidence, case),
    )


def _build_email_reply_draft(case: dict, report: dict | None, template: str | None = None,
                             *, engineer_restored: bool = False) -> tuple[str, str]:
    """Build one concise, evidence-limited draft using the shared template library."""
    subject = str(case.get("subject") or "Re: Connectivity report")
    error = case.get("diagnostic_error") if case.get("diagnostic_status") == "Failed" else None
    if not error and case.get("diagnostic_status") == "Failed":
        error = "The diagnostic run failed; further error details are unavailable."
    assessment = assess_email_report(report, diagnostic_error=error)
    body, _ = response_body(
        assessment, template, record=case.get("confirmed_inventory_record"),
        engineer_restored=engineer_restored,
    )
    return subject, body


def _render_reply_draft(st, store: CaseStore, case: dict) -> None:
    """Allow an engineer to edit and explicitly save a response draft."""
    report = case.get("lab_diagnostics")
    subject_default, body_default = _build_email_reply_draft(case, report)

    key_base = f"reply_draft_{case['case_id']}"

    if f"{key_base}_subject" not in st.session_state:
        st.session_state[f"{key_base}_subject"] = (
            case.get("reply_draft_subject") or subject_default
        )

    if f"{key_base}_body" not in st.session_state:
        st.session_state[f"{key_base}_body"] = (
            case.get("reply_draft_body") or body_default
        )

    st.markdown("### Suggested email response")
    st.caption(
        "Review and edit this response before using it. "
        "It is a draft only; this workspace does not send email."
    )

    assessment = assess_email_report(report, diagnostic_error=case.get("diagnostic_error") if case.get("diagnostic_status") == "Failed" else None)
    eligible, recommended = response_options(assessment, case.get("confirmed_inventory_record"))
    if not report:
        st.subheader("Diagnostic assessment and recommendations")
        _render_email_assessment(st, {}, case)
    st.caption(f"Recommended response: {RESPONSE_TEMPLATES[recommended][0]}")
    selected = st.selectbox(
        "Response template", list(RESPONSE_TEMPLATES),
        index=list(RESPONSE_TEMPLATES).index(recommended),
        format_func=lambda key: RESPONSE_TEMPLATES[key][0],
        key=f"{key_base}_template",
    )
    engineer_restored = False
    if selected == "restored":
        engineer_restored = st.checkbox(
            "An engineer has explicitly confirmed restoration of the affected live service",
            key=f"{key_base}_engineer_restored",
        )
    previous_selection = st.session_state.get(f"{key_base}_selection")
    selection = (selected, engineer_restored)
    if previous_selection is not None and previous_selection != selection:
        _, st.session_state[f"{key_base}_body"] = _build_email_reply_draft(
            case, report, selected, engineer_restored=engineer_restored,
        )
    st.session_state[f"{key_base}_selection"] = selection
    if selected not in eligible and not (selected == "restored" and engineer_restored):
        st.caption("The evidence does not support that template's conclusion. The suggested draft uses a cautious update; review any retained edits before use.")

    subject = st.text_input(
        "Reply subject",
        key=f"{key_base}_subject",
    )

    body = st.text_area(
        "Reply body",
        height=260,
        key=f"{key_base}_body",
    )

    with st.expander("Copy response", expanded=False):
        st.caption("Use the copy button on this block to copy the current edited response.")
        st.code(body, language=None, wrap_lines=True)

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Save draft to case",
            key=f"{key_base}_save",
        ):
            case["reply_draft_subject"] = subject
            case["reply_draft_body"] = body
            store.save(case)
            st.success("Reply draft saved to this case.")

    with col2:
        st.download_button(
            "Download reply draft",
            data=f"Subject: {subject}\n\n{body}",
            file_name=f"{case['case_id']}_reply_draft.txt",
            mime="text/plain",
            key=f"{key_base}_download",
        )


def _reset_email_investigation() -> None:
    """Reset before rendering so even an identical email requires new confirmation."""
    for key in ("proactiveness_scan", "proactiveness_scan_sites",
                "proactiveness_scan_email", "proactiveness_open_case",
                "inventory_lookup_email", "inventory_candidate_selection",
                "inventory_confirmed_records", "inventory_matches"):
        st.session_state.pop(key, None)
    st.session_state["proactiveness_investigation_id"] = uuid.uuid4().hex


def _render_delete_case(st, store: CaseStore, case: dict) -> None:
    record = case.get("confirmed_inventory_record") or {}
    if (record.get("synthetic") is not True
            or record.get("diagnostic_environment") != "LAB"):
        return
    case_id = case["case_id"]
    pending_key = f"delete_case_pending_{case_id}"
    if st.button("Delete Case", key=f"delete_case_{case_id}"):
        st.session_state[pending_key] = True
    if st.session_state.get(pending_key):
        st.warning(f"Permanently delete LAB case {case_id} and its saved report/draft?")
        if st.button("Confirm Delete Case", key=f"confirm_delete_case_{case_id}"):
            store.delete_lab_case(case_id, confirmed=True)
            st.session_state.pop(pending_key, None)
            # Reset on the next render; the saved-case selector is already mounted.
            st.session_state["clear_deleted_case_selection"] = True
            st.rerun()
        if st.button("Cancel deletion", key=f"cancel_delete_case_{case_id}"):
            st.session_state.pop(pending_key, None)
            st.rerun()


def _process_confirmed_case(
    st,
    store: CaseStore,
    email_text: str,
    result: dict,
    site_identifiers: list[str],
    l2_scope_confirmed: bool,
    inventory_record: dict,
    username: str,
    password: str,
    retry: bool = False,
    existing_case_id: str | None = None,
    investigation_id: str | None = None,
) -> dict | None:
    """
    Create a case once and run approved LAB diagnostics.

    Normal processing uses the automation key to avoid duplicate cases.
    Explicit retries retrieve the original case by its exact case ID.
    """

    automation_key = _automation_key(email_text, inventory_record)
    if investigation_id:
        automation_key = f"{automation_key}:{investigation_id}"

    try:
        if retry:
            if not existing_case_id:
                raise ValueError(
                    "A case ID is required for an explicit diagnostic retry."
                )

            case = store.get(existing_case_id)

            if not case:
                raise ValueError(
                    f"Case {existing_case_id} could not be found."
                )

            if case.get("case_id") != existing_case_id:
                raise ValueError(
                    "The retrieved case ID does not match the requested case."
                )

            current_status = case.get(
                "diagnostic_status",
                "Not started",
            )

            if current_status not in ("Failed", "Running"):
                raise ValueError(
                    "Retry is only available for failed or interrupted diagnostics."
                )

            if case.get("lab_diagnostics"):
                raise ValueError(
                    "A saved diagnostic report already exists. "
                    "This case cannot be retried."
                )

            if case.get("l2_scope_status") != "Confirmed":
                raise ValueError(
                    "Retry blocked: the saved case does not have confirmed L2 scope."
                )

            saved_record = case.get("confirmed_inventory_record") or {}

            if not saved_record:
                raise ValueError(
                    "Retry blocked: no confirmed inventory record is saved."
                )

            if saved_record.get("synthetic") is not True:
                raise ValueError(
                    "Retry blocked: the saved inventory record is not synthetic."
                )

            # Use the original saved record, not a newly selected record.
            inventory_record = saved_record

            packet_count = case.get("diagnostic_packet_count")

            if packet_count not in (5, 10, 20, 100):
                # Compatibility with older cases that stored the count
                # only in their per-site investigation rows.
                for site_item in case.get("site_items", []):
                    if not isinstance(site_item, dict):
                        continue

                    candidate = site_item.get("ping_count")

                    if candidate in (5, 10, 20, 100):
                        packet_count = candidate
                        break

            if packet_count not in (5, 10, 20, 100):
                raise ValueError(
                    "Retry blocked: no approved packet count is saved "
                    "in this case."
                )

        else:
            existing_cases = store.list_cases()
            case = next(
                (
                    item
                    for item in existing_cases
                    if item.get("automation_key") == automation_key
                ),
                None,
            )

            if case is None:
                case = create_case(
                    email_text,
                    result,
                    site_identifiers=site_identifiers,
                    l2_scope_status=(
                        "Confirmed" if l2_scope_confirmed else "Unconfirmed"
                    ),
                )

                case["automation_key"] = automation_key
                case["confirmed_inventory_record"] = inventory_record
                case["diagnostic_packet_count"] = result.get("ping_count")

                if not l2_scope_confirmed:
                    case["diagnostic_status"] = "Blocked"
                    case["diagnostic_error"] = (
                        "L2 scope must be confirmed before diagnostics."
                    )

                elif result.get("ping_count") not in (5, 10, 20, 100):
                    case["diagnostic_status"] = "Blocked"
                    case["diagnostic_error"] = (
                        "No approved diagnostic packet count is available."
                    )

                store.save(case)

                st.success(
                    f"Case {case['case_id']} was created automatically "
                    "after site confirmation."
                )

            # Keep the confirmed record and packet count on the case.
            case["confirmed_inventory_record"] = inventory_record

            if not case.get("diagnostic_packet_count"):
                case["diagnostic_packet_count"] = result.get("ping_count")

            packet_count = case.get("diagnostic_packet_count")

        # Both normal processing and retries must pass these checks.
        if not l2_scope_confirmed and not retry:
            case["diagnostic_status"] = "Blocked"
            case["diagnostic_error"] = (
                "L2 scope must be confirmed before diagnostics."
            )
            store.save(case)
            return case

        if retry and case.get("l2_scope_status") != "Confirmed":
            raise ValueError(
                "Retry blocked: the saved case does not have confirmed L2 scope."
            )

        if inventory_record.get("synthetic") is not True:
            case["diagnostic_status"] = "Blocked"
            case["diagnostic_error"] = (
                "The selected inventory record is not explicitly marked synthetic."
            )
            store.save(case)
            return case

        if (
            inventory_record.get("diagnostic_environment") != "LAB"
            or inventory_record.get("lab_diagnostic_enabled") != "YES"
        ):
            case["diagnostic_status"] = "Blocked"
            case["diagnostic_error"] = (
                "This inventory record is not approved for LAB diagnostics."
            )
            store.save(case)
            return case

        if packet_count not in (5, 10, 20, 100):
            case["diagnostic_status"] = "Blocked"
            case["diagnostic_error"] = (
                "No approved diagnostic packet count is available."
            )
            store.save(case)
            return case

        current_status = case.get("diagnostic_status", "Not started")

        if current_status == "Completed" or case.get("lab_diagnostics"):
            return case

        if current_status in ("Failed", "Running") and not retry:
            return case

        if current_status not in ("Not started", "Failed", "Running"):
            return case

        if not username.strip() or not password:
            if retry:
                case["diagnostic_error"] = (
                    "Enter the LAB router credentials before retrying diagnostics."
                )
            else:
                case["diagnostic_status"] = "Not started"
                case["diagnostic_error"] = (
                    "Enter the LAB router credentials before running diagnostics."
                )

            store.save(case)
            return case

        if retry and current_status not in ("Failed", "Running"):
            raise ValueError(
                "Retry is only available for failed or interrupted diagnostics."
            )

        # Persist the packet count and Running state before connecting.
        case["diagnostic_packet_count"] = packet_count
        case["diagnostic_status"] = "Running"
        case["diagnostic_error"] = None
        store.save(case)

        try:
            with st.spinner(
                "Connecting to the approved LAB CHR and running diagnostics..."
            ):
                diagnostic_result = run_confirmed_lab_diagnostics(
                    inventory_record,
                    username,
                    password,
                    packet_count,
                )

            case["lab_diagnostics"] = diagnostic_result
            case["diagnostic_status"] = "Completed"
            case["diagnostic_error"] = None
            store.save(case)

            st.success(
                f"LAB diagnostics completed and saved for case "
                f"{case['case_id']}."
            )

        except Exception as exc:
            case["diagnostic_status"] = "Failed"
            message = str(exc)
            # Exceptions may echo connection parameters; never persist session credentials.
            for credential in sorted({username, username.strip(), password}, key=len, reverse=True):
                if credential:
                    message = message.replace(credential, "[redacted]")
            case["diagnostic_error"] = message
            store.save(case)
            st.error(f"LAB diagnostics failed: {message}")

        return case

    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        st.error(f"Could not process the confirmed case: {exc}")
        return None


def _render_saved_case(st, store: CaseStore, saved_case: dict) -> None:
    """Render a saved case and its available recovery actions."""
    _render_delete_case(st, store, saved_case)
    st.write(
        f"**Case:** {saved_case['case_id']} · "
        f"**Status:** {saved_case['status']}"
    )
    st.write(
        f"**Subject:** {saved_case.get('subject') or 'Not provided'} · "
        f"**Sender:** {saved_case.get('sender') or 'Not provided'}"
    )
    st.write(
        f"**Issue:** {saved_case['detected_issue']} · "
        f"**Confidence:** {saved_case['confidence']} · "
        f"**Review required:** "
        f"{'Yes' if saved_case['review_required'] else 'No'}"
    )
    st.write(f"**Reason:** {saved_case['classification_reason']}")
    st.write(f"**Test selection:** {saved_case['test_selection_reason']}")
    st.write(f"**Plan:** {saved_case['proposed_diagnostic_plan']}")
    st.write(f"**L2 scope:** {saved_case['l2_scope_status']}")
    st.write(
        f"**Affected sites:** "
        f"{', '.join(saved_case['affected_sites']) or 'Not identified'}"
    )

    with st.expander("Email body and evidence"):
        st.text(saved_case.get("body") or "No body captured")
        st.write(saved_case.get("evidence", []))

    new_status = st.selectbox(
        "Update lifecycle status",
        CASE_STATUSES,
        index=CASE_STATUSES.index(saved_case["status"]),
        key=f"case_status_{saved_case['case_id']}",
    )

    if st.button(
        "Save status",
        key=f"save_status_{saved_case['case_id']}",
    ):
        if new_status != saved_case["status"]:
            store.update_status(saved_case["case_id"], new_status)
            st.success("Case status saved.")
            st.rerun()

    st.divider()
    st.subheader("LAB diagnostic status")

    diagnostic_status = saved_case.get(
        "diagnostic_status",
        "Not started",
    )

    st.write(f"**Status:** {diagnostic_status}")

    if saved_case.get("diagnostic_error"):
        st.warning(saved_case["diagnostic_error"])

    saved_record = saved_case.get("confirmed_inventory_record")

    if saved_record:
        st.write(
            f"**Confirmed site:** "
            f"{saved_record.get('organisation', 'Unknown')} — "
            f"{saved_record.get('branch', 'Unknown')}"
        )
        st.write(
            f"**LAB router:** "
            f"{saved_record.get('lab_router_host', 'Not configured')}"
        )
        st.write(
            f"**Inventory type:** "
            f"{'Synthetic' if saved_record.get('synthetic') is True else 'Not verified'}"
        )

    if saved_case.get("lab_diagnostics"):
        _render_diagnostic_report(
            st,
            saved_case["lab_diagnostics"],
            case=saved_case,
        )

    # A failed or interrupted case can be retried only after explicit confirmation.
    retryable = (
        diagnostic_status in ("Failed", "Running")
        and not saved_case.get("lab_diagnostics")
    )

    if retryable:
        st.warning(
            "This case has no saved completed diagnostic report. "
            "It may have been interrupted or may have failed."
        )

        retry_confirmed = st.checkbox(
            "I have reviewed this case and want to retry the LAB checks.",
            key=f"retry_confirm_{saved_case['case_id']}",
        )

        if st.button(
            "Retry LAB diagnostics",
            type="primary",
            disabled=not retry_confirmed,
            key=f"retry_lab_{saved_case['case_id']}",
        ):
            record = saved_case.get("confirmed_inventory_record") or {}

            if saved_case.get("l2_scope_status") != "Confirmed":
                st.error(
                    "Retry blocked: the case does not have confirmed L2 scope."
                )
            elif not record:
                st.error(
                    "Retry blocked: no confirmed inventory record is saved."
                )
            elif record.get("synthetic") is not True:
                st.error(
                    "Retry blocked: the saved inventory record is not "
                    "explicitly synthetic."
                )
            else:
                retry_result = {
                    "ping_count": saved_case.get("diagnostic_packet_count"),
                }

                retried_case = _process_confirmed_case(
                    st=st,
                    store=store,
                    email_text=saved_case.get("raw_email", ""),
                    result=retry_result,
                    site_identifiers=saved_case.get("affected_sites", []),
                    l2_scope_confirmed=True,
                    inventory_record=record,
                    username=st.session_state.get(
                        "lab_router_username", ""
                    ),
                    password=st.session_state.get(
                        "lab_router_password", ""
                    ),
                    retry=True,
                    existing_case_id=saved_case["case_id"],
                )

                if retried_case:
                    st.rerun()

    _render_reply_draft(st, store, saved_case)


def render_email_proactiveness() -> None:
    """Render email triage, automatic case creation and approved LAB diagnostics."""

    st.title("Email Proactiveness")

    st.caption(
        "Review escalation emails, confirm inventory matches and run "
        "controlled diagnostics against the approved virtual LAB CHR."
    )

    st.info(
        "Email scanning does not connect to a router or send email. "
        "Diagnostics run only after a site is confirmed and "
        "LAB credentials are available."
    )

    store = CaseStore(
        os.getenv(
            "FIELDBASE_CASE_DB",
            str(Path(__file__).with_name("email_cases.sqlite3")),
        )
    )

    st.subheader("LAB router credentials")

    st.warning(
        "LAB ONLY. These credentials are used only for the approved "
        "virtual CHR. They are not saved in the case database."
    )

    credential_col1, credential_col2 = st.columns(2)

    with credential_col1:
        router_username = st.text_input(
            "Router username",
            key="lab_router_username",
        )

    with credential_col2:
        router_password = st.text_input(
            "Router password",
            type="password",
            key="lab_router_password",
        )

    if router_username.strip() and router_password:
        st.success("LAB credentials are available for this session.")
    else:
        st.caption(
            "Enter both credentials. Confirmed cases can be created "
            "without them, but diagnostics will wait."
        )

    st.divider()

    if st.session_state.pop("clear_deleted_case_selection", False):
        st.session_state.pop("proactiveness_open_case", None)
        st.session_state.pop("inventory_confirmed_records", None)

    # Saved case queue.
    try:
        saved_cases = store.list_cases()

        if saved_cases:
            st.subheader("Saved case queue")

            labels = {
                f"{case['case_id']} · {case['status']} · "
                f"{case.get('subject') or case['detected_issue']}": case
                for case in saved_cases
            }

            chosen = st.selectbox(
                "Open a saved case",
                ["— Select a case —", *labels],
                key="proactiveness_open_case",
            )

            if chosen != "— Select a case —":
                _render_saved_case(st, store, labels[chosen])

    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        st.error(
            "Local case storage is unavailable. Existing database "
            f"contents were left untouched. Details: {exc}"
        )
        return

    st.divider()

    configured_address = st.text_input(
        "L2 Support address (optional)",
        value=os.getenv("FIELDBASE_L2_SUPPORT_EMAIL", ""),
        help=(
            "Used only to check pasted To/Cc/Delivered-To headers. "
            "No address is assumed."
        ),
        key="proactiveness_l2_address",
    )

    email_text = st.text_area(
        "Escalation email",
        height=220,
        placeholder="Paste the email text here…",
        key="proactiveness_email_text",
    )

    uploaded_image = st.file_uploader(
        "Attach a site-list screenshot or photo (optional)",
        type=["png", "jpg", "jpeg", "webp"],
        key="proactiveness_site_image",
    )

    if uploaded_image:
        st.image(uploaded_image, caption="Review the attached site list")
        st.warning(
            "OCR is not installed in this environment. Review the image "
            "and enter site identifiers below; no text is extracted automatically."
        )

    reviewed_sites_text = st.text_area(
        "Site identifiers to review (one per line)",
        placeholder=(
            "Paste identifiers from the email or transcribe them "
            "from the image…"
        ),
        key="proactiveness_reviewed_sites",
    )

    reviewed_sites = parse_site_identifiers(
        email_text + "\n" + reviewed_sites_text
    )

    if reviewed_sites:
        reviewed_sites = st.multiselect(
            "Confirm affected sites",
            reviewed_sites,
            default=reviewed_sites,
            key="proactiveness_confirmed_sites",
        )

    if st.button(
        "Scan email",
        type="primary",
        key="scan_proactiveness_email",
        on_click=_reset_email_investigation,
    ):
        if email_text.strip():
            st.session_state["proactiveness_scan"] = classify_email(email_text)
            st.session_state["proactiveness_scan_sites"] = reviewed_sites
            st.session_state["proactiveness_scan_email"] = email_text
        else:
            st.session_state.pop("proactiveness_scan", None)
            st.warning("Paste an email before scanning.")

    result = st.session_state.get("proactiveness_scan")

    if not result:
        return

    scanned_email = st.session_state.get(
        "proactiveness_scan_email",
        email_text,
    )

    recipient_match = detect_l2_recipient(
        scanned_email,
        configured_address,
    )

    # L2 recipient evidence is informational, not a triage prerequisite.
    l2_scope_confirmed = True

    if recipient_match is True:
        st.success(
            "Pasted recipient headers match the configured L2 Support address."
        )
    elif recipient_match is False:
        st.caption(
            "Pasted recipient headers do not match the optional L2 Support address."
        )

    st.subheader("Triage result")
    st.write(f"**Detected issue:** {result['issue']}")
    st.write(f"**Reason:** {result['reason']}")
    if result.get("reported_subject"):
        st.caption(f"Reported subject: {result['reported_subject']}")
    if result.get("reported_site_references"):
        st.caption("Reported site references: " + "; ".join(result["reported_site_references"]))
    if result.get("reported_root_cause"):
        st.caption(f"Customer-reported root cause (unverified): {result['reported_root_cause']}")
    st.write(f"**Proposed investigation:** {result['proposed_investigation']}")
    st.write(f"**Why this ping count:** {result['why_ping_count']}")

    if result["evidence"]:
        st.write(
            "**Email evidence:** "
            + ", ".join(f"“{phrase}”" for phrase in result["evidence"])
        )

    if result["ping_count"] is not None and l2_scope_confirmed:
        st.write(
            f"**Selected investigation:** {result['ping_count']}-ping test"
        )
    elif result["ping_count"] is not None:
        st.caption(
            "Investigation suggestion is held for review until L2 scope is confirmed."
        )
    else:
        st.caption(
            "Review the email and select an investigation manually."
        )

    sites_for_plan = (
        st.session_state.get("proactiveness_scan_sites", [])
        or result.get("site_identifiers", [])
    )

    if sites_for_plan:
        st.subheader("Per-site investigation plan")

        plan = site_investigation_plan(
            sites_for_plan,
            result["ping_count"] if l2_scope_confirmed else None,
        )

        st.dataframe(
            plan,
            hide_index=True,
            use_container_width=True,
        )

        st.caption(
            "Each site's result applies only to that site. "
            "Confirm the correct inventory record before diagnostics."
        )

    confirmed_inventory_record = _render_site_inventory_lookup(
        st,
        sites_for_plan,
        scanned_email,
    )

    if confirmed_inventory_record:
        st.divider()
        st.subheader("Confirmed site workflow")

        st.write(
            f"**Site:** {confirmed_inventory_record.get('organisation')} — "
            f"{confirmed_inventory_record.get('branch')}"
        )
        st.write(
            f"**Company ID:** "
            f"{confirmed_inventory_record.get('company_id')}"
        )

        processed_case = _process_confirmed_case(
            st=st,
            store=store,
            email_text=scanned_email,
            result=result,
            site_identifiers=sites_for_plan,
            l2_scope_confirmed=l2_scope_confirmed,
            inventory_record=confirmed_inventory_record,
            username=router_username,
            password=router_password,
            investigation_id=st.session_state.get("proactiveness_investigation_id"),
        )

        if processed_case:
            open_case = st.session_state.get("proactiveness_open_case", "")
            if open_case.startswith(f"{processed_case['case_id']} · "):
                st.caption("This case and its draft are open in the saved case queue above.")
                return
            status = processed_case.get(
                "diagnostic_status",
                "Not started",
            )

            st.write(f"**Case ID:** {processed_case['case_id']}")
            st.write(f"**Diagnostic status:** {status}")

            if processed_case.get("diagnostic_error"):
                st.info(processed_case["diagnostic_error"])

            if processed_case.get("lab_diagnostics"):
                _render_diagnostic_report(
                    st,
                    processed_case["lab_diagnostics"],
                    case=processed_case,
                )

            _render_reply_draft(st, store, processed_case)

def _render_site_inventory_lookup(
    st,
    affected_sites: list[str],
    scanned_email: str,
) -> dict | None:
    from site_inventory import (
        inventory_summary,
        load_inventory,
        match_inventory,
        match_inventory_from_email,
    )

    st.divider()
    st.subheader("Automatic Site Inventory Match")
    st.caption(
        "The scanned email is matched against the simulated inventory. "
        "Review and confirm the correct site."
    )

    st.warning(
        "SIMULATED DATA ONLY. All inventory details are fictional. "
        "This lookup does not ping devices or connect to routers."
    )

    refresh_clicked = st.button(
        "Refresh inventory",
        key="inventory_refresh_button",
    )

    previous_email = st.session_state.get("inventory_lookup_email")
    email_changed = previous_email != scanned_email

    if (
        email_changed
        or refresh_clicked
        or "inventory_loaded" not in st.session_state
    ):
        st.session_state.pop("inventory_candidate_selection", None)
        st.session_state.pop("inventory_confirmed_records", None)

        inventory = load_inventory(force_refresh=refresh_clicked)

        st.session_state["inventory_loaded"] = inventory
        st.session_state["inventory_lookup_email"] = scanned_email

        matches = match_inventory_from_email(
            scanned_email,
            inventory.get("records", []),
        )

        matched_ids = {
            item.get("company_id") for item in matches
        }

        for site in affected_sites:
            for item in match_inventory(
                site,
                inventory.get("records", []),
            ):
                company_id = item.get("company_id")

                if company_id and company_id not in matched_ids:
                    matches.append(item)
                    matched_ids.add(company_id)

        st.session_state["inventory_matches"] = matches

    inventory = st.session_state.get("inventory_loaded")

    if not inventory:
        st.info("Inventory has not been loaded yet.")
        return None

    # Do not use an inventory source unless it explicitly identifies
    # the dataset as synthetic.
    if inventory.get("synthetic") is not True:
        st.error(
            "Inventory blocked: the loaded inventory source is not "
            "explicitly marked synthetic. No diagnostics can be authorised."
        )
        return None

    summary = inventory_summary(inventory)

    st.caption(
        f"Source: {summary['source']} | "
        f"Records: {summary['record_count']} | "
        f"Organisations: {summary['organisation_count']}"
    )

    if summary["refreshed_at"]:
        st.caption(f"Last refreshed: {summary['refreshed_at']}")

    if inventory.get("error"):
        st.warning(inventory["error"])

    for warning in inventory.get("warnings", []):
        st.warning(warning)

    matches = st.session_state.get("inventory_matches", [])

    if not matches:
        st.info(
            "No likely inventory match was found from the email. "
            "Check that the email includes the organisation and branch name."
        )
        return None

    st.write(f"**Possible inventory matches: {len(matches)}**")

    selected = None

    if len(matches) == 1:
        selected = matches[0]
        st.info(
            "One possible match was found. Review its details before confirming."
        )
    else:
        st.warning(
            "Multiple matches found. Select the correct organisation and branch."
        )

        index = st.selectbox(
            "Select matching site",
            options=list(range(len(matches))),
            index=None,
            placeholder="Choose a site...",
            format_func=lambda i: (
                f"{matches[i]['organisation']} — "
                f"{matches[i]['branch']} ({matches[i]['company_id']})"
            ),
            key="inventory_candidate_selection",
        )

        if index is not None:
            selected = matches[index]

    if selected is None:
        return None

    with st.container(border=True):
        st.markdown("**Matched inventory record**")
        st.subheader(selected["branch"])
        st.caption(
            f"{selected['organisation']} · Company ID: {selected['company_id']}"
        )
        if (
            selected.get("synthetic") is True
            and selected.get("diagnostic_environment") == "LAB"
            and selected.get("lab_diagnostic_enabled") == "YES"
            and all(
                str(selected.get(field, "")).strip() == APPROVED_LAB[target]
                for field, target in (
                    ("lab_router_host", "router"),
                    ("lab_gateway_target", "gateway"),
                    ("lab_bank_router_target", "bank_router"),
                )
            )
        ):
            st.success("LAB-approved configuration")

        st.write(f"**Match reason:** {', '.join(selected['matched_by'])}")
        details_left, details_right = st.columns(2)
        with details_left:
            st.write(f"**Contact person:** {selected['contact_person']}")
        with details_right:
            st.write(f"**Phone number:** {selected['phone_number']}")
        vlan_left, vlan_right = st.columns(2)
        with vlan_left:
            st.write(f"**Service VLAN:** {selected['service_vlan']}")
        with vlan_right:
            st.write(f"**DR VLAN:** {selected['dr_vlan']}")

        st.markdown("**LAB diagnostic targets**")
        st.table([
            {"Component": label, "IP address": selected.get(field) or "Not configured"}
            for label, field in (
                ("Management router", "lab_router_host"),
                ("Host-side endpoint", "lab_gateway_target"),
                ("Bank router", "lab_bank_router_target"),
            )
        ])

        with st.expander("Additional inventory details", expanded=False):
            st.write(f"**Service IP block:** {selected['ip_block']}")
            st.write(f"**DR IP block:** {selected['dr_ip_block']}")
            st.write("**Known aliases:** " + (", ".join(selected["aliases"]) or "None available"))
            st.write("**Device identifiers:** " + (", ".join(selected["device_identifiers"]) or "None available"))
            st.markdown("**LAB authorisation metadata**")
            st.write(
                f"**Synthetic record:** "
                f"{'Yes' if selected.get('synthetic') is True else 'Not verified'}"
            )
            st.write(
                f"**Environment:** {selected.get('diagnostic_environment') or 'Not configured'}"
            )
            st.write(
                f"**LAB diagnostics enabled:** "
                f"{selected.get('lab_diagnostic_enabled') or 'Not configured'}"
            )

    confirmed = st.session_state.setdefault(
        "inventory_confirmed_records",
        {},
    )

    company_id = selected["company_id"]

    if company_id in confirmed:
        st.success("This site has already been confirmed in this session.")

    elif st.button(
        "Confirm this site",
        type="primary",
        key=f"confirm_inventory_{company_id}",
    ):
        if selected.get("synthetic") is not True:
            st.error(
                "This record cannot be confirmed for LAB diagnostics "
                "because it is not explicitly marked synthetic."
            )
            return None

        confirmed[company_id] = selected
        st.session_state["inventory_confirmed_records"] = confirmed

        st.success(
            f"Confirmed: {selected['branch']} ({company_id})."
        )

    if confirmed:
        st.caption(
            f"Confirmed sites in this session: {len(confirmed)}"
        )

    if company_id in confirmed:
        return confirmed[company_id]

    return None