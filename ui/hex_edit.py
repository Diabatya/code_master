"""Поле ввода одного HEX-байта с автопереходом фокуса."""

from typing import List, Tuple

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QFont, QKeyEvent, QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QScrollArea, QWidget

_HEX_CHARS = set("0123456789ABCDEFabcdef")


class HexDataEdit(QLineEdit):
    """QLineEdit для ввода байта HEX: автопереход вперёд и назад по Backspace."""

    def __init__(self, placeholder: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setMaxLength(2)
        self.setPlaceholderText(placeholder)
        self.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,2}")))
        self.textEdited.connect(self._on_text_edited)
        self._siblings: List[QLineEdit] = []

    def set_siblings(self, siblings: List[QLineEdit]) -> None:
        """Задаёт список соседних полей Data для перехода фокуса."""
        self._siblings = siblings

    def _on_text_edited(self, text: str) -> None:
        upper = text.upper()
        if text != upper:
            self.blockSignals(True)
            self.setText(upper)
            self.blockSignals(False)
            text = upper
        if len(text) == 2 and all(ch in _HEX_CHARS for ch in text):
            self._focus_next()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Backspace and self.text() == "":
            self._focus_prev()
            return
        super().keyPressEvent(event)

    def _focus_next(self) -> None:
        try:
            idx = self._siblings.index(self)
        except ValueError:
            return
        if idx + 1 < len(self._siblings):
            self._siblings[idx + 1].setFocus()

    def _focus_prev(self) -> None:
        try:
            idx = self._siblings.index(self)
        except ValueError:
            return
        if idx > 0:
            prev = self._siblings[idx - 1]
            if prev.isEnabled():
                prev.setFocus()
                prev.selectAll()
            else:
                prev._focus_prev() if isinstance(prev, HexDataEdit) else None


def create_data_field_widget(
    font: QFont, count: int, edit_width: int = 40, placeholder_prefix: str = "D"
) -> Tuple[List[QLineEdit], QWidget]:
    """Создаёт виджет с count полями HexDataEdit. Для count > 8 оборачивает в QScrollArea."""
    edits: List[QLineEdit] = []
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setSpacing(2)
    layout.setContentsMargins(0, 0, 0, 0)
    for i in range(count):
        edit = HexDataEdit(f"{placeholder_prefix}{i}")
        edit.setFixedWidth(edit_width)
        edit.setFont(font)
        edits.append(edit)
    for edit in edits:
        edit.set_siblings(edits)
        layout.addWidget(edit)
    layout.addStretch()

    if count > 8:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(40)
        scroll.setMinimumWidth(280)
        scroll.setMaximumWidth(420)
        scroll.setWidget(container)
        return edits, scroll
    return edits, container
