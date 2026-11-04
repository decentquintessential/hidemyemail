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

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QStackedWidget, QToolBar, QVBoxLayout, QWidget,
)

from hme_helper import Backend

ACCENT = "#3a6ff0"


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
        self.setCentralWidget(self.stack)
        self.starting_page = centered(heading("Connecting to iCloud…", 14))
        self.login_page = self._build_login()
        self.code_page = self._build_code()
        self.main_page = self._build_main()
        for page in (self.starting_page, self.login_page, self.code_page, self.main_page):
            self.stack.addWidget(page)
        self.toolbar.setVisible(False)

    # ------------------------------------------------------------ plumbing

    def run(self, fn, on_ok, on_error=None):
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
            QMessageBox.warning(self, "Something went wrong", message)
            if on_error:
                on_error()

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        self.pool.start(job)

    def copy(self, text: str):
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied {text}", 2500)

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
            ("Sign out", None, self.sign_out),
        ):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            self.toolbar.addAction(action)

        self.search = QLineEdit(placeholderText="Search address, label, note", clearButtonEnabled=True)
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
        self.list.currentItemChanged.connect(self.show_detail)
        self.count = QLabel()
        self.count.setStyleSheet("color: palette(placeholder-text);")

        left = QWidget()
        lv = QVBoxLayout(left)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.filter)
        top.addWidget(self.sort)
        lv.addLayout(top)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.count)

        self.detail_stack = QStackedWidget()
        self.empty_detail = centered(muted("Select an address, or press Ctrl+N to create one."))
        self.detail = self._build_detail()
        self.detail_stack.addWidget(self.empty_detail)
        self.detail_stack.addWidget(self.detail)

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
        self.d_label = QLineEdit()
        self.d_note = QPlainTextEdit()
        self.d_note.setMaximumHeight(110)
        self.save = QPushButton("Save")
        self.save.clicked.connect(self.save_detail)
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self.toggle_active)
        delete = QPushButton("Delete…")
        delete.setStyleSheet("color: #c62828;")
        delete.clicked.connect(self.delete_alias)

        head = QHBoxLayout()
        head.addWidget(self.d_address, 1)
        head.addWidget(copy)
        form = QFormLayout()
        form.addRow("Status", self.d_status)
        form.addRow("Forwards to", self.d_forward)
        form.addRow("Created", self.d_created)
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

    def show_main(self):
        self.toolbar.setVisible(True)
        self.stack.setCurrentWidget(self.main_page)
        self.reload()

    def reload(self, select: str | None = None):
        def ok(aliases):
            self.set_aliases(aliases, select)

        self.run(lambda: self.backend.cmd_list({}), ok)

    def set_aliases(self, aliases, select=None):
        keep = select or (self.current() or {}).get("hme")
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
            and (not q or any(q in (a.get(k) or "").lower() for k in ("hme", "label", "note")))
        ]
        return sort_aliases(shown, self.sort.currentText())

    def refresh_list(self, *_, keep=None):
        keep = keep or (self.current() or {}).get("hme")
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
            if a.get("hme") == keep:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.count.setText(f"{len(shown)} of {len(self.aliases)} addresses")
        self.show_detail()

    def current(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def show_detail(self, *_):
        a = self.current()
        if not a:
            self.detail_stack.setCurrentWidget(self.empty_detail)
            return
        self.detail_stack.setCurrentWidget(self.detail)
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
            fn = lambda: self.backend.cmd_deactivate({"alias_id": a["anonymousId"]})
        else:
            fn = lambda: self.backend.cmd_reactivate({"alias_id": a["anonymousId"]})
        self.run(fn, lambda _: self.reload(), on_error=self.reload)

    def delete_alias(self):
        a = self.current()
        box = QMessageBox(QMessageBox.Warning, "Delete address",
                          f"Permanently delete {a['hme']}?\n\nMail sent to it will bounce. This cannot be undone.",
                          QMessageBox.Cancel, self)
        box.addButton("Delete permanently", QMessageBox.DestructiveRole)
        if box.exec() == QMessageBox.Cancel:
            return
        params = {"alias_id": a["anonymousId"], "active": a.get("isActive", True)}
        # On failure, reload anyway: deactivation may have succeeded before deleting failed.
        self.run(lambda: self.backend.cmd_delete(params), lambda _: self.reload(), on_error=self.reload)

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
     "isActive": True, "createTimestamp": 1761900000000, "anonymousId": "a", "forwardToEmail": "me@example.com"},
    {"hme": "brisk-maple.2q@icloud.com", "label": "Bol.com", "note": "", "isActive": True,
     "createTimestamp": 1758000000000, "anonymousId": "b", "forwardToEmail": "me@example.com"},
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
    win.toolbar.setVisible(True)
    win.stack.setCurrentWidget(win.main_page)
    win.set_aliases(SAMPLE, select=SAMPLE[0]["hme"])
    win.show()
    app.processEvents()
    win.grab().save(str(out / "screenshot.png"))
    report.write("selftest ok\n")
    report.close()
    win.close()
    del win
    app.processEvents()
    return 0


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
        # Exit hard: no error dialog that could hang a headless runner.
        sys.stdout.flush()
        os._exit(code)
    app = QApplication(sys.argv)
    app.setApplicationName("Hide My Email")
    app.setStyle("Fusion") if sys.platform != "win32" else None
    win = MainWindow(Backend())
    win.show()
    QTimer.singleShot(0, win.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
