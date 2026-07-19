"""Signature verification: the security boundary of the webhook."""

from __future__ import annotations

import json

from app.security import compute_signature, verify_signature

# A throwaway secret assembled at runtime so nothing secret-shaped is on disk.
APP_SECRET = "".join(["test", "-", "app", "-", "secret"])


def test_valid_signature_roundtrip():
    body = json.dumps({"hello": "world"}).encode("utf-8")
    header = compute_signature(APP_SECRET, body)
    assert header.startswith("sha256=")
    assert verify_signature(APP_SECRET, body, header) is True


def test_tampered_body_is_rejected():
    body = b'{"amount": 10}'
    header = compute_signature(APP_SECRET, body)
    tampered = b'{"amount": 1000}'
    assert verify_signature(APP_SECRET, tampered, header) is False


def test_wrong_secret_is_rejected():
    body = b'{"a": 1}'
    header = compute_signature(APP_SECRET, body)
    assert verify_signature("different-secret", body, header) is False


def test_missing_or_malformed_header():
    body = b"{}"
    assert verify_signature(APP_SECRET, body, None) is False
    assert verify_signature(APP_SECRET, body, "not-a-signature") is False
    assert verify_signature(APP_SECRET, body, "md5=abc") is False


def test_empty_secret_never_verifies():
    body = b"{}"
    header = compute_signature(APP_SECRET, body)
    assert verify_signature("", body, header) is False
