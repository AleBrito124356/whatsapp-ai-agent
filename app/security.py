"""Webhook signature verification.

Meta signs every webhook POST with an HMAC-SHA256 of the raw request body,
keyed by your App Secret, in the ``X-Hub-Signature-256`` header as
``sha256=<hexdigest>``. We recompute it over the *raw* bytes (never the
re-serialized JSON, whose key order/spacing may differ) and compare in constant
time.
"""

from __future__ import annotations

import hashlib
import hmac


def compute_signature(app_secret: str, body: bytes) -> str:
    """Return the ``sha256=...`` header value for a body. Used by the tester."""
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret:
        # No secret configured -> cannot verify. Caller decides policy.
        return False
    if not header or "=" not in header:
        return False
    algorithm, _, provided = header.partition("=")
    if algorithm != "sha256" or not provided:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())
