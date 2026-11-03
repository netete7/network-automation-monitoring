"""Small, read-only normalizer for pasted NOC email text and incident fields."""
from __future__ import annotations

from html.parser import HTMLParser
import re

FIELD_LABELS = {
    'operation_id': ('Operation ID',),
    'operation_type': ('Operation Type',),
    'service_provider': ('Service Provider',),
    'service_outlet_code': ('Service Outlet Code',),
    'sol_id': ('Sol ID', 'SOL Code'),
    'service_outlet_name': ('Service Outlet Name',),
    'service_outlet': ('Service Outlet',),
    'fault_description': ('Faulty Description',),
    'incident_status': ('Faulty Status', 'Status'),
    'reported_root_cause': ('Root Cause',),
    'fault_message': ('Fault Message',),
    'associated_service_id': ('Associated Service ID',),
    'link_technology': ('Link Technology',),
}
_LABELS = {label.casefold(): key for key, labels in FIELD_LABELS.items() for label in labels}
_LABEL_PATTERN = '|'.join(re.escape(x) for x in sorted(_LABELS, key=len, reverse=True))
_FIELD_LINE = re.compile(rf'^\s*({_LABEL_PATTERN})\s*(?::|\t|\|)\s*(.*?)\s*$', re.I)
_HEADER = re.compile(r'^(?:from|to|cc|bcc|sent|date|delivered-to|x-original-to):', re.I)
_SYMPTOM = re.compile(r'\b(?:link|outage|intermittency|incident|latency|connectivity|packet loss|device status|router reachable)\b', re.I)
_SITE_KEYS = ('service_outlet_name', 'service_outlet', 'service_outlet_code', 'sol_id', 'associated_service_id')


