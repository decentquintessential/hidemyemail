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
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QSystemTrayIcon,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import theme  # noqa: E402
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


def launch_installer(installer: Path):
    """Start the update installer, detached, with /RELAUNCH=1 so it reopens the app afterwards.

    The packaged app unpacks itself into a temporary _MEI folder and tells its own child
    processes about it through environment variables. Left in place, they reach the app
    the installer reopens, which then looks for its files in this app's _MEI folder -
    deleted by the time it starts ("Failed to load Python DLL"). So start the installer
    with those variables removed, and tell PyInstaller to start fresh.

    The app's launcher process also puts everything it starts into a Windows job that it
    waits for, so the installer (and the app it reopens) must break away from that job.
    """
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI", "_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    log = Path(installer).parent / "install.log"  # Inno Setup's own log, for diagnosing failed updates
    args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1", f"/LOG={log}"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        subprocess.Popen(args, creationflags=flags | breakaway, close_fds=True, env=env,
                         cwd=str(Path(installer).parent))
    except OSError:  # the job doesn't allow breaking away
        subprocess.Popen(args, creationflags=flags, close_fds=True, env=env, cwd=str(Path(installer).parent))


def record_start():
    """Note the version and time of every start, so tests can tell the app really started."""
    from hme_helper import APP_DIR

    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "last-start.txt").write_text(f"{APP_VERSION} {datetime.now().isoformat()}\n")
    except OSError:
        pass


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


def is_dark_mode() -> bool:
    return theme.is_dark()


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
    """Blue rounded tile with a white "@"; also the source of the .exe icon."""
    from PySide6.QtGui import QLinearGradient

    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 16, 0, 240)
    gradient.setColorAt(0, QColor("#4c86f7"))
    gradient.setColorAt(1, QColor("#1f55d6"))
    p.setBrush(gradient)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(16, 16, 224, 224, 54, 54)
    p.setPen(QColor("white"))
    font = theme.ui_font(96, QFont.DemiBold)
    font.setPixelSize(132)
    p.setFont(font)
    p.drawText(pm.rect().adjusted(0, -8, 0, 0), Qt.AlignCenter, "@")
    p.end()
    return QIcon(pm)


