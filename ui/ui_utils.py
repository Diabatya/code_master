"""Вспомогательные функции для настройки UI-элементов."""

from typing import Optional

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPushButton, QSizePolicy


_INDICATOR_PREFIX = "✓ "


def setup_button(button: QPushButton, bold: bool = False, height: int = 28) -> None:
    """Устанавливает политику размера кнопки по содержимому.

    Args:
        button: Кнопка, которую нужно настроить.
        bold: Использовать ли полужирный шрифт.
        height: Минимальная высота кнопки.
    """
    button.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)
    button.setMinimumHeight(height)
    if bold:
        button.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
    button.adjustSize()


def _indicator_text(button: QPushButton) -> str:
    """Возвращает базовый текст кнопки без префикса-галочки."""
    text = getattr(button, "_base_text", "")
    if text:
        return text
    text = button.text()
    if text.startswith(_INDICATOR_PREFIX):
        text = text[len(_INDICATOR_PREFIX):]
    return text


def _update_checkable_indicator(button: QPushButton, checked: bool) -> None:
    """Добавляет или убирает галочку в начале текста кнопки."""
    base = _indicator_text(button)
    button._base_text = base
    if checked:
        button.setText(_INDICATOR_PREFIX + base)
    else:
        button.setText(base)


def setCheckableWithIndicator(button: QPushButton, text: Optional[str] = None) -> None:
    """Делает кнопку checkable и добавляет ✓ в начало текста при включении.

    Args:
        button: Кнопка, которую нужно настроить.
        text: Базовый текст (если не задан, используется текущий текст кнопки).
    """
    button.setCheckable(True)
    if text is not None:
        button._base_text = text
    button.toggled.connect(lambda checked, b=button: _update_checkable_indicator(b, checked))
    _update_checkable_indicator(button, button.isChecked())
