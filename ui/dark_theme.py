"""Flat Modern Dark/Light темы для приложения «Код Мастер».

Функции apply_dark_theme() и apply_light_theme() задают палитру и глобальную
таблицу стилей, чтобы все окна выглядели современно и единообразно.
"""

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication
from pathlib import Path
from string import Template


_CHECK_ICON = str((Path(__file__).parent.parent / "assets" / "icons" / "check.svg").resolve())

_BASE_QSS = """
QMainWindow, QDialog, QWidget {
    background-color: $bg_window;
    color: $text;
}
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTableView, QTextEdit, QPlainTextEdit {
    background-color: $bg_widget;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px 8px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid $accent;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid $text;
    width: 0px;
    height: 0px;
}
QComboBox QAbstractItemView {
    background-color: $bg_widget;
    color: $text;
    selection-background-color: $accent;
    selection-color: $text_selected;
    border: 1px solid $border;
}
QPushButton {
    background-color: $bg_button;
    color: $text;
    border: none;
    border-radius: 8px;
    padding: 6px 14px;
    min-height: 24px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: $bg_button_hover;
}
QPushButton:pressed {
    background-color: $bg_button_pressed;
}
QPushButton:disabled {
    background-color: $bg_disabled;
    color: $text_disabled;
}
QTableWidget {
    gridline-color: $border;
    background-color: $bg_widget;
    alternate-background-color: $bg_alternate;
}
QTableWidget::item {
    background-color: $bg_widget;
    padding: 4px;
}
QTableWidget::item:alternate {
    background-color: $bg_alternate;
}
QTableWidget::item:selected {
    background-color: $bg_selected;
    color: $text;
}
QHeaderView::section {
    background-color: $bg_button;
    color: $text;
    border: 1px solid $border;
    padding: 6px 8px;
    font-weight: 600;
}
QScrollBar:vertical {
    background: $bg_widget;
    width: 12px;
    border-radius: 6px;
}
QScrollBar::handle:vertical {
    background: $border;
    border-radius: 6px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background: $accent;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    background: $bg_widget;
    height: 12px;
    border-radius: 6px;
}
QScrollBar::handle:horizontal {
    background: $border;
    border-radius: 6px;
    min-width: 20px;
}
QScrollBar::handle:horizontal:hover {
    background: $accent;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}
QTabWidget::pane {
    border: 1px solid $border;
    background-color: $bg_widget;
    border-radius: 8px;
    top: -1px;
}
QTabBar::tab {
    background: $bg_button;
    color: $text;
    padding: 8px 18px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: $accent;
    color: $text_selected;
    font-weight: 600;
}
QTabBar::tab:hover:!selected {
    background: $bg_button_hover;
}
QGroupBox {
    border: 1px solid $border;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 12px;
    padding: 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: $text_title;
}
QProgressBar {
    background-color: $bg_widget;
    border: 1px solid $border;
    border-radius: 6px;
    text-align: center;
    color: $text;
    font-weight: 600;
}
QProgressBar::chunk {
    background-color: $accent;
    border-radius: 6px;
}
QLabel {
    color: $text;
    background-color: transparent;
}
QLabel[title="true"] {
    color: $text_title;
    font-weight: 700;
    font-size: 11pt;
}
QCheckBox {
    color: $text;
    spacing: 8px;
    font-size: 10pt;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid $border;
    background-color: $bg_widget;
    color: $text;
}
QCheckBox::indicator:checked {
    background-color: $accent;
    border-color: $accent;
    image: url("$check_icon");
    color: $text_selected;
}
QCheckBox::indicator:hover {
    background-color: $bg_button_hover;
    border-color: $accent;
}
QRadioButton {
    color: $text;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 1px solid $border;
    background-color: $bg_widget;
}
QRadioButton::indicator:checked {
    background-color: $accent;
    border-color: $accent;
}
QSpinBox {
    min-width: 50px;
    min-height: 24px;
    font-size: 11pt;
    color: $text;
    background-color: $bg_widget;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 2px 4px;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: $bg_button;
    border: 1px solid $border;
    width: 18px;
    min-height: 10px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: $bg_button_hover;
}
QMenuBar {
    background-color: $bg_window;
    color: $text;
}
QMenuBar::item:selected {
    background-color: $bg_button_hover;
}
QMenu {
    background-color: $bg_widget;
    color: $text;
    border: 1px solid $border;
}
QMenu::item:selected {
    background-color: $accent;
    color: $text_selected;
}
QToolTip {
    background-color: $bg_widget;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px 8px;
}
QStatusBar {
    background-color: $bg_panel;
    color: $text_secondary;
    border: none;
    padding: 4px 12px;
}
QStatusBar::item {
    border: none;
}
#topPanel {
    background-color: $bg_panel;
    border: none;
}
#menuPanel {
    background-color: $bg_window;
    border: none;
}
#menuButton {
    background-color: $bg_card;
    color: $text_secondary;
    border: none;
    border-radius: 8px;
    padding: 6px 4px;
    font-weight: 600;
    text-align: center;
}
#menuButton:hover {
    background-color: $bg_card_hover;
    color: $text;
}
#menuButton:checked {
    background-color: $bg_card_checked;
    color: $accent;
    border: 2px solid $accent;
}
#menuButton:checked:hover {
    background-color: $bg_card_checked;
}
QSplitter::handle {
    background-color: $border;
}
QSplitter::handle:horizontal {
    width: 2px;
}
QSplitter::handle:vertical {
    height: 2px;
}
"""


