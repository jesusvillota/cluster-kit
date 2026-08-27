"""Shared visual theme for desktop, phone, and ttyd TUI shells."""

from __future__ import annotations

from textual.theme import Theme

GITHUB_DARK_THEME = Theme(
    name="cluster-github-dark",
    primary="#58a6ff",
    secondary="#bc8cff",
    accent="#1f6feb",
    success="#3fb950",
    warning="#d29922",
    error="#ff7b72",
    background="#0d1117",
    surface="#161b22",
    panel="#21262d",
    dark=True,
)

__all__ = ["GITHUB_DARK_THEME"]
