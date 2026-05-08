"""Ownership helpers for queue selection policies in the TUI."""

from __future__ import annotations


def user_matches_allowed_owner(job_user: str, allowed_user: str) -> bool:
    """Return True when the job owner matches the configured selectable user."""

    normalized_allowed_user = allowed_user.strip()
    if not normalized_allowed_user:
        return False
    return job_user.strip() == normalized_allowed_user


__all__ = ["user_matches_allowed_owner"]
