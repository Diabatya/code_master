"""Поле ввода одного HEX-байта с автопереходом фокуса."""


from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QFont, QKeyEvent, QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QScrollArea, QWidget

_HEX_CHARS = set("0123456789ABCDEFabcdef")


class HexDataEdit(QLineEdit):
    """QLineEdit для ввода байта HEX: автопереход вперёд и назад по Backspace.

    allow_x=True разрешает символ «X» — wildcard-байт: позиция не
    участвует в сравнении (поля приёма и маски кэша триггеров)."""

    def __init__(self, placeholder: str = "", parent=None, allow_x: bool = False) -> None:
        super().__init__(parent)
        self.setMaxLength(2)
        self.setPlaceholderText(placeholder)
        self._allow_x = allow_x
        pattern = "[0-9A-Fa-f]{0,2}" if not allow_x else "([0-9A-Fa-f]{0,2}|[Xx]{1,2})"
        self.setValidator(QRegularExpressionValidator(QRegularExpression(pattern)))
        self.textEdited.connect(self._on_text_edited)
        self._siblings: list[QLineEdit] = []
        # Флаг подавляет автопереход на следующий байт при перезаписи
        # первого символа — курсор шагает посимвольно, а не побайтно.
        self._suppress_autofocus = False

    def set_siblings(self, siblings: list[QLineEdit]) -> None:
        """Задаёт список соседних полей Data для перехода фокуса."""
        self._siblings = siblings

    def _on_text_edited(self, text: str) -> None:
        upper = text.upper()
        if self._allow_x and upper in ("X", "XX"):
            upper = "X"  # wildcard хранится одним символом
        if text != upper:
            self.blockSignals(True)
            self.setText(upper)
            self.blockSignals(False)
            text = upper
        complete = len(text) == 2 and all(ch in _HEX_CHARS for ch in text)
        if (complete or (self._allow_x and text == "X")) and not self._suppress_autofocus:
            self._focus_next()

    @staticmethod
    def _is_wild(text: str) -> bool:
        return text.upper() == "X"

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Backspace and self.text() == "":
            self._focus_prev()
            return
        text = event.text()
        # «X» — не смешивается с цифрами: заменяет всё содержимое поля,
        # а ввод hex поверх wildcard перезаписывает его с нуля.
        if (
            self._allow_x
            and len(text) == 1
            and text.upper() == "X"
            and not self.hasSelectedText()
            and self.text() != "X"
        ):
            self.setText("X")
            self.textEdited.emit("X")
            return
        if (
            len(text) == 1
            and text in _HEX_CHARS
            and not self.hasSelectedText()
            and self._is_wild(self.text())
        ):
            self.setText(text.upper())
            self.setCursorPosition(1)
            self.textEdited.emit(text.upper())
            return
        # Поле заполнено и нет выделения: ввод заменяет символ под
        # курсором (overwrite-режим) — иначе maxLength=2 не давал бы
        # переписать байт без предварительного стирания.
        if (
            len(text) == 1
            and text in _HEX_CHARS
            and not self.hasSelectedText()
            and len(self.text()) == self.maxLength()
        ):
            pos = min(self.cursorPosition(), len(self.text()) - 1)
            new_text = (self.text()[:pos] + text + self.text()[pos + 1 :]).upper()
            # Посимвольный шаг: заменили первый символ байта — курсор
            # остаётся в этом поле на позиции 2; переход к следующему
            # байту только после второго символа (при DLC=8 курсор
            # перескакивает 16 раз). textEdited испускаем сигналом —
            # иначе dirty-tracking («Сохранить») не видел перезапись.
            self._suppress_autofocus = pos + 1 < self.maxLength()
            try:
                self.setText(new_text)
                self.setCursorPosition(pos + 1)
                self.textEdited.emit(new_text)
            finally:
                self._suppress_autofocus = False
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
    font: QFont,
    count: int,
    edit_width: int = 40,
    placeholder_prefix: str = "D",
    allow_x: bool = False,
) -> tuple[list[QLineEdit], QWidget]:
    """Создаёт виджет с count полями HexDataEdit. Для count > 8 оборачивает в QScrollArea."""
    edits: list[QLineEdit] = []
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setSpacing(2)
    layout.setContentsMargins(0, 0, 0, 0)
    for i in range(count):
        edit = HexDataEdit(f"{placeholder_prefix}{i}", allow_x=allow_x)
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
