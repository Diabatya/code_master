"""Страница «Гибкая логика» — программы ЕСЛИ-ПРИ-ТО.

Каждая программа — карточка: шапка (галочка включения, имя, сводка,
свёртка, ✕) и три колонки с союзами между ними (отчёт мастера):

    ЕСЛИ [Событие]  ПРИ [Условие]  ТО [Действие]

Событие — один из четырёх видов:
* «Динамическая переменная» — выбор из динамических переменных
  раздела «Переменные», «Стало больше»/«Стало меньше» и порог;
  программа срабатывает при переходе величины через порог;
* «Статическая переменная» — выбор из статических переменных;
  отрабатывает и на пакет включения, и на пакет выключения
  (фильтр фронта «Изменилась / Включилась / Выключилась»);
* «Доп канал» — фронт состояния OUT1-4 («Активен»/«Не активен»);
  состояние каналов зеркалируется ПК из отправленных команд CMD_AUX_SET;
* «Фрейм» — ручной ввод CAN-пакета (канал, битность, ID, DLC,
  DATA по байтам с «X», «Сработок на DATA») — приход такого кадра
  запускает программу.

Настроенное событие/условие сворачивается до одной строки-сводки
(«Спорт вкл», «Обороты ДВС стали больше 1500», «Доп канал №1 активен»);
клик по строке разворачивает редактор с анимацией — отчёт мастера.

Условие — необязательная проверка текущего состояния перед действием:
статическая переменная (активна/не активна), динамическая
(больше/меньше/равно порогу), доп. канал (активен/не активен).

Действие — установка переменной из раздела «Управление», ручной
CAN-фрейм (байт «X» подставляется из кадра-события), доп. канал
OUT1-4 («Вкл»/«Выкл»/«Подать импульсы»/«Вкл ШИМ» с паузой до начала
и графиком импульсов — исполняет МК по CMD_AUX_SET, см. aux_out.h)
и/или «Автоматическая запись DATA в кэш» — по образцу триггеров:
приходящий кадр, подходящий под спецификацию источника, кэшируется,
а при срабатывании программы отправляется последний сохранённый кадр.

Сохранение — в общий конфиг приложения (ключ flexible_rules). Старый
формат правил (id/mask/condition_data → resp_*) автоматически
мигрирует в событие «Фрейм» + действие «Фрейм».
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Slot
from shiboken6 import isValid
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int
from ui.hex_edit import create_data_field_widget
from ui.ui_utils import setup_button
from ui.variables_tab import _HexIdEdit, _tokens_match

logger = get_logger(__name__)

_EVENT_NONE = "none"
_EVENT_DYN = "dyn"
_EVENT_NUM = "num"   # «Численная переменная» — отдельный вид (отчёт мастера)
_EVENT_IMPULSE = "impulse"  # «Импульсная переменная» (отчёт мастера)
_EVENT_STATIC = "static"
_EVENT_AUX = "aux"
_EVENT_FRAME = "frame"

_COND_NONE = "none"
_COND_STATIC = "static"
_COND_DYN = "dyn"
_COND_NUM = "num"    # «Численная переменная» (отчёт мастера)
_COND_IMPULSE = "impulse"  # «Импульсная переменная» (отчёт мастера)
_COND_AUX = "aux"

_ACT_NONE = "none"
_ACT_VAR = "var"
_ACT_FRAME = "frame"
_ACT_AUX = "aux"
_ACT_CACHE = "cache"

# Каналы в спецификациях фреймов ГЛ: 0 — CAN1, 1 — CAN2,
# 2 — «CAN1 или CAN2» (отчёт мастера: слово «Любой» заменено).
_CHANNELS_ANY = ("CAN1", "CAN2", "")
_ANY_CHANNEL_TEXT = "CAN1 или CAN2"
_BIT_RATES = ("11 Бит", "29 Бит")

# Голубая рамка вокруг каждого события/условия/действия
# (отчёт мастера).
_ITEM_FRAME_STYLE = (
    "{cls} {{ border: 1px solid #3A7BD5; border-radius: 6px;"
    " background: rgba(58,123,213,0.06); }}"
)


def _set_data_enabled(edits: list[QLineEdit], count: int) -> None:
    """DLC ограничивает поля DATA: за пределами DLC поля пустые
    и неактивные — как в триггерах (отчёт мастера)."""
    for i, edit in enumerate(edits):
        if i >= count:
            edit.setText("")
            edit.setEnabled(False)
        else:
            edit.setEnabled(True)


def _tokens_to_text(edits: list[QLineEdit]) -> str:
    tokens = [e.text().strip().upper() for e in edits]
    while tokens and tokens[-1] == "":
        tokens.pop()
    return " ".join(tokens)


def _text_to_tokens(edits: list[QLineEdit], text: Any) -> None:
    tokens = (
        [str(t) for t in text]
        if isinstance(text, (list, tuple))
        else str(text or "").replace(",", " ").split()
    )
    for i, edit in enumerate(edits):
        edit.setText(tokens[i].upper() if i < len(tokens) else "")


def _tokens_range_match(lo: list[str], hi: list[str], data: bytes) -> bool:
    """Проверка «От–До»: «»/«X» — байт игнорируется."""
    for i in range(max(len(lo), len(hi))):
        lo_t = lo[i] if i < len(lo) else ""
        hi_t = hi[i] if i < len(hi) else ""
        if (not lo_t or lo_t == "X") and (not hi_t or hi_t == "X"):
            continue
        lo_v = hex_to_int(lo_t) if lo_t and lo_t != "X" else 0x00
        hi_v = hex_to_int(hi_t) if hi_t and hi_t != "X" else 0xFF
        byte = data[i] if i < len(data) else 0
        if lo_v is None:
            lo_v = 0
        if hi_v is None:
            hi_v = 0xFF
        if byte < lo_v or byte > hi_v:
            return False
    return True


def _interpolate(points: list[Any], raw: float) -> float:
    """Линейная интерполяция по точкам графика динамической переменной."""
    pts: list[tuple[float, float]] = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        pts.append((x, y))
    if not pts:
        return raw
    pts.sort()
    if raw <= pts[0][0]:
        return pts[0][1]
    if raw >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= raw <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
    return pts[-1][1]


def _inverse_interpolate(points: list[Any], value: float) -> float:
    """Обратная интерполяция: величина → сырое значение байт."""
    pts: list[tuple[float, float]] = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        pts.append((x, y))
    if len(pts) < 2:
        return value
    pts.sort(key=lambda p: p[1])
    if value <= pts[0][1]:
        return pts[0][0]
    if value >= pts[-1][1]:
        return pts[-1][0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if y0 <= value <= y1:
            if y1 == y0:
                return x0
            return x0 + (x1 - x0) * (value - y0) / (y1 - y0)
    return pts[-1][0]


def _small_label(text: str, font: QFont) -> QLabel:
    label = QLabel(text)
    label.setFont(font)
    return label


def _connector(text: str) -> QLabel:
    """Союз между колонками: ЕСЛИ / ПРИ / ТО (отчёт мастера)."""
    label = QLabel(text)
    label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
    label.setStyleSheet("color: #7C9EFF;")
    return label


def _close_button(font: QFont, tooltip: str) -> QPushButton:
    """Голубой крестик закрытия события/условия (отчёт мастера)."""
    button = QPushButton("✕")
    button.setFont(QFont(font.family(), font.pointSize(), QFont.Weight.Bold))
    button.setFixedSize(20, 20)
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(
        "QPushButton { color: #7C9EFF; border: none;"
        " background: transparent; padding: 0; }"
        "QPushButton:hover { color: #AEC6FF; }"
        "QPushButton:pressed { color: #5A7FD5; }"
    )
    return button


class _ClickableSummary(QLabel):
    """Строка-сводка события/условия: клик разворачивает редактор."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.setStyleSheet("color: #7C9EFF;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            p = self.parent()
            toggle = getattr(p, "_toggle_editor", None)
            if callable(toggle):
                toggle()
        super().mousePressEvent(event)


class _PulsePreview(QWidget):
    """Мини-график импульсов доп. канала: меандр с длительностью
    импульса и паузой из полей настройки (отчёт мастера)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._on_ms = 100
        self._off_ms = 100
        self._count = 3
        self.setMinimumHeight(56)
        self.setMaximumHeight(64)

    def set_params(self, on_ms: int, off_ms: int, count: int) -> None:
        self._on_ms = max(1, on_ms)
        self._off_ms = max(0, off_ms)
        self._count = max(1, min(20, count))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        top, bottom = 8, h - 14
        mid_hi, mid_lo = top, bottom
        # Ось времени.
        painter.setPen(QPen(Qt.GlobalColor.gray, 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, mid_lo, w, mid_lo)
        # Меандр: on_ms/high, off_ms/low, count периодов.
        pen = QPen(QColor("#7C9EFF"), 2)
        painter.setPen(pen)
        total = self._count * (self._on_ms + max(1, self._off_ms))
        scale = (w - 8) / total if total else 1.0
        x = 4.0
        y = mid_lo
        path = QPainterPath()
        path.moveTo(x, y)
        for _i in range(self._count):
            on_w = self._on_ms * scale
            off_w = max(1, self._off_ms) * scale
            path.lineTo(x, mid_hi)          # фронт вверх
            path.lineTo(x + on_w, mid_hi)   # импульс
            path.lineTo(x + on_w, mid_lo)   # спад вниз
            x += on_w + off_w
            y = mid_lo
            path.lineTo(x, mid_lo)          # пауза (низкий уровень)
        painter.drawPath(path)
        # Подписи первого импульса и паузы.
        painter.setPen(QPen(QColor("#9A9AA5")))
        painter.setFont(QFont("Segoe UI", 7))
        on_w = self._on_ms * scale
        painter.drawText(
            4, mid_hi - 3, int(max(40, on_w)), 10,
            Qt.AlignmentFlag.AlignLeft, f"{self._on_ms} мс",
        )
        painter.drawText(
            int(4 + on_w), mid_lo - 10, 80, 10,
            Qt.AlignmentFlag.AlignLeft, f"пауза {self._off_ms} мс",
        )


class _VarCombo(QComboBox):
    """Комбобокс выбора переменной из вкладки «Переменные».

    Список имён подгружается позже (когда вкладка переменных привязана
    к ГЛ), поэтому выбранное имя запоминается в «desired» и
    восстанавливается при появлении в списке — иначе сохранённая
    привязка программы терялась бы при первом открытии."""

    def __init__(self, font: QFont, parent=None) -> None:
        super().__init__(parent)
        self.setFont(font)
        self._desired = ""

    def set_names(self, names: list[str], placeholder: str) -> None:
        current = self.get_name()
        self.blockSignals(True)
        self.clear()
        self.addItem(placeholder, "")
        for name in names:
            self.addItem(name, name)
        idx = self.findText(current) if current else -1
        self.setCurrentIndex(idx if idx >= 1 else 0)
        if idx < 1 and current:
            self._desired = current
        elif idx >= 1:
            self._desired = ""
        self.blockSignals(False)

    def set_name(self, name: str) -> None:
        idx = self.findText(name)
        if idx >= 1:
            self.setCurrentIndex(idx)
            self._desired = ""
        else:
            self.setCurrentIndex(0)
            self._desired = name

    def get_name(self) -> str:
        if self.currentIndex() >= 1:
            return self.currentText()
        return self._desired


class _DynEventPage(QWidget):
    """Событие «Динамическая/Численная переменная»: выбор переменной,
    «Стало больше/меньше», порог из графика переменной.
    ``event_type`` — сохраняемый тип события («dyn»/«num»): оба вида
    числовых переменных — отдельные пункты меню (отчёт мастера)."""

    def __init__(
        self,
        font: QFont,
        mark_dirty,
        event_type: str = _EVENT_DYN,
        get_tab=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._event_type = event_type
        self._get_tab = get_tab
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)

        self.direction: QComboBox | None = None
        self.value: QLineEdit | None = None
        self.value_combo: QComboBox | None = None
        if event_type == _EVENT_DYN:
            # «Динамическая переменная»: событие — переход в указанное
            # состояние из таблицы привязки, выбор из реального списка
            # имён, а не произвольный текст (отчёт мастера).
            layout.addWidget(_small_label(tr("Состояние:"), font))
            self.value_combo = QComboBox()
            self.value_combo.setFont(font)
            self.value_combo.setEditable(True)
            layout.addWidget(self.value_combo)
            self.value_combo.currentTextChanged.connect(mark_dirty)
        else:
            row = QHBoxLayout()
            self.direction = QComboBox()
            self.direction.setFont(font)
            self.direction.addItem(tr("Стало больше"), "gt")
            self.direction.addItem(tr("Стало меньше"), "lt")
            row.addWidget(self.direction)
            self.value = QLineEdit()
            self.value.setFont(font)
            self.value.setPlaceholderText(tr("значение"))
            self.value.setFixedWidth(90)
            row.addWidget(self.value)
            row.addStretch()
            layout.addLayout(row)
            self.direction.currentIndexChanged.connect(mark_dirty)
            self.value.textChanged.connect(mark_dirty)
        layout.addStretch()

        self.var.currentIndexChanged.connect(mark_dirty)
        if self.value_combo is not None:
            self.var.currentIndexChanged.connect(
                lambda *_a: self.refresh_binding_names()
            )

    def refresh_binding_names(self) -> None:
        """Заполняет список состояний реальными именами из таблицы
        привязки выбранной «Динамической переменной» (отчёт мастера)."""
        if self.value_combo is None:
            return
        current = self.value_combo.currentText()
        tab = self._get_tab() if self._get_tab is not None else None
        names = (
            tab.variable_binding_names("read", self.var.get_name())
            if tab and self.var.get_name()
            else []
        )
        self.value_combo.blockSignals(True)
        self.value_combo.clear()
        self.value_combo.addItems(names)
        idx = self.value_combo.findText(current)
        if idx >= 0:
            self.value_combo.setCurrentIndex(idx)
        else:
            self.value_combo.setEditText(current)
        self.value_combo.blockSignals(False)

    def read(self) -> dict[str, Any]:
        if self.value_combo is not None:
            return {
                "type": self._event_type,
                "var": self.var.get_name(),
                "dir": "eq",
                "value": self.value_combo.currentText().strip(),
            }
        return {
            "type": self._event_type,
            "var": self.var.get_name(),
            "dir": self.direction.currentData(),
            "value": self.value.text().strip(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))
        if self.value_combo is not None:
            self.refresh_binding_names()
            self.value_combo.setEditText(str(event.get("value", "")))
            return
        didx = self.direction.findData(event.get("dir", "gt"))
        self.direction.setCurrentIndex(didx if didx >= 0 else 0)
        self.value.setText(str(event.get("value", "")))


class _StaticEventPage(QWidget):
    """Событие «Статическая переменная»: срабатывает на пакет,
    включающий переменную, и на пакет, выключающий её (отчёт мастера);
    фронт можно сузить выбором."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)

        layout.addWidget(_small_label(tr("Отрабатывает:"), font))
        self.edge = QComboBox()
        self.edge.setFont(font)
        # Флаг статической переменной — выбор «1»/«0»
        # (отчёт мастера); «любая смена» сохранена для программ,
        # которые ждут любого перехода бита.
        self.edge.addItem("1", "on")
        self.edge.addItem("0", "off")
        self.edge.addItem(tr("1 или 0"), "both")
        layout.addWidget(self.edge)
        layout.addStretch()

        self.var.currentIndexChanged.connect(mark_dirty)
        self.edge.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_STATIC,
            "var": self.var.get_name(),
            "edge": self.edge.currentData(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))
        eidx = self.edge.findData(event.get("edge", "both"))
        self.edge.setCurrentIndex(eidx if eidx >= 0 else 0)


