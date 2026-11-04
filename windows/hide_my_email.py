"""Hide My Email — Qt desktop app (built for Windows, also runs on macOS/Linux).

Uses the same Backend as the Mac app's helper (hme_helper.py), in-process.
Run with --selftest [screenshot.png] to check the build without logging in.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# A windowed .exe has no stdout/stderr; pyicloud calls sys.stdout.isatty() on import.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if "--selftest" in sys.argv:
    # Dump every thread's stack if startup hangs, so a CI run shows where.
    import faulthandler

    _i = sys.argv.index("--selftest")
    _dir = Path(sys.argv[_i + 1] if len(sys.argv) > _i + 1 else "selftest")
    _dir.mkdir(parents=True, exist_ok=True)
    _trace = (_dir / "trace.txt").open("w")
    _trace.write("started\n")
    _trace.flush()
    faulthandler.dump_traceback_later(60, exit=True, file=_trace)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import (
    QAbstractNativeEventFilter, QItemSelectionModel, QObject, QRunnable, QSettings, Qt, QThreadPool,
    QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QStackedWidget, QSystemTrayIcon, QToolBar,
    QVBoxLayout, QWidget,
)

from hme_helper import Backend

try:
    from _version import APP_VERSION  # written by the GitHub build
except ImportError:
    APP_VERSION = "0.0.0"  # running from source: no automatic updates

ACCENT = "#3a6ff0"
RELEASES_API = "https://api.github.com/repos/decentquintessential/hidemyemail/releases/latest"
SETUP_ASSET = "HideMyEmail-Setup.exe"
UPDATE_CHECK_HOURS = 6


# ---------------------------------------------------------------- updates


def parse_version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.strip().lstrip("vV").split("."))
    except ValueError:
        return (0,)


def find_update(current: str = APP_VERSION) -> dict | None:
    """Return {"version", "url", "size"} when GitHub's latest release is newer, else None."""
    import requests

    response = requests.get(RELEASES_API, timeout=20, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"HideMyEmail/{current}"})
    if response.status_code == 404:  # no releases yet
        return None
    response.raise_for_status()
    release = response.json()
    version = release.get("tag_name", "").lstrip("vV")
    if parse_version(version) <= parse_version(current):
        return None
    asset = next((a for a in release.get("assets", []) if a.get("name") == SETUP_ASSET), None)
    if not asset:
        return None
    return {"version": version, "url": asset["browser_download_url"], "size": asset.get("size", 0)}


def download_update(update: dict) -> Path:
    """Download the installer to %TEMP%; checks the size GitHub reported."""
    import tempfile

    import requests

    folder = Path(tempfile.gettempdir()) / "HideMyEmail-update"
    folder.mkdir(exist_ok=True)
    target = folder / f"HideMyEmail-Setup-{update['version']}.exe"
    if target.exists() and target.stat().st_size == update["size"]:
        return target
    partial = target.with_suffix(".part")
    with requests.get(update["url"], stream=True, timeout=60,
                      headers={"User-Agent": f"HideMyEmail/{APP_VERSION}"}) as response:
        response.raise_for_status()
        with partial.open("wb") as f:
            for chunk in response.iter_content(1 << 16):
                f.write(chunk)
    if update["size"] and partial.stat().st_size != update["size"]:
        partial.unlink(missing_ok=True)
        raise RuntimeError("The downloaded update was incomplete.")
    partial.replace(target)
    return target


# ---------------------------------------------------------------- background work


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


SORT_ORDERS = ["Newest first", "Oldest first", "Name A–Z", "Name Z–A"]


def sort_aliases(aliases: list[dict], order: str) -> list[dict]:
    if order.startswith("Name"):
        # Sort by label; unlabelled addresses always go last, by address.
        labelled = [a for a in aliases if a.get("label")]
        rest = sorted((a for a in aliases if not a.get("label")), key=lambda a: a.get("hme") or "")
        labelled.sort(key=lambda a: a["label"].casefold(), reverse=order.endswith("Z–A"))
        return labelled + rest
    return sorted(aliases, key=lambda a: a.get("createTimestamp") or 0, reverse=order == "Newest first")


