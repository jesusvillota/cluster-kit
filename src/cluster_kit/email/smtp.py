"""SMTP delivery with a fallback DNS resolver for flaky compute nodes.

Some cluster compute nodes cannot resolve public hostnames through their
system resolver (``socket.gaierror`` on ``smtplib.SMTP(host, port)``).  When
that happens :func:`_connect_smtp` retries by resolving one A record through a
public resolver (``1.1.1.1``) with a hand-rolled UDP query, then connects by
IP while keeping the real hostname for SNI / certificate verification.

Configuration via environment variables (loaded from ``.env`` via
python-dotenv):

    NOTIFY_EMAIL_TO      - Recipient email address
    NOTIFY_EMAIL_FROM    - Sender email address (Gmail account)
    NOTIFY_SMTP_HOST     - SMTP server (default: smtp.gmail.com)
    NOTIFY_SMTP_PORT     - SMTP port (default: 587)
    NOTIFY_SMTP_PASSWORD - Gmail App Password

All notification failures are caught and logged as warnings -- a failed email
must never crash the calling script.
"""

from __future__ import annotations

import logging
import os
import smtplib
import socket
import ssl
import struct
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_logger = logging.getLogger("cluster_kit.email")

# Maximum attachment size in bytes (25 MB).
MAX_ATTACHMENT_BYTES = 25_000_000
DNS_FALLBACK_RESOLVER = "1.1.1.1"
DNS_FALLBACK_TIMEOUT_SECONDS = 5


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_loaded_env_files: set[Path] = set()


def _ensure_env_loaded(env_file: Path | None = None) -> None:
    """Load an ``.env`` file once via python-dotenv (best-effort).

    *env_file* is the explicit path a consumer wants loaded (e.g. a repo's
    project-root ``.env``).  When ``None``, fall back to ``.env`` in the
    current working directory.  Each distinct path is loaded at most once.
    """
    target = Path(env_file) if env_file is not None else Path(".env")
    if target in _loaded_env_files:
        return
    _loaded_env_files.add(target)
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # python-dotenv not installed; rely on env vars set externally
    if target.exists():
        load_dotenv(target)


def _load_email_config(env_file: Path | None = None) -> dict[str, str | int]:
    """Return email configuration from environment variables."""
    _ensure_env_loaded(env_file)
    return {
        "to": os.environ.get("NOTIFY_EMAIL_TO", ""),
        "from": os.environ.get("NOTIFY_EMAIL_FROM", ""),
        "host": os.environ.get("NOTIFY_SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("NOTIFY_SMTP_PORT", "587")),
        "password": os.environ.get("NOTIFY_SMTP_PASSWORD", ""),
    }


# ---------------------------------------------------------------------------
# Fallback DNS resolution
# ---------------------------------------------------------------------------


def _dns_name_end(packet: bytes, offset: int) -> int:
    """Return the byte after a DNS name, including compressed names."""
    while True:
        if offset >= len(packet):
            raise ValueError("truncated DNS name")
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            return offset + 2
        if length & 0xC0 or offset + length >= len(packet):
            raise ValueError("invalid DNS name")
        offset += length + 1


def _resolve_ipv4_via_fallback_dns(host: str) -> str:
    """Resolve *host* through the fallback resolver, returning one IPv4 address."""
    labels = host.rstrip(".").split(".")
    if not host or any(not label or len(label.encode()) > 63 for label in labels):
        raise ValueError(f"invalid DNS hostname: {host!r}")

    transaction_id = os.urandom(2)
    question = b"".join(
        bytes([len(label.encode())]) + label.encode() for label in labels
    ) + b"\0" + struct.pack("!HH", 1, 1)
    query = transaction_id + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0) + question

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(DNS_FALLBACK_TIMEOUT_SECONDS)
        sock.connect((DNS_FALLBACK_RESOLVER, 53))
        sock.send(query)
        response = sock.recv(512)

    if len(response) < 12 or response[:2] != transaction_id:
        raise ValueError("invalid DNS response")
    flags, question_count, answer_count, _, _, _ = struct.unpack(
        "!HHHHHH", response[2:14]
    )
    if flags & 0x000F or question_count != 1:
        raise ValueError("DNS lookup failed")

    offset = 12
    offset = _dns_name_end(response, offset) + 4
    for _ in range(answer_count):
        offset = _dns_name_end(response, offset)
        if offset + 10 > len(response):
            raise ValueError("truncated DNS answer")
        record_type, record_class, _, record_length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        if offset + record_length > len(response):
            raise ValueError("truncated DNS record")
        if record_type == 1 and record_class == 1 and record_length == 4:
            return socket.inet_ntoa(response[offset : offset + 4])
        offset += record_length

    raise ValueError(f"no IPv4 address returned for {host}")


def _connect_smtp(host: str, port: int) -> smtplib.SMTP:
    """Connect to SMTP, using public DNS only if the system resolver fails."""
    try:
        return smtplib.SMTP(host, port, timeout=30)
    except socket.gaierror:
        fallback_ip = _resolve_ipv4_via_fallback_dns(host)
        _logger.warning(
            "System DNS could not resolve %s; retrying SMTP through %s",
            host,
            DNS_FALLBACK_RESOLVER,
        )
        server = smtplib.SMTP(fallback_ip, port, timeout=30)
        # smtplib uses _host for SNI + cert hostname verification in starttls().
        server._host = host
        return server


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------


def send_notification(
    subject: str,
    body_html: str,
    body_plain: str = "",
    attachments: list[Path] | None = None,
    env_file: Path | None = None,
) -> bool:
    """
    Send an email notification via SMTP.

    Returns ``True`` on success, ``False`` on failure.  Never raises -- a
    notification failure must not crash the calling script.
    """
    try:
        config = _load_email_config(env_file)

        if not config["to"] or not config["from"] or not config["password"]:
            _logger.warning(
                "Email notification skipped: set NOTIFY_EMAIL_TO, "
                "NOTIFY_EMAIL_FROM, and NOTIFY_SMTP_PASSWORD in .env"
            )
            return False

        # Build the message
        msg = MIMEMultipart("alternative")
        msg["From"] = str(config["from"])
        msg["To"] = str(config["to"])
        msg["Subject"] = subject

        if body_plain:
            msg.attach(MIMEText(body_plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        # Wrap in mixed multipart if there are attachments
        if attachments:
            mixed = MIMEMultipart("mixed")
            mixed["From"] = msg["From"]
            mixed["To"] = msg["To"]
            mixed["Subject"] = msg["Subject"]
            mixed.attach(msg)

            for file_path in attachments:
                if not file_path.exists():
                    _logger.warning(f"Attachment not found, skipping: {file_path}")
                    continue
                part = MIMEBase("application", "octet-stream")
                with open(file_path, "rb") as fh:
                    part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=file_path.name,
                )
                mixed.attach(part)
            msg = mixed

        # Send
        host = str(config["host"])
        with _connect_smtp(host, int(config["port"])) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(str(config["from"]), str(config["password"]))
            server.send_message(msg)

        _logger.info(f"[bold green]Notification sent to {config['to']}[/bold green]")
        return True

    except Exception as exc:
        _logger.warning(f"Failed to send email notification: {exc}")
        return False
