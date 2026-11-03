
"""Parse read-only RouterOS ping summaries for the diagnostic runner."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class PingSummary:
    sent: int
    received: int
    packet_loss: float
    status: str
    min_rtt: str | None = None
    avg_rtt: str | None = None
    max_rtt: str | None = None
    execution_status: str = "completed"
    stop_reasons: tuple[str, ...] = ()
    error: str | None = None
    attempts_started: int = 0


def parse_packet_count(value: str) -> int | None:
    """Return an accepted packet count; a blank response means the default."""
    value = value.strip()
    if not value:
        return 10
    if value in {"5", "10", "20", "100"}:
        return int(value)
    return None


def _parse_rtt(value: str | None) -> str | None:
    if not value:
        return None
    parts = list(re.finditer(r"\d+(?:\.\d+)?(?:ms|us|ns|d|h|m|s)", value))
    if not parts or "".join(part.group(0) for part in parts) != value:
        return None
    return value


def rtt_milliseconds(value: str | None) -> float | None:
    """Convert a RouterOS composite duration such as 2ms14us to milliseconds."""
    value = _parse_rtt(value)
    if value is None:
        return None

    units = {
        "d": 86_400_000,
        "h": 3_600_000,
        "m": 60_000,
        "s": 1_000,
        "ms": 1,
        "us": 0.001,
        "ns": 0.000001,
    }
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|us|ns|d|h|m|s)", value))
    total = sum(float(match.group(1)) * units[match.group(2)] for match in matches)
    return total if math.isfinite(total) else None


def _format_milliseconds(value: float) -> str:
    """Format aggregate latency in RouterOS-style milliseconds/microseconds."""
    total_microseconds = max(0, round(value * 1000))
    if total_microseconds == 0:
        return "0ms"

    milliseconds, microseconds = divmod(total_microseconds, 1000)

    if milliseconds and microseconds:
        return f"{milliseconds}ms{microseconds}us"
    if milliseconds:
        return f"{milliseconds}ms"
    return f"{microseconds}us"


def aggregate_ping_results(
    observations: list[PingSummary],
    requested: int,
    *,
    execution_status: str = "completed",
    stop_reasons: tuple[str, ...] = (),
    error: str | None = None,
    attempts_started: int | None = None,
) -> PingSummary:
    """Summarize only completed, individually verified one-packet results."""
    sent = sum(item.sent for item in observations)
    received = sum(item.received for item in observations)
    lost = sent - received
    loss_pct = (lost * 100 / sent) if sent else 0.0

    rtts = [
        (
            rtt_milliseconds(item.avg_rtt or item.max_rtt or item.min_rtt),
            item.avg_rtt or item.max_rtt or item.min_rtt,
        )
        for item in observations
        if item.received
    ]
    rtts = [
        (milliseconds, raw)
        for milliseconds, raw in rtts
        if milliseconds is not None
    ]

    min_rtt = min(rtts, key=lambda item: item[0])[1] if rtts else None
    max_rtt = max(rtts, key=lambda item: item[0])[1] if rtts else None

    avg_rtt = (
        rtts[0][1]
        if len(rtts) == 1
        else _format_milliseconds(sum(item[0] for item in rtts) / len(rtts))
        if rtts
        else None
    )

    if execution_status != "completed":
        status = execution_status.upper()
    elif received == 0 and sent:
        status = "FAIL"
    elif lost:
        status = "WARNING"
    else:
        status = "PASS"

    return PingSummary(
        sent=sent,
        received=received,
        packet_loss=loss_pct,
        status=status,
        min_rtt=min_rtt,
        avg_rtt=avg_rtt,
        max_rtt=max_rtt,
        execution_status=execution_status,
        stop_reasons=stop_reasons,
        error=error,
        attempts_started=sent if attempts_started is None else attempts_started,
    )


def format_ping_summary_line(summary: PingSummary) -> str:
    """Render one compact, final RouterOS-style aggregate summary line."""
    if not summary.sent:
        return ""

    packet_loss = f"{round(summary.packet_loss, 2):g}%"
    fields = [
        f"sent={summary.sent}",
        f"received={summary.received}",
        f"packet-loss={packet_loss}",
    ]

    fields.extend(
        f"{key}={value}"
        for key, value in (
            ("min-rtt", summary.min_rtt),
            ("avg-rtt", summary.avg_rtt),
            ("max-rtt", summary.max_rtt),
        )
        if value is not None
    )

    return " ".join(fields)


def extract_ping_result_row(output: str) -> str | None:
    """Return an actual RouterOS per-packet table row, excluding headers/summaries."""
    if not isinstance(output, str):
        return None

    for line in output.splitlines():
        row = line.strip()

        if not row or "sent=" in row.lower():
            continue

        if re.match(r"^\d+\s+\S+(?:\s+\S+){1,}$", row):
            return row

    return None


def ping_stop_reasons(
    summary: PingSummary,
    *,
    packet_loss_limit: int = 2,
    max_latency_ms: float = 99,
) -> tuple[str, ...]:
    """Return every FieldBase stop condition present in observed results."""
    reasons = []

    if summary.sent - summary.received >= packet_loss_limit:
        reasons.append("packet_loss")

    observed_max = rtt_milliseconds(summary.max_rtt)

    if (
        summary.received
        and observed_max is not None
        and observed_max > max_latency_ms
    ):
        reasons.append("max_latency")

    return tuple(reasons)


def parse_ping_summary(output: str, expected_count: int) -> PingSummary | None:
    """Parse one complete RouterOS key=value ping summary, or return None."""
    if (
        not isinstance(expected_count, int)
        or expected_count < 1
        or not isinstance(output, str)
    ):
        return None

    summary_lines = [
        line.strip()
        for line in output.splitlines()
        if all(
            key in line.lower()
            for key in ("sent=", "received=", "packet-loss=")
        )
    ]

    complete_lines = [
        line
        for line in summary_lines
        if re.search(rf"\bsent={expected_count}(?:\s|$)", line, re.I)
    ]

    if len(complete_lines) != 1:
        return None

    field_matches = re.findall(
        r"([a-z][a-z-]*)=([^\s]+)",
        complete_lines[0],
        re.I,
    )

    fields = {key.lower(): value for key, value in field_matches}

    if len(fields) != len(field_matches):
        return None

    try:
        sent_text = fields["sent"]
        received_text = fields["received"]
        loss_text = fields["packet-loss"]

        if not sent_text.isdecimal() or not received_text.isdecimal():
            return None

        sent = int(sent_text)
        received = int(received_text)

        if not loss_text.endswith("%"):
            return None

        loss_number = loss_text[:-1]
        packet_loss = float(loss_number)

    except (KeyError, ValueError):
        return None

    if (
        sent != expected_count
        or sent <= 0
        or received < 0
        or received > sent
        or not math.isfinite(packet_loss)
        or not 0 <= packet_loss <= 100
    ):
        return None

    expected_loss = (sent - received) * 100 / sent
    decimal_places = len(loss_number.partition(".")[2])
    rounding_tolerance = 0.5 * (10 ** -decimal_places) + 1e-9

    if abs(packet_loss - expected_loss) > rounding_tolerance:
        return None

    if received == 0:
        status = "FAIL"
    elif packet_loss > 0:
        status = "WARNING"
    else:
        status = "PASS"

    return PingSummary(
        sent=sent,
        received=received,
        packet_loss=packet_loss,
        status=status,
        min_rtt=_parse_rtt(fields.get("min-rtt")),
        avg_rtt=_parse_rtt(fields.get("avg-rtt")),
        max_rtt=_parse_rtt(fields.get("max-rtt")),
    )


def restore_ping_summary(value):
    """Restore a PingSummary saved as a JSON dictionary."""
    if isinstance(value, PingSummary):
        return value

    if not isinstance(value, dict):
        return None

    return PingSummary(
        sent=int(value.get("sent", 0)),
        received=int(value.get("received", 0)),
        packet_loss=float(value.get("packet_loss", 0)),
        status=str(value.get("status", "Unknown")),
        min_rtt=value.get("min_rtt"),
        avg_rtt=value.get("avg_rtt"),
        max_rtt=value.get("max_rtt"),
        execution_status=str(value.get("execution_status", "completed")),
        stop_reasons=tuple(value.get("stop_reasons") or ()),
        error=value.get("error"),
        attempts_started=int(value.get("attempts_started", 0)),
    )


def restore_diagnostic_report(report):
    """Restore PingSummary objects inside a saved diagnostic report."""
    if report is None:
        return None

    if not isinstance(report, dict):
        raise TypeError("Diagnostic report must be a dictionary.")

    restored = dict(report)
    restored_pings = []

    for ping in report.get("pings", []):
        if not isinstance(ping, dict):
            continue

        restored_ping = dict(ping)
        restored_ping["summary"] = restore_ping_summary(
            ping.get("summary")
        )
        restored_pings.append(restored_ping)

    restored["pings"] = restored_pings
    return restored
