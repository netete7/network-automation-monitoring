
"""Local case model and SQLite store for email triage and diagnostics."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from email import policy
from email.parser import Parser
from email.utils import parseaddr
from pathlib import Path
import json
import uuid
import sqlite3

CASE_STATUSES = (
    "New",
    "Investigating",
    "Awaiting Contact",
    "Ready for Review",
    "Resolved",
)

DIAGNOSTIC_STATUSES = (
    "Not started",
    "Running",
    "Completed",
    "Failed",
    "Blocked",
)

SCHEMA_VERSION = 1

# These keys must never be written into the case payload.
CREDENTIAL_KEYS = {
    "password",
    "router_password",
    "lab_router_password",
    "username",
    "router_username",
    "lab_router_username",
    "credential",
    "credentials",
    "secret",
    "access_token",
}


def to_json_safe(value):
    """Convert nested diagnostic evidence into JSON-serialisable values."""

    if is_dataclass(value) and not isinstance(value, type):
        return to_json_safe(asdict(value))

    if isinstance(value, dict):
        return {
            str(key): to_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [to_json_safe(item) for item in value]

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    raise TypeError(
        f"Unsupported value in case payload: {type(value).__name__}"
    )


def _find_credential_key(value, path="case"):
    """Find credential fields anywhere inside a nested case payload."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalised = str(key).strip().casefold()

            if normalised in CREDENTIAL_KEYS:
                return f"{path}.{key}"

            found = _find_credential_key(item, f"{path}.{key}")
            if found:
                return found

    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_credential_key(item, f"{path}[{index}]")
            if found:
                return found

    return None


def create_case(
    email_text: str,
    classification: dict[str, object],
    *,
    site_identifiers: list[str] | None = None,
    l2_scope_status: str = "Unconfirmed",
) -> dict[str, object]:
    """Build a reviewable case without performing diagnostics."""

    if not isinstance(email_text, str) or not email_text.strip():
        raise ValueError("Email text must not be empty.")

    if not isinstance(classification, dict) or not classification.get("issue"):
        raise ValueError("A classification with an issue is required.")

    sites = list(dict.fromkeys(
        item.strip()
        for item in (site_identifiers or [])
        if isinstance(item, str) and item.strip()
    ))

    parsed = Parser(policy=policy.default).parsestr(email_text)

    body = (
        email_text.split("\n\n", 1)[1].strip()
        if "\n\n" in email_text
        else email_text.strip()
    )

    issue = str(classification["issue"])

    confidence = (
        "Low"
        if issue in {"Needs review", "Direction uncertain"}
        else "Moderate"
    )

    ping_count = classification.get("ping_count")

    return {
        "case_id": uuid.uuid4().hex[:12].upper(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subject": str(parsed.get("subject", "")).strip(),
        "sender": (
            parseaddr(str(parsed.get("from", "")))[1]
            or str(parsed.get("from", "")).strip()
        ),
        "body": body,
        "detected_issue": issue,
        "classification_reason": str(classification.get("reason", "")),
        "test_selection_reason": str(
            classification.get("why_ping_count", "")
        ),
        "evidence": list(classification.get("evidence", [])),
        "affected_sites": sites,
        "site_items": [
            {
                "site": site,
                "proposed_plan": classification.get(
                    "proposed_investigation", "Review manually"
                ),
                "ping_count": ping_count,
                "confirmation_required": True,
            }
            for site in sites
        ],
        "proposed_diagnostic_plan": str(
            classification.get("proposed_investigation", "Review manually")
        ),
        "confidence": confidence,
        "review_required": (
            confidence == "Low"
            or not sites
            or l2_scope_status != "Confirmed"
        ),
        "l2_scope_status": l2_scope_status,
        "status": "New",

        # Inventory and diagnostic lifecycle.
        "confirmed_inventory_record": None,
        "diagnostic_status": "Not started",
        "diagnostic_error": None,
        "lab_diagnostics": None,
    }


class CaseStore:
    """Versioned local SQLite storage."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)

        try:
            check = connection.execute("PRAGMA quick_check").fetchone()

            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError(
                    "Case database failed its integrity check."
                )

            version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            if version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    "Case database was created by a newer application version."
                )

            if version == 0:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS cases (
                        case_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                """)

                connection.execute(
                    f"PRAGMA user_version = {SCHEMA_VERSION}"
                )

            connection.commit()
            return connection

        except Exception:
            connection.close()
            raise

    def save(self, case: dict[str, object]) -> None:
        """Validate and save a JSON-safe copy of a case."""

        self._validate(case)

        # IMPORTANT:
        # Convert dataclasses such as PingSummary before JSON serialization.
        # This also recursively converts nested dataclasses inside the report.
        payload = to_json_safe(case)

        # Serialize the converted payload, NOT the original case dictionary.
        serialized_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        )

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cases (
                    case_id, created_at, updated_at, status, payload
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    payload=excluded.payload
                """,
                (
                    payload["case_id"],
                    payload["created_at"],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    payload["status"],
                    serialized_payload,
                ),
            )

    def list_cases(self) -> list[dict[str, object]]:
        """Return all saved cases, newest first."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload FROM cases
                ORDER BY created_at DESC, case_id DESC
                """
            ).fetchall()

        return [json.loads(row[0]) for row in rows]

    def get(self, case_id: str) -> dict[str, object] | None:
        """Retrieve one saved case by its ID."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT payload FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()

        return json.loads(row[0]) if row else None

    def delete_lab_case(self, case_id: str, *, confirmed: bool = False) -> bool:
        """Delete only the explicitly confirmed synthetic LAB case by exact ID."""
        if confirmed is not True:
            raise ValueError("Explicit deletion confirmation is required.")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            if row is None:
                return False
            record = json.loads(row[0]).get("confirmed_inventory_record") or {}
            if (record.get("synthetic") is not True
                    or record.get("diagnostic_environment") != "LAB"):
                raise ValueError("Only saved synthetic LAB cases can be deleted.")
            connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
        return True

    def update_status(
        self,
        case_id: str,
        status: str,
    ) -> dict[str, object]:
        """Update the workflow status of an existing case."""

        if status not in CASE_STATUSES:
            raise ValueError("Unknown case status.")

        case = self.get(case_id)

        if case is None:
            raise KeyError(case_id)

        case["status"] = status
        self.save(case)

        return case

    @staticmethod
    def _validate(case: dict[str, object]) -> None:
        """Validate case structure and prevent credential storage."""

        if not isinstance(case, dict):
            raise ValueError("Case must be a dictionary.")

        if not case.get("case_id") or not case.get("created_at"):
            raise ValueError("Case ID and creation time are required.")

        if case.get("status") not in CASE_STATUSES:
            raise ValueError("Unknown case status.")

        diagnostic_status = case.get("diagnostic_status", "Not started")

        if diagnostic_status not in DIAGNOSTIC_STATUSES:
            raise ValueError("Unknown diagnostic status.")

        credential_path = _find_credential_key(case)

        if credential_path:
            raise ValueError(
                f"Credential field cannot be saved in a case: {credential_path}"
            )