class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.ignored += 1
        elif not self.ignored:
            if tag == 'blockquote':
                self.parts.append('\n-----Original Message-----\n')
            elif tag in {'br', 'p', 'div', 'tr', 'li', 'table'}:
                self.parts.append('\n')
            elif tag in {'td', 'th'}:
                self.parts.append('\t')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in {'p', 'div', 'tr', 'li', 'table'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _latest(text):
    retained = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if (re.match(r'-{2,}\s*(?:original message|forwarded message)', stripped, re.I)
                or re.match(r'On .+wrote:', stripped, re.I)
                or stripped.startswith('>')):
            break
        # An appended Outlook header block is history, unlike a top-of-email header.
        if (index and re.match(r'From:', stripped, re.I)
                and any(re.match(r'(?:Sent|Date|To):', x.strip(), re.I) for x in text.splitlines()[index+1:index+5])
                and any(x.strip() and not _HEADER.match(x.strip()) and not x.lower().startswith('subject:') and not _headline(x.strip()) for x in retained)):
            break
        retained.append(line)
    return '\n'.join(retained)


def _headline(line):
    if _FIELD_LINE.match(line.strip().strip('|').strip()):
        return False
    value = re.sub(r'^(?:(?:RE|FW|FWD)\s*:\s*)+', '', line, flags=re.I).strip()
    return (len(line) <= 300 and bool(_SYMPTOM.search(value))
            and bool(re.search(r'\b(?:at|sol[_ -]?\d+)\b|\s[—–|]\s|\s-\s', value, re.I)))


def parse_noc_email(text: str) -> dict:
    """Keep original text, recognize reliable subjects, and isolate current site clues.

    Fields are customer reports, never verified diagnostic observations. Headers,
    greetings, signatures, quoted history and root-cause claims do not become sites.
    """
    plain = text
    if re.search(r'<(?:html|body|table|tr|td|p|div|br|blockquote)\b', text, re.I):
        parser = _TextHTML(); parser.feed(text); plain = ''.join(parser.parts)
    latest = _latest(plain.replace('\xa0', ' '))
    lines = latest.splitlines()
    subject = None
    subject_indices = set()
    for i, line in enumerate(lines):
        match = re.match(r'^\s*Subject\s*:\s*(.+?)\s*$', line, re.I)
        if match and subject is None:
            subject = match.group(1); subject_indices.add(i)
        elif subject is None and re.fullmatch(r'\s*Subject\s*:\s*', line, re.I):
            following = next((j for j in range(i+1, len(lines)) if lines[j].strip()), None)
            if following is not None and not re.fullmatch(r'Body\s*:', lines[following].strip(), re.I):
                subject = lines[following].strip(); subject_indices.update((i, following))
    nonempty = [i for i, line in enumerate(lines) if line.strip()]
    if subject is None:
        for i in dict.fromkeys(nonempty[:1] + nonempty[-1:]):
            if _headline(lines[i].strip()):
                subject = lines[i].strip(); subject_indices.add(i); break
    fields = {}
    narrative = []
    pending = None
    signed_off = False
    for i, line in enumerate(lines):
        if i in subject_indices or _HEADER.match(line.strip()) or re.fullmatch(r'Body\s*:', line.strip(), re.I):
            continue
        stripped = line.strip().strip('|').strip()
        if not stripped or re.fullmatch(r'[\s|:—–-]+', stripped):
            continue
        cleaned = re.sub(r'\*\*|`', '', stripped)
        match = _FIELD_LINE.match(cleaned)
        if match:
            key = _LABELS[match.group(1).casefold()]
            value = match.group(2).strip().strip('|').strip()
            # Preserve contradictory duplicate fields for review, rather than choosing silently.
            if value:
                fields[key] = value if key not in fields or fields[key] == value else fields[key] + ' / ' + value
                pending = None
            else:
                pending = key
            continue
        if cleaned.casefold().rstrip(':').strip() in _LABELS:
            pending = _LABELS[cleaned.casefold().rstrip(':').strip()]
            continue
        closing = bool(re.match(r'^(?:(?:warm|kind|best)\s+)?regards\b|^best wishes\b|^sincerely\b|^--\s*$', cleaned, re.I))
        if pending:
            if not closing:
                fields[pending] = cleaned
                pending = None
                continue
            pending = None
        if re.match(r'^(?:(?:warm|kind|best)\s+)?regards\b|^best wishes\b|^sincerely\b|^--\s*$', cleaned, re.I):
            signed_off = True
        if signed_off:
            continue
        if re.match(r'^(?:dear|hi|hello|good morning|good afternoon|good evening)\b', cleaned, re.I):
            # A greeting can share a line with the actual complaint. Drop only
            # the salutation when a comma clearly separates it from the body.
            remainder = cleaned.split(',', 1)[1].strip() if ',' in cleaned else ''
            if remainder:
                narrative.append(remainder)
            continue
        narrative.append(line.strip())
    body = '\n'.join(narrative)
    symptoms = '\n'.join(f'{FIELD_LABELS[k][0]}: {fields[k]}' for k in ('fault_description', 'fault_message', 'incident_status') if fields.get(k))
    analysis = '\n'.join(x for x in (f'Subject: {subject}' if subject else '', body, symptoms) if x)
    site_fields = [fields[k] for k in _SITE_KEYS if fields.get(k)]
    matching_text = '\n'.join(x for x in ([subject] + [body] + site_fields) if x)
    references = list(site_fields)
    if subject:
        heading = re.sub(r'^(?:(?:RE|FW|FWD)\s*:\s*)+', '', subject, flags=re.I)
        at = re.search(r'\bat\s+(.+)$', heading, re.I)
        tail = at.group(1).strip() if at else re.split(r'\s[—–|]\s|\s-\s', heading)[-1].strip()
        if tail != heading and not re.fullmatch(r'(?:link\s+)?(?:down|intermittency|optimization|optimisation|outage|slow link)', tail, re.I):
            references.append(tail)
    references.extend(match.group(0) for match in re.finditer(r'\bSOL[_ -]*\d+\b', matching_text, re.I))
    return dict(original=text, latest_text=latest, subject=subject, body=body, fields=fields,
                analysis_text=analysis, matching_text=matching_text,
                site_references=list(dict.fromkeys(references)))
