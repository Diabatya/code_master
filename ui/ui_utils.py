"""Вспомогательные функции для настройки UI-элементов."""


from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPolygon
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from models.translations import _ as tr


_INDICATOR_PREFIX = "✓ "

_graveyard: QWidget | None = None


class DirtyBadge(QWidget):
    """Оранжевый треугольник в левом верхнем углу элемента — программа
    ГЛ/триггер отличается от записанной в устройство (отчёт мастера:
    замена кнопки «Что изменится»). Размер — как кнопка закрытия."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setToolTip(
            tr("Отличается от записанного в устройство — сохраните")
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FF9800"))
        rect = self.rect()
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(rect.left(), rect.top()),
                    QPoint(rect.right(), rect.top()),
                    QPoint(rect.left(), rect.bottom()),
                ]
            )
        )
        painter.end()


def retire(widget: QWidget) -> None:
    """Прячет виджет на «кладбище» вместо deleteLater().

    DeferredDelete, исполненный посреди processEvents() внутри serial-сессии,
    приводит к нативному падению Qt на Windows. Удаляемые строки/панели
    просто перепрятываются на скрытый контейнер — C++-объект живёт до
    выхода приложения, события и сигналы остаются валидными.
    """
    global _graveyard
    if widget is None:
        return
    widget.hide()
    widget.setEnabled(False)
    if _graveyard is None:
        _graveyard = QWidget()
    widget.setParent(_graveyard)


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


def setCheckableWithIndicator(button: QPushButton, text: str | None = None) -> None:
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
