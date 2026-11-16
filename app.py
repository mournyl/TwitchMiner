"""
Native PyQt6 GUI supporting independent node configurations per account.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
import re
import shutil
import subprocess
import tempfile
import zipfile

import requests
from PyQt6.QtCore import QEvent, QPointF, QSize, QTimer, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QMenu, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget
)

from auth import login, validate_token
from pathlib import Path
from config import ACCOUNTS_PATH, APP_VERSION, DATA_DIR, GITHUB_REPO, Settings
from miner import AccountNodeState, MiningNode, Miner
from proxies import SCHEMES, ProxyStore, describe_error, make_failover, mask

# ---------------------------------------------------------------------------
# Theme — near-black ground, bone-white text and accents, blackletter wordmark.
# ---------------------------------------------------------------------------
C_BG = "#0c0b0e"
C_PANEL = "#141217"
C_PANEL2 = "#1a171f"
C_LINE = "#2a2631"
C_ROW = "#1f1c25"
C_TEXT = "#ece8f1"
C_SOFT = "#c9c2d6"
C_MUTED = "#9c95a8"
C_OK = "#6fd08c"
C_WARN = "#d9bf6a"
C_AMBER = "#e0a34a"
C_FAIL = "#f07a7a"
C_FOLLOW = "#86b4f0"

# Optional font files (.ttf/.otf) are loaded from a "fonts" folder next to the
# app: UnifrakturMaguntia (wordmark), IBM Plex Sans (text), IBM Plex Mono
# (numbers). Without them the app falls back to fonts Windows already has.
FONTS_DIR = DATA_DIR.parent / "fonts"

APP_STYLE = """
QMainWindow, QDialog { background: @BG; }
QWidget { font-family: "@SANS"; font-size: 13px; }
QLabel, QCheckBox { background: transparent; }
QToolTip { background: @PANEL2; color: @TEXT; border: 1px solid @LINE; padding: 4px 6px; }

#wordmark { font-family: "@GOTHIC"; font-size: 34px; color: @TEXT; }
#wordmarkSub { font-family: "@MONO"; font-size: 11px; color: @MUTED; }
#caption { font-family: "@MONO"; font-size: 11px; color: @MUTED; }
#muted { color: @MUTED; }
#soft { color: @SOFT; }
#mono { font-family: "@MONO"; font-size: 12px; }
#monoMuted { font-family: "@MONO"; font-size: 12px; color: @MUTED; }
#warn { color: @AMBER; }
#error { color: @FAIL; }
#nodeTitle { font-size: 15px; }
#dropName { font-size: 16px; font-weight: 600; }
#bigPct { font-family: "@MONO"; font-size: 24px; font-weight: 500; }

QFrame#card, QFrame#tile, QFrame#tileHot {
    background: @PANEL; border: 1px solid @LINE; border-radius: 10px; }
QFrame#tileHot { border: 1px solid @TEXT; }
#tileValue { font-size: 28px; font-weight: 600; }

QPushButton { background: @PANEL2; color: @TEXT; border: 1px solid @LINE;
    border-radius: 8px; padding: 8px 14px; font-weight: 500; }
