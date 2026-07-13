"""Tests for cluster_kit.email: SMTP send, DNS fallback, and log capture."""

import logging
import socket
from pathlib import Path

import pytest

from cluster_kit.email import (
    send_notification,
    setup_log_capture,
    teardown_log_capture,
)


def _fake_config(env_file: Path | None = None) -> dict[str, object]:
    return {
        "to": "to@example.com",
        "from": "from@example.com",
        "host": "smtp.example.com",
        "port": 587,
        "password": "secret",
    }


def test_send_notification_quotes_scoped_attachment_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attachment = tmp_path / "whale_counts_monthly_[all_dates].pdf"
    attachment.write_bytes(b"pdf-bytes")

    monkeypatch.setattr(
        "cluster_kit.email.smtp._load_email_config", _fake_config
    )

    captured: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def ehlo(self) -> None:
            return None

        def starttls(self, *, context) -> None:
            captured["tls_context"] = context

        def login(self, username: str, password: str) -> None:
            captured["login"] = (username, password)

        def send_message(self, msg) -> None:
            captured["msg"] = msg

    monkeypatch.setattr("cluster_kit.email.smtp.smtplib.SMTP", FakeSMTP)

    sent = send_notification(
        "[WSB] | OK | test | 1s",
        "<p>html</p>",
        "plain",
        [attachment],
    )

    assert sent is True
    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 587
    assert captured["login"] == ("from@example.com", "secret")
    assert captured["tls_context"].check_hostname is True

    msg = captured["msg"]
    attachment_parts = [
        part for part in msg.walk() if part.get_content_disposition() == "attachment"
    ]
    assert len(attachment_parts) == 1
    assert attachment_parts[0]["Content-Disposition"] == (
        'attachment; filename="whale_counts_monthly_[all_dates].pdf"'
    )
    assert attachment_parts[0].get_filename() == "whale_counts_monthly_[all_dates].pdf"


def test_send_notification_retries_through_fallback_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cluster_kit.email.smtp._load_email_config",
        lambda env_file=None: {
            "to": "to@example.com",
            "from": "from@example.com",
            "host": "smtp.gmail.com",
            "port": 587,
            "password": "secret",
        },
    )
    monkeypatch.setattr(
        "cluster_kit.email.smtp._resolve_ipv4_via_fallback_dns",
        lambda host: "192.0.2.1",
    )

    captured: dict[str, object] = {"hosts": []}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            captured["hosts"].append(host)
            if host == "smtp.gmail.com":
                raise socket.gaierror("system DNS unavailable")
            self._host = host

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def ehlo(self) -> None:
            return None

        def starttls(self, *, context) -> None:
            captured["tls_context"] = context
            captured["tls_host"] = self._host

        def login(self, username: str, password: str) -> None:
            return None

        def send_message(self, msg) -> None:
            return None

    monkeypatch.setattr("cluster_kit.email.smtp.smtplib.SMTP", FakeSMTP)

    assert send_notification("subject", "<p>html</p>") is True
    assert captured["hosts"] == ["smtp.gmail.com", "192.0.2.1"]
    assert captured["tls_host"] == "smtp.gmail.com"
    assert captured["tls_context"].check_hostname is True


def test_send_notification_returns_false_when_fallback_dns_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cluster_kit.email.smtp._load_email_config",
        lambda env_file=None: {
            "to": "to@example.com",
            "from": "from@example.com",
            "host": "smtp.gmail.com",
            "port": 587,
            "password": "secret",
        },
    )

    def raise_dns_error(*args, **kwargs):
        raise socket.gaierror("system DNS unavailable")

    monkeypatch.setattr("cluster_kit.email.smtp.smtplib.SMTP", raise_dns_error)
    monkeypatch.setattr(
        "cluster_kit.email.smtp._resolve_ipv4_via_fallback_dns",
        lambda host: (_ for _ in ()).throw(ValueError("fallback DNS unavailable")),
    )

    assert send_notification("subject", "<p>html</p>") is False


def test_log_capture_captures_from_non_propagating_named_logger() -> None:
    """Capture must work for a caller's own logger (propagate=False), not just root.

    A repo that logs through a ``propagate=False`` logger would never reach root
    handlers, so setup_log_capture must attach to that named logger directly.
    """
    logger = logging.getLogger("cluster_kit_test_app")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # messages never reach the root logger

    path = setup_log_capture(logger_name="cluster_kit_test_app")
    try:
        logger.info("captured-marker-line")  # flushed to disk on teardown
    finally:
        teardown_log_capture()

    assert path.exists()
    assert "captured-marker-line" in path.read_text(encoding="utf-8")
    # handler removed on teardown
    assert not any(
        isinstance(h, logging.FileHandler) for h in logger.handlers
    )
    path.unlink()
