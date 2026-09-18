"""Неблокирующее всплывающее уведомление («тост») в углу окна.

Заменяет модальный QMessageBox.information для рутинных подтверждений
(«Конфигурация сохранена» и т.п.) — оператору не нужно закрывать окно
рукой: сообщение само появляется, висит пару секунд и растворяется.
Ошибки и вопросы остаются модальными — тост только информирует.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

_DURATION_MS = 2600
_FADE_MS = 350


class Toast(QLabel):
    """Плавающий лейбл-уведомление поверх родительского окна."""

    _active: "list[Toast]" = []

    def __init__(self, parent: QWidget, text: str, success: bool = True) -> None:
        super().__init__(text, parent)
        self.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        color = "#4CAF50" if success else "#E65100"
        self.setStyleSheet(
            f"color: #FFFFFF; background-color: {color};"
            "border-radius: 8px; padding: 8px 16px;"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowFlags(Qt.WindowType.SubWindow)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self.adjustSize()

    @classmethod
    def show_message(
        cls, parent: Optional[QWidget], text: str, success: bool = True
    ) -> "Toast | None":
        """Показывает тост в нижнем правом углу parent и самоуничтожается.

        Несколько тостов подряд стопкой уезжают вверх, а не перекрывают
        друг друга."""
        if parent is None:
            return None
        toast = cls(parent, text, success)
        cls._active.append(toast)
        cls._restack(parent)
        toast.show()
        toast.raise_()

        fade_in = QPropertyAnimation(toast._opacity, b"opacity", toast)
        fade_in.setDuration(_FADE_MS)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade_in.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        toast._fade_in = fade_in  # удержать ссылку

        QTimer.singleShot(_DURATION_MS, toast._fade_out)
        return toast

    def _fade_out(self) -> None:
        fade = QPropertyAnimation(self._opacity, b"opacity", self)
        fade.setDuration(_FADE_MS)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        fade.finished.connect(self._dismiss)
        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_out_anim = fade

    def _dismiss(self) -> None:
        if self in Toast._active:
            Toast._active.remove(self)
            parent = self.parentWidget()
            if parent is not None:
                Toast._restack(parent)
        self.hide()
        self.deleteLater()

    @classmethod
    def _restack(cls, parent: QWidget) -> None:
        """Раскладывает живые тосты стопкой от нижнего правого угла вверх."""
        margin = 16
        y = parent.height() - margin
        for toast in reversed(cls._active):
            if toast.parentWidget() is not parent:
                continue
            y -= toast.height()
            toast.move(parent.width() - toast.width() - margin, y)
            y -= 8


def show_toast(parent: Optional[QWidget], text: str, success: bool = True) -> None:
    """Короткий помощник: показать тост поверх окна parent."""
    Toast.show_message(parent, text, success)
