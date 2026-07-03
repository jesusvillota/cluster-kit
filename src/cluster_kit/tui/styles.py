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
    height: auto;
    min-height: 15;
    background: $surface;
    border: thick $primary;
    padding: 1 2;
    grid-size: 1 3;
    grid-rows: auto auto auto;
    grid-gutter: 1;
}

ConfirmCancelScreen.phone-compact > Grid {
    width: 92vw;
    max-width: 36;
    height: auto;
    min-height: 15;
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
    content-align: center middle;
    text-align: center;
}

ConfirmCancelScreen #keep {
    width: 1fr;
    height: 3;
    content-align: center middle;
    text-align: center;
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
    background: $surface-darken-1;
}

#phone-shell {
    height: 1fr;
    padding: 0;
}

/* ---- Header bar -------------------------------------------------------- */
#phone-title {
    height: 1;
    content-align: center middle;
    text-align: center;
    background: $surface-darken-1;
    color: $text;
    text-style: bold;
    padding: 0 1;
}

/* ---- Top segmented control (view tabs) --------------------------------- */
#phone-nav-row {
    layout: grid;
    grid-size: 3 1;
    grid-columns: 1fr 1fr 1fr;
    grid-rows: 3;
    grid-gutter: 1;
    width: 100%;
    height: 3;
    padding: 0 1;
    margin: 1 0 0 0;
}

#phone-nav-row Button {
    width: 1fr;
    height: 3;
    min-height: 3;
    margin: 0;
    padding: 0;
    content-align: center middle;
    text-align: center;
    text-style: none;
    color: $text-muted;
    border: round $panel-lighten-1;
    background: $boost;
}

#phone-nav-row Button:hover {
    border: round $accent;
}

#phone-nav-row Button.active-view {
    background: $accent;
    color: $text;
    text-style: bold;
    border: round $accent-lighten-2;
}

#phone-nav-row Button:disabled {
    color: $text-disabled;
    background: $panel-darken-1;
    border: round $panel;
    text-style: none;
}

/* ---- Status line ------------------------------------------------------- */
#phone-status {
    height: 1;
    padding: 0 1;
    margin: 1 0 0 0;
    content-align: left middle;
    color: $text-muted;
    text-style: bold;
}

ConnectionStatus.connected {
    color: $success;
}

ConnectionStatus.stale {
    color: $warning;
}

ConnectionStatus.error {
    color: $error;
}

/* ---- Content viewport -------------------------------------------------- */
#phone-views {
    height: 1fr;
    border: round $primary;
    background: $surface;
    padding: 0;
    margin: 1 1 0 1;
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

/* ---- Bottom action dock (thumb reach) ---------------------------------- */
#phone-action-dock {
    dock: bottom;
    height: auto;
    padding: 0 1;
    background: $panel-darken-1;
    border-top: round $primary;
}

#phone-action-row-queue-primary,
#phone-action-row-queue-secondary,
#phone-action-row-log {
    layout: grid;
    grid-size: 2 1;
    grid-columns: 1fr 1fr;
    grid-rows: 3;
    grid-gutter: 1;
    width: 100%;
    height: 3;
    margin: 0 0 1 0;
}

#phone-action-row-queue-primary {
    grid-size: 3 1;
    grid-columns: 1fr 1fr 1fr;
}

#phone-action-row-queue-secondary {
    grid-size: 1 1;
    grid-columns: 1fr;
    margin: 0;
}

#phone-action-row-log {
    margin: 0;
}

#phone-action-dock Button {
    width: 1fr;
    height: 3;
    min-height: 3;
    margin: 0;
    padding: 0;
    content-align: center middle;
    text-align: center;
    text-style: none;
    color: $text;
    border: round $panel-lighten-2;
    background: $panel;
}

#phone-action-dock Button:hover {
    border: round $accent;
}

#phone-action-dock Button:disabled {
    color: $text-disabled;
    background: $panel-darken-1;
    border: round $panel;
    text-style: none;
}
"""
)
