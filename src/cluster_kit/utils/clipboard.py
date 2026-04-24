"""
Cross-platform clipboard utilities with SSH session detection.

Provides clipboard copy functionality that works across Linux (X11/Wayland),
macOS, and Windows, with graceful fallback chains and SSH session detection.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def is_ssh_session() -> bool:
    """
    Detect if running inside an SSH session.

    Checks for the presence of ``$SSH_CLIENT`` or ``$SSH_TTY`` environment
    variables, which are set by OpenSSH when a remote session is established.

    Returns:
        True if either environment variable is set, False otherwise.
    """
    return "SSH_CLIENT" in os.environ or "SSH_TTY" in os.environ


def copy_to_clipboard(
    text: str,
    max_size: int = 10 * 1024 * 1024,
) -> tuple[bool, str | None]:
    """
    Copy text to the system clipboard with cross-platform fallback.

    Attempts clipboard copy in this order:
    1. ``pyxclip`` (Python library wrapping xclip/wl-clipboard)
    2. ``wl-copy`` (Linux Wayland)
    3. ``xclip`` (Linux X11)
    4. ``pbcopy`` (macOS)
    5. ``clip`` (Windows)

    Args:
        text: Text content to copy to clipboard.
        max_size: Maximum allowed size in bytes (default 10 MB).

    Returns:
        Tuple of ``(success, error_message)``. On success, returns
        ``(True, None)``. On failure, returns ``(False, error_string)``.
    """
    # ── Size validation ──
    byte_size = len(text.encode("utf-8"))
    if byte_size > max_size:
        return (
            False,
            f"Text size ({byte_size / (1024 * 1024):.1f} MB) exceeds "
            f"maximum allowed ({max_size / (1024 * 1024):.0f} MB)",
        )

    if not text:
        return (False, "Empty text provided")

    # ── Attempt 1: pyxclip ──
    try:
        import pyxclip

        pyxclip.copy(text)
        return (True, None)
    except Exception as exc:
        pyxclip_error = str(exc)

    # ── Attempt 2: Subprocess fallback chain ──
    fallbacks: list[tuple[str, list[str]]] = [
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("pbcopy", ["pbcopy"]),
        ("clip", ["clip"]),
    ]

    for name, cmd in fallbacks:
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.run(
                    cmd,
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                if proc.returncode == 0:
                    return (True, None)
            except subprocess.TimeoutExpired:
                continue
            except subprocess.CalledProcessError:
                continue
            except OSError:
                continue

    # ── All methods failed ──
    return (
        False,
        f"All clipboard methods failed. pyxclip: {pyxclip_error}. "
        f"No working subprocess fallback found (tried: wl-copy, xclip, pbcopy, clip).",
    )


__all__ = ["copy_to_clipboard", "is_ssh_session"]
