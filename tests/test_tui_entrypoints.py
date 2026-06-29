"""Tests for direct TUI entry points."""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest

from cluster_kit.tui.app import parse_args as parse_desktop_args
from cluster_kit.tui.app_phone import parse_args as parse_phone_args


@pytest.mark.parametrize(
    ("parse_args", "argv"),
    [
        (parse_desktop_args, ["app"]),
        (parse_phone_args, ["app_phone"]),
    ],
)
def test_tui_entrypoints_default_to_all_users(
    monkeypatch: pytest.MonkeyPatch,
    parse_args: Callable[[], object],
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    args = parse_args()

    assert getattr(args, "all_users") is True


@pytest.mark.parametrize(
    ("parse_args", "argv"),
    [
        (parse_desktop_args, ["app", "--user-only"]),
        (parse_phone_args, ["app_phone", "--user-only"]),
    ],
)
def test_tui_entrypoints_user_only_flag(
    monkeypatch: pytest.MonkeyPatch,
    parse_args: Callable[[], object],
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    args = parse_args()

    assert getattr(args, "all_users") is False
