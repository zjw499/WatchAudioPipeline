from __future__ import annotations

import re


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


def normalize_recipient(value: str | None) -> str | None:
    """Return one safe recipient address or None when no override was supplied."""
    recipient = (value or "").strip()
    if not recipient:
        return None
    if len(recipient) > 320 or "\r" in recipient or "\n" in recipient:
        raise ValueError("recipient must be a single valid email address")
    if not _EMAIL_PATTERN.fullmatch(recipient):
        raise ValueError("recipient must be a single valid email address")
    return recipient


def normalize_client_id(value: str | None) -> str:
    client_id = (value or "legacy").strip()
    if client_id == "legacy":
        return client_id
    if not _CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError("client_id is invalid")
    return client_id
