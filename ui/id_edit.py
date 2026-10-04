"""Поле ввода ID с автоматическим парсингом вставленного пакета."""

from typing import Any
from collections.abc import Callable

from PySide6.QtCore import Qt, QMimeData
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLineEdit

from models.utils import CYRILLIC_HEX_MAP, parse_packet_string


class IdPasteEdit(QLineEdit):
    """QLineEdit для ID, который при вставке пакета заполняет связанные поля."""

    def __init__(
        self,
        fill_callback: Callable[[dict[str, Any]], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._fill_callback = fill_callback

    def set_fill_callback(self, fill_callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._fill_callback = fill_callback

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key.Key_V
            and event.modifiers() in (
                Qt.KeyboardModifier.ControlModifier,
                Qt.KeyboardModifier.MetaModifier,
            )
            and self._fill_callback is not None
        ):
            text = QApplication.clipboard().text()
            parsed = parse_packet_string(text)
            if parsed is not None:
                self._fill_callback(parsed)
                return
        # Кириллица на той же клавише раскладки, что латинские HEX-цифры
        # A-F (не переключил раскладку) — подменяем на латиницу прямо
        # при вводе, не отдавая оригинальный символ валидатору (отчёт
        # мастера: касается всех HEX-полей ID во всём приложении).
        typed = event.text()
        if typed in CYRILLIC_HEX_MAP:
            self.insert(CYRILLIC_HEX_MAP[typed])
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        """Обрабатывает вставку из контекстного меню и drag-and-drop."""
        if self._fill_callback is not None and source.hasText():
            parsed = parse_packet_string(source.text())
            if parsed is not None:
                self._fill_callback(parsed)
                return
        super().insertFromMimeData(source)
