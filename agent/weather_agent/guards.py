"""Guardrails around what the agent can do and see.

These are defense in depth. The real control is identity: in production the
agent runs as a service principal that only has SELECT on the tables it
needs and an API token scoped to analytics-read. Code-level guards catch
mistakes; permissions stop them from mattering.
"""
from __future__ import annotations

import re

_ALLOWED_FIRST = {"select", "with", "describe", "desc", "show", "explain"}
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|optimize|"
    r"vacuum|refresh|set|use|call|msck|restore|undrop)\b",
    re.IGNORECASE,
)
_SECRET = re.compile(r"(?<![A-Za-z0-9_])(sig|se|sp|sv|sr|st|spr)=[^&\s\"'<>]+")


class GuardError(ValueError):
    pass


def check_readonly_sql(sql: str) -> str:
    """Return the statement if it is a single read-only query, else raise GuardError."""
    text = re.sub(r"--[^\n]*", " ", sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL).strip().rstrip(";").strip()
    if not text:
        raise GuardError("empty statement")
    without_strings = re.sub(r"'(?:[^']|'')*'", "''", text)
    if ";" in without_strings:
        raise GuardError("only one statement is allowed")
    first = without_strings.split(None, 1)[0].lower()
    if first not in _ALLOWED_FIRST:
        raise GuardError(f"only read-only queries are allowed (got '{first}')")
    bad = _FORBIDDEN.search(without_strings)
    if bad:
        raise GuardError(f"keyword '{bad.group(1).lower()}' is not allowed in a read-only query")
    return text


def redact(text: str) -> str:
    """Strip SAS token parts from anything that is about to be shown to the model or logged."""
    return _SECRET.sub(lambda m: f"{m.group(1)}=REDACTED", text)


def drop_drill_lines(trace: str) -> str:
    """Remove traceback lines that mention the drill switch. Without this the
    agent could read the answer to a drill straight out of a stack trace."""
    return "\n".join(line for line in trace.splitlines() if "fault" not in line.lower())
