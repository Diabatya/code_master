"""Неблокирующее всплывающее уведомление («тост») в углу окна.

Заменяет модальный QMessageBox.information для рутинных подтверждений
(«Конфигурация сохранена» и т.п.) — оператору не нужно закрывать окно
рукой: сообщение само появляется, висит пару секунд и растворяется.
Ошибки и вопросы остаются модальными — тост только информирует.
"""

from __future__ import annotations


from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from shiboken6 import isValid
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

_DURATION_MS = 2600
_FADE_MS = 350


class Toast(QLabel):
    """Плавающий лейбл-уведомление поверх родительского окна."""

    _active: list[Toast] = []
    # Скрытые тосты переиспользуются вместо deleteLater: DeferredDelete
    # от тоста срабатывал посреди processEvents() внутри serial-сессии
    # опроса статистики и нативно ронял приложение на Windows ~3 с
    # после заводского сброса (тост «Сброшено» + анимация прозрачности —
    # отчёт мастера, логи 31/32).
    _pooled: list[Toast] = []

    def __init__(self, parent: QWidget, text: str, success: bool = True) -> None:
        super().__init__(text, parent)
        self.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowFlags(Qt.WindowType.SubWindow)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._setup(text, success)

    def _setup(self, text: str, success: bool) -> None:
        """Текст/цвет для нового или переиспользованного тоста."""
        self.setText(text)
        # Серый вместо зелёного — яркая зелёная плашка в углу читалась
        # как тревожный/праздничный акцент (отчёт мастера); анимация
        # появления/исчезновения (fade) уже есть ниже.
        color = "#4A4A58" if success else "#E65100"
        self.setStyleSheet(
            f"color: #FFFFFF; background-color: {color};"
            "border-radius: 8px; padding: 8px 16px;"
        )
        self.adjustSize()

    @classmethod
    def _take(cls, parent: QWidget, text: str, success: bool) -> Toast:
        """Тост из пула (тот же parent) либо новый экземпляр."""
        for idx, toast in enumerate(cls._pooled):
            try:
                alive = toast.parentWidget() is parent
            except RuntimeError:
                alive = False  # parent/тост уже разрушен на стороне C++
            if not alive:
                continue
            cls._pooled.pop(idx)
            toast._setup(text, success)
            return toast
        return cls(parent, text, success)

    @classmethod
    def show_message(
        cls, parent: QWidget | None, text: str, success: bool = True
    ) -> Toast | None:
        """Показывает тост в нижнем правом углу parent и самоуничтожается.

        Несколько тостов подряд стопкой уезжают вверх, а не перекрывают
        друг друга."""
        if parent is None:
            return None
        toast = cls._take(parent, text, success)
        toast._opacity.setOpacity(0.0)
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
        if not isValid(self) or not isValid(self._opacity):
            return
        fade = QPropertyAnimation(self._opacity, b"opacity", self)
        fade.setDuration(_FADE_MS)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        fade.finished.connect(self._dismiss)
        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_out_anim = fade

    def _dismiss(self) -> None:
        if not isValid(self):
            return
        if self in Toast._active:
            Toast._active.remove(self)
            parent = self.parentWidget()
            if parent is not None:
                Toast._restack(parent)
        self.hide()
        self._opacity.setOpacity(0.0)
        # Без deleteLater — виджет прячется в пул и переиспользуется
        # следующим тостом (см. комментарий у _pooled).
        Toast._pooled.append(self)

    @classmethod
    def _restack(cls, parent: QWidget) -> None:
        """Раскладывает живые тосты стопкой от нижнего правого угла вверх."""
        margin = 16
        y = parent.height() - margin
        # Отложенные singleShot могут прийти к тосту, чей parent уже
        # разрушен — dead-ref в списке не должен ронять остальные.
        cls._active[:] = [t for t in cls._active if isValid(t)]
        cls._pooled[:] = [t for t in cls._pooled if isValid(t)]
        for toast in reversed(cls._active):
            if toast.parentWidget() is not parent:
                continue
            y -= toast.height()
            toast.move(parent.width() - toast.width() - margin, y)
            y -= 8


def show_toast(parent: QWidget | None, text: str, success: bool = True) -> None:
    """Короткий помощник: показать тост поверх окна parent."""
    Toast.show_message(parent, text, success)