class _CondVarPage(QWidget):
    """Страница условия по числовой переменной: имя переменной +
    «Больше/Меньше/Равно» + порог. Одинакова для «Динамической»
    (МК-кэш) и «Численной» переменной — это разные пункты меню,
    фильтрующие список имён по виду переменной (отчёт мастера)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        row = QHBoxLayout()
        self.op = QComboBox()
        self.op.setFont(font)
        self.op.addItem(tr("Больше"), "gt")
        self.op.addItem(tr("Меньше"), "lt")
        self.op.addItem(tr("Равно"), "eq")
        row.addWidget(self.op)
        self.value = QLineEdit()
        self.value.setFont(font)
        self.value.setPlaceholderText(tr("значение"))
        self.value.setFixedWidth(80)
        row.addWidget(self.value)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

        self.var.currentIndexChanged.connect(mark_dirty)
        self.op.currentIndexChanged.connect(mark_dirty)
        self.value.textChanged.connect(mark_dirty)


class _ImpulseVarPage(QWidget):
    """Страница «Импульсная переменная» — имя переменной + подпись
    смысла: в событиях «получено» (срабатывает на каждый импульс —
    совпадение DATA на шине), в условиях — истинно, пока импульс
    активен (0.5 с — отчёт мастера)."""

    def __init__(
        self,
        font: QFont,
        mark_dirty,
        caption: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        if caption:
            label = QLabel(caption)
            label.setFont(font)
            label.setStyleSheet("color: #9A9AA5;")
            layout.addWidget(label)
        layout.addStretch()
        self.var.currentIndexChanged.connect(mark_dirty)


class _AuxEventPage(QWidget):
    """Событие «Доп канал»: номер канала и состояние
    «Активен»/«Не активен» (отчёт мастера)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(_small_label(tr("Доп канал №"), font))
        self.channel = QSpinBox()
        self.channel.setFont(font)
        self.channel.setRange(1, 8)
        self.channel.setValue(1)
        self.channel.setFixedWidth(64)
        row.addWidget(self.channel)
        self.state = QComboBox()
        self.state.setFont(font)
        self.state.addItem(tr("Активен"), 1)
        self.state.addItem(tr("Не активен"), 0)
        row.addWidget(self.state)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

        self.channel.valueChanged.connect(mark_dirty)
        self.state.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_AUX,
            "channel": self.channel.value(),
            "state": self.state.currentData(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.channel.setValue(int(event.get("channel", 1) or 1))
        sidx = self.state.findData(int(event.get("state", 1) or 0))
        self.state.setCurrentIndex(sidx if sidx >= 0 else 0)


class _FrameEventPage(QWidget):
    """Событие «Фрейм»: ручной ввод CAN-пакета — настройки как
    в триггерах (канал, битность, ID, DLC, DATA по байтам с «X»,
    RTR — срабатывание по кадру-запросу, «Количество сработок до
    смены DATA» — галочкой, как в триггерах — отчёт мастера)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Канал"), font))
        self.channel = QComboBox()
        self.channel.setFont(font)
        for i, name in enumerate(_CHANNELS_ANY):
            self.channel.addItem(name or tr(_ANY_CHANNEL_TEXT), i)
        self.channel.setFixedWidth(120)
        row1.addWidget(self.channel)
        row1.addWidget(_small_label(tr("Бит"), font))
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItems(_BIT_RATES)
        # Шире — слово «Бит» должно влезать целиком (отчёт мастера).
        self.bit.setFixedWidth(92)
        row1.addWidget(self.bit)
        row1.addStretch()
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(_small_label("ID", font))
        self.can_id = _HexIdEdit(font)
        row2.addWidget(self.can_id)
        row2.addWidget(_small_label("DLC", font))
        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(1, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(54)
        row2.addWidget(self.dlc)
        # RTR вместо «Из DBC» (отчёт мастера): программа стартует
        # по приходу кадра-запроса Remote Transmission Request —
        # у RTR-кадра DATA нет, поля блокируются.
        self.rtr = QCheckBox(tr("RTR"))
        self.rtr.setFont(font)
        self.rtr.setToolTip(
            tr("Срабатывать на RTR-запрос (Remote Transmission Request) "
               "с этим ID — DATA у такого кадра отсутствует")
        )
        row2.addWidget(self.rtr)
        row2.addStretch()
        layout.addLayout(row2)

        self._data_label = _small_label("DATA (X — любой байт)", font)
        layout.addWidget(self._data_label)
        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=32, allow_x=True
        )
        layout.addWidget(data_widget)

        # «Количество сработок до смены DATA» — галочкой, как в
        # триггерах (отчёт мастера): без галочки каждый подошедший
        # кадр запускает программу.
        row3 = QHBoxLayout()
        self.fire_check = QCheckBox(tr("Количество сработок до смены DATA"))
        self.fire_check.setFont(font)
        self.fire_check.setToolTip(
            tr("Отработать N кадров с одинаковой Data и молчать до смены "
               "содержимого; новая Data запускает счёт заново")
        )
        row3.addWidget(self.fire_check)
        self.fire_limit = QSpinBox()
        self.fire_limit.setFont(font)
        self.fire_limit.setRange(1, 99)
        self.fire_limit.setValue(1)
        self.fire_limit.setFixedWidth(64)
        self.fire_limit.setEnabled(False)
        row3.addWidget(self.fire_limit)
        row3.addStretch()
        layout.addLayout(row3)
        layout.addStretch()

        self.channel.currentIndexChanged.connect(mark_dirty)
        self.bit.currentIndexChanged.connect(mark_dirty)
        self.can_id.textChanged.connect(mark_dirty)
        self.dlc.valueChanged.connect(mark_dirty)
        self.fire_check.toggled.connect(self.fire_limit.setEnabled)
        self.fire_check.toggled.connect(mark_dirty)
        self.fire_limit.valueChanged.connect(mark_dirty)
        for edit in self.data:
            edit.textChanged.connect(mark_dirty)
        self.dlc.valueChanged.connect(
            lambda v: _set_data_enabled(self.data, 0 if self.rtr.isChecked() else v)
        )
        self.rtr.toggled.connect(self._on_rtr_toggled)
        self.rtr.toggled.connect(mark_dirty)
        _set_data_enabled(self.data, self.dlc.value())

    def _on_rtr_toggled(self, checked: bool) -> None:
        """RTR-кадр данных не несёт — поля DATA недоступны (как
        в триггерах)."""
        self._data_label.setEnabled(not checked)
        _set_data_enabled(self.data, 0 if checked else self.dlc.value())

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_FRAME,
            "channel": self.channel.currentData(),
            "extended": self.bit.currentIndex() == 1,
            "id": self.can_id.text().strip(),
            "dlc": self.dlc.value(),
            "data": _tokens_to_text(self.data),
            "rtr": self.rtr.isChecked(),
            # 0 — без ограничения (галочка снята): каждый подошедший
            # кадр запускает программу, как в триггерах.
            "fire_limit": self.fire_limit.value()
            if self.fire_check.isChecked() else 0,
        }

    def write(self, event: dict[str, Any]) -> None:
        ch = int(event.get("channel", 2))
        cidx = self.channel.findData(ch)
        self.channel.setCurrentIndex(cidx if cidx >= 0 else 2)
        self.bit.setCurrentIndex(1 if event.get("extended") else 0)
        self.can_id.setText(str(event.get("id", "")))
        self.dlc.setValue(int(event.get("dlc", 8)))
        self.rtr.setChecked(bool(event.get("rtr", False)))
        _text_to_tokens(self.data, event.get("data"))
        _set_data_enabled(
            self.data, 0 if self.rtr.isChecked() else self.dlc.value()
        )
        limit = int(event.get("fire_limit", 1) or 0)
        # Старые конфиги: явный fire_limit ≥ 1 → галочка включена.
        self.fire_check.setChecked(limit > 0)
        self.fire_limit.setValue(max(1, limit))


def _detach_item(item, sep) -> None:
    """Отсоединяет и удаляет пункт события/условия/действия (и его
    разделитель) — выполняется на следующем тике после клика по
    крестику, см. _remove_event/_remove_cond/_remove_action."""
    if isValid(item):
        item.setParent(None)
        item.deleteLater()
    if sep is not None and isValid(sep):
        sep.setParent(None)
        sep.deleteLater()


def _animate_editor_toggle(item) -> None:
    """Сворачивает/разворачивает редактор пункта анимацией высоты.

    Обработчик finished() хранится в атрибуте, чтобы disconnect()
    реально снимал старую лямбду: иначе они копятся и выстреливают по
    уже удалённому редактору (падение приложения при закрытии условия).
    """
    editor = item._editor
    anim = item._editor_anim
    expanded = editor.maximumHeight() != 0
    end = 0 if expanded else max(1, editor.sizeHint().height())
    final = 0 if expanded else 16777215
    old = getattr(item, "_anim_finished_cb", None)
    if old is not None:
        with contextlib.suppress(RuntimeError, TypeError):
            anim.finished.disconnect(old)
        item._anim_finished_cb = None

    def _apply_final(v: int = final) -> None:
        if isValid(editor):
            editor.setMaximumHeight(v)

    item._anim_finished_cb = _apply_final
    anim.finished.connect(_apply_final)
    anim.stop()
    anim.setStartValue(editor.maximumHeight())
    anim.setEndValue(end)
    anim.start()


class _EventItem(QWidget):
    """Одно событие программы: компактная строка «имя · функция»,
    выбор типа события, страница настроек и крестик удаления
    (в программе событий может быть несколько — ИЛИ, отчёт мастера)."""

    _PAGES = (_EVENT_NONE, _EVENT_STATIC, _EVENT_DYN, _EVENT_NUM,
              _EVENT_IMPULSE, _EVENT_AUX, _EVENT_FRAME)

    def __init__(self, row: RuleRowWidget, font: QFont, event: dict | None = None) -> None:
        super().__init__(row)
        self._row = row
        self.setStyleSheet(_ITEM_FRAME_STYLE.format(cls="_EventItem"))
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 4, 6, 6)

        # Компактная строка: «Обороты ДВС стали больше 1500» — клик
        # разворачивает редактор с анимацией (отчёт мастера).
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._summary = _ClickableSummary(self)
        head.addWidget(self._summary, 1)
        self._remove = _close_button(font, tr("Удалить событие"))
        self._remove.clicked.connect(lambda: row._remove_event(self))
        head.addWidget(self._remove)
        layout.addLayout(head)

        # Редактор события — скрывается целиком после настройки,
        # программа остаётся одной строкой (отчёт мастера).
        self._editor = QWidget()
        editor_layout = QVBoxLayout(self._editor)
        editor_layout.setSpacing(4)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        self._type = QComboBox()
        self._type.setFont(font)
        # Новая программа стартует с «Не выбрано» — оператор сам
        # задаёт тип события (отчёт мастера).
        self._type.addItem(tr("Не выбрано"), _EVENT_NONE)
        self._type.addItem(tr("Статическая переменная"), _EVENT_STATIC)
        self._type.addItem(tr("Динамическая переменная"), _EVENT_DYN)
        self._type.addItem(tr("Численная переменная"), _EVENT_NUM)
        self._type.addItem(tr("Импульсная переменная"), _EVENT_IMPULSE)
        self._type.addItem(tr("Доп канал"), _EVENT_AUX)
        self._type.addItem(tr("Фрейм"), _EVENT_FRAME)
        self._type.currentIndexChanged.connect(self._on_type)
        editor_layout.addWidget(self._type)

        self._stack = QStackedWidget()
        none_label = _small_label(tr("— не выбрано —"), font)
        self._none = QWidget()
        QVBoxLayout(self._none).addWidget(none_label)
        self._static = _StaticEventPage(font, row._mark_dirty)
        self._dyn = _DynEventPage(
            font, row._mark_dirty, _EVENT_DYN,
            get_tab=lambda: row._tab._variables_tab,
        )
        self._num = _DynEventPage(font, row._mark_dirty, _EVENT_NUM)
        self._impulse = _ImpulseVarPage(
            font, row._mark_dirty, caption=tr("получено")
        )
        self._aux = _AuxEventPage(font, row._mark_dirty)
        self._frame = _FrameEventPage(font, row._mark_dirty)
        for page in (
            self._none, self._static, self._dyn, self._num,
            self._impulse, self._aux, self._frame
        ):
            self._stack.addWidget(page)
        editor_layout.addWidget(self._stack)
        layout.addWidget(self._editor)
        self._editor_anim = QPropertyAnimation(
            self._editor, b"maximumHeight", self
        )
        self._editor_anim.setDuration(160)
        self._editor_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Сводка обновляется при любом изменении полей события.
        for page in (
            self._dyn, self._num, self._impulse,
            self._static, self._aux, self._frame
        ):
            for child in page.findChildren(QWidget):
                if isinstance(child, QComboBox):
                    child.currentIndexChanged.connect(self._update_summary)
                elif isinstance(child, QLineEdit):
                    child.textChanged.connect(self._update_summary)
                elif isinstance(child, QSpinBox):
                    child.valueChanged.connect(self._update_summary)
                elif isinstance(child, QCheckBox):
                    child.toggled.connect(self._update_summary)
        if event is not None:
            self.write(event)
            # Запрограммированное событие — свёрнуто до одной строки
            # (отчёт мастера); клик по сводке разворачивает редактор.
            self._editor.setMaximumHeight(0)
        self._update_summary()

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке: развернуть/свернуть редактор
        с анимацией высоты (отчёт мастера)."""
        _animate_editor_toggle(self)

    def _on_type(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._update_summary()
        self._row._mark_dirty()

    def read(self) -> dict[str, Any]:
        etype = self._type.currentData()
        if etype == _EVENT_NONE:
            # Ненастроенное событие не участвует в программе.
            return {"type": _EVENT_NONE}
        if etype == _EVENT_IMPULSE:
            return {
                "type": _EVENT_IMPULSE,
                "var": self._impulse.var.get_name(),
            }
        pages = {
            _EVENT_DYN: self._dyn,
            _EVENT_NUM: self._num,
            _EVENT_STATIC: self._static,
            _EVENT_AUX: self._aux,
            _EVENT_FRAME: self._frame,
        }
        return pages[etype].read()

    def write(self, event: dict[str, Any]) -> None:
        etype = event.get("type", _EVENT_NONE)
        idx = self._type.findData(etype)
        self._type.setCurrentIndex(idx if idx >= 0 else 0)
        self._stack.setCurrentIndex(self._type.currentIndex())
        if etype == _EVENT_IMPULSE:
            self._impulse.var.set_name(str(event.get("var", "")))
            return
        page = {
            _EVENT_DYN: self._dyn,
            _EVENT_NUM: self._num,
            _EVENT_STATIC: self._static,
            _EVENT_AUX: self._aux,
            _EVENT_FRAME: self._frame,
        }.get(etype)
        if page is not None:
            page.write(event)

    def refresh_variables(self) -> None:
        """Обновляет списки переменных в комбобоксах события."""
        tab = self._row._tab._variables_tab
        # «Динамическая переменная» — вид с МК-кэшем байтов;
        # «Численная переменная» — вид с графиком интерполяции;
        # в событиях/условиях это отдельные пункты (отчёт мастера).
        dyn = tab.variable_names("read", "dyn_cache") if tab else []
        num = tab.variable_names("read", "dynamic") if tab else []
        imp = tab.variable_names("read", "impulse") if tab else []
        st = tab.variable_names("read", "static") if tab else []
        self._dyn.var.set_names(dyn, tr("— не выбрано —"))
        self._num.var.set_names(num, tr("— не выбрано —"))
        self._impulse.var.set_names(imp, tr("— не выбрано —"))
        self._static.var.set_names(st, tr("— не выбрано —"))
        # Список состояний «Динамической переменной» подтягивается
        # из её таблицы привязки (отчёт мастера).
        self._dyn.refresh_binding_names()

    def _update_summary(self, *_args) -> None:
        """Компактная подпись события: имя + функция (отчёт мастера)."""
        etype = self._type.currentData()
        if etype == _EVENT_NONE:
            text = tr("Не выбрано")
        elif etype == _EVENT_DYN:
            # «Динамическая переменная»: имя + состояние из таблицы
            # привязки, в которое она перешла (отчёт мастера).
            name = self._dyn.var.get_name() or "—"
            state = self._dyn.value_combo.currentText().strip()
            text = f"{name} → {state}".rstrip()
        elif etype == _EVENT_NUM:
            # «Численная переменная»: «Стало больше»/«Стало меньше»
            # порога (отчёт мастера).
            name = self._num.var.get_name() or "—"
            func = (
                tr("Стало больше")
                if self._num.direction.currentData() == "gt"
                else tr("Стало меньше")
            )
            text = f"{name}: {func} {self._num.value.text().strip()}".rstrip()
        elif etype == _EVENT_IMPULSE:
            # «Импульсная переменная»: событие — «получено»
            # (отчёт мастера).
            text = (
                f"{self._impulse.var.get_name() or '—'} "
                f"{tr('получено')}"
            )
        elif etype == _EVENT_STATIC:
            # «Статическая переменная»: имя + флаг «1»/«0»
            # (отчёт мастера).
            name = self._static.var.get_name() or "—"
            func = {
                "on": "→ 1",
                "off": "→ 0",
                "both": tr("→ 1 или 0"),
            }.get(self._static.edge.currentData(), "")
            text = f"{name} {func}".rstrip()
        elif etype == _EVENT_FRAME:
            can_id = self._frame.can_id.text().strip()
            if self._frame.rtr.isChecked():
                text = f"ID {can_id} RTR" if can_id else "RTR"
            else:
                text = f"ID {can_id}" if can_id else tr("Фрейм")
        else:
            state = (
                tr("активен") if self._aux.state.currentData() == 1
                else tr("не активен")
            )
            text = tr("Доп канал №{0} {1}").format(
                self._aux.channel.value(), state
            )
        self._summary.setText(text)


class _CondItem(QWidget):
    """Одно условие программы: компактная строка + тип + настройки.
    Условий может быть несколько — все должны выполняться (И)."""

    _PAGES = (_COND_NONE, _COND_STATIC, _COND_DYN, _COND_NUM,
              _COND_IMPULSE, _COND_AUX)

    def __init__(self, row: RuleRowWidget, font: QFont, cond: dict | None = None) -> None:
        super().__init__(row)
        self._row = row
        self.setStyleSheet(_ITEM_FRAME_STYLE.format(cls="_CondItem"))
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 4, 6, 6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._summary = _ClickableSummary(self)
        head.addWidget(self._summary, 1)
        self._remove = _close_button(font, tr("Удалить условие"))
        self._remove.clicked.connect(lambda: row._remove_cond(self))
        head.addWidget(self._remove)
        layout.addLayout(head)

        # Редактор условия сворачивается до строки-сводки (отчёт мастера).
        self._editor = QWidget()
        editor_layout = QVBoxLayout(self._editor)
        editor_layout.setSpacing(4)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        self._type = QComboBox()
        self._type.setFont(font)
        # «Не выбрано» — позиция нового условия до настройки
        # (отчёт мастера).
        self._type.addItem(tr("Не выбрано"), _COND_NONE)
        self._type.addItem(tr("Статическая переменная"), _COND_STATIC)
        self._type.addItem(tr("Динамическая переменная"), _COND_DYN)
        self._type.addItem(tr("Численная переменная"), _COND_NUM)
        self._type.addItem(tr("Импульсная переменная"), _COND_IMPULSE)
        self._type.addItem(tr("Доп канал"), _COND_AUX)
        self._type.currentIndexChanged.connect(self._on_type)
        editor_layout.addWidget(self._type)

        self._stack = QStackedWidget()
        none_page = QWidget()
        none_layout = QVBoxLayout(none_page)
        none_layout.setContentsMargins(0, 0, 0, 0)
        none_label = QLabel(tr("— не выбрано —"))
        none_label.setFont(font)
        none_label.setStyleSheet("color: #9A9AA5;")
        none_layout.addWidget(none_label)
        none_layout.addStretch()

        static_page = QWidget()
        st_layout = QVBoxLayout(static_page)
        st_layout.setSpacing(4)
        st_layout.setContentsMargins(0, 0, 0, 0)
        st_layout.addWidget(_small_label(tr("Переменная:"), font))
        self.st_var = _VarCombo(font)
        st_layout.addWidget(self.st_var)
        st_layout.addWidget(_small_label(tr("Состояние:"), font))
        self.st_state = QComboBox()
        self.st_state.setFont(font)
        self.st_state.addItem(tr("Активна (1)"), 1)
        self.st_state.addItem(tr("Не активна (0)"), 0)
        st_layout.addWidget(self.st_state)
        st_layout.addStretch()

        # Условие «Динамическая переменная» — имя + «Активно»
        # (истинно, пока переменная находится в одном из состояний
        # таблицы привязки — отчёт мастера). У «Численной» остаются
        # «Больше/Меньше/Равно» + порог.
        dyn_page = _ImpulseVarPage(
            font, row._mark_dirty, caption=tr("Активно")
        )
        num_page = _CondVarPage(font, row._mark_dirty)
        impulse_page = _ImpulseVarPage(font, row._mark_dirty)
        self.imp_var = impulse_page.var
        self.dyn_var = dyn_page.var
        self.num_var = num_page.var
        self.num_op = num_page.op
        self.num_value = num_page.value

        aux_page = QWidget()
        aux_layout = QVBoxLayout(aux_page)
        aux_layout.setSpacing(4)
        aux_layout.setContentsMargins(0, 0, 0, 0)
        aux_row = QHBoxLayout()
        aux_row.addWidget(_small_label(tr("Доп канал №"), font))
        self.aux_channel = QSpinBox()
        self.aux_channel.setFont(font)
        self.aux_channel.setRange(1, 8)
        self.aux_channel.setValue(1)
        self.aux_channel.setFixedWidth(64)
        aux_row.addWidget(self.aux_channel)
        self.aux_state = QComboBox()
        self.aux_state.setFont(font)
        self.aux_state.addItem(tr("Активен"), 1)
        self.aux_state.addItem(tr("Не активен"), 0)
        aux_row.addWidget(self.aux_state)
        aux_row.addStretch()
        aux_layout.addLayout(aux_row)
        aux_layout.addStretch()

        for page in (
            none_page, static_page, dyn_page, num_page,
            impulse_page, aux_page
        ):
            self._stack.addWidget(page)
        editor_layout.addWidget(self._stack)
        layout.addWidget(self._editor)
        self._editor_anim = QPropertyAnimation(
            self._editor, b"maximumHeight", self
        )
        self._editor_anim.setDuration(160)
        self._editor_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.st_var.currentIndexChanged.connect(self._update_summary)
        self.st_state.currentIndexChanged.connect(self._update_summary)
        self.dyn_var.currentIndexChanged.connect(self._update_summary)
        self.num_var.currentIndexChanged.connect(self._update_summary)
        self.num_op.currentIndexChanged.connect(self._update_summary)
        self.num_value.textChanged.connect(self._update_summary)
        self.imp_var.currentIndexChanged.connect(self._update_summary)
        self.aux_channel.valueChanged.connect(self._update_summary)
        self.aux_state.currentIndexChanged.connect(self._update_summary)
        self.st_var.currentIndexChanged.connect(row._mark_dirty)
        self.st_state.currentIndexChanged.connect(row._mark_dirty)
        self.dyn_var.currentIndexChanged.connect(row._mark_dirty)
        self.imp_var.currentIndexChanged.connect(row._mark_dirty)
        self.aux_channel.valueChanged.connect(row._mark_dirty)
        self.aux_state.currentIndexChanged.connect(row._mark_dirty)

        if cond is not None:
            self.write(cond)
            self._editor.setMaximumHeight(0)
        self._update_summary()

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке разворачивает/сворачивает редактор."""
        _animate_editor_toggle(self)

    def _on_type(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._update_summary()
        self._row._mark_dirty()

    def read(self) -> dict[str, Any]:
        ctype = self._type.currentData()
        if ctype == _COND_STATIC:
            return {
                "type": _COND_STATIC,
                "var": self.st_var.get_name(),
                "state": self.st_state.currentData(),
            }
        if ctype == _COND_DYN:
            # «Активно» — без порога: истинно, пока переменная в одном
            # из состояний таблицы привязки (отчёт мастера).
            return {"type": _COND_DYN, "var": self.dyn_var.get_name()}
        if ctype == _COND_NUM:
            return {
                "type": ctype,
                "var": self.num_var.get_name(),
                "op": self.num_op.currentData(),
                "value": self.num_value.text().strip(),
            }
        if ctype == _COND_IMPULSE:
            # «Импульс активен» — пока горит вспышка 0.5 с.
            return {"type": _COND_IMPULSE, "var": self.imp_var.get_name()}
        if ctype == _COND_AUX:
            return {
                "type": _COND_AUX,
                "channel": self.aux_channel.value(),
                "state": self.aux_state.currentData(),
            }
        return {"type": _COND_NONE}

    def write(self, cond: dict[str, Any]) -> None:
        ctype = cond.get("type", _COND_NONE)
        idx = self._type.findData(ctype)
        self._type.setCurrentIndex(idx if idx >= 0 else 0)
        self._stack.setCurrentIndex(self._type.currentIndex())
        if ctype == _COND_STATIC:
            self.st_var.set_name(str(cond.get("var", "")))
            sidx = self.st_state.findData(int(cond.get("state", 1)))
            self.st_state.setCurrentIndex(sidx if sidx >= 0 else 0)
        elif ctype == _COND_DYN:
            self.dyn_var.set_name(str(cond.get("var", "")))
        elif ctype == _COND_NUM:
            self.num_var.set_name(str(cond.get("var", "")))
            oidx = self.num_op.findData(cond.get("op", "gt"))
            self.num_op.setCurrentIndex(oidx if oidx >= 0 else 0)
            self.num_value.setText(str(cond.get("value", "")))
        elif ctype == _COND_IMPULSE:
            self.imp_var.set_name(str(cond.get("var", "")))
        elif ctype == _COND_AUX:
            self.aux_channel.setValue(int(cond.get("channel", 1) or 1))
            sidx = self.aux_state.findData(int(cond.get("state", 1) or 0))
            self.aux_state.setCurrentIndex(sidx if sidx >= 0 else 0)

    def refresh_variables(self) -> None:
        tab = self._row._tab._variables_tab
        # Отдельные пункты: «Динамическая» (МК-кэш байтов),
        # «Численная» (график интерполяции), «Статическая»
        # (отчёт мастера).
        dyn = tab.variable_names("read", "dyn_cache") if tab else []
        num = tab.variable_names("read", "dynamic") if tab else []
        imp = tab.variable_names("read", "impulse") if tab else []
        st = tab.variable_names("read", "static") if tab else []
        self.st_var.set_names(st, tr("— не выбрано —"))
        self.dyn_var.set_names(dyn, tr("— не выбрано —"))
        self.num_var.set_names(num, tr("— не выбрано —"))
        self.imp_var.set_names(imp, tr("— не выбрано —"))

    def _update_summary(self, *_args) -> None:
        ctype = self._type.currentData()
        if ctype == _COND_STATIC:
            # «Дверь открыта (1)» — имя + состояние (отчёт мастера).
            text = f"{self.st_var.get_name() or '—'} ({self.st_state.currentData()})"
        elif ctype == _COND_DYN:
            # «Состояние АКПП — Активно» (отчёт мастера).
            text = (
                f"{self.dyn_var.get_name() or '—'} {tr('Активно')}"
            )
        elif ctype == _COND_NUM:
            # «Обороты ДВС меньше 1500».
            op = {
                "gt": tr("больше"), "lt": tr("меньше"), "eq": tr("равно"),
            }.get(self.num_op.currentData(), "")
            text = (
                f"{self.num_var.get_name() or '—'} {op} "
                f"{self.num_value.text().strip()}"
            ).rstrip()
        elif ctype == _COND_IMPULSE:
            # «Кнопка — импульс активен».
            text = (
                f"{self.imp_var.get_name() or '—'} "
                f"{tr('— импульс активен')}"
            )
        elif ctype == _COND_AUX:
            # «Доп канал 1 активен».
            state = (
                tr("активен") if self.aux_state.currentData() == 1
                else tr("не активен")
            )
            text = tr("Доп канал {0} {1}").format(
                self.aux_channel.value(), state
            )
        else:
            text = tr("Не выбрано")
        self._summary.setText(text)


def _centered_group(title: str, font: QFont) -> QGroupBox:
    """Группа с заголовком по центру сверху (отчёт мастера)."""
    group = QGroupBox(title)
    group.setFont(font)
    group.setStyleSheet(
        "QGroupBox::title { subcontrol-origin: margin;"
        " subcontrol-position: top center; padding: 0 6px; }"
    )
    return group


class _ActionItem(QWidget):
    """Одно действие программы (колонка ТО): компактная строка
    «Вкл доп канал №1» / «Открыть авто» / «Фрейм» + крестик, клик
    разворачивает настройки. Действий может быть несколько —
    выполняются все по порядку (отчёт мастера)."""

    _PAGES = (_ACT_NONE, _ACT_AUX, _ACT_VAR, _ACT_FRAME, _ACT_CACHE)

    def __init__(
        self,
        row: RuleRowWidget,
        font: QFont,
        action: dict | None = None,
    ) -> None:
        super().__init__(row)
        self._row = row
        self.setStyleSheet(_ITEM_FRAME_STYLE.format(cls="_ActionItem"))
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 4, 6, 6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._summary = _ClickableSummary(self)
        head.addWidget(self._summary, 1)
        self._remove = _close_button(font, tr("Удалить действие"))
        self._remove.clicked.connect(lambda: row._remove_action(self))
        head.addWidget(self._remove)
        layout.addLayout(head)

        self._editor = QWidget()
        editor_layout = QVBoxLayout(self._editor)
        editor_layout.setSpacing(4)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        self._type = QComboBox()
        self._type.setFont(font)
        # «Не выбрано» — позиция нового действия (отчёт мастера).
        self._type.addItem(tr("Не выбрано"), _ACT_NONE)
        self._type.addItem(tr("Доп канал"), _ACT_AUX)
        self._type.addItem(tr("Переменные управления"), _ACT_VAR)
        self._type.addItem(tr("Отправить фрейм"), _ACT_FRAME)
        self._type.addItem(tr("Запись DATA в кэш"), _ACT_CACHE)
        self._type.currentIndexChanged.connect(self._on_type)
        editor_layout.addWidget(self._type)

        self._stack = QStackedWidget()
        none_label = _small_label(tr("— не выбрано —"), font)
        self._none = QWidget()
        QVBoxLayout(self._none).addWidget(none_label)
        self._stack.addWidget(self._none)
        self._build_aux_page(font, row._mark_dirty)
        self._build_var_page(font, row._mark_dirty)
        self._build_frame_page(font, row._mark_dirty)
        self._build_cache_page(font, row._mark_dirty)
        editor_layout.addWidget(self._stack)
        layout.addWidget(self._editor)
        self._editor_anim = QPropertyAnimation(
            self._editor, b"maximumHeight", self
        )
        self._editor_anim.setDuration(160)
        self._editor_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        if action is not None:
            self.write(action)
            self._editor.setMaximumHeight(0)
        self._update_summary()

    # ---- страницы настроек -------------------------------------------

    def _build_aux_page(self, font: QFont, mark_dirty) -> None:
        """«Доп канал»: №, режим Вкл/Выкл/Импульсы/ШИМ, пауза до
        действия и под-страница параметров (отчёт мастера)."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(4)
        pl.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(_small_label(tr("Канал №"), font))
        self.aux_channel = QSpinBox()
        self.aux_channel.setFont(font)
        self.aux_channel.setRange(1, 4)
        self.aux_channel.setValue(1)
        self.aux_channel.setFixedWidth(64)
        self.aux_channel.setToolTip(tr("OUT1…OUT4"))
        top.addWidget(self.aux_channel)
        self.aux_mode = QComboBox()
        self.aux_mode.setFont(font)
        self.aux_mode.addItem(tr("Вкл"), "on")
        self.aux_mode.addItem(tr("Выкл"), "off")
        self.aux_mode.addItem(tr("Подать импульсы"), "pulse")
        self.aux_mode.addItem(tr("Вкл ШИМ"), "pwm")
        top.addWidget(self.aux_mode)
        top.addWidget(_small_label(tr("Пауза до действия"), font))
        self.aux_delay = QSpinBox()
        self.aux_delay.setFont(font)
        self.aux_delay.setRange(0, 999999)
        self.aux_delay.setSuffix(tr(" мс"))
        self.aux_delay.setFixedWidth(96)
        top.addWidget(self.aux_delay)
        top.addStretch()
        pl.addLayout(top)

        self._aux_stack = QStackedWidget()
        plain = QWidget()
        plain_layout = QVBoxLayout(plain)
        plain_layout.setContentsMargins(0, 0, 0, 0)
        plain_hint = QLabel(
            tr("«Вкл» — до команды «Выкл» или до снятия питания МК")
        )
        plain_hint.setFont(font)
        plain_hint.setStyleSheet("color: #9A9AA5;")
        plain_hint.setWordWrap(True)
        plain_layout.addWidget(plain_hint)
        plain_layout.addStretch()

        pulse = QWidget()
        pulse_layout = QVBoxLayout(pulse)
        pulse_layout.setSpacing(4)
        pulse_layout.setContentsMargins(0, 0, 0, 0)
        pulse_row = QHBoxLayout()
        self.aux_pulse_on = QSpinBox()
        self.aux_pulse_on.setFont(font)
        self.aux_pulse_on.setRange(1, 999999)
        self.aux_pulse_on.setValue(100)
        self.aux_pulse_on.setSuffix(tr(" мс"))
        self.aux_pulse_on.setFixedWidth(96)
        pulse_row.addWidget(_small_label(tr("Импульс"), font))
        pulse_row.addWidget(self.aux_pulse_on)
        self.aux_pulse_off = QSpinBox()
        self.aux_pulse_off.setFont(font)
        self.aux_pulse_off.setRange(1, 999999)
        self.aux_pulse_off.setValue(100)
        self.aux_pulse_off.setSuffix(tr(" мс"))
        self.aux_pulse_off.setFixedWidth(96)
        pulse_row.addWidget(_small_label(tr("Пауза"), font))
        pulse_row.addWidget(self.aux_pulse_off)
        self.aux_pulse_count = QSpinBox()
        self.aux_pulse_count.setFont(font)
        self.aux_pulse_count.setRange(1, 1000)
        self.aux_pulse_count.setValue(3)
        self.aux_pulse_count.setFixedWidth(64)
        pulse_row.addWidget(_small_label(tr("Кол-во"), font))
        pulse_row.addWidget(self.aux_pulse_count)
        pulse_row.addStretch()
        pulse_layout.addLayout(pulse_row)
        self.aux_pulse_graph = _PulsePreview()
        pulse_layout.addWidget(self.aux_pulse_graph)

        pwm = QWidget()
        pwm_layout = QVBoxLayout(pwm)
        pwm_layout.setSpacing(4)
        pwm_layout.setContentsMargins(0, 0, 0, 0)
        pwm_row = QHBoxLayout()
        self.aux_pwm_freq = QSpinBox()
        self.aux_pwm_freq.setFont(font)
        self.aux_pwm_freq.setRange(1, 20000)
        self.aux_pwm_freq.setValue(1000)
        self.aux_pwm_freq.setSuffix(tr(" Гц"))
        self.aux_pwm_freq.setFixedWidth(96)
        pwm_row.addWidget(_small_label(tr("Частота"), font))
        pwm_row.addWidget(self.aux_pwm_freq)
        self.aux_pwm_duty = QSpinBox()
        self.aux_pwm_duty.setFont(font)
        self.aux_pwm_duty.setRange(1, 100)
        self.aux_pwm_duty.setValue(50)
        self.aux_pwm_duty.setSuffix(" %")
        self.aux_pwm_duty.setFixedWidth(80)
        pwm_row.addWidget(_small_label(tr("Заполнение"), font))
        pwm_row.addWidget(self.aux_pwm_duty)
        self.aux_pwm_time = QSpinBox()
        self.aux_pwm_time.setFont(font)
        self.aux_pwm_time.setRange(0, 999999)
        self.aux_pwm_time.setValue(0)
        self.aux_pwm_time.setSuffix(tr(" мс"))
        self.aux_pwm_time.setFixedWidth(96)
        self.aux_pwm_time.setToolTip(
            tr("Время работы ШИМ, 0 — до команды «Выкл»")
        )
        pwm_row.addWidget(_small_label(tr("Время"), font))
        pwm_row.addWidget(self.aux_pwm_time)
        pwm_row.addStretch()
        pwm_layout.addLayout(pwm_row)
        pwm_hint = QLabel(tr("0 мс — ШИМ работает до команды «Выкл»"))
        pwm_hint.setFont(font)
        pwm_hint.setStyleSheet("color: #9A9AA5;")
        pwm_layout.addWidget(pwm_hint)

        for sub in (plain, pulse, pwm):
            self._aux_stack.addWidget(sub)
        pl.addWidget(self._aux_stack)
        self._stack.addWidget(page)

        self.aux_channel.valueChanged.connect(mark_dirty)
        self.aux_mode.currentIndexChanged.connect(self._on_aux_mode)
        self.aux_mode.currentIndexChanged.connect(mark_dirty)
        self.aux_delay.valueChanged.connect(mark_dirty)
        for spin in (
            self.aux_pulse_on, self.aux_pulse_off, self.aux_pulse_count,
            self.aux_pwm_freq, self.aux_pwm_duty, self.aux_pwm_time,
        ):
            spin.valueChanged.connect(mark_dirty)
        for spin in (
            self.aux_pulse_on, self.aux_pulse_off, self.aux_pulse_count,
        ):
            spin.valueChanged.connect(self._refresh_pulse_graph)
        self._refresh_pulse_graph()

    def _build_var_page(self, font: QFont, mark_dirty) -> None:
        """«Переменные управления»: выбор команды из раздела
        «Управление» вкладки «Переменные» — при срабатывании
        программы отправляется вся её последовательность фреймов
        (паузы/количество/кэш заданы в самой команде — отчёт
        мастера)."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(4)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Команда:"), font))
        self.var = _VarCombo(font)
        row1.addWidget(self.var, 1)
        pl.addLayout(row1)
        hint = QLabel(tr(
            "Отправляет фреймы выбранной команды «Управления» — "
            "с её паузами, количеством и подстановкой байтов «X» "
            "из автоматически записанного кэша."
        ))
        hint.setFont(font)
        hint.setStyleSheet("color: #9A9AA5;")
        hint.setWordWrap(True)
        pl.addWidget(hint)
        pl.addStretch()
        self._stack.addWidget(page)

        self.var.currentIndexChanged.connect(mark_dirty)
        self.var.currentIndexChanged.connect(self._update_summary)

    def _build_frame_page(self, font: QFont, mark_dirty) -> None:
        """«Отправить фрейм»: канал/бит/ID/DLC/DATA (X — из кадра
        события) + пауза/кол-во/между — ручная рассылка оператором."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(4)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Канал"), font))
        self.fr_channel = QComboBox()
        self.fr_channel.setFont(font)
        self.fr_channel.addItems(["CAN1", "CAN2"])
        row1.addWidget(self.fr_channel)
        row1.addWidget(_small_label(tr("Бит"), font))
        self.fr_bit = QComboBox()
        self.fr_bit.setFont(font)
        self.fr_bit.addItems(_BIT_RATES)
        self.fr_bit.setFixedWidth(92)
        row1.addWidget(self.fr_bit)
        row1.addWidget(_small_label("ID", font))
        self.fr_id = _HexIdEdit(font)
        row1.addWidget(self.fr_id)
        row1.addWidget(_small_label("DLC", font))
        self.fr_dlc = QSpinBox()
        self.fr_dlc.setFont(font)
        self.fr_dlc.setRange(1, 8)
        self.fr_dlc.setValue(8)
        self.fr_dlc.setFixedWidth(54)
        row1.addWidget(self.fr_dlc)
        row1.addStretch()
        pl.addLayout(row1)
        pl.addWidget(_small_label(tr("DATA (X — из кадра события)"), font))
        self.fr_data, data_widget = create_data_field_widget(
            font, 8, edit_width=32, allow_x=True
        )
        pl.addWidget(data_widget)
        row2 = QHBoxLayout()
        row2.addWidget(_small_label(tr("Пауза"), font))
        self.fr_delay = QSpinBox()
        self.fr_delay.setRange(0, 9999)
        self.fr_delay.setSuffix(tr(" мс"))
        self.fr_delay.setFont(font)
        self.fr_delay.setFixedWidth(86)
        row2.addWidget(self.fr_delay)
        row2.addWidget(_small_label(tr("Кол-во"), font))
        self.fr_count = QSpinBox()
        self.fr_count.setRange(1, 100)
        self.fr_count.setValue(1)
        self.fr_count.setFont(font)
        self.fr_count.setFixedWidth(60)
        row2.addWidget(self.fr_count)
        row2.addWidget(_small_label(tr("Между"), font))
        self.fr_between = QSpinBox()
        self.fr_between.setRange(0, 9999)
        self.fr_between.setSuffix(tr(" мс"))
        self.fr_between.setFont(font)
        self.fr_between.setFixedWidth(86)
        row2.addWidget(self.fr_between)
        row2.addStretch()
        pl.addLayout(row2)
        pl.addStretch()
        self._stack.addWidget(page)

        self.fr_channel.currentIndexChanged.connect(mark_dirty)
        self.fr_bit.currentIndexChanged.connect(mark_dirty)
        self.fr_id.textChanged.connect(mark_dirty)
        self.fr_dlc.valueChanged.connect(mark_dirty)
        self.fr_dlc.valueChanged.connect(
            lambda v: _set_data_enabled(self.fr_data, v)
        )
        for edit in self.fr_data:
            edit.textChanged.connect(mark_dirty)
        self.fr_delay.valueChanged.connect(mark_dirty)
        self.fr_count.valueChanged.connect(mark_dirty)
        self.fr_between.valueChanged.connect(mark_dirty)
        _set_data_enabled(self.fr_data, self.fr_dlc.value())

    def _build_cache_page(self, font: QFont, mark_dirty) -> None:
        """«Запись DATA в кэш»: маска кадра-источника + приём/отправка
        в выбранный канал — как в триггерах."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(4)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Канал"), font))
        self.cache_channel = QComboBox()
        self.cache_channel.setFont(font)
        for i, name in enumerate(_CHANNELS_ANY):
            self.cache_channel.addItem(name or tr(_ANY_CHANNEL_TEXT), i)
        self.cache_channel.setFixedWidth(120)
        row1.addWidget(self.cache_channel)
        row1.addWidget(_small_label(tr("Бит"), font))
        self.cache_bit = QComboBox()
        self.cache_bit.setFont(font)
        self.cache_bit.addItems(_BIT_RATES)
        self.cache_bit.setFixedWidth(92)
        row1.addWidget(self.cache_bit)
        row1.addWidget(_small_label("ID", font))
        self.cache_id = _HexIdEdit(font)
        row1.addWidget(self.cache_id)
        row1.addWidget(_small_label("DLC", font))
        self.cache_dlc = QSpinBox()
        self.cache_dlc.setFont(font)
        self.cache_dlc.setRange(1, 8)
        self.cache_dlc.setValue(8)
        self.cache_dlc.setFixedWidth(54)
        row1.addWidget(self.cache_dlc)
        row1.addStretch()
        pl.addLayout(row1)
        pl.addWidget(_small_label(tr("DATA от:"), font))
        self.cache_from, from_widget = create_data_field_widget(
            font, 8, edit_width=30, allow_x=True
        )
        pl.addWidget(from_widget)
        pl.addWidget(_small_label(tr("DATA до:"), font))
        self.cache_to, to_widget = create_data_field_widget(
            font, 8, edit_width=30, allow_x=True
        )
        pl.addWidget(to_widget)
        row2 = QHBoxLayout()
        row2.addWidget(_small_label(tr("Отправить в"), font))
        self.cache_tx_channel = QComboBox()
        self.cache_tx_channel.setFont(font)
        self.cache_tx_channel.addItems(["CAN1", "CAN2"])
        row2.addWidget(self.cache_tx_channel)
        row2.addWidget(_small_label(tr("Пауза"), font))
        self.cache_delay = QSpinBox()
        self.cache_delay.setRange(0, 9999)
        self.cache_delay.setSuffix(tr(" мс"))
        self.cache_delay.setFont(font)
        self.cache_delay.setFixedWidth(86)
        row2.addWidget(self.cache_delay)
        row2.addWidget(_small_label(tr("Кол-во"), font))
        self.cache_count = QSpinBox()
        self.cache_count.setRange(1, 100)
        self.cache_count.setValue(1)
        self.cache_count.setFont(font)
        self.cache_count.setFixedWidth(60)
        row2.addWidget(self.cache_count)
        row2.addStretch()
        pl.addLayout(row2)
        pl.addStretch()
        self._stack.addWidget(page)

        self.cache_channel.currentIndexChanged.connect(mark_dirty)
        self.cache_bit.currentIndexChanged.connect(mark_dirty)
        self.cache_id.textChanged.connect(mark_dirty)
        self.cache_dlc.valueChanged.connect(mark_dirty)
        for edit in (*self.cache_from, *self.cache_to):
            edit.textChanged.connect(mark_dirty)
        self.cache_tx_channel.currentIndexChanged.connect(mark_dirty)
        self.cache_delay.valueChanged.connect(mark_dirty)
        self.cache_count.valueChanged.connect(mark_dirty)

    # ---- обработчики ---------------------------------------------------

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке разворачивает/сворачивает редактор."""
        _animate_editor_toggle(self)

    def _on_type(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._update_summary()
        self._row._mark_dirty()

    def _on_aux_mode(self, *_args) -> None:
        mode = self.aux_mode.currentData()
        self._aux_stack.setCurrentIndex(
            {"on": 0, "off": 0, "pulse": 1, "pwm": 2}.get(mode, 0)
        )
        self._update_summary()

    def _refresh_pulse_graph(self, *_args) -> None:
        self.aux_pulse_graph.set_params(
            self.aux_pulse_on.value(),
            self.aux_pulse_off.value(),
            self.aux_pulse_count.value(),
        )

    def refresh_variables(self) -> None:
        tab = self._row._tab._variables_tab
        # «Переменные управления» — команды из раздела «Управление»
        # (отчёт мастера).
        ctrl = tab.variable_names("control") if tab else []
        self.var.set_names(ctrl, tr("— не выбрано —"))

    # ---- схема -----------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """Плоский словарь с теми же ключами, что у старого единого
        блока «Действие» + маркер «type» — совместимо с исполнением."""
        atype = self._type.currentData()
        if atype == _ACT_VAR:
            return {
                "type": _ACT_VAR,
                "var": self.var.get_name(),
            }
        if atype == _ACT_FRAME:
            return {
                "type": _ACT_FRAME,
                "frame_enabled": True,
                "channel": self.fr_channel.currentIndex(),
                "extended": self.fr_bit.currentIndex() == 1,
                "id": self.fr_id.text().strip(),
                "dlc": self.fr_dlc.value(),
                "data": _tokens_to_text(self.fr_data),
                "delay": self.fr_delay.value(),
                "count": self.fr_count.value(),
                "between": self.fr_between.value(),
            }
        if atype == _ACT_AUX:
            return {
                "type": _ACT_AUX,
                "aux_enabled": True,
                "aux_channel": self.aux_channel.value(),
                "aux_mode": self.aux_mode.currentData(),
                "aux_delay": self.aux_delay.value(),
                "aux_pulse_on": self.aux_pulse_on.value(),
                "aux_pulse_off": self.aux_pulse_off.value(),
                "aux_pulse_count": self.aux_pulse_count.value(),
                "aux_pwm_freq": self.aux_pwm_freq.value(),
                "aux_pwm_duty": self.aux_pwm_duty.value(),
                "aux_pwm_time": self.aux_pwm_time.value(),
            }
        if atype == _ACT_CACHE:
            return {
                "type": _ACT_CACHE,
                "cache_enabled": True,
                "cache_channel": self.cache_channel.currentData(),
                "cache_extended": self.cache_bit.currentIndex() == 1,
                "cache_id": self.cache_id.text().strip(),
                "cache_dlc": self.cache_dlc.value(),
                "cache_from": _tokens_to_text(self.cache_from),
                "cache_to": _tokens_to_text(self.cache_to),
                "cache_tx_channel": self.cache_tx_channel.currentIndex(),
                "cache_delay": self.cache_delay.value(),
                "cache_count": self.cache_count.value(),
            }
        return {"type": _ACT_NONE}

    def write(self, action: dict[str, Any]) -> None:
        """action — словарь одного действия (с ключом «type») либо
        часть старого объединённого блока; тип угадывается по ключам."""
        atype = action.get("type")
        if atype not in self._PAGES:
            if action.get("aux_enabled"):
                atype = _ACT_AUX
            elif action.get("frame_enabled"):
                atype = _ACT_FRAME
            elif action.get("cache_enabled"):
                atype = _ACT_CACHE
            elif action.get("var"):
                atype = _ACT_VAR
            else:
                atype = _ACT_NONE
        idx = self._type.findData(atype)
        self._type.setCurrentIndex(idx if idx >= 0 else 0)
        self._stack.setCurrentIndex(self._type.currentIndex())
        if atype == _ACT_VAR:
            self.var.set_name(str(action.get("var", "")))
        elif atype == _ACT_FRAME:
            self.fr_channel.setCurrentIndex(int(action.get("channel", 0) or 0))
            self.fr_bit.setCurrentIndex(1 if action.get("extended") else 0)
            self.fr_id.setText(str(action.get("id", "")))
            self.fr_dlc.setValue(int(action.get("dlc", 8) or 8))
            _text_to_tokens(self.fr_data, action.get("data"))
            _set_data_enabled(self.fr_data, self.fr_dlc.value())
            self.fr_delay.setValue(int(action.get("delay", 0) or 0))
            self.fr_count.setValue(int(action.get("count", 1) or 1))
            self.fr_between.setValue(int(action.get("between", 0) or 0))
        elif atype == _ACT_AUX:
            self.aux_channel.setValue(int(action.get("aux_channel", 1) or 1))
            midx = self.aux_mode.findData(action.get("aux_mode", "on"))
            self.aux_mode.setCurrentIndex(midx if midx >= 0 else 0)
            self.aux_delay.setValue(int(action.get("aux_delay", 0) or 0))
            self.aux_pulse_on.setValue(
                int(action.get("aux_pulse_on", 100) or 100)
            )
            self.aux_pulse_off.setValue(
                int(action.get("aux_pulse_off", 100) or 100)
            )
            self.aux_pulse_count.setValue(
                int(action.get("aux_pulse_count", 3) or 3)
            )
            self.aux_pwm_freq.setValue(
                int(action.get("aux_pwm_freq", 1000) or 1000)
            )
            self.aux_pwm_duty.setValue(
                int(action.get("aux_pwm_duty", 50) or 50)
            )
            self.aux_pwm_time.setValue(
                int(action.get("aux_pwm_time", 0) or 0)
            )
            self._refresh_pulse_graph()
        elif atype == _ACT_CACHE:
            ch = int(action.get("cache_channel", 2) or 0)
            cidx = self.cache_channel.findData(ch)
            self.cache_channel.setCurrentIndex(cidx if cidx >= 0 else 2)
            self.cache_bit.setCurrentIndex(
                1 if action.get("cache_extended") else 0
            )
            self.cache_id.setText(str(action.get("cache_id", "")))
            self.cache_dlc.setValue(int(action.get("cache_dlc", 8) or 8))
            _text_to_tokens(self.cache_from, action.get("cache_from"))
            _text_to_tokens(self.cache_to, action.get("cache_to"))
            self.cache_tx_channel.setCurrentIndex(
                int(action.get("cache_tx_channel", 0) or 0)
            )
            self.cache_delay.setValue(int(action.get("cache_delay", 0) or 0))
            self.cache_count.setValue(int(action.get("cache_count", 1) or 1))
        self._update_summary()

    def _update_summary(self, *_args) -> None:
        """Короткая строка действия: «Вкл доп канал №1», «Вкл
        импульсы доп канал №1», «ВКЛ ШИМ №2», «Фрейм» (отчёт мастера)."""
        atype = self._type.currentData()
        if atype == _ACT_AUX:
            ch = self.aux_channel.value()
            mode = self.aux_mode.currentData()
            if mode == "off":
                text = tr("Выкл доп канал №{0}").format(ch)
            elif mode == "pulse":
                text = tr("Вкл импульсы доп канал №{0}").format(ch)
            elif mode == "pwm":
                text = tr("ВКЛ ШИМ №{0}").format(ch)
            else:
                text = tr("Вкл доп канал №{0}").format(ch)
        elif atype == _ACT_VAR:
            text = self.var.get_name() or tr("Переменная")
        elif atype == _ACT_FRAME:
            text = tr("Фрейм")
        elif atype == _ACT_CACHE:
            text = tr("Кэш")
        else:
            text = tr("Не выбрано")
        self._summary.setText(text)


class RuleRowWidget(QWidget):
    """Одна программа: шапка (галочка, №, имя, сводка, свёртка, ✕)
    + колонки ЕСЛИ «Событие» / ПРИ «Условие» / ТО «Действие».
    Событий может быть несколько — программа стартует по любому
    (ИЛИ); условий — тоже, но выполняться должны все (И)."""

    def __init__(
        self,
        tab: FlexibleLogicTab,
        rule: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self._rule = rule or {}
        self._create_widgets()
        self._build_layout()
        self._load_rule()

    @Slot()
    def _mark_dirty(self, *_args) -> None:
        self._tab.mark_dirty()

    # ---- виджеты -------------------------------------------------------

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        # Шапка программы: вкл/выкл, №, имя, свёртка, удаление.
        self._active_check = QCheckBox()
        self._active_check.setFont(font)
        self._active_check.setToolTip(tr("Включить/выключить программу"))
        self._active_check.toggled.connect(self._mark_dirty)
        self._name_edit = QLineEdit()
        self._name_edit.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._name_edit.setPlaceholderText(tr("Программа"))
        self._name_edit.setClearButtonEnabled(True)
        self._name_edit.textChanged.connect(self._mark_dirty)
        # Сворачивание — нижним подчёркиванием, как кнопка
        # минимизации в окнах Windows (отчёт мастера).
        self._collapse_button = QPushButton("_")
        self._collapse_button.setFont(
            QFont("Segoe UI", 10, QFont.Weight.Bold)
        )
        self._collapse_button.setFixedWidth(30)
        self._collapse_button.setToolTip(tr("Свернуть программу"))
        self._collapse_button.clicked.connect(self._toggle_collapsed)
        # Крестик закрытия — голубой, как у событий/условий/действий
        # (отчёт мастера).
        self._remove_button = _close_button(font, tr("Удалить программу"))
        self._remove_button.clicked.connect(self._on_remove)
        self._summary_label = QLabel()
        self._summary_label.setFont(font)
        self._summary_label.setVisible(False)
        self._counter_label = QLabel("0")
        self._counter_label.setFont(font)
        self._counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter_label.setFixedWidth(36)
        self._counter_label.setToolTip(tr("Срабатываний"))

        self._number_label = QLabel("№")
        self._number_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._number_label.setStyleSheet("color: #9A9AA5;")

        # ---- События (несколько, ИЛИ между ними) ----------------------
        self._event_group = _centered_group(
            tr("Событие"), QFont("Segoe UI", 9, QFont.Weight.Bold)
        )
        self._event_items: list[_EventItem] = []
        self._event_separators: list[QLabel] = []
        self._events_layout: QVBoxLayout | None = None
        self._add_event_button = QPushButton(tr("＋ событие"))
        self._add_event_button.setFont(font)
        self._add_event_button.clicked.connect(lambda: self._add_event())

        # ---- Условия (несколько, И между ними) ------------------------
        self._cond_group = _centered_group(
            tr("Условие"), QFont("Segoe UI", 9, QFont.Weight.Bold)
        )
        self._cond_items: list[_CondItem] = []
        self._cond_separators: list[QLabel] = []
        self._conds_layout: QVBoxLayout | None = None
        self._add_cond_button = QPushButton(tr("＋ условие"))
        self._add_cond_button.setFont(font)
        self._add_cond_button.clicked.connect(lambda: self._add_cond())

        # ---- Действия (несколько — выполняются все по порядку) --------
        self._action_group = _centered_group(
            tr("Действие"), QFont("Segoe UI", 9, QFont.Weight.Bold)
        )
        self._action_items: list[_ActionItem] = []
        self._action_separators: list[QLabel] = []
        self._actions_layout: QVBoxLayout | None = None
        self._add_action_button = QPushButton(tr("＋ действие"))
        self._add_action_button.setFont(font)
        self._add_action_button.clicked.connect(lambda: self._add_action())

    def _build_layout(self) -> None:
        font = QFont("Segoe UI", 9)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 8)

        self._header = QFrame()
        self._header.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.04);"
            " border: 1px solid #454552; border-radius: 8px; }"
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(8)
        header_layout.addWidget(self._active_check)
        header_layout.addWidget(self._number_label)
        header_layout.addWidget(self._name_edit, 1)
        header_layout.addWidget(self._summary_label, 2)
        header_layout.addWidget(_small_label(tr("Срабатываний:"), font))
        header_layout.addWidget(self._counter_label)
        header_layout.addWidget(self._collapse_button)
        header_layout.addWidget(self._remove_button)
        layout.addWidget(self._header)

        self._body = QWidget()
        body_layout = QHBoxLayout(self._body)
        body_layout.setSpacing(8)
        body_layout.setContentsMargins(0, 0, 0, 0)

        # ЕСЛИ — События (ИЛИ между событиями); кнопка добавления —
        # в верхней части колонки (отчёт мастера).
        body_layout.addWidget(_connector(tr("ЕСЛИ")), 0)
        ev_layout = QVBoxLayout(self._event_group)
        ev_layout.setSpacing(3)
        ev_layout.setContentsMargins(8, 10, 8, 8)
        ev_layout.addWidget(self._add_event_button)
        self._events_layout = QVBoxLayout()
        self._events_layout.setSpacing(1)
        ev_layout.addLayout(self._events_layout, 1)
        body_layout.addWidget(self._event_group, 1)

        # ПРИ — Условия (И между условиями — выполняются все)
        body_layout.addWidget(_connector(tr("ПРИ")), 0)
        cd_layout = QVBoxLayout(self._cond_group)
        cd_layout.setSpacing(3)
        cd_layout.setContentsMargins(8, 10, 8, 8)
        cd_layout.addWidget(self._add_cond_button)
        self._conds_layout = QVBoxLayout()
        self._conds_layout.setSpacing(1)
        cd_layout.addLayout(self._conds_layout, 1)
        body_layout.addWidget(self._cond_group, 1)

        # ТО — Действия (выполняются все по порядку)
        body_layout.addWidget(_connector(tr("ТО")), 0)
        act_layout = QVBoxLayout(self._action_group)
        act_layout.setSpacing(3)
        act_layout.setContentsMargins(8, 10, 8, 8)
        act_layout.addWidget(self._add_action_button)
        self._actions_layout = QVBoxLayout()
        self._actions_layout.setSpacing(1)
        act_layout.addLayout(self._actions_layout, 1)
        body_layout.addWidget(self._action_group, 1)
        layout.addWidget(self._body)

    # ---- переменные из вкладки «Переменные» ----------------------------

    def refresh_variables(self) -> None:
        """Обновляет списки переменных в комбобоксах из вкладки
        «Переменные» (события/условия — колонка «Чтение», действия —
        «Управление»). Выбранное имя сохраняется."""
        for item in self._event_items:
            item.refresh_variables()
        for item in self._cond_items:
            item.refresh_variables()
        for item in self._action_items:
            item.refresh_variables()

    # ---- события/условия (списки) --------------------------------------

    def _add_event(self, event: dict | None = None) -> _EventItem:
        """Новое событие в колонке ЕСЛИ; между событиями — ИЛИ."""
        font = QFont("Segoe UI", 9)
        item = _EventItem(self, font, event)
        item.refresh_variables()
        assert self._events_layout is not None
        if self._event_items:
            sep = self._separator_label(tr("ИЛИ"), font)
            self._event_separators.append(sep)
            self._events_layout.addWidget(sep)
        self._event_items.append(item)
        self._events_layout.addWidget(item)
        self._mark_dirty()
        return item

    def _remove_event(self, item: _EventItem) -> None:
        if len(self._event_items) <= 1:
            return  # хотя бы одно событие должно остаться
        if item not in self._event_items:
            return  # повторный клик по уже удалённому виджету
        # Анимация разворота могла ещё идти — останавливаем,
        # иначе finished() придёт уже удалённому виджету.
        item._editor_anim.stop()
        idx = self._event_items.index(item)
        self._event_items.pop(idx)
        sep = None
        if self._event_items and self._event_separators:
            sep_idx = idx - 1 if idx > 0 else 0
            if sep_idx < len(self._event_separators):
                sep = self._event_separators.pop(sep_idx)
        # setParent(None) переносится на следующий тик цикла событий:
        # вызов пришёл из clicked() самой кнопки внутри item — синхронный
        # reparent/удаление предка кнопки прямо во время обработки его
        # же сигнала рушило native-дерево виджетов (отчёт мастера:
        # «закрываем крестиком событие — приложение закрывается»).
        QTimer.singleShot(0, lambda: _detach_item(item, sep))
        self._mark_dirty()

    def _add_cond(self, cond: dict | None = None) -> _CondItem:
        """Новое условие в колонке ПРИ; между условиями — И."""
        font = QFont("Segoe UI", 9)
        item = _CondItem(self, font, cond)
        item.refresh_variables()
        assert self._conds_layout is not None
        if self._cond_items:
            sep = self._separator_label(tr("И"), font)
            self._cond_separators.append(sep)
            self._conds_layout.addWidget(sep)
        self._cond_items.append(item)
        self._conds_layout.addWidget(item)
        self._mark_dirty()
        return item

    def _remove_cond(self, item: _CondItem) -> None:
        if len(self._cond_items) <= 1:
            return  # хотя бы одно условие должно остаться («Не выбрано»)
        if item not in self._cond_items:
            return
        item._editor_anim.stop()
        idx = self._cond_items.index(item)
        self._cond_items.pop(idx)
        sep = None
        if self._cond_items and self._cond_separators:
            sep_idx = idx - 1 if idx > 0 else 0
            if sep_idx < len(self._cond_separators):
                sep = self._cond_separators.pop(sep_idx)
        # См. _remove_event — reparent отложен на следующий тик, чтобы
        # не рушить native-дерево виджетов прямо во время clicked()
        # кнопки-потомка (отчёт мастера).
        QTimer.singleShot(0, lambda: _detach_item(item, sep))
        self._mark_dirty()

    def _add_action(self, action: dict | None = None) -> _ActionItem:
        """Новое действие в колонке ТО; действия выполняются все,
        по порядку списка (отчёт мастера)."""
        font = QFont("Segoe UI", 9)
        item = _ActionItem(self, font, action)
        item.refresh_variables()
        assert self._actions_layout is not None
        if self._action_items:
            sep = self._separator_label("+", font)
            self._action_separators.append(sep)
            self._actions_layout.addWidget(sep)
        self._action_items.append(item)
        self._actions_layout.addWidget(item)
        self._mark_dirty()
        return item

    def _remove_action(self, item: _ActionItem) -> None:
        if len(self._action_items) <= 1:
            return  # хотя бы одно действие должно остаться
        if item not in self._action_items:
            return
        item._editor_anim.stop()
        idx = self._action_items.index(item)
        self._action_items.pop(idx)
        sep = None
        if self._action_items and self._action_separators:
            sep_idx = idx - 1 if idx > 0 else 0
            if sep_idx < len(self._action_separators):
                sep = self._action_separators.pop(sep_idx)
        # См. _remove_event — reparent отложен на следующий тик цикла
        # событий (отчёт мастера).
        QTimer.singleShot(0, lambda: _detach_item(item, sep))
        self._mark_dirty()

    def _separator_label(self, text: str, font: QFont) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont(font.family(), font.pointSize(), QFont.Weight.Bold))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #9A9AA5;")
        label.setFixedHeight(14)
        return label

    # ---- правило ---------------------------------------------------------

    def _migrate_legacy(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Старый формат (id/mask/condition_data → resp_*) — в новую
        схему: событие «Фрейм» + действие «Фрейм». Байт маски 0x00
        становится «X», ответная маска 0x00 — подстановкой из кадра."""
        if "event" in rule or "events" in rule:
            return rule
        # Пустая новая программа — не наследие: строки события,
        # условия и действия стартуют с «Не выбрано» (отчёт мастера).
        if not any(
            k in rule for k in ("id", "mask", "condition_data", "resp_id")
        ):
            return rule
        mask = str(rule.get("mask", "")).split()
        cond = str(rule.get("condition_data", "")).split()
        data_tokens = []
        for i in range(8):
            token = cond[i] if i < len(cond) else ""
            m = mask[i] if i < len(mask) else "00"
            data_tokens.append(token if m.upper() == "FF" else "X")
        resp_data = str(rule.get("resp_data", "")).split()
        resp_mask = str(rule.get("resp_mask", "")).split()
        act_tokens = []
        for i in range(8):
            token = resp_data[i] if i < len(resp_data) else ""
            m = resp_mask[i] if i < len(resp_mask) else "00"
            act_tokens.append(token if m.upper() == "FF" else "X")
        channel = str(rule.get("resp_channel", "")).strip()
        migrated = dict(rule)
        migrated["event"] = {
            "type": _EVENT_FRAME,
            "channel": 2,
            "extended": False,
            "id": str(rule.get("id", "")),
            "dlc": 8,
            "data": " ".join(data_tokens),
            "fire_limit": int(rule.get("fire_limit", 1) or 1),
        }
        migrated["condition"] = {"type": _COND_NONE}
        migrated["action"] = {
            "var": "",
            "frame_enabled": True,
            "channel": int(channel) - 1 if channel in ("1", "2") else 0,
            "extended": False,
            "id": str(rule.get("resp_id", "")),
            "dlc": 8,
            "data": " ".join(act_tokens),
            "delay": int(rule.get("delay", 0) or 0),
            "count": 1,
            "between": 0,
            "cache_enabled": False,
        }
        return migrated

    def _load_rule(self) -> None:
        rule = self._migrate_legacy(self._rule)
        self._active_check.setChecked(rule.get("active", True))
        self._name_edit.setText(str(rule.get("title", "")))

        # События: список «events» или одиночное «event» (старое);
        # у новой программы — «Не выбрано» (отчёт мастера).
        events = rule.get("events")
        if not isinstance(events, list) or not events:
            events = [rule.get("event") or {"type": _EVENT_NONE}]
        for event in events:
            self._add_event(event if isinstance(event, dict) else None)

        # Условия: список «conditions» или одиночное «condition».
        conds = rule.get("conditions")
        if not isinstance(conds, list) or not conds:
            conds = [rule.get("condition") or {"type": _COND_NONE}]
        for cond in conds:
            self._add_cond(cond if isinstance(cond, dict) else None)

        # Действия: список «actions» (новый формат) либо старый
        # объединённый блок «action» — раскладывается на пункты
        # по типам в прежнем визуальном порядке (переменная, фрейм,
        # доп канал, кэш).
        actions = rule.get("actions")
        if not isinstance(actions, list) or not actions:
            action = rule.get("action") or {}
            actions = []
            if action.get("var"):
                actions.append({"type": _ACT_VAR, **action})
            if action.get("frame_enabled"):
                actions.append({"type": _ACT_FRAME, **action})
            if action.get("aux_enabled"):
                actions.append({"type": _ACT_AUX, **action})
            if action.get("cache_enabled"):
                actions.append({"type": _ACT_CACHE, **action})
        if not actions:
            self._add_action()
        else:
            for act in actions:
                self._add_action(act if isinstance(act, dict) else None)

        if rule.get("collapsed"):
            self._toggle_collapsed()

    def _on_remove(self) -> None:
        self._tab._remove_row(self)

    def _toggle_collapsed(self) -> None:
        """Сворачивает тело программы до шапки со сводкой и обратно."""
        collapsed = not self._body.isHidden()
        self._body.setVisible(not collapsed)
        self._summary_label.setVisible(collapsed)
        self._collapse_button.setToolTip(
            tr("Развернуть программу") if collapsed
            else tr("Свернуть программу")
        )
        if collapsed:
            # Сводка: компактные строки событий через «ИЛИ» (имя +
            # функция), затем «→ действие» короткой строкой
            # («Вкл доп канал №1», «ВКЛ ШИМ», «Фрейм» — отчёт мастера).
            parts = [item._summary.text() for item in self._event_items]
            src = " / ".join(p for p in parts if p) or "—"
            self._summary_label.setText(
                tr("{0} → {1}").format(src, self._action_summary() or "—")
            )
        self._mark_dirty()

    def _action_summary(self) -> str:
        """Короткая строка действий для свёрнутой программы —
        все пункты списка через запятую."""
        return ", ".join(
            s for s in (item._summary.text() for item in self._action_items)
            if s and s != tr("Не выбрано")
        )

    def get_rule(self) -> dict[str, Any]:
        """Собирает программу из полей строки."""
        events = [item.read() for item in self._event_items]
        conditions = [item.read() for item in self._cond_items]
        actions = [item.read() for item in self._action_items]

        # Совместимость со старым форматом: объединённый блок
        # «action» — слияние всех пунктов списка действий.
        action: dict[str, Any] = {
            "var": "",
            "var_value": 1,
            "var_num": "",
            "var_delay": 0,
            "var_duration": 0,
            "frame_enabled": False,
            "channel": 0,
            "extended": False,
            "id": "",
            "dlc": 8,
            "data": "",
            "delay": 0,
            "count": 1,
            "between": 0,
            "aux_enabled": False,
            "aux_channel": 1,
            "aux_mode": "on",
            "aux_delay": 0,
            "aux_pulse_on": 100,
            "aux_pulse_off": 100,
            "aux_pulse_count": 3,
            "aux_pwm_freq": 1000,
            "aux_pwm_duty": 50,
            "aux_pwm_time": 0,
            "cache_enabled": False,
            "cache_channel": 2,
            "cache_extended": False,
            "cache_id": "",
            "cache_dlc": 8,
            "cache_from": "",
            "cache_to": "",
            "cache_tx_channel": 0,
            "cache_delay": 0,
            "cache_count": 1,
        }
        for act in actions:
            act = {k: v for k, v in act.items() if k != "type"}
            action.update(act)
        return {
            "title": self._name_edit.text().strip(),
            "active": self._active_check.isChecked(),
            "events": events,
            "conditions": conditions,
            "actions": actions,
            "action": action,
            "collapsed": self._body.isHidden(),
        }

    def set_counter(self, value: int) -> None:
        self._counter_label.setText(str(value))

    def set_number(self, number: int) -> None:
        """Номер программы в шапке («№ N», отчёт мастера)."""
        self._number_label.setText(f"№ {number}")

    def retranslate_ui(self) -> None:
        self._event_group.setTitle(tr("Событие"))
        self._cond_group.setTitle(tr("Условие"))
        self._action_group.setTitle(tr("Действие"))
        self._add_event_button.setText(tr("＋ событие"))
        self._add_cond_button.setText(tr("＋ условие"))
        self._add_action_button.setText(tr("＋ действие"))
        self._name_edit.setPlaceholderText(tr("Программа"))
        self._collapse_button.setToolTip(
            tr("Развернуть программу") if self._body.isHidden()
            else tr("Свернуть программу")
        )


class FlexibleLogicTab(QWidget):
    """Вкладка гибкой логики: программы ЕСЛИ-ПРИ-ТО применяются сами —
    включённая галочка = программа активна."""

    def __init__(
        self,
        serial_manager: SerialManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._rules: list[dict[str, Any]] = []
        self._rule_counters: list[int] = []
        self._row_widgets: list[RuleRowWidget] = []
        self._internal_rules: list[dict[str, Any]] = []
        self._rules_dirty = True
        # Исполнительное состояние переменных (ПК-режим): имя → 0/1
        # для статических и имя → величина для динамических; словари
        # заполняются по описаниям из вкладки «Переменные» на каждом
        # входящем кадре.
        self._variables_tab = None
        self._static_states: dict[str, int] = {}
        self._dyn_values: dict[str, float] = {}
        # Кэши байтов «Динамических переменных» (имя → {позиция: байт}):
        # каждый подошедший кадр перезаписывает только указанные
        # оператором позиции DATA (отчёт мастера).
        self._dyn_cache_bytes: dict[str, dict[int, int]] = {}
        # Состояние доп. каналов OUT1-4 (ПК-зеркало команд CMD_AUX_SET):
        # канал → 0/1. События/условия «Доп канал» опираются на него.
        self._aux_states: dict[int, int] = {}
        self._aux_flags: dict[tuple[int, int, int], bool] = {}
        # Признак «условие дин. события уже истинно» — событие «Стало
        # больше/меньше» — фронт булева состояния, а не каждый кадр.
        # Ключ: (программа, событие, имя переменной).
        self._dyn_flags: dict[tuple[int, int, str], bool] = {}
        # «Импульсные переменные»: имя → время (monotonic) гашения
        # вспышки; _pulsed_now — имена, вспыхнувшие на текущем кадре
        # (фронт для событий ГЛ — отчёт мастера).
        self._impulse_until: dict[str, float] = {}
        self._pulsed_now: set[str] = set()
        # Кэш действий: индекс программы → последний подошедший кадр.
        self._fl_cache: dict[int, dict[str, Any]] = {}
        # «Автоматическая запись DATA в кэш» команд «Управления»:
        # имя команды → последний кадр, подошедший под её маску
        # (байты «X» во фреймах команды при отправке подставляются
        # из него — отчёт мастера).
        self._cmd_caches: dict[str, bytes] = {}
        # «Сработок на DATA» событий-фреймов:
        # (программа, событие) → (last_data, n).
        self._event_fire: dict[tuple[int, int], tuple[bytes | None, int]] = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_config)
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def set_variables_tab(self, variables_tab) -> None:
        """Привязывает вкладку «Переменные» — списки имён и описания
        фреймов для исполнения (вызывается окном настроек)."""
        self._variables_tab = variables_tab
        self._refresh_variable_lists()

    def _refresh_variable_lists(self) -> None:
        for row in self._row_widgets:
            row.refresh_variables()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Переменные могли добавиться/переименоваться — комбобоксы
        # обновляем при каждом показе вкладки.
        self._refresh_variable_lists()

    def _create_widgets(self) -> None:
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setSpacing(10)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._rows_widget)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self._add_button = QPushButton(tr("＋ Добавить программу"))
        setup_button(self._add_button, bold=True, height=34)
        self._add_button.setMinimumWidth(240)
        self._add_button.clicked.connect(self._on_add)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self._add_button)
        top.addStretch()
        layout.addLayout(top)

        layout.addWidget(self._scroll, 1)

    def _load_config(self) -> None:
        """Загружает программы из общей конфигурации."""
        rules = self._config.get("flexible_rules", [])
        if not isinstance(rules, list):
            rules = []
        self._rules = rules
        self._rule_counters = [0] * len(rules)
        self._rebuild_rows()
        self._rules_dirty = True

    def mark_dirty(self) -> None:
        """Поля поменялись: пересобрать правила и отложенно сейвить."""
        self._rules_dirty = True
        self._save_timer.start()

    def _collect_rules(self) -> list[dict[str, Any]]:
        """Собирает программы из всех строк."""
        return [row.get_rule() for row in self._row_widgets]

    def _save_config(self) -> None:
        """Сохраняет программы в общую конфигурацию."""
        self._rules = self._collect_rules()
        self._config.set("flexible_rules", self._rules)

    def get_config(self) -> list[dict[str, Any]]:
        """Возвращает текущие программы для экспорта."""
        self._save_config()
        return self._config.get("flexible_rules", [])

    def set_config(self, rules: list[dict[str, Any]]) -> None:
        """Загружает программы из импортированного профиля."""
        self._config.set("flexible_rules", rules)
        self._load_config()

    def _rebuild_rows(self) -> None:
        """Пересоздаёт виджеты строк из self._rules."""
        while self._row_widgets:
            self._remove_row(self._row_widgets[-1])
        for rule in self._rules:
            self._add_row_widget(rule)
        self._rule_counters = [0] * len(self._row_widgets)
        self._refresh_variable_lists()

    def _add_row_widget(
        self,
        rule: dict[str, Any] | None = None,
    ) -> RuleRowWidget:
        """Добавляет виджет программы в конец списка."""
        row = RuleRowWidget(self, rule)
        self._row_widgets.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._renumber_rows()
        return row

    def _remove_row(self, widget: RuleRowWidget) -> None:
        """Удаляет виджет программы."""
        if widget in self._row_widgets:
            self._row_widgets.remove(widget)
        # setParent(None) до отложенного deleteLater — снимок полей окна
        # настроек сразу перестаёт видеть удалённую строку.
        widget.setParent(None)
        widget.deleteLater()
        self._rule_counters = [0] * len(self._row_widgets)
        self._renumber_rows()
        self.mark_dirty()

    def _renumber_rows(self) -> None:
        """Нумерация программ в шапках (№ 1, № 2, …)."""
        for i, row in enumerate(self._row_widgets, 1):
            row.set_number(i)

    def _on_add(self) -> None:
        """Добавляет новую пустую программу."""
        row = self._add_row_widget()
        row.refresh_variables()
        self._rule_counters.append(0)
        self.mark_dirty()

    # ---- исполнение -------------------------------------------------

    def _variable_defs(self) -> list[dict[str, Any]]:
        """Все описания переменных из вкладки «Переменные»."""
        if self._variables_tab is None:
            return []
        data = self._variables_tab.export_config()
        return (data.get("read") or []) + (data.get("control") or [])

    def _update_variable_states(self, frame_id: int, data: bytes) -> dict[str, int]:
        """Обновляет состояния переменных по пришедшему кадру.
        Возвращает статические переменные, сменившие состояние
        (имя → новое значение) — это фронты событий «Статическая»."""
        changed: dict[str, int] = {}
        self._pulsed_now.clear()
        for var in self._variable_defs():
            name = var.get("name", "").strip()
            if not name:
                continue
            if var.get("type") == "impulse":
                # «Импульсная переменная»: один фрейм, совпадение
                # указанных байтов DATA (X — любой) вспыхивает
                # переменную в «1» на 0.5 с (отчёт мастера).
                fid = hex_to_int(str(var.get("id", "")))
                if fid is None or fid != frame_id:
                    continue
                tokens = str(var.get("data", "")).split()
                if not _tokens_match(tokens, data):
                    continue
                self._impulse_until[name] = time.monotonic() + 0.5
                self._pulsed_now.add(name)
            elif var.get("type") == "static":
                for frame_def in var.get("frames") or []:
                    fid = hex_to_int(str(frame_def.get("id", "")))
                    if fid is None or fid != frame_id:
                        continue
                    tokens = str(frame_def.get("data", "")).split()
                    if not _tokens_match(tokens, data):
                        continue
                    value = int(frame_def.get("value", 1) or 0)
                    if self._static_states.get(name) != value:
                        self._static_states[name] = value
                        changed[name] = value
            elif var.get("type") == "dyn_cache":
                # «Динамическая переменная»: МК-кэш байтов — каждый
                # подошедший кадр перезаписывает указанные позиции,
                # значение переменной — имя привязки из таблицы
                # (символьное), либо None до совпадения (отчёт мастера).
                fid = hex_to_int(str(var.get("id", "")))
                if fid is None or fid != frame_id:
                    continue
                used = var.get("bytes") or []
                if not used:
                    continue
                cache = self._dyn_cache_bytes.setdefault(name, {})
                for pos in used:
                    if 0 <= pos < len(data):
                        cache[pos] = data[pos]
                raw = 0
                complete = True
                for pos in used:
                    if pos in cache:
                        raw = (raw << 8) | cache[pos]
                    else:
                        complete = False
                if not complete:
                    continue
                self._dyn_values[name] = self._dyn_binding_name(
                    var.get("points"), raw
                )
            else:
                fid = hex_to_int(str(var.get("id", "")))
                if fid is None or fid != frame_id:
                    continue
                lo = str(var.get("from", "")).split()
                hi = str(var.get("to", "")).split()
                if not _tokens_range_match(lo, hi, data):
                    continue
                # «Численная переменная»: сырое значение — СУММА
                # выбранных байтов кадра (отчёт мастера).
                raw = 0
                for pos in var.get("bytes") or []:
                    if 0 <= pos < len(data):
                        raw += data[pos]
                self._dyn_values[name] = _interpolate(
                    var.get("points"), raw
                )
        return changed

    @staticmethod
    def _dyn_binding_name(points: Any, raw: int) -> str:
        """Имя привязки «Динамической переменной» по сырому значению
        кэша: таблица points хранит пары [значение DATA, имя] — одно имя
        может быть у нескольких значений (отчёт мастера)."""
        for point in points or []:
            try:
                if int(point[0]) == raw:
                    return str(point[1])
            except (TypeError, ValueError, IndexError):
                continue
        return ""

    def _build_internal_rules(self) -> None:
        """Формирует внутренний список активных программ."""
        self._internal_rules = []
        self._rule_counters = [0] * len(self._row_widgets)
        for row_index, row in enumerate(self._row_widgets):
            rule = row.get_rule()
            if not rule.get("active", False):
                continue
            self._internal_rules.append({"index": row_index, "rule": rule})

    @staticmethod
    def _pad_8(data: bytes) -> bytes:
        data = data[:8]
        return data + b"\x00" * (8 - len(data))

    def _frame_event_matches(self, event: dict[str, Any], frame: dict[str, Any]) -> bool:
        can_id = hex_to_int(str(event.get("id", "")))
        if can_id is None or can_id != int(frame["id"]):
            return False
        if bool(event.get("extended")) != bool(frame.get("extended", int(frame["id"]) > 0x7FF)):
            return False
        ch = int(event.get("channel", 2))
        if ch != 2 and ch + 1 != int(frame["channel"]):
            return False
        # Галочка «RTR»: программа стартует по кадру-запросу —
        # DATA у RTR-кадра нет (как rx_rtr в триггерах: 0 — любой
        # кадр, 1 — только RTR). Отчёт мастера.
        if event.get("rtr"):
            return bool(frame.get("rtr"))
        tokens = str(event.get("data", "")).split()
        return _tokens_match(tokens, bytes(frame["data"]))

    def _condition_passed(self, cond: dict[str, Any]) -> bool:
        """ПРИ: проверка текущего состояния перед действием."""
        ctype = cond.get("type", _COND_NONE)
        if ctype == _COND_STATIC:
            state = self._static_states.get(str(cond.get("var", "")), 0)
            return state == int(cond.get("state", 1))
        if ctype == _COND_DYN:
            # «Активно»: переменная сейчас находится в одном из
            # состояний таблицы привязки (сырое значение кэша
            # совпало с именованной точкой — отчёт мастера).
            return bool(self._dyn_values.get(str(cond.get("var", ""))))
        if ctype == _COND_NUM:
            value = self._dyn_values.get(str(cond.get("var", "")))
            if value is None:
                return False
            try:
                threshold = float(str(cond.get("value", "")).replace(",", "."))
            except ValueError:
                # Символьное имя привязки «Динамической переменной»:
                # «равно» — совпадение состояния с именем.
                return cond.get("op", "eq") == "eq" and str(value) == str(
                    cond.get("value", "")
                ).strip()
            if not isinstance(value, (int, float)):
                return False
            op = cond.get("op", "gt")
            if op == "lt":
                return value < threshold
            if op == "eq":
                return value == threshold
            return value > threshold
        if ctype == _COND_IMPULSE:
            # «Импульс активен» — пока не истекли 0.5 с вспышки.
            until = self._impulse_until.get(str(cond.get("var", "")), 0.0)
            return time.monotonic() < until
        if ctype == _COND_AUX:
            # «Доп канал N активен» — ПК-зеркало команд CMD_AUX_SET.
            ch = int(cond.get("channel", 1) or 1)
            state = self._aux_states.get(ch, 0)
            return state == int(cond.get("state", 1))
        return True  # «Нет» — не блокирует.

    def _update_cache(self, rule_index: int, action: dict[str, Any], frame: dict[str, Any]) -> None:
        """«Автоматическая запись DATA в кэш»: входящий кадр, подходящий
        под спецификацию источника, сохраняется — при срабатывании
        программы уходит последний сохранённый (как в триггерах)."""
        if not action.get("cache_enabled"):
            return
        can_id = hex_to_int(str(action.get("cache_id", "")))
        if can_id is None or can_id != int(frame["id"]):
            return
        if bool(action.get("cache_extended")) != bool(
            frame.get("extended", int(frame["id"]) > 0x7FF)
        ):
            return
        ch = int(action.get("cache_channel", 2))
        if ch != 2 and ch + 1 != int(frame["channel"]):
            return
        lo = str(action.get("cache_from", "")).split()
        hi = str(action.get("cache_to", "")).split()
        data = bytes(frame["data"])
        if not _tokens_range_match(lo, hi, data):
            return
        self._fl_cache[rule_index] = {
            "id": can_id,
            "data": self._pad_8(data),
        }

    def _send_frame(
        self,
        channel: int,
        can_id: int,
        data: bytes,
        delay_ms: int = 0,
        rtr: bool = False,
        dlc: int | None = None,
    ) -> None:
        packed = pack_can_frame(channel, can_id, data, rtr=rtr, dlc=dlc)
        if delay_ms > 0:
            QTimer.singleShot(
                delay_ms, lambda p=packed: self._serial_manager.send_data(p)
            )
        else:
            self._serial_manager.send_data(packed)

    def _control_commands(self) -> list[dict[str, Any]]:
        """Команды раздела «Управление» вкладки «Переменные»
        (без записей папок — отчёт мастера)."""
        if self._variables_tab is None:
            return []
        data = self._variables_tab.export_config()
        return [
            c for c in (data.get("control") or [])
            if isinstance(c, dict) and c.get("type") == "command"
        ]

    def _update_command_caches(
        self, frame_id: int, data: bytes, frame: dict[str, Any]
    ) -> None:
        """«Автоматическая запись DATA в кэш» команд «Управления»:
        входящий кадр, подходящий под маску кэша команды (канал/ID/
        DATA «от–до»), запоминается — байты «X» во фреймах команды
        при отправке подставляются из него (отчёт мастера)."""
        for cmd in self._control_commands():
            spec = cmd.get("cache") or {}
            if not spec.get("enabled"):
                continue
            can_id = hex_to_int(str(spec.get("id", "")))
            if can_id is None or can_id != frame_id:
                continue
            if bool(spec.get("extended")) != bool(
                frame.get("extended", frame_id > 0x7FF)
            ):
                continue
            ch = int(spec.get("channel", 2))
            if ch != 2 and ch + 1 != int(frame["channel"]):
                continue
            lo = str(spec.get("from", "")).split()
            hi = str(spec.get("to", "")).split()
            if not _tokens_range_match(lo, hi, data):
                continue
            self._cmd_caches[str(cmd.get("name", "")).strip()] = data

    def _run_command(self, cmd: dict[str, Any]) -> None:
        """Отправка последовательности фреймов команды «Управления»
        (как «Ответ» триггера): у каждого фрейма «пауза перед
        отправкой», «кол-во» повторов с «паузой между» и «пауза до
        следующего»; байты «X» подставляются из кэша команды."""
        name = str(cmd.get("name", "")).strip()
        cached = self._cmd_caches.get(name)
        t = 0
        for fr in cmd.get("frames") or []:
            can_id = hex_to_int(str(fr.get("id", "")))
            if can_id is None:
                continue
            t += int(fr.get("delay_before_send", 0) or 0)
            count = max(1, int(fr.get("count", 1) or 1))
            between = int(fr.get("delay_between", 0) or 0)
            channel = int(fr.get("channel", 0) or 0) + 1
            rtr = bool(fr.get("rtr", False))
            dlc = max(0, min(8, int(fr.get("dlc", 8) or 8)))
            if rtr:
                for n in range(count):
                    self._send_frame(
                        channel, can_id, b"", t + n * between,
                        rtr=True, dlc=dlc,
                    )
            else:
                tokens = str(fr.get("data", "")).split()
                payload = bytearray(8)
                for i, tok in enumerate(tokens[:8]):
                    if tok and tok.upper() != "X":
                        value = hex_to_int(tok)
                        payload[i] = value if value is not None else 0
                    elif cached is not None and i < len(cached):
                        payload[i] = cached[i]
                for n in range(count):
                    self._send_frame(
                        channel, can_id, bytes(payload[:dlc]),
                        t + n * between,
                    )
            t += (count - 1) * between + int(fr.get("next_delay", 0) or 0)

    def _run_actions(
        self, rule_index: int, action: dict[str, Any], frame: dict[str, Any]
    ) -> None:
        # 1. «Переменные управления» — команда из раздела
        #    «Управление»: отправляет свою последовательность фреймов
        #    (паузы/количество/пауза до следующего заданы в команде;
        #    байты «X» подставляются из автоматически записанного
        #    кэша команды — отчёт мастера).
        var_name = str(action.get("var", "")).strip()
        if var_name:
            candidates = [var_name]
            # Старые программы ссылались на статическую переменную
            # «Управления» — она конвертировалась в команды
            # «имя → 1»/«имя → 0» (обратная совместимость).
            if "var_value" in action:
                candidates.append(
                    f"{var_name} → {int(action.get('var_value', 1) or 0)}"
                )
            for cmd in self._control_commands():
                if cmd.get("name", "").strip() in candidates:
                    self._run_command(cmd)
                    break

        # 2. Ручной фрейм: «X»-байты подставляются из кадра события.
        if action.get("frame_enabled"):
            can_id = hex_to_int(str(action.get("id", "")))
            if can_id is not None:
                src = self._pad_8(bytes(frame["data"]))
                tokens = str(action.get("data", "")).split()
                payload = bytearray(src)
                for i, t in enumerate(tokens[:8]):
                    if not t or t == "X":
                        continue
                    v = hex_to_int(t)
                    if v is not None:
                        payload[i] = v
                dlc = int(action.get("dlc", 8) or 8)
                count = max(1, int(action.get("count", 1) or 1))
                base_delay = int(action.get("delay", 0) or 0)
                between = int(action.get("between", 0) or 0)
                channel = int(action.get("channel", 0)) + 1
                for n in range(count):
                    self._send_frame(
                        channel, can_id, bytes(payload[:dlc]),
                        base_delay + n * between,
                    )

        # 3. Доп канал OUT1-4: «Вкл» держится до «Выкл»/питания,
        #    импульсы и ШИМ крутит сам МК (aux_out.c). «Пауза до
        #    действия» — PC-таймер, как у фреймов.
        if action.get("aux_enabled"):
            aux_delay = int(action.get("aux_delay", 0) or 0)
            channel = int(action.get("aux_channel", 1) or 1)
            mode_name = str(action.get("aux_mode", "on"))
            mode = {"off": 0, "on": 1, "pulse": 2, "pwm": 3}.get(
                mode_name, 1
            )
            kwargs = {
                "pulse_on_ms": int(action.get("aux_pulse_on", 0) or 0),
                "pulse_off_ms": int(action.get("aux_pulse_off", 0) or 0),
                "pulse_count": int(action.get("aux_pulse_count", 0) or 0),
                "pwm_freq_hz": int(action.get("aux_pwm_freq", 0) or 0),
                "pwm_duty_pct": int(action.get("aux_pwm_duty", 0) or 0),
                "pwm_time_ms": int(action.get("aux_pwm_time", 0) or 0),
            }

            def fire_aux(
                ch=channel, m=mode, kw=kwargs, mn=mode_name,
            ) -> None:
                # ПК-зеркало состояния — для условий «Доп канал N
                # активен» (уровень на ноге следует за mode).
                self._aux_states[ch] = 0 if m == 0 else 1
                self._serial_manager.send_aux(ch, m, **kw)
                logger.info(
                    "ГЛ: доп канал №%d → %s", ch, mn,
                )
                # Смена состояния канала — это событие само по себе:
                # программы, подписанные на «Доп канал N …», должны
                # сработать сразу, а не на следующем CAN-кадре.
                self._fire_aux_events()

            if aux_delay > 0:
                QTimer.singleShot(aux_delay, fire_aux)
            else:
                fire_aux()

        # 4. Кэш: уходит последний сохранённый кадр (как в триггерах —
        #    строка без заполненного кэша пропускается).
        if action.get("cache_enabled"):
            cached = self._fl_cache.get(rule_index)
            if cached is not None:
                count = max(1, int(action.get("cache_count", 1) or 1))
                delay = int(action.get("cache_delay", 0) or 0)
                channel = int(action.get("cache_tx_channel", 0)) + 1
                for n in range(count):
                    self._send_frame(
                        channel, cached["id"], cached["data"], delay * (n + 1)
                    )

    def _fire_aux_events(self) -> None:
        """Смена состояния доп. канала — проход по программам с
        событиями «Доп канал»: условия И → действия (ИЛИ по событиям).
        Реентерабельность ограничена: действие может само щёлкать
        каналом — глубже одного уровня не идём, чтобы кольцевая
        программа («канал 1 → канал 1») не зациклила ПК."""
        if getattr(self, "_aux_events_busy", False):
            return
        self._aux_events_busy = True
        try:
            self._fire_aux_events_inner()
        finally:
            self._aux_events_busy = False

    def _fire_aux_events_inner(self) -> None:
        if self._rules_dirty:
            self._build_internal_rules()
            self._rules_dirty = False
        dummy_frame = {"id": 0, "channel": 0, "data": b"", "extended": False}
        for internal in self._internal_rules:
            rule_index = internal["index"]
            rule = internal["rule"]
            events = rule.get("events")
            if not isinstance(events, list) or not events:
                events = [rule.get("event") or {}]
            conditions = rule.get("conditions")
            if not isinstance(conditions, list) or not conditions:
                conditions = [rule.get("condition") or {}]
            fired = False
            for sub_index, event in enumerate(events):
                if event.get("type") == _EVENT_AUX and self._event_fired(
                    rule_index, sub_index, event, dummy_frame,
                    b"", {},
                ):
                    fired = True
                    break
            if not fired:
                continue
            if not all(
                self._condition_passed(cond) for cond in conditions
            ):
                continue
            self._rule_counters[rule_index] += 1
            self._row_widgets[rule_index].set_counter(
                self._rule_counters[rule_index]
            )
            actions = rule.get("actions")
            if not isinstance(actions, list) or not actions:
                actions = [rule.get("action") or {}]
            for action in actions:
                self._run_actions(rule_index, action, dummy_frame)

    def set_dbc(self, dbc_manager) -> None:
        """Обновляет логику при смене DBC (заглушка)."""

    def _event_fired(
        self,
        rule_index: int,
        sub_index: int,
        event: dict[str, Any],
        frame: dict[str, Any],
        frame_data: bytes,
        changed_static: dict[str, int],
    ) -> bool:
        """Проверяет одно событие программы по пришедшему кадру."""
        etype = event.get("type", _EVENT_FRAME)
        if etype == _EVENT_FRAME:
            if not self._frame_event_matches(event, frame):
                return False
            # «Количество сработок до смены DATA» (галочка — как в
            # триггерах): 0 = без ограничения, каждый подошедший
            # кадр запускает программу; N>0 — одинаковый поток
            # перезапускает программу не более N раз, новая DATA —
            # заново. Счётчик ведётся по (программа, событие).
            limit = int(event.get("fire_limit", 1) or 0)
            if limit <= 0:
                return True
            key = (rule_index, sub_index)
            last, count = self._event_fire.get(key, (None, 0))
            if last != frame_data:
                last, count = frame_data, 0
            fired = count < limit
            if fired:
                count += 1
            self._event_fire[key] = (last, count)
            return fired
        if etype == _EVENT_STATIC:
            name = str(event.get("var", "")).strip()
            if name not in changed_static:
                return False
            edge = event.get("edge", "both")
            return (
                edge == "both"
                or (edge == "on" and changed_static[name] == 1)
                or (edge == "off" and changed_static[name] == 0)
            )
        if etype in (_EVENT_DYN, _EVENT_NUM):
            name = str(event.get("var", "")).strip()
            if name not in self._dyn_values:
                return False
            current = self._dyn_values[name]
            try:
                threshold = float(
                    str(event.get("value", "")).replace(",", ".")
                )
            except ValueError:
                # Символьное состояние «Динамической переменной»:
                # срабатывание на совпадение имени привязки.
                flag = str(current) == str(event.get("value", "")).strip()
            else:
                if not isinstance(current, (int, float)):
                    return False
                flag = (
                    current > threshold
                    if event.get("dir", "gt") == "gt"
                    else current < threshold
                )
            key = (rule_index, sub_index, name)
            # Фронт истинности: «стало больше/меньше» стреляет
            # один раз на переход через порог.
            fired = flag and not self._dyn_flags.get(key, False)
            self._dyn_flags[key] = flag
            return fired
        if etype == _EVENT_IMPULSE:
            # «Импульсная переменная»: событие — фронт вспышки на
            # этом кадре (каждый подошедший кадр — новая сработка,
            # как нажатие кнопки — отчёт мастера).
            return str(event.get("var", "")).strip() in self._pulsed_now
        if etype == _EVENT_AUX:
            # Фронт состояния доп. канала по ПК-зеркалу CMD_AUX_SET:
            # «активен»/«не активен» стреляет на переходе уровня.
            ch = int(event.get("channel", 1) or 1)
            want = int(event.get("state", 1) or 0)
            flag = self._aux_states.get(ch, 0) == want
            key = (rule_index, sub_index, ch)
            fired = flag and not self._aux_flags.get(key, False)
            self._aux_flags[key] = flag
            return fired
        return False

    def process_frame(self, frame: dict[str, Any]) -> None:
        """Проверяет входящий кадр: обновляет переменные, кэши действий,
        затем события ЕСЛИ → условия ПРИ → действия ТО.

        Эхо собственной передачи МК игнорируется — иначе ответ
        программы, совпадающий с событием-фреймом, перезапускал бы
        программу бесконечно."""
        if frame.get("tx_echo"):
            return
        if self._rules_dirty:
            self._build_internal_rules()
            self._rules_dirty = False
        if not self._internal_rules and self._variables_tab is None:
            return

        frame_id = int(frame["id"])
        frame_data = self._pad_8(bytes(frame["data"]))

        # Сначала состояние переменных — от него зависят и события,
        # и условия ПРИ.
        changed_static = self._update_variable_states(frame_id, frame_data)
        # «Автоматическая запись DATA в кэш» команд «Управления» —
        # работает независимо от срабатывания программ (отчёт мастера).
        self._update_command_caches(frame_id, frame_data, frame)

        for internal in self._internal_rules:
            rule_index = internal["index"]
            rule = internal["rule"]
            events = rule.get("events")
            if not isinstance(events, list) or not events:
                events = [rule.get("event") or {}]
            conditions = rule.get("conditions")
            if not isinstance(conditions, list) or not conditions:
                conditions = [rule.get("condition") or {}]
            # Действия — список «actions» (новый формат) либо старый
            # объединённый блок «action»; выполняются все по порядку.
            actions = rule.get("actions")
            if not isinstance(actions, list) or not actions:
                actions = [rule.get("action") or {}]

            # Кэш пополняется независимо от срабатывания программы
            # (как строки кэша триггеров).
            for action in actions:
                self._update_cache(rule_index, action, frame)

            # Несколько событий объединены по ИЛИ: программа
            # стартует по любому из них (отчёт мастера).
            fired = False
            fired_etype = ""
            for sub_index, event in enumerate(events):
                if self._event_fired(
                    rule_index, sub_index, event, frame,
                    frame_data, changed_static,
                ):
                    fired = True
                    fired_etype = str(event.get("type", _EVENT_FRAME))
                    break

            if not fired:
                continue
            # Несколько условий объединены по И: должны
            # выполняться все (отчёт мастера).
            if not all(
                self._condition_passed(cond) for cond in conditions
            ):
                continue

            self._rule_counters[rule_index] += 1
            self._row_widgets[rule_index].set_counter(
                self._rule_counters[rule_index]
            )
            for action in actions:
                self._run_actions(rule_index, action, frame)
            logger.info(
                "Сработала программа ГЛ «%s» (событие %s)",
                rule.get("title") or rule_index,
                fired_etype,
            )

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки."""
        self._add_button.setText(tr("＋ Добавить программу"))
        for row in self._row_widgets:
            row.retranslate_ui()