def search_icon() -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = p.pen()
    pen.setColor(QColor(theme.t("subtle")))
    pen.setWidthF(2.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawEllipse(7, 7, 13, 13)
    p.drawLine(18, 18, 25, 25)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- pages


def centered(*widgets, width=360) -> QWidget:
    """A page with the widgets in a column of fixed width, centred."""
    page = QWidget(objectName="page")
    outer = QVBoxLayout(page)
    outer.addStretch()
    box = QWidget()
    box.setFixedWidth(width)
    inner = QVBoxLayout(box)
    inner.setContentsMargins(0, 0, 0, 0)
    inner.setSpacing(10)
    for w in widgets:
        if isinstance(w, int):
            inner.addSpacing(w)
        else:
            inner.addWidget(w)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(box)
    row.addStretch()
    outer.addLayout(row)
    outer.addStretch()
    return page


def heading(text: str, role: str = "title", align=Qt.AlignCenter) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setAlignment(align)
    label.setWordWrap(True)
    return label


def muted(text: str, align=Qt.AlignCenter) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setAlignment(align)
    label.setProperty("role", "muted")
    return label


def styled(widget, kind: str):
    """Mark a button as "primary", "danger" or "ghost" for the style sheet."""
    widget.setProperty("kind", kind)
    return widget


def card(spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(spacing)
    return frame, layout


def section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "section")
    return label


def icon_label(size: int = 56) -> QLabel:
    label = QLabel()
    label.setPixmap(app_icon().pixmap(size, size))
    label.setAlignment(Qt.AlignCenter)
    return label


class Segmented(QWidget):
    """Segmented control (Active | Inactive | All) with a QComboBox-like API."""

    changed = Signal(str)

    def __init__(self, options: list[str]):
        super().__init__(objectName="segmented")
        self.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        self.group = QButtonGroup(self, exclusive=True)
        for text in options:
            b = QPushButton(text, checkable=True)
            b.setCursor(Qt.PointingHandCursor)
            self.group.addButton(b)
            row.addWidget(b)
        self.group.buttons()[0].setChecked(True)
        self.group.buttonToggled.connect(lambda b, on: on and self.changed.emit(b.text()))

    def currentText(self) -> str:
        return self.group.checkedButton().text()

    def setCurrentText(self, text: str):
        for b in self.group.buttons():
            if b.text() == text:
                b.setChecked(True)


class NewAliasDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.address: str | None = None
        self.setWindowTitle("New address")
        self.setMinimumWidth(460)

        title = heading("New Hide My Email address", "heading", Qt.AlignLeft)
        intro = muted("A random address that forwards to your inbox. Give it a label so you "
                      "remember where you used it.", Qt.AlignLeft)
        self.address_label = QLabel("Generating…")
        self.address_label.setFont(theme.mono_font(11))
        self.address_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        again = styled(QPushButton("Another one"), "ghost")
        again.clicked.connect(self.regenerate)
        address_box, address_row = card(0)
        address_box.layout().setContentsMargins(14, 10, 8, 10)
        row = QHBoxLayout()
        row.addWidget(self.address_label, 1)
        row.addWidget(again)
        address_row.addLayout(row)
        self.label = QLineEdit(placeholderText="Label, for example Netflix")
        self.note = QLineEdit(placeholderText="Note (optional)")
        self.create = styled(QPushButton("Create && Copy"), "primary")
        self.create.setDefault(True)
        self.create.clicked.connect(self.submit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.label.textChanged.connect(self._update)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.create)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addSpacing(4)
        layout.addWidget(address_box)
        layout.addWidget(self.label)
        layout.addWidget(self.note)
        layout.addSpacing(6)
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
        self.setMinimumWidth(400)
        self.label = QLineEdit(placeholderText="What's it for? For example Netflix")
        self.label.returnPressed.connect(self.create)
        self.status = muted("Press Enter to create it and copy it.", Qt.AlignLeft)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_app = styled(QPushButton("Open Hide My Email"), "ghost")
        open_app.clicked.connect(lambda: (self.hide(), win.bring_to_front()))
        hint = muted(HOTKEY_TEXT, Qt.AlignRight)
        row = QHBoxLayout()
        row.addWidget(open_app)
        row.addStretch()
        row.addWidget(hint)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 14)
        v.setSpacing(10)
        v.addWidget(heading("New address", "heading", Qt.AlignLeft))
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

    def unregister(self):
        if self.registered:
            import ctypes

            ctypes.windll.user32.UnregisterHotKey(None, 1)
            QApplication.instance().removeNativeEventFilter(self)
            self.registered = False

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
        self.resize(980, 640)
        self.setMinimumSize(760, 500)

        self.stack = QStackedWidget()
        self.update_bar = self._build_update_bar()
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self.update_bar)
        cv.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addPermanentWidget(QLabel(f"Version {APP_VERSION}"))
        self.update_ready: tuple[dict, Path] | None = None
        self._quitting = False
        self.starting_page = centered(icon_label(48), 6, muted("Connecting to iCloud…"))
        self.login_page = self._build_login()
        self.code_page = self._build_code()
        self.main_page = self._build_main()
        for page in (self.starting_page, self.login_page, self.code_page, self.main_page):
            self.stack.addWidget(page)
        self.toolbar.setVisible(False)
        self.apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.apply_theme)

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
        """Tray icon with quick-create while the app runs.

        Closing the window quits the app completely, unless "Keep running in the tray"
        is switched on: then it stays in the tray so the global shortcut keeps working.
        """
        self.quick = QuickCreateDialog(self)
        self.hotkey = GlobalHotKey(self.quick.popup)
        self.tray = None
        QApplication.instance().setQuitOnLastWindowClosed(False)  # closeEvent decides
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        menu = QMenu()
        menu.addAction("New address…", self.quick.popup)
        menu.addAction("Open Hide My Email", self.bring_to_front)
        menu.addAction("Check for updates", lambda: self.check_for_updates(manual=True))
        menu.addSeparator()
        menu.addAction(self.keep_running_action)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(f"Hide My Email — {HOTKEY_TEXT} for a new address")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.quick.popup() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self._told_about_tray = False

    @staticmethod
    def keep_running() -> bool:
        return QSettings("HideMyEmail", "HideMyEmail").value("keep_running", False, type=bool)

    @staticmethod
    def _set_keep_running(on: bool):
        QSettings("HideMyEmail", "HideMyEmail").setValue("keep_running", on)

    def quit_app(self):
        """Quit completely: no tray icon, global shortcut or background work left behind."""
        self._quitting = True
        if getattr(self, "hotkey", None):
            self.hotkey.unregister()
        if getattr(self, "tray", None):
            self.tray.hide()
        for widget in QApplication.topLevelWidgets():
            widget.hide()
        QApplication.instance().quit()

    # ------------------------------------------------------------ updates

    def _build_update_bar(self) -> QWidget:
        bar = QWidget(objectName="updateBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        self.update_text = QLabel()
        restart = styled(QPushButton("Restart to update"), "primary")
        restart.setObjectName("restart")
        restart.clicked.connect(self.install_update)
        later = styled(QPushButton("Later"), "ghost")
        later.setObjectName("later")
        later.clicked.connect(bar.hide)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 8, 12, 8)
        row.addWidget(self.update_text, 1)
        row.addWidget(later)
        row.addWidget(restart)
        bar.hide()
        return bar

    def apply_theme(self, *_):
        """Apply the light or dark design to the whole app (also when Windows switches)."""
        theme.apply(QApplication.instance())
        if hasattr(self, "search"):
            self.search_action.setIcon(search_icon())
        if hasattr(self, "list"):
            self.list.viewport().update()

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
        self._quitting = True  # so the installer's close request isn't turned into "hide to tray"
        launch_installer(self.update_ready[1])
        self.quit_app()

    def closeEvent(self, event):
        if getattr(self, "tray", None) and not self._quitting and self.keep_running():
            event.ignore()
            self.hide()
            if not self._told_about_tray:
                self._told_about_tray = True
                self.tray.showMessage("Hide My Email is still running",
                                      f"Click the tray icon or press {HOTKEY_TEXT} for a new address. "
                                      "Right-click it to quit.", app_icon(), 5000)
        else:
            event.accept()
            self.quit_app()

    # ------------------------------------------------------------ sign in

    def _build_login(self) -> QWidget:
        self.apple_id = QLineEdit(placeholderText="Apple ID (email address)")
        self.password = QLineEdit(placeholderText="Password", echoMode=QLineEdit.Password)
        self.password.returnPressed.connect(self.sign_in)
        button = styled(QPushButton("Sign In"), "primary")
        button.clicked.connect(self.sign_in)
        return centered(
            icon_label(), 6,
            heading("Sign in to iCloud"),
            muted("Use your Apple ID. Your password is stored securely in Windows Credential Manager."), 10,
            self.apple_id, self.password, 4, button,
        )

    def _build_code(self) -> QWidget:
        self.code = QLineEdit(placeholderText="000000", maxLength=6, alignment=Qt.AlignCenter)
        self.code.setFont(theme.mono_font(18))
        self.code.returnPressed.connect(self.verify)
        button = styled(QPushButton("Verify"), "primary")
        button.clicked.connect(self.verify)
        return centered(
            icon_label(), 6,
            heading("Enter verification code"),
            muted("Apple sent a 6-digit code to your iPhone, iPad or Mac."), 10,
            self.code, 4, button,
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
        # Toolbar: the main action on the left, everything else under Options.
        self.toolbar = QToolBar("Main", movable=False)
        self.toolbar.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(self.toolbar)
        new_button = styled(QPushButton("+  New address"), "primary")
        new_button.setToolTip("New address (Ctrl+N)")
        new_button.clicked.connect(self.new_alias)
        self.toolbar.addWidget(new_button)
        refresh = QToolButton(text="Refresh", toolTip="Refresh (F5)")
        refresh.clicked.connect(lambda: self.reload())
        self.toolbar.addWidget(refresh)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.toolbar.addWidget(spacer)
        for shortcut, slot in ((QKeySequence.New, self.new_alias), (QKeySequence.Refresh, lambda: self.reload())):
            action = QAction(self, shortcut=shortcut)
            action.triggered.connect(slot)
            self.addAction(action)

        self.keep_running_action = QAction("Keep running in the tray when closed", self, checkable=True)
        self.keep_running_action.setChecked(self.keep_running())
        self.keep_running_action.toggled.connect(self._set_keep_running)
        options = QMenu("Options", self)
        options.addAction("Export CSV…", self.export_csv)
        options.addAction("Check for updates", lambda: self.check_for_updates(manual=True))
        options.addSeparator()
        options.addAction(self.keep_running_action)
        options.addSeparator()
        options.addAction("Sign out", self.sign_out)
        options_button = QToolButton(text="Options  ▾", popupMode=QToolButton.InstantPopup)
        options_button.setMenu(options)
        self.toolbar.addWidget(options_button)

        # Left: search, filter, sort and the list.
        self.search = QLineEdit(placeholderText="Search addresses, labels, notes, websites", clearButtonEnabled=True)
        self.search_action = self.search.addAction(search_icon(), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self.refresh_list)
        self.filter = Segmented(["Active", "Inactive", "All"])
        self.filter.changed.connect(self.refresh_list)
        self.sort = QComboBox(toolTip="Sort addresses")
        self.sort.addItems(SORT_ORDERS)
        self.sort.setCurrentText(QSettings("HideMyEmail", "HideMyEmail").value("sort", SORT_ORDERS[0]))
        self.sort.currentTextChanged.connect(self._sort_changed)
        self.list = QListWidget(objectName="aliasList")
        self.list.setItemDelegate(theme.AliasDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # rows elide long text instead
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self.show_detail)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_menu)
        QShortcut(QKeySequence.Copy, self.list, self.copy_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence.Delete, self.list, lambda: self.ask_delete(self.selected()),
                  context=Qt.WidgetShortcut)
        self.count = QLabel()
        self.count.setProperty("role", "muted")

        left = QWidget(objectName="page")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 16, 8, 12)
        lv.setSpacing(10)
        options_row = QHBoxLayout()
        options_row.addWidget(self.filter)
        options_row.addStretch()
        options_row.addWidget(self.sort)
        lv.addWidget(self.search)
        lv.addLayout(options_row)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.count)

        # Right: details of the selection.
        self.detail_stack = QStackedWidget()
        self.empty_detail = centered(
            icon_label(48), 8, heading("No address selected", "heading"),
            muted("Select an address to see its details, or press Ctrl+N to create one. "
                  "Ctrl-click or Shift-click to select several."), width=380)
        self.detail = self._build_detail()
        self.batch = self._build_batch()
        for page in (self.empty_detail, self.detail, self.batch):
            self.detail_stack.addWidget(page)

        splitter = QSplitter(objectName="page")
        splitter.setHandleWidth(8)
        splitter.addWidget(left)
        splitter.addWidget(self.detail_stack)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 620])
        return splitter

    def _build_detail(self) -> QWidget:
        # Header: label, status, address with Copy.
        self.d_title = heading("", "title", Qt.AlignLeft)
        self.d_status = QLabel()
        self.d_address = QLabel()
        self.d_address.setFont(theme.mono_font(10.5))
        self.d_address.setProperty("role", "address")
        self.d_address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        copy = QPushButton("Copy address")
        copy.clicked.connect(lambda: self.copy(self.current()["hme"]))
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.addWidget(self.d_title)
        title_row.addWidget(self.d_status, 0, Qt.AlignVCenter)
        title_row.addStretch()
        address_row = QHBoxLayout()
        address_row.addWidget(self.d_address, 1)
        address_row.addWidget(copy)

        # Details card.
        info, info_layout = card()
        info_layout.addWidget(section("Details"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
        self.d_forward, self.d_created, self.d_origin = QLabel(), QLabel(), QLabel()
        for row, (key, value) in enumerate((("Forwards to", self.d_forward), ("Created", self.d_created),
                                            ("Created from", self.d_origin))):
            k = QLabel(key)
            k.setProperty("role", "key")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            grid.addWidget(k, row, 0, Qt.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        info_layout.addLayout(grid)

        # Label and note card.
        edit, edit_layout = card(8)
        edit_layout.addWidget(section("Label and note"))
        self.d_label = QLineEdit(placeholderText="Label")
        self.d_note = QPlainTextEdit(placeholderText="Note")
        self.d_note.setFixedHeight(84)
        self.d_edit_hint = muted("Reactivate this address to change its label or note.", Qt.AlignLeft)
        self.save = styled(QPushButton("Save"), "primary")
        self.save.clicked.connect(self.save_detail)
        self.d_label.textChanged.connect(self._update_save)
        self.d_note.textChanged.connect(self._update_save)
        save_row = QHBoxLayout()
        save_row.addWidget(self.d_edit_hint, 1)
        save_row.addStretch()
        save_row.addWidget(self.save)
        edit_layout.addWidget(self.d_label)
        edit_layout.addWidget(self.d_note)
        edit_layout.addLayout(save_row)

        # Status card.
        status, status_layout = card(8)
        status_layout.addWidget(section("Forwarding"))
        self.d_status_text = muted("", Qt.AlignLeft)
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self.toggle_active)
        delete = styled(QPushButton("Delete…"), "danger")
        delete.clicked.connect(lambda: self.ask_delete(self.selected(), allow_deactivate=False))
        actions = QHBoxLayout()
        actions.addWidget(self.toggle)
        actions.addStretch()
        actions.addWidget(delete)
        status_layout.addWidget(self.d_status_text)
        status_layout.addLayout(actions)

        content = QWidget(objectName="page")
        v = QVBoxLayout(content)
        v.setContentsMargins(16, 18, 20, 18)
        v.setSpacing(14)
        v.addLayout(title_row)
        v.addLayout(address_row)
        v.addSpacing(4)
        v.addWidget(info)
        v.addWidget(edit)
        v.addWidget(status)
        v.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _update_save(self):
        a = self.current()
        dirty = bool(a) and (self.d_label.text() != (a.get("label") or "")
                             or self.d_note.toPlainText() != (a.get("note") or ""))
        self.save.setEnabled(dirty and a.get("isActive", True))

    def _build_batch(self) -> QWidget:
        self.b_title = heading("", "title")
        self.b_counts = muted("")
        buttons = QHBoxLayout()
        buttons.addStretch()
        for text, slot, kind in (
            ("Copy all", self.copy_selected, None),
            ("Deactivate", lambda: self.set_active(self.selected(), False), None),
            ("Reactivate", lambda: self.set_active(self.selected(), True), None),
            ("Delete…", lambda: self.ask_delete(self.selected(), allow_deactivate=False), "danger"),
        ):
            button = QPushButton(text)
            if kind:
                styled(button, kind)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        box, layout = card(10)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.addWidget(self.b_title)
        layout.addWidget(self.b_counts)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        return centered(box, width=500)

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
            item = QListWidgetItem(f"{a.get('label') or 'No label'} {a.get('hme')}")  # text for accessibility
            item.setData(Qt.UserRole, a)
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
        self.count.setText(f"{len(chosen)} selected  ·  {shown} of {plural(len(self.aliases))}"
                           if len(chosen) > 1 else f"{shown} of {plural(len(self.aliases))}")
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
        active = a.get("isActive", True)
        self.d_title.setText(a.get("label") or "No label")
        self.d_status.setText("Active" if active else "Inactive")
        self.d_status.setProperty("role", "pill-active" if active else "pill-inactive")
        self.d_status.style().unpolish(self.d_status)
        self.d_status.style().polish(self.d_status)
        self.d_address.setText(a.get("hme", ""))
        self.d_forward.setText(a.get("forwardToEmail") or "—")
        self.d_created.setText(fmt_date(a.get("createTimestamp")) or "—")
        self.d_origin.setText(origin_text(a) or "—")
        self.d_label.setText(a.get("label") or "")
        self.d_note.setPlainText(a.get("note") or "")
        self.d_label.setEnabled(active)
        self.d_note.setEnabled(active)
        self.d_edit_hint.setVisible(not active)
        self._update_save()
        self.d_status_text.setText(
            "Mail sent to this address is forwarded to your inbox. Deactivate it to stop forwarding; "
            "you can turn it back on later." if active else
            "Mail sent to this address is not forwarded. Reactivate it to receive mail again, "
            "or delete it for good.")
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
    theme.apply(app)
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
    for scheme, name in ((Qt.ColorScheme.Light, "update-light.png"), (Qt.ColorScheme.Dark, "update-dark.png")):
        QGuiApplication.styleHints().setColorScheme(scheme)
        app.processEvents()
        win.apply_theme()
        app.processEvents()
        win.grab().save(str(out / name))
    report.write(f"color scheme switching: {'ok' if is_dark_mode() else 'not supported here'}\n")
    report.write("selftest ok\n")
    report.close()
    # Keep Qt objects alive until the process is terminated: letting Python destroy
    # the window and QApplication on return crashed Windows runs (access violation).
    _KEEP_ALIVE.extend([app, win])
    return 0


_KEEP_ALIVE: list = []


def hard_exit(code: int):
    """End the process now: background threads can't keep it alive, and no error dialog
    can appear. On Windows this also skips unloading Qt's DLLs, which can crash on exit."""
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        # Declare the types: without them ctypes truncates the 64-bit process handle.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess(k32.GetCurrentProcess(), code)
    os._exit(code)


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
        hard_exit(code)
    record_start()
    if "--test-update" in sys.argv:
        # CI: start an installer exactly as "Restart to update" does, then quit.
        launch_installer(Path(sys.argv[sys.argv.index("--test-update") + 1]).resolve())
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("Hide My Email")
    theme.apply(app)
    win = MainWindow(Backend())
    win.setup_tray()
    win.show()
    QTimer.singleShot(0, win.start)
    win.start_update_checks()
    code = app.exec()
    hard_exit(code)


if __name__ == "__main__":
    sys.exit(main())
