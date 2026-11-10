"""Bounded, strict parsing of rsyslog envelopes and Cisco ISE accounting messages.

Supported wire formats and assumptions are documented in docs/rsyslog-parser.md.
No received-at timestamp or missing transfer counter is invented as usage.
"""

import copy
import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from bandwidth.schemas import MAX_COUNTER, Measurement

MAX_MESSAGE = 1_048_576
MAX_STATE = 2_097_152
MAX_GROUPS = 128
MAX_PARTS = 64
FIELDS = {
    re.sub(r"[^a-z0-9]", "", name.lower()): name
    for name in (
        "User-Name",
        "Acct-Status-Type",
        "Acct-Session-Id",
        "Acct-Unique-Session-Id",
        "Acct-Session-Time",
        "NAS-IP-Address",
        "NAS-Identifier",
        "Event-Timestamp",
        "Acct-Input-Octets",
        "Acct-Output-Octets",
        "Acct-Input-Gigawords",
        "Acct-Output-Gigawords",
    )
}
CISE = re.compile(r"^CISE_(\w+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(.*)$", re.S)
CISE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)? [+-]\d{2}:\d{2})\s+")
RFC5424 = re.compile(r"^(?:<\d{1,3}>)?1 (\S+) (\S+) (\S+) (\S+) (\S+) ")
RFC3164 = re.compile(r"^(?:<\d{1,3}>)?[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} (\S+) (.*)$", re.S)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result and result[key] != value:
            raise ValueError("Conflicting duplicate JSON field")
        result[key] = value
    return result