QPushButton:hover { border-color: #4a4553; }
QPushButton:pressed { background: @PANEL; }
QPushButton:disabled { color: #6b6575; }
QPushButton[kind="primary"] { background: @TEXT; color: @BG; border-color: @TEXT; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: #ffffff; border-color: #ffffff; }
QPushButton[kind="primary"]:disabled { background: #4a4553; border-color: #4a4553; color: @BG; }
QPushButton[kind="ghost"] { background: transparent; border-color: transparent; color: @MUTED; }
QPushButton[kind="ghost"]:hover { color: @TEXT; }
QPushButton[kind="danger"] { background: transparent; color: @FAIL; }

QLineEdit, QComboBox, QTextEdit, QPlainTextEdit { background: @BG; color: @TEXT;
    border: 1px solid @LINE; border-radius: 8px; padding: 7px 10px;
    selection-background-color: #4a4553; selection-color: @TEXT; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus { border-color: @TEXT; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView { background: @PANEL; color: @TEXT; border: 1px solid @LINE;
    selection-background-color: @ROW; selection-color: @TEXT; outline: 0; }
QTextEdit#log { font-family: "@MONO"; font-size: 12px; color: @SOFT; border: 0;
    border-top: 1px solid @LINE; border-radius: 0; background: transparent; }

QCheckBox { spacing: 10px; font-weight: 500; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1.5px solid @TEXT;
    border-radius: 5px; background: transparent; }
QCheckBox::indicator:checked { background: @TEXT; image: url(@CHECK); }

QProgressBar { background: @ROW; border: 0; border-radius: 3px; }
QProgressBar::chunk { background: @TEXT; border-radius: 3px; }
QProgressBar#thick { border-radius: 6px; }
QProgressBar#thick::chunk { border-radius: 6px; }

QTableWidget { background: @PANEL; alternate-background-color: @PANEL;
    border: 1px solid @LINE; border-radius: 10px; gridline-color: @ROW;
    selection-background-color: @ROW; selection-color: @TEXT; outline: 0; }
QHeaderView { background: transparent; }
QHeaderView::section { background: @PANEL; color: @MUTED; border: 0;
    border-bottom: 1px solid @LINE; padding: 8px 10px; font-weight: 500; }
QTableCornerButton::section { background: @PANEL; border: 0; }

QListWidget { background: @PANEL; border: 1px solid @LINE;
    border-radius: 10px; padding: 4px; outline: 0; }
QListWidget::item { padding: 9px 10px; border-radius: 6px; }
QListWidget::item:hover { background: @PANEL2; }
QListWidget::item:selected { background: @ROW; }

QTabWidget::pane { border: 0; border-top: 1px solid @LINE; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: @MUTED; border: 0;
    border-bottom: 2px solid transparent; padding: 9px 14px; font-weight: 600; }
QTabBar::tab:hover { color: @TEXT; }
QTabBar::tab:selected { color: @TEXT; border-bottom: 2px solid @TEXT; }
QTabWidget#mainTabs > QTabBar::tab { padding: 14px 16px; font-size: 14px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @LINE; border-radius: 3px; min-height: 28px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: @LINE; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:hover { background: #4a4553; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def _pick_font(candidates: list[str], keyword: str, loaded: list[str], fallback: str) -> str:
    """First installed/app family from candidates; else any app-loaded family
    whose name contains keyword (so a dropped-in font file is used even when
    its family name is spelled differently); else fallback."""
    have = set(QFontDatabase.families())
    for name in candidates:
        if name in have:
            return name
    key = keyword.lower()
    for fam in loaded:
        if key in fam.lower().replace(" ", ""):
            return fam
    return fallback


def _write_check_png() -> str:
    """Dark tick drawn once into data/ for the white checkbox (QSS needs a file)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "ui_check.png"
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(C_BG))
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(4.5, 9.5), QPointF(7.5, 12.5))
    p.drawLine(QPointF(7.5, 12.5), QPointF(13.5, 5.5))
    p.end()
    pm.save(str(path), "PNG")
    return path.as_posix()


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in (
        (QPalette.ColorRole.Window, C_BG), (QPalette.ColorRole.Base, C_PANEL),
        (QPalette.ColorRole.AlternateBase, C_PANEL), (QPalette.ColorRole.Button, C_PANEL2),
        (QPalette.ColorRole.WindowText, C_TEXT), (QPalette.ColorRole.Text, C_TEXT),
        (QPalette.ColorRole.ButtonText, C_TEXT), (QPalette.ColorRole.ToolTipBase, C_PANEL2),
        (QPalette.ColorRole.ToolTipText, C_TEXT), (QPalette.ColorRole.Highlight, C_ROW),
        (QPalette.ColorRole.HighlightedText, C_TEXT), (QPalette.ColorRole.PlaceholderText, C_MUTED),
    ):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)

    loaded_families: list[str] = []
    if FONTS_DIR.is_dir():
        for f in sorted(FONTS_DIR.iterdir()):
            if f.suffix.lower() in (".ttf", ".otf"):
                fid = QFontDatabase.addApplicationFont(str(f))
                if fid != -1:
                    loaded_families += QFontDatabase.applicationFontFamilies(fid)

    try:
        check = _write_check_png()
    except Exception:
        check = ""
    style = APP_STYLE
    for token, value in (
        ("@SANS", _pick_font(["IBM Plex Sans", "Segoe UI"], "plexsans", loaded_families, app.font().family())),
        ("@MONO", _pick_font(["IBM Plex Mono", "Cascadia Mono", "Consolas"], "mono", loaded_families, "monospace")),
        ("@GOTHIC", _pick_font(["UnifrakturMaguntia", "Old English Text MT"], "fraktur", loaded_families, app.font().family())),
        ("@CHECK", check),
        ("@PANEL2", C_PANEL2), ("@PANEL", C_PANEL), ("@BG", C_BG), ("@LINE", C_LINE),
        ("@ROW", C_ROW), ("@TEXT", C_TEXT), ("@SOFT", C_SOFT), ("@MUTED", C_MUTED),
        ("@AMBER", C_AMBER), ("@FAIL", C_FAIL),
    ):
        style = style.replace(token, value)
    app.setStyleSheet(style)


def caption(text: str) -> QLabel:
    """Small spaced-out mono heading used above every section."""
    lbl = QLabel(text.upper())
    lbl.setObjectName("caption")
    f = lbl.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
    lbl.setFont(f)
    return lbl


def styled_button(text: str, kind: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    return b


def tune_table(t: QTableWidget, row_height: int = 38) -> None:
    t.setShowGrid(False)
    t.verticalHeader().setDefaultSectionSize(row_height)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.horizontalHeader().setHighlightSections(False)


class Card(QFrame):
    """Bordered panel with an optional caption; put content in .body."""

    def __init__(self, title: str = "", note: str = ""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)
        if title:
            head = QHBoxLayout()
            head.addWidget(caption(title))
            head.addStretch(1)
            if note:
                n = QLabel(note)
                n.setObjectName("muted")
                head.addWidget(n)
            self.head = head
            self.body.addLayout(head)


class ThinBar(QWidget):
    """Flat white progress bar with its text beside it instead of on top.
    Drop-in for the QProgressBar calls used here: setRange/setValue/setFormat."""

    def __init__(self, height: int = 6, label: QLabel | None = None, margins: int = 10):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(margins, 0, margins, 0)
        lay.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(height)
        if height >= 10:
            self.bar.setObjectName("thick")
        lay.addWidget(self.bar, 1)
        if label is None:
            label = QLabel("")
            label.setObjectName("mono")
            label.setMinimumWidth(40)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(label)
        self.lbl = label

    def setRange(self, lo: int, hi: int) -> None:
        self.bar.setRange(lo, hi)

    def setValue(self, v: int) -> None:
        self.bar.setValue(v)

    def setFormat(self, text: str) -> None:
        self.lbl.setText(text)


_DOT_ICONS: dict[str, QIcon] = {}


def dot_icon(color: str) -> QIcon:
    """Small state dot for the node list."""
    if color not in _DOT_ICONS:
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(2, 2, 8, 8)
        p.end()
        _DOT_ICONS[color] = QIcon(pm)
    return _DOT_ICONS[color]


_TWITCH_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/", re.I)


def parse_channel_input(text: str) -> str:
    """Accepts 'shroud', 'twitch.tv/shroud', 'https://www.twitch.tv/shroud?x=1'."""
    text = _TWITCH_URL_RE.sub("", text.strip())
    text = re.split(r"[/?#\s]", text, maxsplit=1)[0]
    return text.lstrip("@").lower()


def _drop_sort_key(d) -> tuple:
    # Ready to claim first, then in-progress by %, then not started, claimed last.
    if d.is_claimed:
        bucket = 3
    elif d.is_ready:
        bucket = 0
    elif d.current_minutes > 0:
        bucket = 1
    else:
        bucket = 2
    return (bucket, -d.percentage, d.game_name.lower(), d.drop_name.lower())


class AccountNode:
    def __init__(self, account_name: str, token: str, proxy: str | None = None):
        self.account_name = account_name
        self.token = token
        self.node = MiningNode(account_name, token, proxy=proxy)
        self.state = self.node.state
        self.miner: Miner | None = None
        self.thread: threading.Thread | None = None

    def start(self, settings: Settings) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.miner = Miner(self.node, settings)
        self.thread = threading.Thread(target=self.miner.run_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.node:
            self.node.stop()
            self.state.connected = False


class ProxyPage(QWidget):
    """The Proxies tab. Import / test / remove the user's own proxies and assign one per account.
    No proxies ship with the app. Only proxies that passed a test this
    session can be assigned."""

    STATUS_COLORS = {
        "ok": QColor(C_OK),
        "failed": QColor(C_FAIL),
        "testing": QColor(C_WARN),
        "untested": QColor(C_MUTED),
    }

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.store: ProxyStore = win.proxy_store

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        title_row = QHBoxLayout()
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("monoMuted")
        title_row.addWidget(caption("Proxies"))
        title_row.addStretch(1)
        title_row.addWidget(self.lbl_summary)
        root.addLayout(title_row)

        # ---- proxy list ----
        list_box = Card("Proxy list", "Your own — none are included")
        lv = list_box.body
        self.tbl_proxies = QTableWidget(0, 3)
        self.tbl_proxies.setHorizontalHeaderLabels(["Proxy", "Used by", "Test"])
        self.tbl_proxies.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_proxies.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_proxies.verticalHeader().setVisible(False)
        tune_table(self.tbl_proxies, 34)
        h = self.tbl_proxies.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lv.addWidget(self.tbl_proxies)

        row = QHBoxLayout()
        lbl_type = QLabel("Default type")
        lbl_type.setObjectName("muted")
        row.addWidget(lbl_type)
        self.combo_scheme = QComboBox()
        self.combo_scheme.addItems(SCHEMES)
        row.addWidget(self.combo_scheme)
        for text, fn in (
            ("Import .txt…", self._import_file),
            ("Paste…", self._paste),
            ("Test selected", self._test_selected),
            ("Test all", self._test_all),
        ):
            b = styled_button(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lv.addLayout(row)
        row2 = QHBoxLayout()
        for text, fn in (
            ("Remove selected", self._remove_selected),
            ("Remove failed", self._remove_failed),
            ("Clear all", self._clear_all),
        ):
            b = styled_button(text, "danger")
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        lv.addLayout(row2)
        hint = QLabel("host:port · host:port:user:pass · user:pass@host:port · "
                      "socks5://host:port (prefix overrides the default type). "
                      "New imports are tested automatically.")
        hint.setWordWrap(True)
        hint.setObjectName("monoMuted")
        lv.addWidget(hint)
        root.addWidget(list_box, 3)

        # ---- assignments ----
        asg_box = Card("Account → proxy", "Only proxies that passed a test are offered")
        av = asg_box.body
        self.tbl_assign = QTableWidget(0, 2)
        self.tbl_assign.setHorizontalHeaderLabels(["Account", "Proxy"])
        self.tbl_assign.verticalHeader().setVisible(False)
        self.tbl_assign.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tune_table(self.tbl_assign, 44)
        ah = self.tbl_assign.horizontalHeader()
        ah.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        av.addWidget(self.tbl_assign)
        root.addWidget(asg_box, 2)

        arow = QHBoxLayout()
        b_auto = styled_button("Auto-assign (working proxies only)")
        b_auto.setToolTip("Accounts with no proxy, or whose proxy failed, get the least-used working proxy.")
        b_auto.clicked.connect(self._auto_assign)
        b_apply = styled_button("Apply", "primary")
        b_apply.clicked.connect(self._apply)
        arow.addWidget(b_auto)
        arow.addStretch(1)
        arow.addWidget(b_apply)
        root.addLayout(arow)

        self._list_sig: tuple = ()
        self._row_status: dict[str, str] = {}
        self._assign_sig_drawn: tuple | None = None
        self._last_assign_rebuild = 0.0
        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(500)

    # -- list --------------------------------------------------------------
    def _status_text(self, url: str) -> str:
        st = self.store.status(url)
        if st == "testing":
            return "testing…"
        if st == "untested":
            return "untested"
        ok, info = self.store.health[url]
        return ("OK  " if ok else "FAIL  ") + info

    def _summary(self) -> str:
        sts = [self._row_status.get(p) or self.store.status(p) for p in self.store.proxies]
        total = len(sts)
        ok = sts.count("ok")
        failed = sts.count("failed")
        testing = sts.count("testing")
        extra = f" · {testing} testing…" if testing else ""
        return (f"<span style='color:{C_OK}'>{ok} working</span> · "
                f"{failed} failed · {total} total{extra}")

    def _refresh(self) -> None:
        """Full rebuild — only when the proxy list itself changes (import,
        remove, clear) or on open. Status updates use _update_status_cells."""
        t = self.tbl_proxies
        t.setUpdatesEnabled(False)
        try:
            t.setRowCount(len(self.store.proxies))
            self._row_status: dict[str, str] = {}
            for r, url in enumerate(self.store.proxies):
                it = QTableWidgetItem(mask(url))
                it.setData(Qt.ItemDataRole.UserRole, url)
                t.setItem(r, 0, it)
                t.setItem(r, 1, QTableWidgetItem(", ".join(self.store.users_of(url)) or "—"))
                st = self.store.status(url)
                cell = QTableWidgetItem(self._status_text(url))
                cell.setForeground(self.STATUS_COLORS[st])
                t.setItem(r, 2, cell)
                self._row_status[url] = st
        finally:
            t.setUpdatesEnabled(True)
        self._list_sig = tuple(self.store.proxies)
        self.lbl_summary.setText(self._summary())
        self._refresh_assignments()

    def _update_status_cells(self) -> bool:
        """Touch only rows whose test status changed. Returns True if any did."""
        changed = False
        t = self.tbl_proxies
        t.setUpdatesEnabled(False)
        try:
            for r in range(t.rowCount()):
                it = t.item(r, 0)
                if not it:
                    continue
                url = it.data(Qt.ItemDataRole.UserRole)
                st = self.store.status(url)
                if self._row_status.get(url) == st:
                    continue
                self._row_status[url] = st
                cell = t.item(r, 2)
                cell.setText(self._status_text(url))
                cell.setForeground(self.STATUS_COLORS[st])
                changed = True
        finally:
            t.setUpdatesEnabled(True)
        return changed

    def _any_combo_open(self) -> bool:
        for r in range(self.tbl_assign.rowCount()):
            w = self.tbl_assign.cellWidget(r, 1)
            if isinstance(w, QComboBox) and w.view().isVisible():
                return True
        return False

    def _assign_sig(self) -> tuple:
        return (
            tuple(self.store.healthy()),
            tuple((k, v["error"]) for k, v in sorted(self.win.offline_accounts.items())),
            tuple(sorted(self.win.nodes)),
            tuple(sorted(self.store.assignments.items())),
        )

    def _poll(self) -> None:
        if not self.isVisible():
            return  # tab not showing; catches up on the next tick after it opens
        if tuple(self.store.proxies) != self._list_sig:
            self._refresh()
            return
        if self._update_status_cells():
            self.lbl_summary.setText(self._summary())
        # Dropdowns: rebuild only if the working set / accounts changed, at
        # most every 2s, and never while the user has one open.
        now = time.monotonic()
        if (now - self._last_assign_rebuild >= 2.0
                and self._assign_sig() != self._assign_sig_drawn
                and not self._any_combo_open()):
            self._refresh_assignments()

    def _selected_urls(self) -> list[str]:
        rows = {i.row() for i in self.tbl_proxies.selectionModel().selectedRows()}
        return [self.tbl_proxies.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in sorted(rows)]

    def _report_import(self, res: tuple) -> None:
        added, dup, bad, new_urls = res
        QMessageBox.information(
            self, "Import",
            f"Added {added}\nDuplicates skipped {dup}\nInvalid lines {bad}"
            + ("\n\nTesting the new proxies now…" if new_urls else ""),
        )
        self._test_async(new_urls, "newly imported proxies")
        self._refresh()

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import proxy list", "", "Text files (*.txt);;All files (*)")
        if path:
            try:
                self._report_import(self.store.import_file(path, self.combo_scheme.currentText()))
            except OSError as e:
                QMessageBox.warning(self, "Import", f"Couldn't read file: {e}")

    def _paste(self) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Paste proxies", "One proxy per line:")
        if ok and text.strip():
            self._report_import(self.store.import_text(text, self.combo_scheme.currentText()))

    def _test_async(self, urls: list[str], label: str = "proxies") -> None:
        if urls:
            threading.Thread(target=self.store.test, args=(urls,),
                             kwargs={"label": label}, daemon=True).start()

    def _test_selected(self) -> None:
        self._test_async(self._selected_urls(), "selected proxies")

    def _test_all(self) -> None:
        self._test_async(list(self.store.proxies), "full proxy list")

    def _remove(self, urls: list[str], what: str) -> None:
        if not urls:
            return
        affected = sorted({a for u in urls for a in self.store.users_of(u)})
        if affected:
            spare = len(self.store.healthy()) > len([u for u in urls if self.store.is_ok(u)])
            where = "another working proxy" if spare else "a DIRECT connection (no working proxy left)"
            if QMessageBox.question(
                self, "Remove", f"Remove {what}? {', '.join(affected)} will move to {where}. Continue?"
            ) != QMessageBox.StandardButton.Yes:
                return
        self.store.remove(urls)
        moved = self.store.auto_assign(affected)
        self._push_live(set(affected) | set(moved))
        self._refresh()

    def _remove_selected(self) -> None:
        self._remove(self._selected_urls(), "the selected proxies")

    def _remove_failed(self) -> None:
        failed = [p for p in self.store.proxies if self.store.status(p) == "failed"]
        if not failed:
            QMessageBox.information(self, "Remove Failed", "No failed proxies.")
            return
        self._remove(failed, f"{len(failed)} failed proxies")

    def _clear_all(self) -> None:
        if not self.store.proxies:
            return
        if QMessageBox.question(self, "Clear", "Remove every proxy? All accounts go direct.") \
                != QMessageBox.StandardButton.Yes:
            return
        affected = set(self.store.assignments)
        self.store.clear()
        self._push_live(affected)
        self._refresh()

    # -- assignments -------------------------------------------------------
    def _refresh_assignments(self) -> None:
        self._assign_sig_drawn = self._assign_sig()
        self._last_assign_rebuild = time.monotonic()
        names = list(self.win.nodes) + [n for n in self.win.offline_accounts if n not in self.win.nodes]
        healthy = self.store.healthy()
        self.tbl_assign.setRowCount(len(names))
        for r, name in enumerate(names):
            off = self.win.offline_accounts.get(name)
            label = QTableWidgetItem(name + (f"  ({off['error']})" if off else ""))
            label.setData(Qt.ItemDataRole.UserRole, name)
            self.tbl_assign.setItem(r, 0, label)
            combo = QComboBox()
            combo.addItem("direct (no proxy)", "")
            for url in healthy:
                combo.addItem("✓ " + mask(url), url)
            cur = self.store.get(name) or ""
            if cur and cur not in healthy:
                # Show the current (non-working) assignment so it's visible,
                # but it can't be picked for anyone else.
                combo.addItem(f"✗ {mask(cur)} ({self.store.status(cur)})", cur)
            combo.setCurrentIndex(max(0, combo.findData(cur)))
            self.tbl_assign.setCellWidget(r, 1, combo)

    def _accounts(self) -> list[str]:
        return list(self.win.nodes) + [n for n in self.win.offline_accounts if n not in self.win.nodes]

    def _auto_assign(self) -> None:
        if not self.store.healthy():
            msg = ("Proxies are still being tested — try again in a moment."
                   if self.store.testing else
                   "No proxies have passed a test yet. Import some and/or click Test All.")
            QMessageBox.information(self, "Auto-assign", msg)
            return
        new = self.store.auto_assign(self._accounts())
        self._push_live(set(new))
        self._refresh()
        QMessageBox.information(self, "Auto-assign",
                                f"Assigned {len(new)} account(s)." if new else "Nothing to change.")

    def _apply(self) -> None:
        changed = set()
        for r in range(self.tbl_assign.rowCount()):
            name = self.tbl_assign.item(r, 0).data(Qt.ItemDataRole.UserRole)
            url = self.tbl_assign.cellWidget(r, 1).currentData() or None
            if (self.store.get(name) or None) != url:
                if url and not self.store.is_ok(url):
                    continue  # only working proxies can be newly assigned
                self.store.assign(name, url)
                changed.add(name)
        self._push_live(changed)
        self._refresh()

    def _push_live(self, accounts: set[str]) -> None:
        for name in accounts:
            n = self.win.nodes.get(name)
            if n:
                n.node.set_proxy(self.store.get(name))
            elif name in self.win.offline_accounts:
                # Retry start-up through the newly chosen route (background).
                self.win.start_account_async(name, self.win.offline_accounts[name]["token"])


class StatTile(QFrame):
    """Caption + big number, used across the top of the Home page."""

    def __init__(self, text: str, hot: bool = False):
        super().__init__()
        self.setObjectName("tileHot" if hot else "tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        self.caption = caption(text)
        self.value = QLabel("—")
        self.value.setObjectName("tileValue")
        lay.addWidget(self.caption)
        lay.addWidget(self.value)

    def set(self, value: str, color: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")


NODE_STATE_COLORS = {
    "Running": C_OK,
    "Following": C_FOLLOW,
    "Idle": C_WARN,
    "Starting": C_WARN,
    "Waiting": C_WARN,
    "Failed": C_FAIL,
}


class HomePage(QWidget):
    """Front page: brand, proxy check progress, and every node at a glance."""

    COLS = ["Node", "State", "Network", "Watching", "Drop", "Progress", "Credited (1h)"]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setObjectName("home")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(12)

        # ---- stat tiles ----
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.t_nodes = StatTile("Active nodes")
        self.t_proxies = StatTile("Working proxies")
        self.t_watching = StatTile("Watching now")
        self.t_ready = StatTile("Drops ready to claim", hot=True)
        for t in (self.t_nodes, self.t_proxies, self.t_watching, self.t_ready):
            tiles.addWidget(t)
        root.addLayout(tiles)

        mid = QHBoxLayout()
        mid.setSpacing(12)

        # ---- games to mine (one list, every node works through it) ----
        games = Card("Games to mine", "All nodes, in order")
        add_row = QHBoxLayout()
        self.combo_games = QComboBox()
        self.combo_games.setEditable(True)  # pick a game that has drops, or type any game
        self.combo_games.setMinimumWidth(160)
        self.btn_add_game = styled_button("Add to list")
        self.btn_add_game.clicked.connect(win._add_game)
        add_row.addWidget(self.combo_games, 1)
        add_row.addWidget(self.btn_add_game)
        games.body.addLayout(add_row)
        self.list_games = QListWidget()
        self.list_games.setMinimumHeight(84)
        games.body.addWidget(self.list_games, 1)
        game_btn_row = QHBoxLayout()
        self.btn_remove_game = styled_button("Remove", "danger")
        self.btn_remove_game.clicked.connect(win._remove_game)
        self.btn_mine_game = styled_button("Mine now")
        self.btn_mine_game.setToolTip("Switch every node to the selected game right away. Restarts the hold timer.")
        self.btn_mine_game.clicked.connect(win._mine_game_now)
        game_btn_row.addWidget(self.btn_remove_game)
        game_btn_row.addStretch(1)
        game_btn_row.addWidget(self.btn_mine_game)
        games.body.addLayout(game_btn_row)
        self.lbl_games_hint = QLabel("")
        self.lbl_games_hint.setObjectName("muted")
        self.lbl_games_hint.setWordWrap(True)
        games.body.addWidget(self.lbl_games_hint)
        mid.addWidget(games, 3)

        side = QVBoxLayout()
        side.setSpacing(12)

        # ---- settings ----
        opts_card = Card("Settings")
        self.chk_auto_claim = QCheckBox("Auto-claim rewards")
        self.chk_auto_claim.setToolTip("Claim every finished drop automatically on all nodes.")
        self.chk_auto_claim.setChecked(win.settings.auto_claim)
        self.chk_auto_claim.toggled.connect(win._set_auto_claim)
        self.chk_strict = QCheckBox("Stay on target game")
        self.chk_strict.setToolTip("Leave a channel when its stream ends or the streamer switches game. "
                                   "Never follows a raid/host.")
        self.chk_strict.setChecked(win.settings.strict_game)
        self.chk_strict.toggled.connect(win._set_strict_game)
        opts_card.body.addWidget(self.chk_auto_claim)
        opts_card.body.addWidget(self.chk_strict)
        self.btn_check_update = styled_button("Check for updates", "ghost")
        self.btn_check_update.clicked.connect(lambda: win._check_for_updates(manual=True))
        opts_card.body.addWidget(self.btn_check_update)
        self.lbl_version = QLabel(f"v{APP_VERSION}")
        self.lbl_version.setObjectName("muted")
        self.lbl_version.setToolTip("The version you're running. Checks GitHub for newer on launch.")
        opts_card.body.addWidget(self.lbl_version)
        side.addWidget(opts_card)

        # ---- proxy progress ----
        prox_card = Card("Proxies")
        self.lbl_proxy_stat = QLabel("")
        self.lbl_proxy_stat.setObjectName("mono")
        prox_card.head.addWidget(self.lbl_proxy_stat)
        self.proxy_bar = ThinBar(height=8, label=self.lbl_proxy_stat, margins=0)
        prox_card.body.addWidget(self.proxy_bar)
        self.lbl_phase = QLabel("")
        self.lbl_phase.setObjectName("muted")
        self.lbl_phase.setWordWrap(True)
        prox_card.body.addWidget(self.lbl_phase)
        side.addWidget(prox_card)
        side.addStretch(1)
        mid.addLayout(side, 2)
        root.addLayout(mid)

        # ---- nodes ----
        root.addSpacing(8)
        root.addWidget(caption("Nodes"))
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setObjectName("homeNodes")
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tune_table(self.tbl)
        hh = self.tbl.horizontalHeader()
        for i, mode in enumerate([QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Stretch,
                                  QHeaderView.ResizeMode.Stretch,
                                  QHeaderView.ResizeMode.Fixed,
                                  QHeaderView.ResizeMode.ResizeToContents]):
            hh.setSectionResizeMode(i, mode)
        self.tbl.setColumnWidth(5, 180)
        self.tbl.doubleClicked.connect(self._open_node)
        root.addWidget(self.tbl, 1)
        self.lbl_empty = QLabel("No accounts yet — click “+ Add account” to log one in.")
        self.lbl_empty.setObjectName("muted")
        root.addWidget(self.lbl_empty)
        hint = QLabel("Double-click a node to open its drops.")
        hint.setObjectName("muted")
        root.addWidget(hint)

        self._rows: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _open_node(self, index) -> None:
        name = self.tbl.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
        if name in self.win.nodes:
            items = self.win.account_list_widget.findItems(name, Qt.MatchFlag.MatchExactly)
            if items:
                self.win.account_list_widget.setCurrentItem(items[0])
            self.win.tabs_main.setCurrentIndex(1)

    @staticmethod
    def _offline_state(err: str) -> str:
        e = err.lower()
        if e.startswith("starting"):
            return "Starting"
        if e.startswith("waiting"):
            return "Waiting"
        return "Failed"

    def _row_data(self, name: str) -> dict:
        store = self.win.proxy_store
        proxy = store.get(name)
        net = mask(proxy) if proxy else "direct"
        if proxy and proxy in store.latency:
            net += f"  ·  {store.latency[proxy]} ms"
        if name in self.win.nodes:
            st = self.win.nodes[name].state
            drop = st.active_drop
            watching = f"{st.current_channel} — {st.current_game}" if st.current_channel else st.status
            if st.following and st.current_channel:
                watching = f"↪ following {st.current_channel} — {st.current_game}"
            pct = drop.percentage if drop else None
            watched, credited = st.credit_last_hour()
            if drop is None:
                # Nothing left to mine on this node, so there is no credit to judge.
                credit, ccol = "no drops to check", None
            elif watched < 1:
                credit, ccol = "—", None
            else:
                ratio = credited / watched
                credit = f"{credited}/{watched:.0f} min"
                ccol = C_OK if ratio >= 0.8 else C_WARN if ratio >= 0.4 else C_FAIL
            if st.proxy_swaps and drop is not None:
                credit += f" · {st.proxy_swaps} swap{'s' if st.proxy_swaps != 1 else ''}"
            return {
                "credit": credit, "ccol": ccol,
                "state": ("Following" if st.following else "Running") if st.current_channel else "Idle",
                "net": net,
                "watching": watching,
                "drop": f"{drop.drop_name} ({drop.current_minutes}/{drop.required_minutes} min)" if drop else "—",
                "pct": pct,
                "tip": st.last_error or st.status,
            }
        off = self.win.offline_accounts.get(name, {})
        err = off.get("error", "")
        return {"state": self._offline_state(err), "net": net, "watching": err,
                "drop": "—", "pct": None, "tip": err, "credit": "—", "ccol": None}

    # -- refresh (called every second by MainWindow) ------------------------
    def refresh(self) -> None:
        store = self.win.proxy_store
        win = self.win

        # tiles
        names = list(win.nodes) + [n for n in win.offline_accounts if n not in win.nodes]
        running = len(win.nodes)
        self.t_nodes.set(f"{running}/{len(names)}" if names else "0",
                         C_OK if names and running == len(names) else None)
        healthy = len(store.healthy())
        self.t_proxies.set(f"{healthy}/{len(store.proxies)}" if store.proxies else "—")
        watching = sum(1 for n in win.nodes.values() if n.state.current_channel)
        self.t_watching.set(str(watching))
        ready = sum(1 for n in win.nodes.values() for d in n.state.drops if d.is_ready and not d.is_claimed)
        self.t_ready.set(str(ready))

        # proxy progress
        in_flight = len(store.testing)
        if not store.proxies:
            self.proxy_bar.setValue(0)
            self.proxy_bar.setFormat("no proxies imported")
            self.lbl_phase.setText("All nodes use a direct connection. Import your own list in the Proxies tab if needed.")
        elif in_flight or win.proxy_check_running:
            total = max(store.batch_total, 1)
            done = min(store.batch_done, total)
            self.proxy_bar.setValue(int(done / total * 1000))
            self.proxy_bar.setFormat(f"testing {done}/{store.batch_total}")
            self.lbl_phase.setText(
                f"Testing {store.batch_label or 'proxies'} — {healthy} working so far, "
                f"{in_flight} in flight"
            )
        else:
            tested = len(store.health)
            failed = tested - healthy
            untested = len(store.proxies) - tested
            self.proxy_bar.setValue(1000 if tested else 0)
            self.proxy_bar.setFormat(f"{healthy} working · {failed} failed" if tested else "not tested yet")
            extra = f" · {untested} not re-tested (Test all in the Proxies tab to recheck)" if untested else ""
            fastest = min(store.latency.values()) if store.latency else None
            self.lbl_phase.setText(
                ("Proxy check done" if tested else "Waiting to test")
                + (f" · fastest {fastest} ms" if fastest is not None else "") + extra
            )

        # nodes table
        self.lbl_empty.setVisible(not names)
        if names != self._rows:
            self._rows = names
            self.tbl.setRowCount(len(names))
            for r, n in enumerate(names):
                for c in range(len(self.COLS)):
                    self.tbl.setItem(r, c, QTableWidgetItem(""))
                self.tbl.item(r, 0).setData(Qt.ItemDataRole.UserRole, n)
                self.tbl.setCellWidget(r, 5, ThinBar())
        for r, n in enumerate(names):
            d = self._row_data(n)
            self.tbl.item(r, 0).setText(n)
            st_item = self.tbl.item(r, 1)
            st_item.setText("● " + d["state"])
            st_item.setForeground(QColor(NODE_STATE_COLORS.get(d["state"], C_MUTED)))
            self.tbl.item(r, 2).setText(d["net"])
            self.tbl.item(r, 3).setText(d["watching"])
            self.tbl.item(r, 3).setToolTip(d["tip"])
            self.tbl.item(r, 4).setText(d["drop"])
            c_item = self.tbl.item(r, 6)
            c_item.setText(d["credit"])
            c_item.setForeground(QColor(d["ccol"] or C_MUTED))
            c_item.setToolTip("Minutes Twitch credited vs minutes watched with segments accepted, last 60 min. "
                              "Green ≥80%, yellow ≥40%, red below.")
            bar = self.tbl.cellWidget(r, 5)
            if d["pct"] is None:
                bar.setValue(0)
                bar.setFormat("—")
            else:
                bar.setValue(int(d["pct"] * 10))
                bar.setFormat(f"{d['pct']:.0f}%")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mournyl TwitchMiner")
        self.resize(1100, 820)

        self.settings = Settings()
        self.proxy_store = ProxyStore()
        self._failover = make_failover(self.proxy_store)
        self.nodes: dict[str, AccountNode] = {}
        # Accounts that couldn't start (network/proxy failure). Kept so they
        # are still saved to accounts.json and can be retried after fixing
        # their proxy in the Proxies window: name -> {"token", "error"}.
        self.offline_accounts: dict[str, dict] = {}
        self.active_node_name: str | None = None
        # Signature of what's currently drawn in the drops table, so it's
        # only rebuilt when the data changes (keeps the selection intact).
        self._drops_table_sig: tuple | None = None

        # Background threads never touch widgets or the account dicts
        # directly — they post callables here, drained on the UI thread.
        self._ui_calls: "queue.Queue" = queue.Queue()
        self.proxy_check_running = False
        # Drop reward pictures: url -> QPixmap (None = failed). Downloaded
        # once in the background and cached in data/drop_images/.
        self._drop_pix: dict[str, QPixmap | None] = {}
        self._drop_pix_loading: set[str] = set()
        self._drop_img_slots = threading.Semaphore(4)  # at most 4 downloads at once
        self._drop_img_shown = ""
        self._games_list_sig: tuple | None = None
        # Live-streamer check for the games-to-mine list: game(lower) -> bool
        # (True has live streamers, False none, missing = not checked yet).
        self._game_live: dict[str, bool] = {}
        self._game_live_checked = 0.0
        self._game_live_running = False
        self._tray_hint_shown = False

        self._build_ui()
        self._build_tray()

        # UI Refresher Timer — ticks every second so the drop-progress bar
        # can interpolate smoothly between real inventory refreshes.
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui_state)
        self.timer.start(1000)
        self._ui_timer = QTimer()
        self._ui_timer.timeout.connect(self._drain_ui_calls)
        self._ui_timer.start(150)
        # Quiet check for a newer GitHub release shortly after startup.
        QTimer.singleShot(4000, lambda: self._check_for_updates(manual=False))

        # App first, proxies second: the window is up before any network
        # work; account start-up and the proxy check run in the background.
        QTimer.singleShot(0, self._load_saved_accounts)

    # ---- UI-thread marshalling ------------------------------------------
    def _post(self, fn, *args) -> None:
        self._ui_calls.put((fn, args))

    def _drain_ui_calls(self) -> None:
        while True:
            try:
                fn, args = self._ui_calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception as e:
                print(f"ui call failed: {e}", file=sys.stderr)

    def _build_ui(self) -> None:
        # ================= Drops page =================
        # Nodes down the side (each with the drop it is working on), the
        # selected node's active drop and full drops table on the right.
        drops_page = QWidget()
        drops_row = QHBoxLayout(drops_page)
        drops_row.setContentsMargins(28, 22, 28, 22)
        drops_row.setSpacing(22)

        drops_left = QVBoxLayout()
        drops_left.setSpacing(10)
        drops_left.addWidget(caption("Nodes"))
        self.drops_node_list = QListWidget()
        self.drops_node_list.setMinimumWidth(190)
        self.drops_node_list.setWordWrap(True)
        self.drops_node_list.currentRowChanged.connect(self._sync_node_row)
        drops_left.addWidget(self.drops_node_list, 1)

        drops_right = QVBoxLayout()
        drops_right.setSpacing(14)

        # Drop Progress Panel
        progress_box = Card("Active drop")
        progress_layout = progress_box.body
        name_row = QHBoxLayout()
        name_row.setSpacing(12)
        self.lbl_drop_img = QLabel()
        self.lbl_drop_img.setFixedSize(48, 48)
        self.lbl_drop_img.setVisible(False)
        name_row.addWidget(self.lbl_drop_img)
        self.lbl_drop_name = QLabel("Current Reward: None")
        self.lbl_drop_name.setObjectName("dropName")
        self.lbl_pct = QLabel("")
        self.lbl_pct.setObjectName("bigPct")
        name_row.addWidget(self.lbl_drop_name, 1)
        name_row.addWidget(self.lbl_pct)

        # Scaled range (0-1000) instead of 0-100 gives room for a smooth,
        # sub-percent fill so the bar visibly creeps every second instead
        # of jumping once every few minutes. The percentage is shown in
        # lbl_pct (ThinBar.setFormat writes there).
        self.progress_bar = ThinBar(height=12, label=self.lbl_pct, margins=0)
        self.progress_bar.setValue(0)

        text_row = QHBoxLayout()
        self.lbl_progress_text = QLabel("0 / 0 minutes (0%)")
        self.lbl_progress_text.setObjectName("mono")
        self.lbl_eta = QLabel("")
        self.lbl_eta.setObjectName("monoMuted")
        text_row.addWidget(self.lbl_progress_text)
        text_row.addStretch(1)
        text_row.addWidget(self.lbl_eta)

        progress_layout.addLayout(name_row)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addLayout(text_row)
        drops_right.addWidget(progress_box)

        # All drops for the selected node, with progress and claim controls.
        drops_right.addWidget(caption("All drops"))
        self.table_drops = QTableWidget(0, 5)
        self.table_drops.setHorizontalHeaderLabels(["Game", "Drop", "Progress", "Minutes", "Status"])
        self.table_drops.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_drops.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_drops.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_drops.verticalHeader().setVisible(False)
        tune_table(self.table_drops)
        header = self.table_drops.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_drops.setColumnWidth(2, 180)
        self.table_drops.setIconSize(QSize(30, 30))
        drops_right.addWidget(self.table_drops, 1)

        drops_btn_row = QHBoxLayout()
        self.lbl_drops_summary = QLabel("")
        self.lbl_drops_summary.setObjectName("muted")
        drops_btn_row.addWidget(self.lbl_drops_summary)
        drops_btn_row.addStretch(1)
        self.btn_refresh_drops = styled_button("Refresh drops", "ghost")
        self.btn_refresh_drops.clicked.connect(self._refresh_drops_now)
        self.btn_claim_selected = styled_button("Claim selected")
        self.btn_claim_selected.clicked.connect(self._claim_selected)
        self.btn_claim_ready = styled_button("Claim ready (this node)")
        self.btn_claim_ready.clicked.connect(self._claim_ready_this_node)
        self.btn_claim_ready_all = styled_button("Claim ready (all nodes)", "primary")
        self.btn_claim_ready_all.clicked.connect(self._claim_ready_all_nodes)
        for b in (self.btn_refresh_drops, self.btn_claim_selected,
                  self.btn_claim_ready, self.btn_claim_ready_all):
            drops_btn_row.addWidget(b)
        drops_right.addLayout(drops_btn_row)

        drops_row.addLayout(drops_left, 1)
        drops_row.addLayout(drops_right, 3)

        # ================= Node settings page =================
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(28, 22, 28, 22)
        main_layout.setSpacing(22)

        # Left Panel: Node Selection & Login controls
        left_layout = QVBoxLayout()
        left_layout.setSpacing(10)
        left_layout.addWidget(caption("Account nodes"))

        self.account_list_widget = QListWidget()
        self.account_list_widget.currentItemChanged.connect(self._on_node_selected)
        left_layout.addWidget(self.account_list_widget, 1)

        self.lbl_offline = QLabel("")
        self.lbl_offline.setWordWrap(True)
        self.lbl_offline.setObjectName("warn")
        left_layout.addWidget(self.lbl_offline)

        self.lbl_proxycheck = QLabel("")
        self.lbl_proxycheck.setWordWrap(True)
        self.lbl_proxycheck.setObjectName("muted")
        left_layout.addWidget(self.lbl_proxycheck)

        self.add_acc_btn = styled_button("+ Add Account Node", "primary")
        self.add_acc_btn.clicked.connect(self._add_account_dialog)
        left_layout.addWidget(self.add_acc_btn)

        # Right Panel: everything that configures the selected node
        right_layout = QVBoxLayout()
        right_layout.setSpacing(14)

        self.info_box = Card("Node")
        info_layout = self.info_box.body
        info_layout.setSpacing(4)
        self.lbl_status = QLabel("Select or add an account node to begin.")
        self.lbl_status.setObjectName("nodeTitle")
        self.lbl_watching = QLabel("Now Watching: None")
        self.lbl_watching.setObjectName("soft")
        info_layout.addWidget(self.lbl_status)
        info_layout.addWidget(self.lbl_watching)
        self.lbl_proxy = QLabel("Network: direct (no proxy)")
        self.lbl_proxy.setObjectName("monoMuted")
        info_layout.addWidget(self.lbl_proxy)
        self.lbl_error = QLabel("")
        self.lbl_error.setObjectName("error")
        self.lbl_error.setWordWrap(True)
        info_layout.addWidget(self.lbl_error)
        right_layout.addWidget(self.info_box)

        # Channel Override — type any channel and watch it directly,
        # on this node or on every node at once.
        override_box = Card("Channel override")
        override_layout = override_box.body
        self.input_channel = QLineEdit()
        self.input_channel.setPlaceholderText("channel name or twitch.tv/... URL")
        self.input_channel.returnPressed.connect(self._override_this_node)
        override_layout.addWidget(self.input_channel)
        override_row = QHBoxLayout()
        self.btn_override_node = styled_button("Watch (this node)")
        self.btn_override_node.clicked.connect(self._override_this_node)
        self.btn_override_all = styled_button("Watch (all nodes)")
        self.btn_override_all.clicked.connect(self._override_all_nodes)
        self.btn_clear_all = styled_button("Clear all (go auto)", "ghost")
        self.btn_clear_all.clicked.connect(self._clear_all_nodes)
        override_row.addWidget(self.btn_override_node)
        override_row.addWidget(self.btn_override_all)
        override_row.addWidget(self.btn_clear_all)
        override_row.addStretch(1)
        override_layout.addLayout(override_row)
        self.lbl_override = QLabel("Override: none (auto)")
        self.lbl_override.setObjectName("muted")
        override_layout.addWidget(self.lbl_override)
        right_layout.addWidget(override_box)

        # Live Streamers — who's currently live for the node's game, so
        # you can hand-pick a channel instead of typing a login blind.
        streamers_box = Card("Live streamers", "Live now for this node's game")
        self.list_streamers = QListWidget()
        streamers_box.body.addWidget(self.list_streamers, 1)
        streamers_btn_row = QHBoxLayout()
        self.btn_refresh_streamers = styled_button("Refresh list", "ghost")
        self.btn_refresh_streamers.clicked.connect(self._refresh_streamer_list)
        self.btn_clear_manual_channel = styled_button("Clear (go auto)")
        self.btn_clear_manual_channel.clicked.connect(self._clear_manual_channel)
        self.btn_watch_selected = styled_button("Watch selected", "primary")
        self.btn_watch_selected.clicked.connect(self._watch_selected_streamer)
        streamers_btn_row.addStretch(1)
        streamers_btn_row.addWidget(self.btn_refresh_streamers)
        streamers_btn_row.addWidget(self.btn_clear_manual_channel)
        streamers_btn_row.addWidget(self.btn_watch_selected)
        streamers_box.body.addLayout(streamers_btn_row)
        right_layout.addWidget(streamers_box, 1)

        # Node log — collapsed by default. Toggle it on when you need to dig in.
        self.btn_toggle_log = styled_button("Show Node Log ▾", "ghost")
        self.btn_toggle_log.clicked.connect(self._toggle_log)
        right_layout.addWidget(self.btn_toggle_log, 0, Qt.AlignmentFlag.AlignLeft)

        self.log_box = Card("Node log")
        self.log_text = QTextEdit()
        self.log_text.setObjectName("log")
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(160)
        self.log_box.body.addWidget(self.log_text)
        self.log_box.setVisible(False)
        right_layout.addWidget(self.log_box)

        main_layout.addLayout(left_layout, 1)
        main_layout.addLayout(right_layout, 3)

        container = QWidget()
        container.setLayout(main_layout)

        # ================= Tabs =================
        self.home = HomePage(self)
        self.proxy_page = ProxyPage(self)
        self.tabs_main = QTabWidget()
        self.tabs_main.setObjectName("mainTabs")
        self.tabs_main.addTab(self.home, "Home")
        self.tabs_main.addTab(drops_page, "Drops")
        self.tabs_main.addTab(container, "Node settings")
        self.tabs_main.addTab(self.proxy_page, "Proxies")

        # Header: wordmark left of the tabs, global actions on the right.
        brand = QWidget()
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(28, 8, 24, 8)
        brand_row.setSpacing(10)
        wordmark = QLabel("mournyl")
        wordmark.setObjectName("wordmark")
        sub = QLabel("MINER")
        sub.setObjectName("wordmarkSub")
        sub_font = sub.font()
        sub_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.4)
        sub.setFont(sub_font)
        brand_row.addWidget(wordmark)
        brand_row.addWidget(sub, 0, Qt.AlignmentFlag.AlignBottom)
        self.tabs_main.setCornerWidget(brand, Qt.Corner.TopLeftCorner)

        actions = QWidget()
        actions_row = QHBoxLayout(actions)
        actions_row.setContentsMargins(0, 8, 28, 8)
        actions_row.setSpacing(8)
        btn_add = styled_button("+ Add account", "primary")
        btn_add.clicked.connect(self._add_account_dialog)
        actions_row.addWidget(btn_add)
        self.tabs_main.setCornerWidget(actions, Qt.Corner.TopRightCorner)

        self.setCentralWidget(self.tabs_main)
        self.tabs_main.currentChanged.connect(self._on_main_tab_changed)

    def _on_main_tab_changed(self, index: int) -> None:
        if index == 0:            # Home
            self.home.refresh()
        elif index == 2:          # Node settings — refresh its live-streamer list
            self._refresh_streamer_list()

    def _sync_node_row(self, row: int) -> None:
        """Drops-tab node list -> Node-settings list (the one that drives selection)."""
        if row >= 0 and self.account_list_widget.currentRow() != row:
            self.account_list_widget.setCurrentRow(row)

    # ---- drop reward pictures ----------------------------------------------
    def _drop_pixmap(self, url: str) -> QPixmap | None:
        """Cached picture for a drop, or None while it loads / if there is none."""
        if not url:
            return None
        if url in self._drop_pix:
            return self._drop_pix[url]
        if url not in self._drop_pix_loading:
            self._drop_pix_loading.add(url)
            threading.Thread(target=self._fetch_drop_image, args=(url,), daemon=True).start()
        return None

    def _fetch_drop_image(self, url: str) -> None:
        """WORKER THREAD. Disk cache first, then a plain download from Twitch's
        image CDN (public file, no account or cookies involved)."""
        data = b""
        self._drop_img_slots.acquire()
        try:
            if not url.lower().startswith("https://"):
                raise ValueError("not an https url")
            cache = DATA_DIR / "drop_images" / (hashlib.sha1(url.encode()).hexdigest() + ".img")
            if cache.exists():
                data = cache.read_bytes()
            else:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.content
                if len(data) <= 2_000_000:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(data)
        except Exception:
            data = b""
        finally:
            self._drop_img_slots.release()
        self._post(self._drop_image_done, url, data)

    def _drop_image_done(self, url: str, data: bytes) -> None:
        pix = QPixmap()
        ok = bool(data) and pix.loadFromData(data)
        self._drop_pix[url] = pix if ok else None
        self._drop_pix_loading.discard(url)
        self._drops_table_sig = None  # redraw the drops table with the picture
        self._drop_img_shown = ""

    def _show_drop_image(self, url: str) -> None:
        pix = self._drop_pixmap(url) if url else None
        key = url if pix is not None else ""
        if key == self._drop_img_shown and (not self.lbl_drop_img.isHidden()) == bool(key):
            return
        self._drop_img_shown = key
        if pix is None:
            self.lbl_drop_img.setVisible(False)
            return
        self.lbl_drop_img.setPixmap(pix.scaled(
            48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.lbl_drop_img.setVisible(True)

    # ---- settings toggles (Home page) -------------------------------------
    def _set_auto_claim(self, on: bool) -> None:
        self.settings.auto_claim = bool(on)
        self.settings.save()
        for n in self.nodes.values():
            n.node.auto_claim = bool(on)
        if on:
            self._claim_ready_all_nodes()  # sweep whatever is claimable right now

    def _set_strict_game(self, on: bool) -> None:
        self.settings.strict_game = bool(on)
        self.settings.save()
        for n in self.nodes.values():
            if n.miner:
                n.miner.strict_game = bool(on)

    def _toggle_log(self) -> None:
        showing = not self.log_box.isVisible()
        self.log_box.setVisible(showing)
        self.btn_toggle_log.setText(("Hide" if showing else "Show") + " Node Log " + ("▴" if showing else "▾"))

    def _add_account_dialog(self) -> None:
        # If proxies are imported, the new account logs in through a proxy
        # that PASSED its test (least-used first) and stays on it.
        store = self.proxy_store
        proxy = store.next_free()
        if store.proxies and proxy is None:
            if self.proxy_check_running or store.testing:
                QMessageBox.information(self, "Add account",
                                        "Proxies are still being tested — try again in a moment.")
                return
            if QMessageBox.question(
                self, "Add account",
                "None of your proxies passed a test.\n\nLog this account in with a DIRECT connection?",
            ) != QMessageBox.StandardButton.Yes:
                return
        self.add_acc_btn.setEnabled(False)
        self.add_acc_btn.setText("Waiting for Twitch login… (see browser)")
        temp_name = f"temp-account-{len(self.nodes) + 1}"

        def work():
            try:
                token, uname = login(temp_name, open_browser=True, proxy=proxy)
            except Exception as e:
                self._post(self._login_done, None, describe_error(e) if proxy else str(e))
                return
            if proxy and not store.get(uname):
                store.assign(uname, proxy)
            self._post(self._login_done, uname, "")
            self._start_account(uname, token)

        threading.Thread(target=work, daemon=True).start()

    def _login_done(self, uname: str | None, error: str) -> None:
        self.add_acc_btn.setEnabled(True)
        self.add_acc_btn.setText("+ Add Account Node")
        if uname is None:
            QMessageBox.warning(self, "Login failed", error)

    # ---- account start-up (background) -----------------------------------
    def _set_offline(self, name: str, token: str, reason: str) -> None:
        if name not in self.nodes:
            self.offline_accounts[name] = {"token": token, "error": reason}

    def _drop_offline(self, name: str) -> None:
        self.offline_accounts.pop(name, None)
        self._save_accounts()

    def _register_node(self, acct: AccountNode) -> None:
        name = acct.account_name
        old = self.nodes.get(name)
        if old is not None:
            acct.stop()  # already running (double start) — keep the old one
            return
        self.nodes[name] = acct
        self.offline_accounts.pop(name, None)
        drops_item = QListWidgetItem(name)
        drops_item.setData(Qt.ItemDataRole.UserRole, name)
        self.drops_node_list.addItem(drops_item)
        item = QListWidgetItem(name)
        self.account_list_widget.addItem(item)
        if self.account_list_widget.count() == 1:
            self.account_list_widget.setCurrentItem(item)
        self._save_accounts()

    def _start_account(self, name: str, token: str) -> None:
        """WORKER THREAD. Validate + build + start one account through its
        assigned proxy. Refuses a proxy that hasn't passed a test."""
        proxy = self.proxy_store.get(name)
        if proxy and not self.proxy_store.is_ok(proxy):
            st = self.proxy_store.status(proxy)
            self._post(self._set_offline, name, token, f"proxy {st} — pick a working one")
            return
        self._post(self._set_offline, name, token, f"starting via {mask(proxy)}…")
        try:
            valid = validate_token(token, proxy)
        except Exception as e:
            self._post(self._set_offline, name, token, describe_error(e))
            return
        if not valid:
            self._post(self._set_offline, name, token, "token expired/revoked — re-add the account")
            return
        try:
            acct = AccountNode(name, token, proxy=proxy)
            acct.node.on_proxy_trouble = self._failover
            acct.node.peer_provider = self._peer_channel_for
            acct.node.want_images = True
            acct.state.game_list = list(self.settings.game_list)
            if acct.state.game_list:
                acct.state.selected_game = acct.state.game_list[0]
            acct.node.refresh_drops()
            acct.start(self.settings)
        except Exception as e:
            self._post(self._set_offline, name, token, describe_error(e))
            return
        self._post(self._register_node, acct)

    def _peer_channel_for(self, node) -> str | None:
        """MINER THREAD. Channel a running node is on, for an idle node to
        follow. Prefers peers actively mining a drop; never another follower."""
        try:
            peers = [n for n in list(self.nodes.values())
                     if n.node is not node and n.state.current_channel and not n.state.following]
        except RuntimeError:  # dict changed mid-iteration; try again next loop
            return None
        if not peers:
            return None
        peers.sort(key=lambda n: n.state.active_drop is None)
        return peers[0].state.current_channel

    def start_account_async(self, name: str, token: str) -> None:
        threading.Thread(target=self._start_account, args=(name, token), daemon=True).start()

    def _load_saved_accounts(self) -> None:
        if not ACCOUNTS_PATH.exists():
            saved = []
        else:
            try:
                saved = json.loads(ACCOUNTS_PATH.read_text("utf-8"))
            except Exception:
                saved = []
        entries = [(e.get("account_name", ""), e.get("token", "")) for e in saved]
        entries = [(n, t) for n, t in entries if n and t]
        store = self.proxy_store

        direct = [(n, t) for n, t in entries if not store.get(n)]
        proxied = [(n, t) for n, t in entries if store.get(n)]

        # Every saved account is tracked from the start, so a save triggered
        # by another account finishing can never drop one still starting.
        for n, t in direct:
            self.offline_accounts[n] = {"token": t, "error": "starting…"}
        for n, t in proxied:
            self.offline_accounts[n] = {"token": t, "error": "waiting for proxy check…"}

        def run_direct():
            for n, t in direct:
                self._start_account(n, t)

        def run_proxied():
            if store.proxies or store.load_working_quiet():
                self.proxy_check_running = True
                phase = {"name": "", "total": 0, "done": 0}

                def on_phase(name, count):
                    phase.update(name=name, total=count, done=0)
                    self._post(self.lbl_proxycheck.setText, f"Testing {name} proxies… 0/{count}")

                def progress(*_):
                    phase["done"] += 1
                    self._post(self.lbl_proxycheck.setText,
                               f"Testing {phase['name']} proxies… {phase['done']}/{phase['total']} "
                               f"({len(store.healthy())} working)")

                store.startup_test(on_result=progress, on_phase=on_phase)
                self.proxy_check_running = False
                tested = len(store.health)
                skipped = len(store.proxies) - tested
                self._post(self.lbl_proxycheck.setText,
                           f"Proxy check: {len(store.healthy())}/{tested} working"
                           + (f" ({skipped} not re-tested — Test all in the Proxies tab to recheck)" if skipped else ""))
            for n, t in proxied:
                cur = store.get(n)
                if not store.is_ok(cur):
                    replacement = store.next_free(exclude_account=n)
                    if replacement is None:
                        reason = store.health.get(cur, (False, "untested"))[1]
                        self._post(self._set_offline, n, t, f"no working proxy (assigned one failed: {reason})")
                        continue
                    store.assign(n, replacement)
                self._start_account(n, t)
            # Failed proxies get re-tested every minute (after a 3 min sit-out)
            # and rejoin the pool when they pass — nothing is thrown away.
            if store.proxies:
                store.start_recheck_loop()
            # Refill the pool from the rest of the list so it doesn't shrink
            # launch after launch. Accounts are already running by now.
            if store.proxies:
                from proxies import MIN_WORKING
                target = MIN_WORKING + 2 * len(entries)
                if len(store.healthy()) < target:
                    self.proxy_check_running = True
                    n_ok = store.top_up(target)
                    self.proxy_check_running = False
                    self._post(self.lbl_proxycheck.setText,
                               f"Proxy pool: {n_ok} working (target {target})")

        threading.Thread(target=run_direct, daemon=True).start()
        threading.Thread(target=run_proxied, daemon=True).start()

    def _save_accounts(self) -> None:
        try:
            data = [
                {"account_name": name, "token": node.token}
                for name, node in self.nodes.items()
            ] + [
                {"account_name": name, "token": info["token"]}
                for name, info in self.offline_accounts.items()
                if name not in self.nodes
            ]
            ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACCOUNTS_PATH.write_text(json.dumps(data, indent=2), "utf-8")
        except Exception:
            pass  # best-effort — never let this break startup/login

    def _on_node_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if not current:
            self.active_node_name = None
            return
        self.active_node_name = current.text()
        row = self.account_list_widget.row(current)
        if self.drops_node_list.currentRow() != row:
            self.drops_node_list.setCurrentRow(row)
        self._refresh_streamer_list()

    # ---- Games to mine (Home page; one list shared by every node) ----------
    def _refresh_games_dropdown(self) -> None:
        """Offer every game any node has drops for. Skipped while typing."""
        combo = self.home.combo_games
        if combo.lineEdit() and combo.lineEdit().hasFocus():
            return
        available = sorted({d.game_name for n in self.nodes.values()
                            for d in n.state.drops if d.game_name})
        if [combo.itemText(i) for i in range(combo.count())] == available:
            return
        typed = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(available)
        combo.setCurrentText(typed or (available[0] if available else ""))
        combo.blockSignals(False)

    def _push_game_list(self) -> None:
        self.settings.save()
        for n in self.nodes.values():
            n.state.game_list = list(self.settings.game_list)
        self._games_list_sig = None
        self._game_live_checked = 0.0  # re-check liveness promptly

    def _add_game(self) -> None:
        selected = self.home.combo_games.currentText().strip()
        if not selected:
            return
        # Match an existing drop game's spelling/case when there is one.
        known = {d.game_name.lower(): d.game_name
                 for n in self.nodes.values() for d in n.state.drops if d.game_name}
        selected = known.get(selected.lower(), selected)
        games = self.settings.game_list
        if selected.lower() in (g.lower() for g in games):
            return
        games.append(selected)
        if len(games) == 1:
            for n in self.nodes.values():
                n.state.selected_game = selected
                n.state.status = f"Switched to {selected}"
        self._push_game_list()

    def _selected_list_game(self) -> str:
        item = self.home.list_games.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _remove_game(self) -> None:
        game = self._selected_list_game()
        games = self.settings.game_list
        if not game or game not in games:
            return
        games.remove(game)
        for n in self.nodes.values():
            if n.state.selected_game == game:
                # Stop mining it now: first game left in the list, or back to auto.
                n.state.selected_game = games[0] if games else ""
                n.state.status = (f"Switched to {games[0]}" if games
                                  else "Game list empty — picking a game automatically")
        self._push_game_list()

    def _mine_game_now(self) -> None:
        game = self._selected_list_game()
        if not game:
            return
        for n in self.nodes.values():
            if n.state.selected_game != game:
                n.state.selected_game = game
                n.state.status = f"Switched to {game}"
                n.node._log(f"game list: switched to {game} by hand")
        self._games_list_sig = None

    def _update_games_list(self) -> None:
        games = self.settings.game_list
        lst = self.home.list_games
        counts = tuple(sum(1 for n in self.nodes.values() if n.state.selected_game == g) for g in games)
        live = tuple(self._game_live.get(g.lower()) for g in games)
        sig = (tuple(games), counts, live)
        if sig != self._games_list_sig:
            self._games_list_sig = sig
            keep = self._selected_list_game()
            lst.clear()
            for i, g in enumerate(games):
                is_live = live[i]
                if is_live is True:
                    color = C_OK
                    status = (f"{counts[i]} node{'s' if counts[i] != 1 else ''} mining"
                              if counts[i] else "live streamers")
                elif is_live is False:
                    color = C_FAIL
                    status = "game not found"
                else:
                    color = C_TEXT
                    status = "checking…" if self.nodes else ""
                item = QListWidgetItem(f"{i + 1}. {g}" + (f" — {status}" if status else ""))
                item.setData(Qt.ItemDataRole.UserRole, g)
                item.setForeground(QColor(color))
                lst.addItem(item)
                if g == keep:
                    lst.setCurrentItem(item)
        hold = self.settings.game_swap_minutes
        self.home.lbl_games_hint.setText(
            f"Every node mines these in order. A node holds a game at least {hold} min "
            "before swapping to the next, and only if it has stopped earning."
            if games else
            "List empty: each node picks a game on its own. Add games to control what gets mined.")

    def _refresh_streamer_list(self) -> None:
        if not self.active_node_name or self.active_node_name not in self.nodes:
            return
        node = self.nodes[self.active_node_name]
        target_game = node.state.selected_game or node.state.current_game
        if not target_game or node.state.live_streamers_loading:
            return
        node.state.live_streamers_loading = True

        def worker():
            try:
                _, channels = node.node.gql.get_live_channels_for_game(target_game)
                parsed = []
                for ch in channels:
                    b = ch.get("broadcaster", {})
                    login = b.get("login", "")
                    if not login:
                        continue
                    parsed.append({
                        "login": login,
                        "display_name": b.get("displayName") or login,
                        "viewers": ch.get("viewersCount", 0),
                    })
                node.state.live_streamers = parsed
                node.state.live_streamers_game = target_game
            except Exception as e:
                node.node._log(f"failed to refresh streamer list for {target_game}: {e}")
            finally:
                node.state.live_streamers_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _watch_selected_streamer(self) -> None:
        if not self.active_node_name or self.active_node_name not in self.nodes:
            return
        item = self.list_streamers.currentItem()
        if not item:
            return
        login = item.data(Qt.ItemDataRole.UserRole)
        if not login:
            return
        node = self.nodes[self.active_node_name]
        node.state.following = False
        node.state.manual_channel = login
        node.state.status = f"Switching to {login}..."

    def _clear_manual_channel(self) -> None:
        if not self.active_node_name or self.active_node_name not in self.nodes:
            return
        node = self.nodes[self.active_node_name]
        node.state.following = False
        node.state.manual_channel = ""
        node.state.status = "Switching back to automatic channel selection..."

    # ---- Channel override ------------------------------------------------

    def _active_node(self) -> AccountNode | None:
        if not self.active_node_name:
            return None
        return self.nodes.get(self.active_node_name)

    def _read_override_input(self) -> str:
        login = parse_channel_input(self.input_channel.text())
        if not login:
            self.lbl_override.setText("Enter a channel name first.")
        return login

    @staticmethod
    def _set_manual(node: AccountNode, login: str) -> None:
        node.state.following = False
        node.state.manual_channel = login
        node.state.status = f"Switching to {login}..." if login else \
            "Switching back to automatic channel selection..."
        node.node._log(f"channel override set to {login}" if login else "channel override cleared")

    def _override_this_node(self) -> None:
        node = self._active_node()
        login = self._read_override_input()
        if not node or not login:
            return
        self._set_manual(node, login)

    def _override_all_nodes(self) -> None:
        login = self._read_override_input()
        if not login or not self.nodes:
            return
        for node in self.nodes.values():
            self._set_manual(node, login)

    def _clear_all_nodes(self) -> None:
        for node in self.nodes.values():
            if node.state.manual_channel:
                self._set_manual(node, "")

    # ---- Drops: refresh / claim -----------------------------------------

    @staticmethod
    def _bg(fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _refresh_drops_now(self) -> None:
        node = self._active_node()
        if node:
            self._bg(node.node.refresh_drops)

    def _claim_selected(self) -> None:
        node = self._active_node()
        if not node:
            return
        ids = {
            self.table_drops.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole)
            for idx in self.table_drops.selectionModel().selectedRows()
        }
        drops = [d for d in node.state.drops if d.drop_id in ids]
        if not drops:
            return

        def work():
            if any(node.node.claim(d) for d in drops):
                node.node.refresh_drops()

        self._bg(work)

    def _claim_ready_this_node(self) -> None:
        node = self._active_node()
        if not node:
            return

        def work():
            n = node.node.claim_ready_drops()
            node.node._log(f"claim sweep: {n} drop(s) claimed")
            node.node.refresh_drops()

        self._bg(work)

    def _claim_ready_all_nodes(self) -> None:
        for node in list(self.nodes.values()):
            def work(n=node):
                count = n.node.claim_ready_drops()
                n.node._log(f"claim sweep: {count} drop(s) claimed")
                n.node.refresh_drops()
            self._bg(work)

    def _update_drops_table(self, node: AccountNode) -> None:
        drops = sorted(node.state.drops, key=_drop_sort_key)
        sig = (node.account_name,) + tuple(
            (d.drop_id, d.current_minutes, d.required_minutes, d.is_claimed,
             d.image_url in self._drop_pix) for d in drops
        )
        if sig == self._drops_table_sig:
            return
        self._drops_table_sig = sig

        selected_ids = set()
        if self.table_drops.rowCount():
            for idx in self.table_drops.selectionModel().selectedRows():
                item = self.table_drops.item(idx.row(), 0)
                if item:
                    selected_ids.add(item.data(Qt.ItemDataRole.UserRole))

        self.table_drops.setRowCount(len(drops))
        ready_bg = QColor("#16241b")
        claimed_fg = QColor(C_MUTED)
        for row, d in enumerate(drops):
            if d.is_claimed:
                status = "Claimed"
            elif d.is_ready:
                status = "Ready to claim"
            elif d.current_minutes > 0:
                status = "In progress"
            else:
                status = "Not started"

            cells = [
                QTableWidgetItem(d.game_name),
                QTableWidgetItem(d.drop_name),
                QTableWidgetItem(""),
                QTableWidgetItem(f"{min(d.current_minutes, d.required_minutes)} / {d.required_minutes}"),
                QTableWidgetItem(status),
            ]
            cells[0].setData(Qt.ItemDataRole.UserRole, d.drop_id)
            cells[0].setToolTip(d.game_name)
            cells[1].setToolTip(d.drop_name)
            pix = self._drop_pixmap(d.image_url)
            if pix is not None:
                cells[1].setIcon(QIcon(pix))
            for col, item in enumerate(cells):
                if d.is_ready:
                    item.setBackground(ready_bg)
                elif d.is_claimed:
                    item.setForeground(claimed_fg)
                self.table_drops.setItem(row, col, item)
            if d.is_ready:
                cells[4].setForeground(QColor(C_OK))

            bar = ThinBar()
            bar.setValue(int(d.percentage * 10))
            bar.setFormat(f"{d.percentage:.0f}%")
            self.table_drops.setCellWidget(row, 2, bar)

            if d.drop_id in selected_ids:
                self.table_drops.selectRow(row)

        ready = sum(1 for d in drops if d.is_ready)
        in_prog = sum(1 for d in drops if not d.is_claimed and not d.is_ready and d.current_minutes > 0)
        claimed = sum(1 for d in drops if d.is_claimed)
        self.lbl_drops_summary.setText(
            f"{len(drops)} drops — {ready} ready to claim, {in_prog} in progress, {claimed} claimed"
        )

    # ---- games-to-mine liveness -------------------------------------------
    def _maybe_check_games_live(self) -> None:
        now = time.time()
        if (self.nodes and self.settings.game_list and not self._game_live_running
                and now - self._game_live_checked >= 60):
            self._game_live_checked = now
            self._game_live_running = True
            threading.Thread(target=self._check_games_live, daemon=True).start()

    def _check_games_live(self) -> None:
        """BACKGROUND. For each game in the list, is anyone live streaming it?
        Sets self._game_live[game]; the Home list then colors green/red."""
        try:
            node = next(iter(self.nodes.values()), None)
            if node is None:
                return
            gql = node.node.gql
            for g in list(self.settings.game_list):
                try:
                    _, channels = gql.get_live_channels_for_game(g)
                    self._game_live[g.lower()] = bool(channels)
                    node.node._log(f"game-live: {g} -> {len(channels)} live streamer(s)")
                except Exception as e:
                    node.node._log(f"game-live check failed for {g}: {e}")
        finally:
            self._game_live_running = False

    # ---- system tray ------------------------------------------------------
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        path = _app_icon_path()
        icon = QIcon(path) if path else self.windowIcon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("mournyl miner")
        menu = QMenu()
        menu.addAction("Show").triggered.connect(self._restore_from_tray)
        menu.addAction("Check for updates").triggered.connect(
            lambda: self._check_for_updates(manual=True))
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        if getattr(self, "tray", None):
            self.tray.hide()
        QApplication.quit()

    # ---- updates ----------------------------------------------------------
    def _check_for_updates(self, manual: bool = False) -> None:
        def worker():
            info = check_latest_release()
            self._post(self._update_check_done, info, manual)
        threading.Thread(target=worker, daemon=True).start()

    def _update_check_done(self, info, manual: bool) -> None:
        if not info:
            if manual:
                QMessageBox.information(self, "Updates",
                                        "Couldn't reach GitHub to check for updates.")
            return
        # The Home label always shows the version THIS build is (APP_VERSION).
        # If a newer release exists, note it alongside — never overwrite the
        # running version, or the label would misreport what's installed.
        if _version_tuple(info["version"]) > _version_tuple(APP_VERSION):
            self.home.lbl_version.setText(f"v{APP_VERSION}  (v{info['version']} available)")
        else:
            self.home.lbl_version.setText(f"v{APP_VERSION}")
        if _version_tuple(info["version"]) <= _version_tuple(APP_VERSION):
            if manual:
                QMessageBox.information(self, "Updates",
                                        f"You're on the latest version ({APP_VERSION}).")
            return
        newer = info["version"] or info["tag"]
        if not getattr(sys, "frozen", False):
            if QMessageBox.question(self, "Update available",
                    f"Version {newer} is available (you have {APP_VERSION}).\n\n"
                    "Update now? This pulls the latest from GitHub and restarts.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self._apply_source_update()
            return
        if not info["url"]:
            QMessageBox.information(self, "Update available",
                f"Version {newer} is available, but that release has no .zip to download.")
            return
        if QMessageBox.question(self, "Update available",
                f"Version {newer} is available (you have {APP_VERSION}).\n\n"
                "Download and install it now? The app will close and reopen.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            self._download_and_apply_update(info)

    def _download_and_apply_update(self, info) -> None:
        def worker():
            try:
                stage = self._stage_update(info["url"])
            except Exception as e:
                self._post(lambda e=e: QMessageBox.warning(
                    self, "Update failed", f"Could not download the update:\n{e}"))
                return
            self._post(self._launch_updater_and_quit, stage)
        threading.Thread(target=worker, daemon=True).start()

    def _stage_update(self, url: str) -> str:
        """Download the release zip and extract it into app_dir/update_tmp.
        Returns the path to the folder that holds the new exe + fonts."""
        app_dir = DATA_DIR.parent
        stage = app_dir / "update_tmp"
        shutil.rmtree(stage, ignore_errors=True)
        extracted = stage / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        zpath = stage / "update.zip"
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        # Validate the zip before touching anything.
        with zipfile.ZipFile(zpath) as z:
            if z.testzip() is not None:
                raise RuntimeError("downloaded update is corrupt")
            z.extractall(extracted)
        # Find the new exe wherever it landed (zip may wrap it in a folder).
        exe_name = Path(sys.executable).name
        matches = list(extracted.rglob(exe_name))
        if not matches:
            exes = list(extracted.rglob("*.exe"))
            if not exes:
                raise RuntimeError("update did not contain an .exe")
            matches = exes
        src_dir = matches[0].parent
        return str(src_dir)

    def _launch_updater_and_quit(self, src_dir: str) -> None:
        """Write a batch that waits for this exe to close, replaces it (and the
        fonts folder) with the downloaded copy, clears stale onefile temp
        folders, then relaunches — and quit so the batch can run."""
        app_dir = str(DATA_DIR.parent)
        exe = sys.executable
        exe_name = Path(exe).name
        stage = str(DATA_DIR.parent / "update_tmp")
        new_exe = str(Path(src_dir) / exe_name)
        bat = Path(tempfile.gettempdir()) / "mournyl_apply_update.bat"
        bat.write_text(
            "@echo off\r\n"
            "echo Updating mournyl miner...\r\n"
            # Bail out if the new exe somehow isn't there — never overwrite with nothing.
            f'if not exist "{new_exe}" (\r\n'
            '  echo Update files missing - aborting, your current version is untouched.\r\n'
            '  timeout /t 4 /nobreak >nul\r\n'
            f'  rmdir /s /q "{stage}" 2>nul\r\n'
            f'  start "" "{exe}"\r\n'
            '  del "%~f0"\r\n'
            '  exit /b\r\n'
            ')\r\n'
            ":wait\r\n"
            f'tasklist /fi "imagename eq {exe_name}" | find /i "{exe_name}" >nul\r\n'
            "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )\r\n"
            # Give Windows a moment to release the file lock after the process exits.
            "timeout /t 1 /nobreak >nul\r\n"
            # Replace the exe (retry a few times in case AV/lock is slow to clear).
            "set tries=0\r\n"
            ":copyexe\r\n"
            f'copy /y "{new_exe}" "{app_dir}\\{exe_name}" >nul\r\n'
            "if errorlevel 1 (\r\n"
            "  set /a tries+=1\r\n"
            "  if %tries% lss 5 ( timeout /t 1 /nobreak >nul & goto copyexe )\r\n"
            ")\r\n"
            # Refresh the fonts folder if the update shipped one.
            f'if exist "{src_dir}\\fonts" xcopy /E /Y /I "{src_dir}\\fonts" "{app_dir}\\fonts" >nul\r\n'
            f'if exist "{src_dir}\\assets" xcopy /E /Y /I "{src_dir}\\assets" "{app_dir}\\assets" >nul\r\n'
            # Clear stale onefile extraction folders so the new exe unpacks clean.
            'for /d %%D in ("%TEMP%\\_MEI*") do rmdir /s /q "%%D" 2>nul\r\n'
            f'rmdir /s /q "{stage}" 2>nul\r\n'
            f'start "" "{app_dir}\\{exe_name}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        self._quit_app()

    def _apply_source_update(self) -> None:
        """Running from source: pull the latest from the git clone and restart,
        with no need to open GitHub. Needs git and a .git folder."""
        app_dir = str(DATA_DIR.parent)
        if not ((Path(app_dir) / ".git").exists() and shutil.which("git")):
            QMessageBox.information(self, "Update",
                "Auto-update from source needs this folder to be a git clone with "
                "git installed. Run \"git pull\" here and restart.")
            return
        py = sys.executable
        script = str(Path(app_dir) / "app.py")
        pid = os.getpid()
        bat = Path(tempfile.gettempdir()) / "mournyl_src_update.bat"
        bat.write_text(
            "@echo off\r\n"
            "echo Updating mournyl TwitchMiner...\r\n"
            ":wait\r\n"
            f'tasklist /fi "pid eq {pid}" | find "{pid}" >nul\r\n'
            "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )\r\n"
            f'git -C "{app_dir}" pull\r\n'
            f'start "" "{py}" "{script}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000010)
        self._quit_app()

    def changeEvent(self, event) -> None:
        # Minimize sends the window to the tray instead of the taskbar.
        if (event.type() == QEvent.Type.WindowStateChange
                and getattr(self, "tray", None)
                and self.windowState() & Qt.WindowState.WindowMinimized):
            QTimer.singleShot(0, self.hide)
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "mournyl miner",
                    "Still running — click the tray icon to restore, or Quit from its menu.",
                    QSystemTrayIcon.MessageIcon.Information, 4000)
        super().changeEvent(event)

    def update_ui_state(self) -> None:
        if self.tabs_main.currentIndex() == 0:
            self.home.refresh()
            self._refresh_games_dropdown()
            self._update_games_list()
        self._maybe_check_games_live()
        for i in range(self.account_list_widget.count()):
            it = self.account_list_widget.item(i)
            n = self.nodes.get(it.text())
            if n:
                st = n.state
                state = ("Following" if st.following else "Running") if st.current_channel else "Idle"
                icon = dot_icon(NODE_STATE_COLORS[state])
                it.setIcon(icon)
                # Drops tab: same node, plus the drop it is working on.
                d_it = self.drops_node_list.item(i)
                if d_it:
                    drop = st.active_drop
                    if drop:
                        line = f"{drop.drop_name} · {drop.percentage:.0f}%"
                    elif st.following and st.current_channel:
                        line = f"following {st.current_channel}"
                    else:
                        line = "no drops to mine"
                    text = f"{it.text()}\n{line}"
                    if d_it.text() != text:
                        d_it.setText(text)
                        d_it.setToolTip(text)
                    d_it.setIcon(icon)
        if self.offline_accounts:
            self.lbl_offline.setText("Not running yet:\n" + "\n".join(
                f"• {n}: {i['error']}" for n, i in self.offline_accounts.items()
            ))
        else:
            self.lbl_offline.setText("")
        if not self.active_node_name or self.active_node_name not in self.nodes:
            self.lbl_status.setText("No active node selected.")
            self.lbl_watching.setText("Now Watching: None")
            self.lbl_drop_name.setText("Current Reward: None")
            self._show_drop_image("")
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
            self.lbl_progress_text.setText("0 / 0 minutes (0%)")
            self.lbl_eta.setText("")
            self.lbl_override.setText("Override: none (auto)")
            self.lbl_proxy.setText("Network: —")
            if self.table_drops.rowCount():
                self.table_drops.setRowCount(0)
                self._drops_table_sig = None
            return

        node = self.nodes[self.active_node_name]

        self._update_drops_table(node)
        overridden = [n.account_name for n in self.nodes.values()
                      if n.state.manual_channel and not n.state.following]
        if node.state.following:
            self.lbl_override.setText(f"Following a running node on <b>{node.state.manual_channel}</b> "
                                      "(this node has nothing of its own to watch — auto)")
        elif node.state.manual_channel:
            others = f" ({len(overridden)}/{len(self.nodes)} nodes overridden)" if len(self.nodes) > 1 else ""
            self.lbl_override.setText(f"Override: <b>{node.state.manual_channel}</b> on this node{others}")
        elif overridden:
            self.lbl_override.setText(f"Override: none on this node ({len(overridden)}/{len(self.nodes)} nodes overridden)")
        else:
            self.lbl_override.setText("Override: none (auto)")
        self.lbl_proxy.setText(f"Network: {mask(node.state.proxy or None)}")
        status_str = f"{node.state.status}" if node.state.connected else "Connecting..."
        self.lbl_status.setText(f"Node: <b>{node.account_name}</b> | Status: {status_str}")
        
        if node.state.current_channel:
            self.lbl_watching.setText(
                f"Now Watching: <b>{node.state.current_channel}</b> — {node.state.current_game}"
            )
        else:
            self.lbl_watching.setText(f"Now Watching: Idle ({node.state.status})")

        # Update Drop Progress Bar — interpolated every second rather than
        # only jumping when a real inventory refresh lands (every
        # campaign_refresh_interval, default 5 min). heartbeats_since_
        # baseline is a confirmed count of successful beacon POSTs since
        # the last real refresh; the fractional part animates progress
        # toward the *next*, not-yet-confirmed minute so the bar visibly
        # creeps every second instead of sitting still between whole-
        # minute ticks.
        active_drop = node.state.active_drop

        if active_drop and active_drop.required_minutes > 0:
            required = active_drop.required_minutes
            if node.state.watch_baseline_drop_id == active_drop.drop_id:
                confirmed = node.state.watch_baseline_minutes + node.state.heartbeats_since_baseline
            else:
                confirmed = active_drop.current_minutes
            confirmed = min(confirmed, required)

            watch_interval = node.miner.watch_interval if node.miner else self.settings.watch_interval
            is_actively_watching = bool(node.state.current_channel) and confirmed < required
            frac = 0.0
            if is_actively_watching and node.state.last_heartbeat_time:
                elapsed = time.time() - node.state.last_heartbeat_time
                frac = max(0.0, min(elapsed / max(watch_interval, 1), 1.0))

            smooth_minutes = min(confirmed + frac, required)
            percent = (smooth_minutes / required) * 100

            self.lbl_drop_name.setText(f"Current Reward: <b>{active_drop.drop_name}</b> ({active_drop.game_name})")
            self._show_drop_image(active_drop.image_url)
            self.progress_bar.setValue(int(percent * 10))
            self.progress_bar.setFormat(f"{percent:.1f}%")
            self.lbl_progress_text.setText(f"{confirmed} / {required} minutes ({percent:.1f}%)")

            if confirmed >= required:
                self.lbl_eta.setText("Ready to claim")
            elif is_actively_watching:
                remaining_minutes = required - confirmed
                eta_seconds = int(remaining_minutes * watch_interval - frac * watch_interval)
                eta_min, eta_sec = divmod(max(eta_seconds, 0), 60)
                self.lbl_eta.setText(f"~{eta_min}m {eta_sec:02d}s remaining")
            else:
                self.lbl_eta.setText("Not currently watching")
        else:
            self.lbl_drop_name.setText("Current Reward: No active drop targeted")
            self._show_drop_image("")
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0.0%")
            self.lbl_progress_text.setText("0 / 0 minutes (0%)")
            self.lbl_eta.setText("")

        if self.log_box.isVisible():
            self.log_text.setPlainText("\n".join(node.state.logs))
            self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

        # Rebuild the streamer list only when the underlying data actually
        # changed (not every tick) so the current selection isn't lost.
        current_logins = [
            self.list_streamers.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_streamers.count())
        ]
        new_logins = [s["login"] for s in node.state.live_streamers]
        if current_logins != new_logins:
            self.list_streamers.clear()
            for s in node.state.live_streamers:
                marker = "▶ " if s["login"] == node.state.manual_channel else ""
                item = QListWidgetItem(f"{marker}{s['display_name']} — {s['viewers']:,} viewers")
                item.setData(Qt.ItemDataRole.UserRole, s["login"])
                self.list_streamers.addItem(item)

        # in update_ui_state():
        self.lbl_error.setText(node.state.last_error or "")

def _app_icon_path() -> str:
    """assets/mournyl.ico — bundled into the onefile exe, or beside the app
    when run from source. Sets the window / taskbar icon."""
    for base in (getattr(sys, "_MEIPASS", None), str(DATA_DIR.parent)):
        if base:
            p = Path(base) / "assets" / "mournyl.ico"
            if p.exists():
                return str(p)
    return ""


GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _version_tuple(v: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", v or "")][:4]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def check_latest_release() -> dict | None:
    """Latest GitHub release as {'version','tag','url','notes'}, or None on any
    error. Network call — run off the UI thread."""
    try:
        r = requests.get(GITHUB_API_LATEST, timeout=15,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or ""
    zip_url = ""
    for a in data.get("assets") or []:
        if (a.get("name") or "").lower().endswith(".zip"):
            zip_url = a.get("browser_download_url") or ""
            break
    return {"version": tag.lstrip("vV"), "tag": tag, "url": zip_url,
            "notes": data.get("body") or ""}


def main():
    app = QApplication(sys.argv)
    apply_theme(app)
    icon = _app_icon_path()
    if icon:
        app.setWindowIcon(QIcon(icon))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()