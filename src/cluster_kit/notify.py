"""Push a SLURM queue summary to a webhook.

The webhook is any HTTP endpoint that accepts ``POST {"recipient", "message"}``
and returns JSON. A chat bridge (WhatsApp, Slack, Telegram, ...) is the common
target, but nothing here is bound to a specific provider — point ``--webhook``
at whatever speaks that shape.
"""

from __future__ import annotations

import json
import urllib.request

from cluster_kit.tui.backend.queue_parser import (
    RUNNING_STATES_WITH_ALLOCATIONS,
    JobInfo,
)

_ACTIVE_STATES = {"R", "RUNNING"}


class NotifyError(RuntimeError):
    """Raised when the webhook rejects the message."""


def build_summary(jobs: list[JobInfo], user: str) -> str:
    """Render queue rows as a compact plain-text summary (no markup)."""
    running = sum(
        1 for j in jobs if j.state.strip().upper() in RUNNING_STATES_WITH_ALLOCATIONS
    )
    pending = len(jobs) - running
    lines = [f"🐋 cluster queue ({user}): {running} running, {pending} pending"]
    for j in jobs:
        if j.state.strip().upper() in _ACTIVE_STATES:
            tag = j.state
        else:
            tag = f"{j.state} {j.reason}".strip()
        lines.append(f"• {j.job_id} {j.name} [{j.partition}] {tag} {j.time}")
    return "\n".join(lines)


def post_webhook(url: str, recipient: str, message: str, timeout: int = 15) -> None:
    """POST ``{recipient, message}`` to *url*; raise NotifyError on refusal."""
    payload = json.dumps({"recipient": recipient, "message": message}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return  # non-JSON 2xx response: assume the endpoint accepted it
    if parsed.get("success") is False:
        raise NotifyError(f"webhook refused send: {parsed.get('message', body)}")