def origin_text(a: dict) -> str:
    """Where the address was made: "Safari — example.com", "Created by hand", or an app name."""
    kind, domain, app = a.get("origin") or "", a.get("domain") or "", a.get("originAppName") or ""
    source = {"SAFARI": "Safari", "ON_DEMAND": "Created by hand", "": ""}.get(kind, app or kind.capitalize())
    if domain:
        source = f"{source} — {domain}" if source else domain
    return source


def plural(n: int, word: str = "address") -> str:
    return f"{n} {word}" + ("" if n == 1 else "es")


def fmt_date(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return ""


def dot_icon(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(1, 1, 10, 10)
    p.end()
    return QIcon(pm)


def app_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(ACCENT))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(16, 16, 224, 224, 50, 50)
    p.setPen(QColor("white"))
    p.setFont(QFont("Segoe UI", 110, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "@")
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- pages


def centered(*widgets, width=340) -> QWidget:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.addStretch()
    box = QWidget()
    box.setFixedWidth(width)
    inner = QVBoxLayout(box)
    inner.setSpacing(12)
    for w in widgets:
        inner.addWidget(w)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(box)
    row.addStretch()
    outer.addLayout(row)
    outer.addStretch()
    return page


def heading(text: str, size=18) -> QLabel:
    label = QLabel(text)
    f = label.font()
    f.setPointSize(size)
    f.setBold(True)
    label.setFont(f)
    label.setAlignment(Qt.AlignCenter)
    return label


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


class NewAliasDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.address: str | None = None
        self.setWindowTitle("New address")
        self.setMinimumWidth(440)

        self.address_label = QLabel("Generating…")
        self.address_label.setStyleSheet("font-family: Consolas, Menlo, monospace; font-size: 14px;")
        self.address_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        again = QPushButton("Another one")
        again.clicked.connect(self.regenerate)
        self.label = QLineEdit(placeholderText="Label, e.g. Netflix")
        self.note = QLineEdit(placeholderText="Note (optional)")
        self.create = QPushButton("Create && Copy")
        self.create.setDefault(True)
        self.create.clicked.connect(self.submit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.label.textChanged.connect(self._update)

        top = QHBoxLayout()
        top.addWidget(self.address_label, 1)
        top.addWidget(again)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.create)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.label)
        layout.addWidget(self.note)
        layout.addLayout(buttons)
        self._update()
        self.regenerate()

    def _update(self):
        self.create.setEnabled(bool(self.address and self.label.text().strip()))

    def regenerate(self):
        self.address = None
        self.address_label.setText("Generating…")
        self._update()

        def ok(result):
            self.address = result["hme"]
            self.address_label.setText(self.address)
            self._update()

        self.win.run(lambda: self.win.backend.cmd_generate({}), ok, on_error=lambda: self.reject())

    def submit(self):
        address, label, note = self.address, self.label.text().strip(), self.note.text().strip()

        def ok(_):
            self.win.copy(address)
            self.win.reload(select=address)
            self.accept()

        self.win.run(
            lambda: self.win.backend.cmd_reserve({"hme": address, "label": label, "note": note}), ok
        )


class QuickCreateDialog(QDialog):
    """Small always-on-top window: type a label, press Enter, the new address is copied."""

    def __init__(self, win: "MainWindow"):
        super().__init__(None, Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.win = win
        self.setWindowTitle("New Hide My Email address")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(380)
        self.label = QLineEdit(placeholderText="What's it for? e.g. Netflix")
        self.label.returnPressed.connect(self.create)
        self.status = QLabel("Press Enter to create it and copy it.")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_app = QPushButton("Open Hide My Email")
        open_app.setFlat(True)
        open_app.clicked.connect(lambda: (self.hide(), win.bring_to_front()))
        hint = muted(HOTKEY_TEXT)
        row = QHBoxLayout()
        row.addWidget(open_app)
        row.addStretch()
        row.addWidget(hint)
        v = QVBoxLayout(self)
        v.addWidget(self.label)
        v.addWidget(self.status)
        v.addLayout(row)

    def popup(self):
        self.label.clear()
        self.label.setEnabled(True)
        self.status.setText("Press Enter to create it and copy it.")
        self.show()
        self.raise_()
        self.activateWindow()
        self.label.setFocus()

    def create(self):
        text = self.label.text().strip()
        if not text:
            return
        if self.win.stack.currentWidget() is not self.win.main_page:
            self.status.setText("Open Hide My Email and sign in first.")
            return
        self.label.setEnabled(False)
        self.status.setText("Creating…")
        backend = self.win.backend

        def work():
            address = backend.cmd_generate({})["hme"]
            backend.cmd_reserve({"hme": address, "label": text, "note": ""})
            return address

        def ok(address):
            self.win.copy(address)
            self.win.reload(select=address)
            self.status.setText(f"✓ {address} — copied")
            self.label.clear()
            self.label.setEnabled(True)
            self.label.setFocus()

        def failed():
            self.label.setEnabled(True)
            self.status.setText("Couldn't create an address. See the message for details.")

        self.win.run(work, ok, on_error=failed, parent=self)


HOTKEY_TEXT = "Ctrl+Alt+Shift+H"
_WM_HOTKEY = 0x0312


class GlobalHotKey(QAbstractNativeEventFilter):
    """Windows-only system-wide shortcut (RegisterHotKey) that calls `callback`."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.registered = False
        if sys.platform != "win32":
            return
        import ctypes

        MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
        self.registered = bool(ctypes.windll.user32.RegisterHotKey(
            None, 1, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, ord("H")))
        if self.registered:
            QApplication.instance().installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam == 1:
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0


class MainWindow(QMainWindow):
    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend
        self.aliases: list[dict] = []
        self.jobs: set[_Job] = set()
        self.pool = QThreadPool.globalInstance()
        self.setWindowTitle("Hide My Email")
        self.setWindowIcon(app_icon())
        self.resize(900, 580)

        self.stack = QStackedWidget()
        self.update_bar = self._build_update_bar()
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self.update_bar)
        cv.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().addPermanentWidget(muted(f"Version {APP_VERSION}"))
        self.update_ready: tuple[dict, Path] | None = None
        self._quitting = False
        self.starting_page = centered(heading("Connecting to iCloud…", 14))
        self.login_page = self._build_login()
        self.code_page = self._build_code()
        self.main_page = self._build_main()
        for page in (self.starting_page, self.login_page, self.code_page, self.main_page):
            self.stack.addWidget(page)
        self.toolbar.setVisible(False)

    # ------------------------------------------------------------ plumbing

    def run(self, fn, on_ok, on_error=None, parent=None):
        job = _Job(fn)
        self.jobs.add(job)
        self.statusBar().showMessage("Working…")
        self.setCursor(Qt.BusyCursor)

        def finish():
            self.jobs.discard(job)
            if not self.jobs:
                self.unsetCursor()
                self.statusBar().clearMessage()

        def done(result):
            finish()
            on_ok(result)

        def failed(message):
            finish()
            QMessageBox.warning(parent or self, "Something went wrong", message)
            if on_error:
                on_error()

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def copy(self, text: str):
        QGuiApplication.clipboard().setText(text)
        lines = text.count("\n") + 1
        self.statusBar().showMessage(f"Copied {plural(lines)}" if lines > 1 else f"Copied {text}", 2500)

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ tray and hotkey

    def setup_tray(self):
        """Tray icon with quick-create; closing the window then keeps the app in the tray."""
        self.quick = QuickCreateDialog(self)
        self.hotkey = GlobalHotKey(self.quick.popup)
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        menu = QMenu()
        menu.addAction("New address…", self.quick.popup)
        menu.addAction("Open Hide My Email", self.bring_to_front)
        menu.addAction("Check for updates", lambda: self.check_for_updates(manual=True))
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(f"Hide My Email — {HOTKEY_TEXT} for a new address")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.quick.popup() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self._told_about_tray = False

    def quit_app(self):
        self._quitting = True
        QApplication.instance().quit()

    # ------------------------------------------------------------ updates

    def _build_update_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("updateBar")
        bar.setStyleSheet("#updateBar { background: #e8f0fe; border-bottom: 1px solid #c6d7f7; }")
        self.update_text = QLabel()
        restart = QPushButton("Restart to update")
        restart.setDefault(True)
        restart.clicked.connect(self.install_update)
        later = QPushButton("Later")
        later.setFlat(True)
        later.clicked.connect(bar.hide)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.addWidget(self.update_text, 1)
        row.addWidget(later)
        row.addWidget(restart)
        bar.hide()
        return bar

    def start_update_checks(self):
        """Check GitHub shortly after start and every few hours (only in released builds)."""
        if parse_version(APP_VERSION) == (0, 0, 0):
            return
        QTimer.singleShot(10_000, self.check_for_updates)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.check_for_updates)
        self.update_timer.start(UPDATE_CHECK_HOURS * 3600 * 1000)

    def check_for_updates(self, manual: bool = False):
        """Find and download a newer release in the background, then offer to install it."""
        if self.update_ready:
            self.show_update_ready()
            return
        job = _Job(lambda: (lambda u: (u, download_update(u)) if u else None)(find_update()))
        self.jobs.add(job)

        def done(result):
            self.jobs.discard(job)
            if result:
                self.update_ready = result
                self.show_update_ready()
            elif manual:
                QMessageBox.information(self, "No updates", f"You have the latest version ({APP_VERSION}).")

        def failed(message):
            self.jobs.discard(job)
            if manual:  # automatic checks stay quiet, e.g. when offline
                QMessageBox.warning(self, "Couldn't check for updates", message)

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def show_update_ready(self):
        version = self.update_ready[0]["version"]
        self.update_text.setText(f"Hide My Email {version} is ready to install (you have {APP_VERSION}).")
        self.update_bar.show()
        if getattr(self, "tray", None) and not self.isVisible():
            self.tray.showMessage("Update ready", f"Hide My Email {version} is ready. Open the app to install it.",
                                  app_icon(), 5000)

    def install_update(self):
        """Run the downloaded installer silently; it reopens the app when done."""
        import subprocess

        _, installer = self.update_ready
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                         creationflags=flags, close_fds=True)
        self.quit_app()

    def closeEvent(self, event):
        if getattr(self, "tray", None) and not self._quitting:
            event.ignore()
            self.hide()
            if not self._told_about_tray:
                self._told_about_tray = True
                self.tray.showMessage("Hide My Email is still running",
                                      f"Click the tray icon or press {HOTKEY_TEXT} for a new address. "
                                      "Right-click it to quit.", app_icon(), 5000)
        else:
            super().closeEvent(event)

    # ------------------------------------------------------------ sign in

    def _build_login(self) -> QWidget:
        self.apple_id = QLineEdit(placeholderText="Apple ID (email)")
        self.password = QLineEdit(placeholderText="Password", echoMode=QLineEdit.Password)
        self.password.returnPressed.connect(self.sign_in)
        button = QPushButton("Sign In")
        button.clicked.connect(self.sign_in)
        return centered(
            heading("Sign in to iCloud"),
            muted("Your password is stored in Windows Credential Manager."),
            self.apple_id, self.password, button,
        )

    def _build_code(self) -> QWidget:
        self.code = QLineEdit(placeholderText="123456", maxLength=6, alignment=Qt.AlignCenter)
        self.code.setStyleSheet("font-size: 22px; letter-spacing: 6px;")
        self.code.returnPressed.connect(self.verify)
        button = QPushButton("Verify")
        button.clicked.connect(self.verify)
        return centered(
            heading("Enter verification code"),
            muted("Apple sent a 6-digit code to your iPhone or other Apple devices."),
            self.code, button,
        )

    def start(self):
        self.stack.setCurrentWidget(self.starting_page)
        status = self.backend.cmd_status({})
        self.apple_id.setText(status.get("apple_id") or "")
        if status.get("has_password"):
            self._login({})
        else:
            self.stack.setCurrentWidget(self.login_page)

    def sign_in(self):
        self._login({"apple_id": self.apple_id.text().strip(), "password": self.password.text()})

    def _login(self, params):
        def ok(result):
            self.password.clear()
            if result["state"] == "2fa":
                self.code.clear()
                self.stack.setCurrentWidget(self.code_page)
                self.code.setFocus()
            elif result["state"] == "security_key":
                QMessageBox.information(self, "Security key", "Click OK, then touch your security key.")
                self._verify({"security_key": True})
            else:
                self.show_main()

        self.run(lambda: self.backend.cmd_login(params), ok,
                 on_error=lambda: self.stack.setCurrentWidget(self.login_page))

    def verify(self):
        self._verify({"code": self.code.text()})

    def _verify(self, params):
        self.run(lambda: self.backend.cmd_verify(params), lambda _: self.show_main())

    def sign_out(self):
        if QMessageBox.question(self, "Sign out", "Sign out and forget the saved password?") != QMessageBox.Yes:
            return

        def ok(_):
            self.aliases = []
            self.toolbar.setVisible(False)
            self.stack.setCurrentWidget(self.login_page)

        self.run(lambda: self.backend.cmd_logout({}), ok)

    # ------------------------------------------------------------ main page

    def _build_main(self) -> QWidget:
        self.toolbar = QToolBar("Main", movable=False)
        self.addToolBar(self.toolbar)
        for text, shortcut, slot in (
            ("New address", QKeySequence.New, self.new_alias),
            ("Refresh", QKeySequence.Refresh, lambda: self.reload()),
            ("Export CSV…", None, self.export_csv),
            ("Check for updates", None, lambda: self.check_for_updates(manual=True)),
            ("Sign out", None, self.sign_out),
        ):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            self.toolbar.addAction(action)

        self.search = QLineEdit(placeholderText="Search address, label, note, website", clearButtonEnabled=True)
        self.search.textChanged.connect(self.refresh_list)
        self.filter = QComboBox()
        self.filter.addItems(["Active", "Inactive", "All"])
        self.filter.currentIndexChanged.connect(self.refresh_list)
        self.sort = QComboBox(toolTip="Sort addresses")
        self.sort.addItems(SORT_ORDERS)
        self.sort.setCurrentText(QSettings("HideMyEmail", "HideMyEmail").value("sort", SORT_ORDERS[0]))
        self.sort.currentTextChanged.connect(self._sort_changed)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self.show_detail)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_menu)
        QShortcut(QKeySequence.Copy, self.list, self.copy_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence.Delete, self.list, lambda: self.ask_delete(self.selected()),
                  context=Qt.WidgetShortcut)
        self.count = QLabel()
        self.count.setStyleSheet("color: palette(placeholder-text);")

        left = QWidget()
        lv = QVBoxLayout(left)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        options = QHBoxLayout()
        options.addWidget(self.filter)
        options.addStretch()
        options.addWidget(QLabel("Sort:"))
        options.addWidget(self.sort)
        lv.addLayout(top)
        lv.addLayout(options)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.count)

        self.detail_stack = QStackedWidget()
        self.empty_detail = centered(muted(
            "Select an address, or press Ctrl+N to create one.\n"
            "Ctrl-click or Shift-click to select several."))
        self.detail = self._build_detail()
        self.batch = self._build_batch()
        for page in (self.empty_detail, self.detail, self.batch):
            self.detail_stack.addWidget(page)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.detail_stack)
        splitter.setSizes([340, 560])
        return splitter

    def _build_detail(self) -> QWidget:
        self.d_address = QLabel()
        self.d_address.setStyleSheet("font-family: Consolas, Menlo, monospace; font-size: 16px;")
        self.d_address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: self.copy(self.current()["hme"]))
        self.d_status = QLabel()
        self.d_forward = QLabel()
        self.d_created = QLabel()
        self.d_origin = QLabel()
        self.d_label = QLineEdit()
        self.d_note = QPlainTextEdit()
        self.d_note.setMaximumHeight(110)
        self.save = QPushButton("Save")
        self.save.clicked.connect(self.save_detail)
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self.toggle_active)
        delete = QPushButton("Delete…")
        delete.setStyleSheet("color: #c62828;")
        delete.clicked.connect(lambda: self.ask_delete(self.selected(), allow_deactivate=False))

        head = QHBoxLayout()
        head.addWidget(self.d_address, 1)
        head.addWidget(copy)
        form = QFormLayout()
        form.addRow("Status", self.d_status)
        form.addRow("Forwards to", self.d_forward)
        form.addRow("Created", self.d_created)
        form.addRow("Created from", self.d_origin)
        form.addRow("Label", self.d_label)
        form.addRow("Note", self.d_note)
        save_row = QHBoxLayout()
        save_row.addStretch()
        save_row.addWidget(self.save)
        actions = QHBoxLayout()
        actions.addWidget(self.toggle)
        actions.addStretch()
        actions.addWidget(delete)
        hint = muted("Deactivating stops forwarding but keeps the address. Deleting removes it for good.")
        hint.setAlignment(Qt.AlignLeft)

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(20, 16, 20, 16)
        v.addLayout(head)
        v.addSpacing(8)
        v.addLayout(form)
        v.addLayout(save_row)
        v.addStretch()
        v.addLayout(actions)
        v.addWidget(hint)
        return page

    def _build_batch(self) -> QWidget:
        self.b_title = heading("", 16)
        self.b_counts = muted("")
        buttons = QHBoxLayout()
        buttons.addStretch()
        for text, slot in (
            ("Copy all", self.copy_selected),
            ("Deactivate", lambda: self.set_active(self.selected(), False)),
            ("Reactivate", lambda: self.set_active(self.selected(), True)),
            ("Delete…", lambda: self.ask_delete(self.selected(), allow_deactivate=False)),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            if text.startswith("Delete"):
                button.setStyleSheet("color: #c62828;")
            buttons.addWidget(button)
        buttons.addStretch()
        page = QWidget()
        v = QVBoxLayout(page)
        v.addStretch()
        v.addWidget(self.b_title)
        v.addWidget(self.b_counts)
        v.addSpacing(12)
        v.addLayout(buttons)
        v.addStretch()
        return page

    def show_main(self):
        self.toolbar.setVisible(True)
        self.stack.setCurrentWidget(self.main_page)
        self.reload()

    def reload(self, select: str | None = None):
        def ok(aliases):
            self.set_aliases(aliases, select)

        self.run(lambda: self.backend.cmd_list({}), ok)

    def set_aliases(self, aliases, select=None):
        keep = {select} if select else {a.get("hme") for a in self.selected()}
        self.aliases = list(aliases)
        self.refresh_list(keep=keep)

    def _sort_changed(self, order: str):
        QSettings("HideMyEmail", "HideMyEmail").setValue("sort", order)
        self.refresh_list()

    def visible(self) -> list[dict]:
        mode = self.filter.currentText()
        q = self.search.text().lower()
        shown = [
            a for a in self.aliases
            if (mode == "All" or a.get("isActive", True) == (mode == "Active"))
            and (not q or any(q in (a.get(k) or "").lower() for k in ("hme", "label", "note"))
                 or q in origin_text(a).lower())
        ]
        return sort_aliases(shown, self.sort.currentText())

    def refresh_list(self, *_, keep=None):
        keep = keep if keep is not None else {a.get("hme") for a in self.selected()}
        self.list.blockSignals(True)
        self.list.clear()
        shown = self.visible()
        for a in shown:
            active = a.get("isActive", True)
            item = QListWidgetItem(dot_icon("#2e9d4f" if active else "#b0b0b0"),
                                   f"{a.get('label') or 'No label'}\n{a.get('hme')}")
            item.setData(Qt.UserRole, a)
            if not a.get("isActive", True):
                item.setForeground(QColor("gray"))
            self.list.addItem(item)
            if a.get("hme") in keep:
                item.setSelected(True)
                self.list.setCurrentItem(item, QItemSelectionModel.NoUpdate)
        self.list.blockSignals(False)
        self.show_detail()

    def selected(self) -> list[dict]:
        """Selected addresses, in list order."""
        rows = sorted(self.list.selectedItems(), key=self.list.row)
        return [item.data(Qt.UserRole) for item in rows]

    def current(self) -> dict | None:
        """The selected address when exactly one is selected."""
        chosen = self.selected()
        return chosen[0] if len(chosen) == 1 else None

    def show_detail(self, *_):
        chosen = self.selected()
        shown = self.list.count()
        self.count.setText(f"{len(chosen)} selected · {shown} of {len(self.aliases)} addresses"
                           if len(chosen) > 1 else f"{shown} of {len(self.aliases)} addresses")
        if len(chosen) > 1:
            active = sum(a.get("isActive", True) for a in chosen)
            self.b_title.setText(f"{len(chosen)} addresses selected")
            self.b_counts.setText(f"{active} active, {len(chosen) - active} inactive")
            self.detail_stack.setCurrentWidget(self.batch)
            return
        a = self.current()
        if not a:
            self.detail_stack.setCurrentWidget(self.empty_detail)
            return
        self.detail_stack.setCurrentWidget(self.detail)
        self.d_origin.setText(origin_text(a) or "—")
        active = a.get("isActive", True)
        self.d_address.setText(a.get("hme", ""))
        self.d_status.setText("Active — forwarding mail" if active else "Inactive — not forwarding")
        self.d_forward.setText(a.get("forwardToEmail") or "")
        self.d_created.setText(fmt_date(a.get("createTimestamp")))
        self.d_label.setText(a.get("label") or "")
        self.d_note.setPlainText(a.get("note") or "")
        self.toggle.setText("Deactivate" if active else "Reactivate")

    # ------------------------------------------------------------ actions

    def new_alias(self):
        NewAliasDialog(self).exec()

    def save_detail(self):
        a = self.current()
        params = {"alias_id": a["anonymousId"], "label": self.d_label.text().strip(), "note": self.d_note.toPlainText()}

        def ok(_):
            self.statusBar().showMessage("Saved", 2500)
            self.reload()

        self.run(lambda: self.backend.cmd_update(params), ok)

    def toggle_active(self):
        a = self.current()
        if a.get("isActive", True):
            if QMessageBox.question(self, "Deactivate", f"Stop forwarding mail from {a['hme']}?") != QMessageBox.Yes:
                return
        self.set_active([a], not a.get("isActive", True))

    def copy_selected(self):
        chosen = self.selected()
        if chosen:
            self.copy("\n".join(a["hme"] for a in chosen))

    def list_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        # Right-click acts on the whole selection when the clicked row is part of it.
        targets = self.selected() if item.isSelected() else [item.data(Qt.UserRole)]
        menu = QMenu(self)
        menu.addAction("Copy address" if len(targets) == 1 else f"Copy {plural(len(targets))}",
                       lambda: self.copy("\n".join(a["hme"] for a in targets)))
        menu.addSeparator()
        if any(a.get("isActive", True) for a in targets):
            menu.addAction("Deactivate", lambda: self.set_active(targets, False))
        if any(not a.get("isActive", True) for a in targets):
            menu.addAction("Reactivate", lambda: self.set_active(targets, True))
        menu.addAction("Delete…", lambda: self.ask_delete(targets, allow_deactivate=False))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def set_active(self, targets: list[dict], active: bool):
        todo = [a for a in targets if a.get("isActive", True) != active]
        command = self.backend.cmd_reactivate if active else self.backend.cmd_deactivate
        self._batch(todo, "Reactivated" if active else "Deactivated",
                    lambda a: command({"alias_id": a["anonymousId"]}))

    def ask_delete(self, targets: list[dict], allow_deactivate: bool = True):
        """Confirm deleting; from the Delete key, also offer the reversible Deactivate."""
        if not targets:
            return
        what = targets[0]["hme"] if len(targets) == 1 else plural(len(targets))
        box = QMessageBox(QMessageBox.Warning, "Delete",
                          f"Permanently delete {what}?\n\nMail sent to it will bounce. This cannot be undone.",
                          QMessageBox.Cancel, self)
        delete = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
        deactivate = None
        if allow_deactivate and any(a.get("isActive", True) for a in targets):
            deactivate = box.addButton("Deactivate instead", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is delete:
            self._batch(targets, "Deleted", lambda a: self.backend.cmd_delete(
                {"alias_id": a["anonymousId"], "active": a.get("isActive", True)}))
        elif deactivate is not None and box.clickedButton() is deactivate:
            self.set_active(targets, False)

    def _batch(self, targets: list[dict], verb: str, call):
        """Run one change per address in the background, keep going past failures, then reload."""
        if not targets:
            return

        def work():
            failures = []
            for a in targets:
                try:
                    call(a)
                except Exception as exc:
                    failures.append(f"{a['hme']}: {exc}")
            return failures

        def ok(failures):
            done = len(targets) - len(failures)
            if done:
                self.statusBar().showMessage(f"{verb} {plural(done)}", 3000)
            self.reload()
            if failures:
                QMessageBox.warning(self, "Some changes failed", "\n\n".join(failures))

        self.run(work, ok, on_error=self.reload)

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export", "Hide My Email.csv", "CSV files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["address", "label", "note", "active", "created", "forward_to"])
            for a in self.aliases:
                w.writerow([a.get("hme"), a.get("label"), a.get("note"), a.get("isActive", True),
                            fmt_date(a.get("createTimestamp")), a.get("forwardToEmail")])
        self.statusBar().showMessage(f"Exported {len(self.aliases)} addresses", 2500)


# ---------------------------------------------------------------- entry points


SAMPLE = [
    {"hme": "quiet-otter_7k@icloud.com", "label": "Netflix", "note": "Streaming account",
     "isActive": True, "createTimestamp": 1761900000000, "anonymousId": "a", "forwardToEmail": "me@example.com",
     "origin": "SAFARI", "domain": "netflix.com"},
    {"hme": "brisk-maple.2q@icloud.com", "label": "Bol.com", "note": "", "isActive": True,
     "createTimestamp": 1758000000000, "anonymousId": "b", "forwardToEmail": "me@example.com",
     "origin": "ON_DEMAND"},
    {"hme": "lunar-pebble04@icloud.com", "label": "Old newsletter", "note": "", "isActive": False,
     "createTimestamp": 1740000000000, "anonymousId": "c", "forwardToEmail": "me@example.com"},
]


def selftest(out_dir: str) -> int:
    """Check that everything the build needs is importable, then render the UI.

    Writes report.txt and screenshot.png to out_dir (a windowed .exe has no console).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = (out / "report.txt").open("w", buffering=1)
    import keyring
    import pyicloud  # noqa: F401
    from pyicloud.services.hidemyemail import HideMyEmailService  # noqa: F401

    kr = keyring.get_keyring()
    names = [type(b).__module__ for b in getattr(kr, "backends", [kr])]
    report.write(f"keyring backends: {names}\n")
    if not any(n.endswith(("Windows", "macOS", "SecretService", "kwallet")) for n in names):
        report.write("ERROR: no usable credential store\n")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(Backend())
    win.setup_tray()
    report.write(f"tray available: {win.tray is not None}\n")
    report.write(f"global hotkey registered: {win.hotkey.registered}\n")

    report.write(f"version: {APP_VERSION}\n")
    assert parse_version("v1.0.10") > parse_version("1.0.9") > parse_version("1.0") > parse_version("0.0.0")
    try:  # live check against GitHub: pretend to be an old version
        found = find_update("0.0.1")
        report.write(f"latest release on GitHub: {found['version'] if found else 'none yet'}\n")
    except Exception as exc:  # network trouble shouldn't fail the build
        report.write(f"update check failed (not fatal): {exc}\n")
    win.toolbar.setVisible(True)
    win.stack.setCurrentWidget(win.main_page)
    win.filter.setCurrentText("All")
    win.set_aliases(SAMPLE, select=SAMPLE[0]["hme"])
    win.show()
    app.processEvents()
    assert win.d_origin.text() == "Safari — netflix.com", win.d_origin.text()
    win.grab().save(str(out / "screenshot.png"))

    win.list.selectAll()
    app.processEvents()
    assert win.detail_stack.currentWidget() is win.batch and len(win.selected()) == 3
    win.copy_selected()
    assert QGuiApplication.clipboard().text().count("\n") == 2
    win.grab().save(str(out / "batch.png"))

    win.quick.popup()
    app.processEvents()
    win.quick.grab().save(str(out / "quick.png"))
    win.quick.hide()

    win.update_ready = ({"version": "9.9.9"}, Path("setup.exe"))
    win.show_update_ready()
    app.processEvents()
    assert win.update_bar.isVisible()
    win.grab().save(str(out / "update.png"))
    report.write("selftest ok\n")
    report.close()
    # Keep Qt objects alive until the process is terminated: letting Python destroy
    # the window and QApplication on return crashed Windows runs (access violation).
    _KEEP_ALIVE.extend([app, win])
    return 0


_KEEP_ALIVE: list = []


def main() -> int:
    if "--selftest" in sys.argv:
        import faulthandler
        import traceback

        i = sys.argv.index("--selftest")
        out_dir = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest"
        try:
            code = selftest(out_dir)
        except BaseException:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            with (Path(out_dir) / "report.txt").open("a") as f:
                f.write("CRASH:\n" + traceback.format_exc())
            code = 2
        faulthandler.cancel_dump_traceback_later()
        sys.stdout.flush()
        # Exit hard: no error dialog that could hang a headless runner. On Windows,
        # skip DLL unload too, where Qt can crash after the test already passed.
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            # Declare the types: without them ctypes truncates the 64-bit process handle.
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.TerminateProcess(k32.GetCurrentProcess(), code)
        os._exit(code)
    app = QApplication(sys.argv)
    app.setApplicationName("Hide My Email")
    app.setStyle("Fusion") if sys.platform != "win32" else None
    win = MainWindow(Backend())
    win.setup_tray()
    win.show()
    QTimer.singleShot(0, win.start)
    win.start_update_checks()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
