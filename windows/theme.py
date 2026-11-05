"""Visual design for the Windows app: colour tokens (light and dark), fonts, the
application style sheet, and the painter for rows in the address list.

Every colour the app uses comes from TOKENS, so light and dark mode stay consistent.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate

TOKENS = {
    "light": {
        "bg": "#f5f6f8", "surface": "#ffffff", "border": "#e2e5ea", "border_strong": "#cdd2da",
        "text": "#16191d", "muted": "#5f6b7a", "subtle": "#8a94a3",
        "accent": "#2563eb", "accent_hover": "#1d4fd7", "accent_soft": "#eaf0fe", "on_accent": "#ffffff",
        "hover": "#f1f3f6", "danger": "#c8372d", "danger_soft": "#fdecea",
        "success": "#16803c", "success_soft": "#e7f6ec", "neutral_soft": "#eef0f3",
        "input": "#ffffff",
    },
    "dark": {
        "bg": "#15171b", "surface": "#1c1f25", "border": "#2b3038", "border_strong": "#3a404a",
        "text": "#e7e9ec", "muted": "#9aa3af", "subtle": "#6f7884",
        "accent": "#4c8dff", "accent_hover": "#6aa1ff", "accent_soft": "#1d2a44", "on_accent": "#ffffff",
        "hover": "#23272e", "danger": "#ff7a6e", "danger_soft": "#3a1f1d",
        "success": "#4cc77a", "success_soft": "#16301f", "neutral_soft": "#262a31",
        "input": "#15171b",
    },
}

UI_FONTS = ["Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Arial"]
MONO_FONTS = ["Cascadia Mono", "Consolas", "Menlo", "monospace"]

_current = TOKENS["light"]


def t(name: str) -> str:
    """A colour from the active theme."""
    return _current[name]


def is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def ui_font(point_size: float = 10, weight: QFont.Weight = QFont.Normal) -> QFont:
    font = QFont()
    font.setFamilies(UI_FONTS)
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font


def mono_font(point_size: float = 10) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FONTS)
    font.setPointSizeF(point_size)
    return font


def apply(app: QApplication, dark: bool | None = None) -> None:
    """Apply the palette, font and style sheet for the current (or given) colour scheme."""
    global _current
    _current = TOKENS["dark" if (is_dark() if dark is None else dark) else "light"]
    app.setStyle("Fusion")
    app.setFont(ui_font())

    pal = QPalette()
    for role, key in (
        (QPalette.Window, "bg"), (QPalette.Base, "surface"), (QPalette.AlternateBase, "hover"),
        (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.ButtonText, "text"),
        (QPalette.Button, "surface"), (QPalette.Highlight, "accent"), (QPalette.HighlightedText, "on_accent"),
        (QPalette.ToolTipBase, "surface"), (QPalette.ToolTipText, "text"), (QPalette.PlaceholderText, "subtle"),
        (QPalette.Link, "accent"), (QPalette.Mid, "border"), (QPalette.Dark, "border_strong"),
    ):
        pal.setColor(role, QColor(t(key)))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t("subtle")))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t("subtle")))
    app.setPalette(pal)
    app.setStyleSheet(style_sheet())


def style_sheet() -> str:
    c = _current
    return f"""
    QMainWindow, QDialog, QWidget#page {{ background: {c['bg']}; }}
    QWidget {{ color: {c['text']}; }}
    QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px 6px; }}

    QToolBar {{ background: {c['surface']}; border: none; border-bottom: 1px solid {c['border']};
               padding: 8px 12px; spacing: 6px; }}
    QToolBar QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 6px 10px; }}
    QToolBar QToolButton:hover {{ background: {c['hover']}; }}
    QToolBar QToolButton::menu-indicator {{ image: none; width: 0; }}

    QPushButton {{ background: {c['surface']}; border: 1px solid {c['border_strong']}; border-radius: 6px;
                  padding: 6px 14px; min-height: 18px; }}
    QPushButton:hover {{ background: {c['hover']}; }}
    QPushButton:pressed {{ background: {c['border']}; }}
    QPushButton:disabled {{ color: {c['subtle']}; border-color: {c['border']}; }}
    QPushButton:focus {{ outline: none; }}
    QPushButton[kind="primary"] {{ background: {c['accent']}; border: 1px solid {c['accent']}; color: {c['on_accent']};
                                   font-weight: 600; }}
    QPushButton[kind="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton[kind="primary"]:disabled {{ background: {c['border']}; border-color: {c['border']}; color: {c['subtle']}; }}
    QPushButton[kind="danger"] {{ color: {c['danger']}; }}
    QPushButton[kind="danger"]:hover {{ background: {c['danger_soft']}; border-color: {c['danger']}; }}
    QPushButton[kind="ghost"] {{ background: transparent; border: none; color: {c['accent']}; padding: 6px 8px; }}
    QPushButton[kind="ghost"]:hover {{ background: {c['accent_soft']}; }}

    QWidget#segmented {{ background: {c['hover']}; border: 1px solid {c['border']}; border-radius: 7px; }}
    QWidget#segmented QPushButton {{ background: transparent; border: none; border-radius: 5px;
                                     padding: 4px 12px; color: {c['muted']}; min-height: 16px; }}
    QWidget#segmented QPushButton:checked {{ background: {c['surface']}; color: {c['text']};
                                             border: 1px solid {c['border_strong']}; }}

    QLineEdit, QPlainTextEdit, QComboBox {{ background: {c['input']}; border: 1px solid {c['border_strong']};
                                            border-radius: 6px; padding: 6px 8px;
                                            selection-background-color: {c['accent']};
                                            selection-color: {c['on_accent']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
    QLineEdit:disabled, QPlainTextEdit:disabled {{ color: {c['subtle']}; background: {c['hover']}; }}
    QComboBox {{ padding: 5px 8px; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{ background: {c['surface']}; border: 1px solid {c['border']}; padding: 4px;
                                   selection-background-color: {c['accent_soft']}; selection-color: {c['text']};
                                   outline: 0; }}

    QListView#aliasList {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
                           padding: 4px; outline: 0; }}

    QFrame#card {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QFrame#card QLabel {{ background: transparent; }}
    QLabel[role="title"] {{ font-size: 15pt; font-weight: 600; }}
    QLabel[role="heading"] {{ font-size: 13pt; font-weight: 600; }}
    QLabel[role="section"] {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 600; }}
    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="key"] {{ color: {c['muted']}; }}
    QLabel[role="address"] {{ color: {c['muted']}; }}
    QLabel[role="pill-active"] {{ background: {c['success_soft']}; color: {c['success']}; border-radius: 9px;
                                  padding: 2px 9px; font-size: 8.5pt; font-weight: 600; }}
    QLabel[role="pill-inactive"] {{ background: {c['neutral_soft']}; color: {c['muted']}; border-radius: 9px;
                                    padding: 2px 9px; font-size: 8.5pt; font-weight: 600; }}

    QStatusBar {{ background: {c['bg']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
    QStatusBar QLabel {{ color: {c['subtle']}; padding: 0 8px; }}
    QStatusBar::item {{ border: none; }}

    QSplitter::handle {{ background: transparent; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 3px; min-height: 30px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 3px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}

    QMenu {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['text']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
    QMenu::indicator {{ width: 14px; height: 14px; margin-left: 4px; }}

    QWidget#updateBar {{ background: {c['accent_soft']}; border-bottom: 1px solid {c['border']}; }}
    QWidget#updateBar QLabel {{ color: {c['text']}; }}
    """


class AliasDelegate(QStyledItemDelegate):
    """Draws one address: label on top, address below, and an "Inactive" tag when off."""

    ROW_HEIGHT = 46

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        alias = index.data(Qt.UserRole) or {}
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        active = alias.get("isActive", True)
        rect = option.rect.adjusted(2, 1, -2, -1)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 7, 7)
            painter.fillPath(path, QColor(t("accent_soft") if selected else t("hover")))
        if selected:
            bar = QRectF(rect.left() + 1, rect.top() + 8, 3, rect.height() - 16)
            path = QPainterPath()
            path.addRoundedRect(bar, 1.5, 1.5)
            painter.fillPath(path, QColor(t("accent")))

        left = rect.left() + 14
        right = rect.right() - 12

        # Tags on the right: "Inactive", and "Apple" for Sign in with Apple addresses.
        for text, show in (("Inactive", not active), ("Apple", alias.get("siwa"))):
            if not show:
                continue
            tag_font = ui_font(8, QFont.DemiBold)
            w = QFontMetrics(tag_font).horizontalAdvance(text) + 16
            tag = QRectF(right - w, rect.center().y() - 10, w, 20)
            path = QPainterPath()
            path.addRoundedRect(tag, 10, 10)
            painter.fillPath(path, QColor(t("neutral_soft")))
            painter.setFont(tag_font)
            painter.setPen(QColor(t("muted")))
            painter.drawText(tag, Qt.AlignCenter, text)
            right -= w + 6

        label = alias.get("label") or "No label"
        title_font = ui_font(10.5, QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(t("text") if (alias.get("label") and active) else t("muted")))
        title_rect = QRect(left, rect.top() + 4, right - left, 19)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(title_font).elidedText(label, Qt.ElideRight, title_rect.width()))

        sub_font = ui_font(9)
        painter.setFont(sub_font)
        painter.setPen(QColor(t("muted") if active else t("subtle")))
        sub_rect = QRect(left, rect.top() + 22, right - left, 17)
        painter.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(sub_font).elidedText(alias.get("hme", ""), Qt.ElideMiddle, sub_rect.width()))
        painter.restore()