def _apply_theme(app: QApplication, colors: dict) -> None:
    """Применяет тему с заданной цветовой схемой."""
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    if not QFont(font).exactMatch():
        font = QFont("sans-serif", 10)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(colors["bg_window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(colors["bg_widget"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["bg_alternate"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["bg_widget"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(colors["bg_button"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["text_selected"]))
    app.setPalette(palette)

    colors["check_icon"] = _CHECK_ICON
    app.setStyleSheet(Template(_BASE_QSS).safe_substitute(**colors))


def apply_theme(app: QApplication, light: bool = False) -> None:
    """Применяет тёмную или светлую тему по флагу."""
    if light:
        apply_light_theme(app)
    else:
        apply_dark_theme(app)


def apply_dark_theme(app: QApplication) -> None:
    """Применяет современную тёмную тему."""
    _apply_theme(app, {
        "bg_window": "#1E1E2E",
        "bg_panel": "#252538",
        "bg_widget": "#2B2B3C",
        "bg_alternate": "#252538",
        "bg_button": "#3A3A5A",
        "bg_button_hover": "#4A4A6A",
        "bg_button_pressed": "#2A2A4A",
        "bg_card": "#2C2C3E",
        "bg_card_hover": "#3A3A5A",
        "bg_card_checked": "#1E2A4A",
        "bg_disabled": "#2B2B3C",
        "bg_selected": "#4A4A6A",
        "accent": "#6C8CFF",
        "border": "#3A3A4A",
        "text": "#E0E0E8",
        "text_secondary": "#A0A0B0",
        "text_selected": "#FFFFFF",
        "text_title": "#FFFFFF",
        "text_disabled": "#6C6C6C",
    })


def apply_light_theme(app: QApplication) -> None:
    """Применяет светлую тему."""
    _apply_theme(app, {
        "bg_window": "#F5F5FA",
        "bg_panel": "#F0F0F0",
        "bg_widget": "#FFFFFF",
        "bg_alternate": "#F0F0F5",
        "bg_button": "#E0E0EC",
        "bg_button_hover": "#D0D0E0",
        "bg_button_pressed": "#C0C0D0",
        "bg_card": "#FFFFFF",
        "bg_card_hover": "#E8E8F0",
        "bg_card_checked": "#D0D8FF",
        "bg_disabled": "#E8E8F0",
        "bg_selected": "#D0D8FF",
        "accent": "#4A6CFF",
        "border": "#C0C0D0",
        "text": "#000000",
        "text_secondary": "#6A6A7A",
        "text_selected": "#FFFFFF",
        "text_title": "#000000",
        "text_disabled": "#8A8A9A",
    })


def apply_starline_theme(app: QApplication) -> None:
    """Применяет профессиональную тему «StarLine»."""
    _apply_theme(app, {
        "bg_window": "#0D1B2A",
        "bg_panel": "#1B2838",
        "bg_widget": "#1B2838",
        "bg_alternate": "#1B2838",
        "bg_button": "#1B2838",
        "bg_button_hover": "#FF8C00",
        "bg_button_pressed": "#CC7000",
        "bg_card": "#1B2838",
        "bg_card_hover": "#FF8C00",
        "bg_card_checked": "#2A3A4A",
        "bg_disabled": "#2B3A4A",
        "bg_selected": "#2A3A4A",
        "accent": "#FF8C00",
        "border": "#3A4A5A",
        "text": "#FFFFFF",
        "text_secondary": "#E0E0E0",
        "text_selected": "#FFFFFF",
        "text_title": "#FFFFFF",
        "text_disabled": "#6C6C6C",
    })
    app.setStyleSheet(app.styleSheet() + "\nQPushButton { border-radius: 4px; }")
