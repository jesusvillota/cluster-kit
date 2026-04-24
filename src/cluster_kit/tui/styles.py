"""TCSS styling constants for the Cluster TUI application."""

from __future__ import annotations

MAIN_CSS: str = """
/* ========================================================================
   Modal overlay — ConfirmCancelScreen
   ======================================================================== */

ConfirmCancelScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.7);
}

ConfirmCancelScreen > Grid {
    width: 60;
    height: 13;
    background: $surface;
    border: thick $primary;
    padding: 1 2;
    grid-size: 1 3;
    grid-rows: 3 3 3;
    grid-gutter: 1;
}

ConfirmCancelScreen.phone-compact > Grid {
    width: 92vw;
    max-width: 36;
    height: auto;
    min-height: 12;
    grid-size: 1;
    grid-rows: auto auto auto;
}

ConfirmCancelScreen #dialog-label {
    width: 1fr;
    height: 3;
    content-align: center middle;
    text-align: center;
}

ConfirmCancelScreen.phone-compact #dialog-label {
    height: auto;
    min-height: 3;
}

ConfirmCancelScreen #confirm {
    width: 1fr;
    height: 3;
}

ConfirmCancelScreen #keep {
    width: 1fr;
    height: 3;
}

/* ========================================================================
   Status bar — ConnectionStatus
   ======================================================================== */

ConnectionStatus {
    height: 1;
    padding: 0 1;
}

ConnectionStatus .connected {
    color: $success;
}

ConnectionStatus .stale {
    color: $warning;
}

ConnectionStatus .error {
    color: $error;
}

/* ========================================================================
   Queue DataTable
   ======================================================================== */

DataTable {
    height: 1fr;
}

DataTable > .datatable--header {
    background: $primary-darken-2;
    color: $text;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: $primary;
    color: $text;
}

/* ========================================================================
   Log viewer (RichLog)
   ======================================================================== */

RichLog {
    height: 1fr;
    padding: 0 1;
    scrollbar-gutter: stable;
}

/* ========================================================================
   Tab content area
   ======================================================================== */

TabPane {
    height: 1fr;
    padding: 0;
}

TabbedContent {
    height: 1fr;
}

/* ========================================================================
   Sync screen
   ======================================================================== */

SyncScreen {
    background: $surface;
}

SyncScreen #sync-title {
    height: 3;
    content-align: center middle;
    text-align: center;
    background: $primary-darken-2;
    color: $text;
    text-style: bold;
}

SyncScreen #sync-log {
    height: 1fr;
}

SyncScreen #sync-cancel {
    dock: bottom;
    height: 3;
    width: 1fr;
}

SyncScreen #sync-close {
    dock: bottom;
    height: 3;
    width: 1fr;
}

SyncScreen.phone-compact #sync-title {
    height: 4;
    padding: 0 1;
}

SyncScreen.phone-compact #sync-log {
    min-height: 8;
}
"""


PHONE_CSS: str = (
    MAIN_CSS
    + """

Screen {
    background: $surface;
}

#phone-shell {
    height: 1fr;
    padding: 0 1;
}

#phone-title {
    height: 3;
    content-align: center middle;
    text-align: center;
    background: $primary-darken-2;
    color: $text;
    text-style: bold;
}

.phone-row {
    width: 100%;
    height: 3;
    margin: 0 0 1 0;
}

.phone-row Button {
    width: 1fr;
    height: 3;
    min-height: 3;
    margin: 0;
    padding: 0 1;
    content-align: center middle;
    text-align: center;
    color: $text;
}

#phone-nav-row,
#phone-action-row-primary,
#phone-action-row-secondary {
    height: 3;
    grid-gutter: 1;
}

#phone-nav-row {
    layout: grid;
    grid-size: 3 1;
    grid-columns: 1fr 1fr 1fr;
    grid-rows: 3;
}

#phone-action-row-primary,
#phone-action-row-secondary {
    layout: grid;
    grid-size: 3 1;
    grid-columns: 1fr 1fr 1fr;
    grid-rows: 3;
}

#phone-status {
    height: 2;
    padding: 0 1;
    content-align: left middle;
}

#phone-views {
    height: 1fr;
    border: round $primary;
    padding: 0;
}

.phone-view {
    height: 1fr;
}

#phone-queue-view PhoneQueueSelector,
#phone-available-view AvailableResourcesTable,
#phone-logs-view LogViewer {
    height: 1fr;
}

#phone-logs-view LogViewer {
    border: none;
    padding: 0 1;
}

Button.active-view {
    background: $accent;
    color: $text;
    text-style: bold;
}

.phone-row Button:disabled {
    color: $text 55%;
}
"""
)