def timestamp(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        text = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return datetime.fromtimestamp(float(Decimal(text)), UTC)
        text = re.sub(r" (\d{2}:\d{2}:\d{2})", r"T\1", text, count=1)
        text = re.sub(r" ([+-]\d{2}:\d{2})$", r"\1", text)
        result = datetime.fromisoformat(text)
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
        raise ValueError("Accounting event time must be an epoch or offset-aware timestamp") from None


def unwrap(raw):
    """Return MSG, optional event-header time, and originating syslog hostname."""
    match = RFC5424.match(raw)
    if match:
        stamp, host, app, _, _ = match.groups()
        rest = raw[match.end() :]
        if rest.startswith("-"):
            rest = rest[1:]
        elif rest.startswith("["):
            quoted = escaped = False
            while rest.startswith("["):
                for index, char in enumerate(rest[1:], 1):
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = not quoted
                    elif char == "]" and not quoted:
                        rest = rest[index + 1 :]
                        break
                else:
                    raise ValueError("Malformed RFC5424 structured data")
        else:
            raise ValueError("Missing RFC5424 structured data marker")
        if rest and not rest.startswith(" "):
            raise ValueError("Malformed RFC5424 message boundary")
        message = rest.lstrip()
        if app.startswith("CISE_") and not message.startswith("CISE_"):
            message = app + " " + message
        return message, None if stamp == "-" else stamp, host
    match = RFC3164.match(raw)
    if match:
        host, message = match.groups()
        if not message.startswith("CISE_"):
            message = re.sub(r"^[\w./-]+(?:\[\d+\])?:\s*", "", message, count=1)
        return message, None, host
    return raw.strip(), None, None


def unquote(value):
    value = value.strip()
    if value.startswith(('"', "'")):
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("Unterminated accounting attribute quote")
        value = value[1:-1].replace(value[0] * 2, value[0])
    return re.sub(r"\\([,=\"'\\])", r"\1", value)


def attributes(message):
    if message.startswith("{"):
        try:
            raw = json.loads(message, object_pairs_hook=unique_object)
        except (ValueError, RecursionError):
            raise ValueError("Malformed accounting JSON payload") from None
        if not isinstance(raw, dict):
            raise ValueError("Accounting JSON payload must be an object")
        pairs = raw.items()
    else:
        tokens, start, quote, escaped = [], 0, None, False
        previous = None
        index = 0
        while index < len(message):
            char = message[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quote:
                if char == quote:
                    if index + 1 < len(message) and message[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in ('"', "'") and previous in (None, "=", ","):
                quote = char
            elif char == ",":
                tokens.append(message[start:index])
                start = index + 1
            if not char.isspace():
                previous = char
            index += 1
        if quote or escaped:
            raise ValueError("Unterminated accounting attribute escape/quote")
        tokens.append(message[start:])
        pairs = []
        for token in tokens:
            token = unquote(token)
            if "=" in token:
                key, value = token.split("=", 1)
                # The first CSV field may follow an ISE timestamp/message preamble.
                key = key.strip()
                if re.sub(r"[^a-z0-9]", "", key.lower()) not in FIELDS:
                    key = key.rsplit(" ", 1)[-1]
                pairs.append((key, unquote(value)))
    result = {}
    for key, value in pairs:
        name = FIELDS.get(re.sub(r"[^a-z0-9]", "", key.lower()))
        if name:
            if name in result and result[name] != value:
                raise ValueError("Conflicting duplicate accounting attribute")
            result[name] = value
    return result


def number(value, name):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise ValueError(f"Invalid nonnegative integer in {name}")
    result = int(value)
    if result > MAX_COUNTER:
        raise ValueError(f"Accounting value exceeds the supported range in {name}")
    return result


def text_field(fields, name, maximum=250):
    value = fields.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or any(c.isspace() for c in value):
        raise ValueError(f"Missing or invalid {name}")
    return value


def measurement(fields, fallback_time):
    status = str(fields.get("Acct-Status-Type", "")).strip().lower().replace("_", "-")
    status = {
        "1": "start",
        "2": "stop",
        "3": "interim-update",
        "7": "accounting-on",
        "8": "accounting-off",
    }.get(status, status)
    if status in ("accounting-on", "accounting-off"):
        return None
    if status not in ("start", "stop", "interim-update"):
        raise ValueError("Missing or unsupported Acct-Status-Type")
    username = text_field(fields, "User-Name", 200)
    nas = fields.get("NAS-IP-Address") or fields.get("NAS-Identifier")
    if nas is not None and (not isinstance(nas, str) or not nas or len(nas) > 250):
        raise ValueError("Invalid NAS identity")
    if "Acct-Unique-Session-Id" in fields:
        session = text_field(fields, "Acct-Unique-Session-Id")
    else:
        session = text_field(fields, "Acct-Session-Id")
        if not isinstance(nas, str) or not nas or len(nas) > 250:
            raise ValueError("NAS identity is required without Acct-Unique-Session-Id")
        session = "nas:" + digest(nas + "\0" + session)
    at = fields.get("Event-Timestamp", fallback_time)
    if at is None:
        raise ValueError("Missing accounting event timestamp; receipt time is not an event timestamp")
    at = timestamp(at)
    counters = []
    for direction in ("Input", "Output"):
        octets_name, words_name = f"Acct-{direction}-Octets", f"Acct-{direction}-Gigawords"
        if octets_name not in fields and status != "start":
            raise ValueError(f"Accounting measurement is missing {octets_name}")
        octets = number(fields.get(octets_name, 0), octets_name)
        words = number(fields.get(words_name, 0), words_name)
        if words_name in fields and (octets >= 2**32 or words >= 2**32):
            raise ValueError("Gigawords require unsigned 32-bit octet and gigaword fields")
        counter = octets + (words << 32)
        if counter > MAX_COUNTER:
            raise ValueError("Combined accounting counter exceeds the supported range")
        counters.append(counter)
    elapsed = number(fields["Acct-Session-Time"], "Acct-Session-Time") if "Acct-Session-Time" in fields else 0
    canonical = json.dumps(
        [username, session, str(nas or ""), at.isoformat(), status, *counters, elapsed], separators=(",", ":")
    )
    return Measurement(
        record_id=f"rsyslog:{elapsed:020d}:" + digest(canonical),
        username=username,
        session_id=session,
        observed_at=at,
        kind="cumulative",
        upload=counters[0],
        download=counters[1],
    )


class SyslogParser:
    def __init__(self, cursor, allowed_senders, now, fragment_timeout=900):
        try:
            self.allowed = [
                ipaddress.ip_network(item.strip(), strict=False) for item in allowed_senders.split(",")
            ]
            if not self.allowed:
                raise ValueError
        except ValueError:
            raise ValueError("RSYSLOG_ALLOWED_SENDERS must contain IP addresses or CIDR networks") from None
        self.now = now
        self.timeout = fragment_timeout
        self.pending = copy.deepcopy(cursor.get("pending", {}))
        self.completed = copy.deepcopy(cursor.get("completed", {}))
        self.stats = {"lines": 0, "parsed": 0, "ignored": 0, **cursor.get("stats", {})}

    def consume(self, envelope):
        if not isinstance(envelope, dict) or not isinstance(envelope.get("raw_message"), str):
            raise ValueError("Rsyslog input requires a JSON envelope with raw_message and source_ip")
        try:
            sender = ipaddress.ip_address(envelope.get("source_ip", ""))
        except ValueError:
            raise ValueError("Rsyslog envelope has an invalid source_ip") from None
        if not any(sender in network for network in self.allowed):
            raise ValueError("Rsyslog sender is not in RSYSLOG_ALLOWED_SENDERS")
        self.stats["lines"] += 1
        raw = envelope["raw_message"]
        if len(raw.encode()) > MAX_MESSAGE:
            raise ValueError("Rsyslog message exceeds 1 MiB")
        message, fallback, hostname = unwrap(raw)
        if message.startswith("CISE_"):
            match = CISE.match(message)
            if not match:
                if message.startswith("CISE_RADIUS_Accounting"):
                    raise ValueError("Malformed CISE accounting fragment header")
                return self.ignored()
            category, identifier, count, index, message = match.groups()
            if category != "RADIUS_Accounting":
                return self.ignored()
            count, index = int(count), int(index)
            if not 1 <= count <= MAX_PARTS or not 0 <= index < count or len(identifier) > 128:
                raise ValueError("Invalid CISE accounting fragment count/index")
            if count > 1:
                key = digest(
                    json.dumps([str(sender), hostname or envelope.get("hostname"), category, identifier])
                )
                assembled = self.fragment(key, count, index, message, str(sender), fallback)
                if assembled is None:
                    return None
                message, fallback = assembled
            cise_time = CISE_TIME.match(message)
            fallback = cise_time.group(1) if cise_time else fallback
        else:
            # Ignore connectivity tests/authentication messages; suspicious
            # accounting-shaped messages must be validated rather than dropped.
            if not re.search(r"Acct[-_]?Status[-_]?Type|Acct[-_]?(?:Input|Output)[-_]?Octets", message, re.I):
                return self.ignored()
        fields = attributes(message)
        record = measurement(fields, fallback)
        if record is None:
            return self.ignored()
        self.stats["parsed"] += 1
        return record

    def ignored(self):
        self.stats["ignored"] += 1
        return None

    def fragment(self, key, count, index, piece, sender, header_time):
        known = self.completed.get(key)
        if known:
            if known["count"] != count or known["hashes"].get(str(index)) != digest(piece):
                raise ValueError("CISE fragment identifier reused with conflicting contents")
            return None
        if key not in self.pending:
            if len(self.pending) >= MAX_GROUPS:
                raise ValueError("Too many incomplete CISE accounting messages")
            self.pending[key] = {
                "count": count,
                "pieces": {},
                "sender": sender,
                "started": self.now.isoformat(),
            }
        group = self.pending[key]
        if group["count"] != count:
            raise ValueError("Conflicting CISE fragment count")
        previous = group["pieces"].get(str(index))
        if previous is not None and previous != piece:
            raise ValueError("Conflicting duplicate CISE fragment")
        group["pieces"][str(index)] = piece
        if index == 0:
            group["header_time"] = header_time
        if sum(len(p.encode()) for p in group["pieces"].values()) > MAX_MESSAGE:
            raise ValueError("Assembled CISE accounting message exceeds 1 MiB")
        if len(json.dumps(self.pending).encode()) > MAX_STATE:
            raise ValueError("Pending CISE accounting fragments exceed 2 MiB")
        if len(group["pieces"]) != count:
            return None
        message = "".join(group["pieces"][str(i)] for i in range(count))
        self.completed[key] = {
            "count": count,
            "hashes": {i: digest(p) for i, p in group["pieces"].items()},
            "at": self.now.isoformat(),
        }
        while len(self.completed) > MAX_GROUPS:
            del self.completed[min(self.completed, key=lambda k: self.completed[k]["at"])]
        del self.pending[key]
        return message, group.get("header_time")

    def finish(self, eof):
        for group in self.pending.values():
            if not any(ipaddress.ip_address(group["sender"]) in network for network in self.allowed):
                raise ValueError("Pending fragment sender is no longer allowed")
            if eof and (self.now - timestamp(group["started"])).total_seconds() >= self.timeout:
                raise ValueError(
                    "CISE accounting fragments are incomplete; inspect the source for missing segments"
                )
        return {"pending": self.pending, "completed": self.completed, "stats": self.stats}
