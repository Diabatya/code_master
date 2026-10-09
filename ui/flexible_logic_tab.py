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
    QApplication,
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
from ui.variables_tab import _HexIdEdit, _bind_id_width, _tokens_match

logger = get_logger(__name__)

_EVENT_NONE = "none"
_EVENT_DYN = "dyn"
_EVENT_NUM = "num"   # «Численная переменная» — отдельный вид (отчёт мастера)
_EVENT_IMPULSE = "impulse"  # «Импульсная переменная» (отчёт мастера)
_EVENT_STATIC = "static"
_EVENT_AUX = "aux"
_EVENT_FRAME = "frame"
# «Включение устройства» — программа стартует сразу после полной
# загрузки камня по включению питания (отчёт мастера). Событие без
# параметров; в «Прервать если» не предлагается.
_EVENT_POWER = "power"
# «Переменная» — именованный бит ОЗУ/ПЗУ из третьей колонки
# «Переменных» (вид «Переменная», отчёт мастера): событие «Стала 1»/
# «Стала 0» — на фронте, условие — по текущему состоянию, действие —
# «включить»/«включить на N мс»/«выключить».
_EVENT_FLAG = "flag"
_COND_FLAG = "flag"
_ACT_FLAG = "flag"

# «Кэш переменная» (отчёт мастера): два буфера на переменную —
# скрытый буфер 1 (ОЗУ, непрерывный захват кадров диапазона ID) и
# буфер 2 (ОЗУ/ПЗУ — выбирается в настройке переменной). События:
# «Приход DATA КЭШ» (фронт захвата кадра в буфер 1 на выбранном
# канале), «Записалась КЭШ переменная» (запись в привязанный кэш —
# захват в буфер 1 или перенос в буфер 2), «Стирание КЭШ
# переменной» (привязанный буфер обнулился). Условия: «Записан
# КЭШ»/«Не записан КЭШ» — по содержимому буфера 2. Действия:
# «Записать КЭШ» (буфер 1 → буфер 2, буфер 1 обнуляется),
# «Отправить КЭШ» (буфер 2 → CAN1/CAN2, кол-во + пауза),
# «Стереть КЭШ» (буфер 2 = 0).
_EVENT_CACHEVAR = "cachevar"
_COND_CACHEVAR = "cachevar"
_ACT_CACHEVAR = "cachevar"
# Под-операции событий/условий/действий кэш-переменной.
_CACHE_OP_RX = "rx"          # приход кадра диапазона → буфер 1
_CACHE_OP_WRITTEN = "written"  # произошла запись в кэш
_CACHE_OP_ERASED = "erased"    # привязанный буфер обнулился
_CACHE_OP_FILLED = "filled"    # условие «Записан КЭШ»
_CACHE_OP_EMPTY = "empty"      # условие «Не записан КЭШ»
_CACHE_OP_COMMIT = "commit"    # действие «Записать КЭШ»
_CACHE_OP_SEND = "send"        # действие «Отправить КЭШ»
_CACHE_OP_ERASE = "erase"      # действие «Стереть КЭШ»

# «Программа (имя) начала работать» — событие-фронт: срабатывает,
# когда указанная программа ГЛ прошла фазу событий и перешла к
# условиям (отчёт мастера). Саму себя программа слушать не может.
_EVENT_PROGRAM = "program"

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
    """Строка-сводка события/условия: клик разворачивает редактор.
    Пока тип «Не выбрано» — строка обведена голубой рамкой
    (отчёт мастера)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._unselected = False
        self._apply_style()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def set_unselected(self, on: bool) -> None:
        """Голубая рамка вокруг «Не выбрано» (отчёт мастера)."""
        if on == self._unselected:
            return
        self._unselected = on
        self._apply_style()

    def _apply_style(self) -> None:
        border = (
            " border: 1px solid #3A7BD5; border-radius: 4px;"
            " padding: 1px 8px;" if self._unselected else ""
        )
        self.setStyleSheet(f"color: #7C9EFF;{border}")

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
        layout.setSpacing(2)
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
        layout.setSpacing(2)
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
        layout.setSpacing(2)
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


class _DynCondPage(QWidget):
    """Условие «Динамическая переменная»: имя переменной + состояние
    из её таблицы привязки (отчёт мастера: список состояний в условии
    не показывался). Пустое состояние — легаси-семантика «Активно»
    (истинно, пока переменная в любом именованном состоянии)."""

    def __init__(
        self,
        font: QFont,
        mark_dirty,
        get_tab=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._get_tab = get_tab
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        layout.addWidget(_small_label(tr("Состояние:"), font))
        self.state = QComboBox()
        self.state.setFont(font)
        # Редактируемый — состояние могло сохраниться, когда переменная
        # была удалена и список пуст: значение не теряется.
        self.state.setEditable(True)
        layout.addWidget(self.state)
        layout.addStretch()
        self.var.currentIndexChanged.connect(mark_dirty)
        self.var.currentIndexChanged.connect(
            lambda *_a: self.refresh_binding_names()
        )
        self.state.currentTextChanged.connect(mark_dirty)

    def refresh_binding_names(self) -> None:
        """Состояния — реальные имена из таблицы привязки выбранной
        «Динамической переменной» (как у события — отчёт мастера)."""
        current = self.state.currentText()
        tab = self._get_tab() if self._get_tab is not None else None
        names = (
            tab.variable_binding_names("read", self.var.get_name())
            if tab and self.var.get_name()
            else []
        )
        self.state.blockSignals(True)
        self.state.clear()
        self.state.addItems(names)
        idx = self.state.findText(current)
        if idx >= 0:
            self.state.setCurrentIndex(idx)
        else:
            self.state.setEditText(current)
        self.state.blockSignals(False)


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
        layout.setSpacing(2)
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
    """Событие «Доп канал»: направление «Вход»/«Выход», номер канала
    и состояние «Активен»/«Не активен» (отчёт мастера: помимо входов
    в событиях доступны и выходы — программа срабатывает по включению
    выбранного выхода)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.direction = QComboBox()
        self.direction.setFont(font)
        self.direction.addItem(tr("Вход"), "in")
        self.direction.addItem(tr("Выход"), "out")
        row.addWidget(self.direction)
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

        self.direction.currentIndexChanged.connect(mark_dirty)
        self.channel.valueChanged.connect(mark_dirty)
        self.state.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_AUX,
            "direction": self.direction.currentData(),
            "channel": self.channel.value(),
            "state": self.state.currentData(),
        }

    def write(self, event: dict[str, Any]) -> None:
        didx = self.direction.findData(event.get("direction", "in"))
        self.direction.setCurrentIndex(didx if didx >= 0 else 0)
        self.channel.setValue(int(event.get("channel", 1) or 1))
        sidx = self.state.findData(int(event.get("state", 1) or 0))
        self.state.setCurrentIndex(sidx if sidx >= 0 else 0)


class _PowerEventPage(QWidget):
    """Событие «Включение устройства» — без параметров: программа
    стартует сразу после полной загрузки камня по включению питания
    (отчёт мастера). На ПК момент определяется подключением и
    опознанием устройства (device_identified)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(tr(
            "Программа запускается один раз сразу после полной "
            "загрузки устройства по включению питания."
        ))
        hint.setFont(font)
        hint.setStyleSheet("color: #9A9AA5;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

    def read(self) -> dict[str, Any]:
        return {"type": _EVENT_POWER}

    def write(self, event: dict[str, Any]) -> None:  # noqa: ARG002
        pass


class _FlagEventPage(QWidget):
    """Событие «Переменная» — именованный бит ОЗУ/ПЗУ: выбор
    переменной из третьей колонки «Переменных» и фронт «Стала 1»/
    «Стала 0» (отчёт мастера)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        row = QHBoxLayout()
        row.addWidget(_small_label(tr("Событие:"), font))
        self.edge = QComboBox()
        self.edge.setFont(font)
        self.edge.addItem(tr("Стала 1"), 1)
        self.edge.addItem(tr("Стала 0"), 0)
        row.addWidget(self.edge)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        self.var.currentIndexChanged.connect(mark_dirty)
        self.edge.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_FLAG,
            "var": self.var.get_name(),
            "state": self.edge.currentData(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))
        eidx = self.edge.findData(int(event.get("state", 1) or 0))
        self.edge.setCurrentIndex(eidx if eidx >= 0 else 0)


class _FrameEventPage(QWidget):
    """Событие «Фрейм»: ручной ввод CAN-пакета — настройки как
    в триггерах (канал, битность, ID, DLC, DATA по байтам с «X»,
    RTR — срабатывание по кадру-запросу, «Количество сработок до
    смены DATA» — галочкой, как в триггерах — отчёт мастера)."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
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
        _bind_id_width(self.bit, self.can_id)
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
            font, 8, edit_width=40, allow_x=True
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


class _CacheVarEventPage(QWidget):
    """Событие «Кэш переменная» (отчёт мастера): три функции —
    «Приход DATA КЭШ» (кадр из диапазона переменной пришёл на
    выбранном канале и записан в скрытый буфер 1), «Записалась КЭШ
    переменная» (в привязанный кэш произошла запись данных),
    «Стирание КЭШ переменной» (привязанный буфер обнулился).
    Канал CAN1/CAN2 выбирается только у «Прихода»."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        row = QHBoxLayout()
        row.addWidget(_small_label(tr("Событие:"), font))
        self.op = QComboBox()
        self.op.setFont(font)
        self.op.addItem(tr("Приход DATA КЭШ"), _CACHE_OP_RX)
        self.op.addItem(
            tr("Записалась КЭШ переменная"), _CACHE_OP_WRITTEN
        )
        self.op.addItem(
            tr("Стирание КЭШ переменной"), _CACHE_OP_ERASED
        )
        row.addWidget(self.op)
        self.channel = QComboBox()
        self.channel.setFont(font)
        self.channel.addItem("CAN1", 1)
        self.channel.addItem("CAN2", 2)
        self.channel.setFixedWidth(80)
        row.addWidget(self.channel)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

        self.var.currentIndexChanged.connect(mark_dirty)
        self.op.currentIndexChanged.connect(mark_dirty)
        self.channel.currentIndexChanged.connect(mark_dirty)
        self.op.currentIndexChanged.connect(self._update_channel)
        self._update_channel()

    def _update_channel(self, *_args) -> None:
        # «из какого CAN получаем» спрашиваем только у прихода DATA
        # (отчёт мастера).
        self.channel.setVisible(self.op.currentData() == _CACHE_OP_RX)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_CACHEVAR,
            "var": self.var.get_name(),
            "cache_op": self.op.currentData(),
            "channel": int(self.channel.currentData()),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))
        oidx = self.op.findData(event.get("cache_op", _CACHE_OP_RX))
        self.op.setCurrentIndex(oidx if oidx >= 0 else 0)
        cidx = self.channel.findData(int(event.get("channel", 1) or 1))
        self.channel.setCurrentIndex(cidx if cidx >= 0 else 0)
        self._update_channel()


class _ProgramEventPage(QWidget):
    """Событие «Программа (имя) начала работать» (отчёт мастера):
    срабатывает, когда выбранная программа ГЛ прошла фазу событий и
    перешла к своим условиям. Список — имена всех программ вкладки."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Программа:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        hint = QLabel(tr(
            "Срабатывает, когда выбранная программа перешла "
            "от событий к условиям — дальше выполняются только "
            "условия этой программы."
        ))
        hint.setFont(font)
        hint.setStyleSheet("color: #9A9AA5;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        self.var.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_PROGRAM,
            "var": self.var.get_name(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))


class _CacheVarCondPage(QWidget):
    """Условие «Кэш переменная» (отчёт мастера): «Записан КЭШ» —
    буфер 2 переменной содержит данные (не нули); «Не записан КЭШ» —
    буфер 2 пуст/нулевой."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)
        row = QHBoxLayout()
        row.addWidget(_small_label(tr("Состояние:"), font))
        self.state = QComboBox()
        self.state.setFont(font)
        self.state.addItem(tr("Записан КЭШ"), _CACHE_OP_FILLED)
        self.state.addItem(tr("Не записан КЭШ"), _CACHE_OP_EMPTY)
        row.addWidget(self.state)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        self.var.currentIndexChanged.connect(mark_dirty)
        self.state.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _COND_CACHEVAR,
            "var": self.var.get_name(),
            "cache_op": self.state.currentData(),
        }

    def write(self, cond: dict[str, Any]) -> None:
        self.var.set_name(str(cond.get("var", "")))
        sidx = self.state.findData(cond.get("cache_op", _CACHE_OP_FILLED))
        self.state.setCurrentIndex(sidx if sidx >= 0 else 0)


class _AbortEventEditor(QWidget):
    """«Прервать если» — событие-прерыватель действия (отчёт
    мастера): те же виды событий, кроме «Включение устройства» —
    оно одноразовое и не может прервать отложенное действие.

    Пока действие ждёт своей «Задержки», рантайм следит за
    событием-прерывателем: сработал → действие отменяется."""

    _TYPES = (
        (_EVENT_STATIC, "Статическая переменная"),
        (_EVENT_DYN, "Динамическая переменная"),
        (_EVENT_NUM, "Численная переменная"),
        (_EVENT_IMPULSE, "Импульсная переменная"),
        (_EVENT_AUX, "Доп канал"),
        (_EVENT_FLAG, "Переменная"),
        (_EVENT_FRAME, "Фрейм"),
    )

    def __init__(self, font: QFont, mark_dirty, get_tab, parent=None) -> None:
        super().__init__(parent)
        self._get_tab = get_tab
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.enable = QCheckBox(tr("Прервать если"))
        self.enable.setFont(font)
        self.enable.setToolTip(tr(
            "Отменить это действие, если за время задержки "
            "произошло выбранное событие"
        ))
        row.addWidget(self.enable)
        self._type = QComboBox()
        self._type.setFont(font)
        for code, title in self._TYPES:
            self._type.addItem(tr(title), code)
        self._type.setEnabled(False)
        row.addWidget(self._type, 1)
        layout.addLayout(row)

        self._stack = QStackedWidget()
        self._stack.setEnabled(False)
        self._static = _StaticEventPage(font, mark_dirty)
        self._dyn = _DynEventPage(font, mark_dirty, _EVENT_DYN, get_tab=get_tab)
        self._num = _DynEventPage(font, mark_dirty, _EVENT_NUM)
        self._impulse = _ImpulseVarPage(font, mark_dirty)
        self._aux = _AuxEventPage(font, mark_dirty)
        self._flag = _FlagEventPage(font, mark_dirty)
        self._frame = _FrameEventPage(font, mark_dirty)
        for page in (
            self._static, self._dyn, self._num, self._impulse,
            self._aux, self._flag, self._frame,
        ):
            self._stack.addWidget(page)
        layout.addWidget(self._stack)

        # Дерево настроек (тип события + страница параметров) видно
        # только при включённой галочке «Прервать если»
        # (отчёт мастера).
        self._type.setVisible(False)
        self._stack.setVisible(False)
        self.enable.toggled.connect(self._type.setEnabled)
        self.enable.toggled.connect(self._stack.setEnabled)
        self.enable.toggled.connect(self._type.setVisible)
        self.enable.toggled.connect(self._stack.setVisible)
        self.enable.toggled.connect(mark_dirty)
        self._type.currentIndexChanged.connect(self._stack.setCurrentIndex)
        self._type.currentIndexChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        """Событие-прерыватель; «Не выбрано», если галочка снята."""
        if not self.enable.isChecked():
            return {"type": _EVENT_NONE}
        etype = self._type.currentData()
        if etype == _EVENT_IMPULSE:
            return {
                "type": _EVENT_IMPULSE,
                "var": self._impulse.var.get_name(),
            }
        page = {
            _EVENT_STATIC: self._static,
            _EVENT_DYN: self._dyn,
            _EVENT_NUM: self._num,
            _EVENT_AUX: self._aux,
            _EVENT_FLAG: self._flag,
            _EVENT_FRAME: self._frame,
        }[etype]
        return page.read()

    def write(self, abort: dict[str, Any] | None) -> None:
        abort = abort or {}
        etype = abort.get("type", _EVENT_NONE)
        enabled = etype not in (None, "", _EVENT_NONE, _EVENT_POWER)
        self.enable.setChecked(enabled)
        if not enabled:
            return
        idx = self._type.findData(etype)
        self._type.setCurrentIndex(idx if idx >= 0 else 0)
        if etype == _EVENT_IMPULSE:
            self._impulse.var.set_name(str(abort.get("var", "")))
            return
        page = {
            _EVENT_STATIC: self._static,
            _EVENT_DYN: self._dyn,
            _EVENT_NUM: self._num,
            _EVENT_AUX: self._aux,
            _EVENT_FLAG: self._flag,
            _EVENT_FRAME: self._frame,
        }.get(etype)
        if page is not None:
            page.write(abort)

    def refresh_variables(self) -> None:
        tab = self._get_tab() if callable(self._get_tab) else None
        dyn = tab.variable_names("read", "dyn_cache") if tab else []
        num = tab.variable_names("read", "dynamic") if tab else []
        imp = tab.variable_names("read", "impulse") if tab else []
        st = tab.variable_names("read", "static") if tab else []
        flags = tab.variable_names("aux", "flag") if tab else []
        self._static.var.set_names(st, tr("— не выбрано —"))
        self._dyn.var.set_names(dyn, tr("— не выбрано —"))
        self._num.var.set_names(num, tr("— не выбрано —"))
        self._impulse.var.set_names(imp, tr("— не выбрано —"))
        self._flag.var.set_names(flags, tr("— не выбрано —"))
        self._dyn.refresh_binding_names()


def _detach_item(item, sep) -> None:
    """Скрывает и удаляет пункт события/условия/действия (и его
    разделитель) — выполняется на следующем тике после клика по
    крестику, см. _remove_event/_remove_cond/_remove_action.

    setParent(None) НЕ используем: он делает виджет топлевел-окном,
    создание/удаление нативного окна рушит Qt на Windows/macOS
    (отчёт мастера — краш по крестику). Само удаление отложено ещё
    на один тик после скрытия: отпускание мыши по кнопке-«крестику»
    внутри item завершается до DeferredDelete — иначе Qt на Windows
    доставлял release уже скрытому/полуудалённому виджету и
    приложение падало (повторный отчёт мастера)."""
    if isValid(item):
        # Анимация свёртки могла быть в полёте (фокус-аут при клике по
        # крестику): бегущая QPropertyAnimation по «maximumHeight»
        # удалённого редактора роняет Qt на Windows — глушим её и
        # отложенные обработчики до deleteLater (повторный отчёт).
        anim = getattr(item, "_editor_anim", None)
        if anim is not None:
            old_cb = getattr(item, "_anim_finished_cb", None)
            if old_cb is not None:
                with contextlib.suppress(RuntimeError, TypeError):
                    anim.finished.disconnect(old_cb)
                item._anim_finished_cb = None
            with contextlib.suppress(RuntimeError):
                anim.stop()
        # Отписываем авто-свёртку СРАЗУ: между отложенным detach и
        # deleteLater обработчик focusChanged ещё жив и мог бы
        # перезапустить анимацию на полуудалённом пункте
        # (повторный отчёт мастера — вылет по крестику).
        app = QApplication.instance()
        conn = getattr(item, "_focus_conn", None)
        if app is not None and conn is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                app.focusChanged.disconnect(conn)
        item._focus_conn = None
        # Открытый выпадающий список комбобокса внутри пункта закрываем
        # до скрытия родителя — иначе попап остаётся сиротой поверх
        # окна, а его отложенное закрытие трогает мёртвые виджеты.
        with contextlib.suppress(RuntimeError):
            for combo in item.findChildren(QComboBox):
                combo.hidePopup()
        item.setEnabled(False)
        parent = item.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().removeWidget(item)
        item.setVisible(False)
        QTimer.singleShot(0, item.deleteLater)
    if sep is not None and isValid(sep):
        sep.setVisible(False)
        QTimer.singleShot(0, sep.deleteLater)


def _auto_collapse(item) -> None:
    """Сворачивает редактор пункта до одной строки-сводки, когда
    фокус уходит за пределы пункта — после настройки события/
    условия/действия в программе остаётся одна строка
    (отчёт мастера)."""
    app = QApplication.instance()
    if app is None:
        return

    holder = {"conn": None}

    def _changed(_old, new) -> None:
        try:
            if not isValid(item):
                # Пункт удалён — отписываемся, иначе замыкания копятся
                # на app.focusChanged навсегда (утечка обработчиков).
                conn = holder["conn"]
                if conn is not None:
                    with contextlib.suppress(RuntimeError, TypeError):
                        app.focusChanged.disconnect(conn)
                    holder["conn"] = None
                    item._focus_conn = None
                return
            if item._editor.maximumHeight() == 0:
                return  # уже свёрнут
            # Выпадающий список комбобокса внутри пункта — это всё ещё
            # редактирование: попап — отдельное окно, focusChanged
            # при его открытии даёт «фокус ушёл» (отчёт мастера:
            # дерево сворачивалось на промежуточных шагах настройки).
            popup = QApplication.activePopupWidget()
            if popup is not None and isValid(popup):
                owner = popup.parentWidget() or popup
                if isValid(owner) and (
                    item.isAncestorOf(owner) or item.isAncestorOf(popup)
                ):
                    return
            if new is not None and isValid(new) and item.isAncestorOf(new):
                return  # фокус внутри пункта — редактирование идёт
            # В одну строку сворачивается только ПОЛНОСТЬЮ настроенный
            # пункт: пока обязательные поля пустые, дерево настройки
            # остаётся развёрнутым (отчёт мастера).
            complete = getattr(item, "is_complete", None)
            if callable(complete) and not complete():
                return
            _animate_editor_toggle(item)
        except RuntimeError:
            # «new» мог быть уже разрушен к моменту доставки сигнала —
            # не роняем приложение на гонке удаления (отчёт мастера).
            pass

    holder["conn"] = app.focusChanged.connect(_changed)
    item._focus_conn = holder["conn"]


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
              _EVENT_IMPULSE, _EVENT_AUX, _EVENT_FLAG, _EVENT_FRAME,
              _EVENT_POWER)

    def __init__(self, row: RuleRowWidget, font: QFont, event: dict | None = None) -> None:
        super().__init__(row)
        self._row = row
        self.setStyleSheet(_ITEM_FRAME_STYLE.format(cls="_EventItem"))
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(6, 2, 6, 4)

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
        editor_layout.setSpacing(2)
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
        self._type.addItem(tr("Переменная"), _EVENT_FLAG)
        self._type.addItem(tr("Фрейм"), _EVENT_FRAME)
        self._type.addItem(tr("Включение устройства"), _EVENT_POWER)
        # «Кэш переменная» и «Программа начала работать» —
        # новые виды событий (отчёт мастера).
        self._type.addItem(tr("Кэш переменная"), _EVENT_CACHEVAR)
        self._type.addItem(
            tr("Программа начала работать"), _EVENT_PROGRAM
        )
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
        self._flag = _FlagEventPage(font, row._mark_dirty)
        self._frame = _FrameEventPage(font, row._mark_dirty)
        self._power = _PowerEventPage(font, row._mark_dirty)
        self._cachevar = _CacheVarEventPage(font, row._mark_dirty)
        self._program = _ProgramEventPage(font, row._mark_dirty)
        for page in (
            self._none, self._static, self._dyn, self._num,
            self._impulse, self._aux, self._flag, self._frame,
            self._power, self._cachevar, self._program,
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
            self._static, self._aux, self._flag, self._frame,
            self._cachevar, self._program,
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
        # После настройки пункт сворачивается в одну строку, когда
        # фокус уходит за его пределы (отчёт мастера).
        _auto_collapse(self)

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке: развернуть/свернуть редактор
        с анимацией высоты (отчёт мастера)."""
        _animate_editor_toggle(self)

    def is_complete(self) -> bool:
        """Событие настроено целиком — только тогда редактору можно
        сворачиваться в одну строку по уходу фокуса (отчёт мастера:
        дерево схлопывалось на промежуточных шагах настройки)."""
        etype = self._type.currentData()
        if etype == _EVENT_NONE:
            return False
        if etype == _EVENT_IMPULSE:
            return bool(self._impulse.var.get_name())
        if etype == _EVENT_DYN:
            return bool(
                self._dyn.var.get_name()
                and self._dyn.value_combo
                and self._dyn.value_combo.currentText().strip()
            )
        if etype == _EVENT_NUM:
            return bool(
                self._num.var.get_name()
                and self._num.value
                and self._num.value.text().strip()
            )
        if etype == _EVENT_STATIC:
            return bool(self._static.var.get_name())
        if etype == _EVENT_FLAG:
            return bool(self._flag.var.get_name())
        if etype == _EVENT_FRAME:
            return bool(self._frame.rtr.isChecked()) or (
                hex_to_int(self._frame.can_id.text()) is not None
            )
        if etype == _EVENT_CACHEVAR:
            return bool(self._cachevar.var.get_name())
        if etype == _EVENT_PROGRAM:
            return bool(self._program.var.get_name())
        # «Включение устройства» и «Доп канал» — полей выбора нет или
        # у всех значения по умолчанию.
        return True

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
            _EVENT_FLAG: self._flag,
            _EVENT_FRAME: self._frame,
            _EVENT_POWER: self._power,
            _EVENT_CACHEVAR: self._cachevar,
            _EVENT_PROGRAM: self._program,
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
            _EVENT_FLAG: self._flag,
            _EVENT_FRAME: self._frame,
            _EVENT_POWER: self._power,
            _EVENT_CACHEVAR: self._cachevar,
            _EVENT_PROGRAM: self._program,
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
        # «Переменная» — именованные биты из третьей колонки
        # «Переменных» (вид «Переменная» — отчёт мастера).
        flags = tab.variable_names("aux", "flag") if tab else []
        # «Кэш переменная» — имена переменных вида «Кэш переменная»
        # из колонки «Чтение» (отчёт мастера).
        cache = tab.variable_names("read", "cache") if tab else []
        self._dyn.var.set_names(dyn, tr("— не выбрано —"))
        self._num.var.set_names(num, tr("— не выбрано —"))
        self._impulse.var.set_names(imp, tr("— не выбрано —"))
        self._static.var.set_names(st, tr("— не выбрано —"))
        self._flag.var.set_names(flags, tr("— не выбрано —"))
        self._cachevar.var.set_names(cache, tr("— не выбрано —"))
        # «Программа начала работать» — имена всех программ вкладки,
        # кроме содержащей это событие (сама себя слушать не может).
        programs = [
            n for n in self._row._tab._program_names()
            if n and n != self._row._name_edit.text().strip()
        ]
        self._program.var.set_names(programs, tr("— не выбрано —"))
        # Список состояний «Динамической переменной» подтягивается
        # из её таблицы привязки (отчёт мастера).
        self._dyn.refresh_binding_names()

    def _update_summary(self, *_args) -> None:
        """Компактная подпись события: имя + функция (отчёт мастера)."""
        etype = self._type.currentData()
        if etype == _EVENT_NONE:
            text = tr("Не выбрано")
        elif etype == _EVENT_DYN:
            # «Динамическая переменная»: «Цвет — Красный» — имя +
            # состояние из таблицы привязки (отчёт мастера).
            name = self._dyn.var.get_name() or "—"
            state = self._dyn.value_combo.currentText().strip()
            text = f"{name} — {state}".rstrip()
        elif etype == _EVENT_NUM:
            # «Численная переменная»: «Обороты стали больше 1500»
            # (отчёт мастера).
            name = self._num.var.get_name() or "—"
            func = (
                tr("стали больше")
                if self._num.direction.currentData() == "gt"
                else tr("стали меньше")
            )
            text = f"{name} {func} {self._num.value.text().strip()}".rstrip()
        elif etype == _EVENT_IMPULSE:
            # «Импульсная переменная»: событие — «получено»
            # (отчёт мастера).
            text = (
                f"{self._impulse.var.get_name() or '—'} "
                f"{tr('получено')}"
            )
        elif etype == _EVENT_STATIC:
            # «Статическая переменная»: «Тормоз 1» / «Тормоз 0»
            # (отчёт мастера).
            name = self._static.var.get_name() or "—"
            func = {
                "on": "1",
                "off": "0",
                "both": tr("1 или 0"),
            }.get(self._static.edge.currentData(), "")
            text = f"{name} {func}".rstrip()
        elif etype == _EVENT_FLAG:
            # «Переменная»: имя бита + фронт «Стала 1»/«Стала 0»
            # (отчёт мастера).
            name = self._flag.var.get_name() or "—"
            func = (
                tr("Стала 1") if self._flag.edge.currentData() == 1
                else tr("Стала 0")
            )
            text = f"{name}: {func}"
        elif etype == _EVENT_FRAME:
            can_id = self._frame.can_id.text().strip()
            if self._frame.rtr.isChecked():
                text = f"ID {can_id} RTR" if can_id else "RTR"
            else:
                text = f"ID {can_id}" if can_id else tr("Фрейм")
        elif etype == _EVENT_POWER:
            text = tr("Включение устройства")
        elif etype == _EVENT_CACHEVAR:
            # «Кэш переменная»: «Приход DATA КЭШ (имя) CAN1» /
            # «Записалась КЭШ (имя)» / «Стирание КЭШ (имя)»
            # (отчёт мастера).
            name = self._cachevar.var.get_name() or "—"
            op = self._cachevar.op.currentData()
            if op == _CACHE_OP_RX:
                text = tr("Приход DATA КЭШ {0} {1}").format(
                    name, self._cachevar.channel.currentText()
                )
            elif op == _CACHE_OP_WRITTEN:
                text = tr("Записалась КЭШ {0}").format(name)
            else:
                text = tr("Стирание КЭШ {0}").format(name)
        elif etype == _EVENT_PROGRAM:
            # «Программа (имя) начала работать» (отчёт мастера).
            text = tr("Программа «{0}» начала работать").format(
                self._program.var.get_name() or "—"
            )
        else:
            direction = (
                tr("вход") if self._aux.direction.currentData() == "in"
                else tr("выход")
            )
            state = (
                tr("активен") if self._aux.state.currentData() == 1
                else tr("не активен")
            )
            text = tr("Доп канал №{0} {1} {2}").format(
                self._aux.channel.value(), direction, state
            )
        self._summary.set_unselected(etype == _EVENT_NONE)
        self._summary.setText(text)


class _CondItem(QWidget):
    """Одно условие программы: компактная строка + тип + настройки.
    Условий может быть несколько — все должны выполняться (И)."""

    _PAGES = (_COND_NONE, _COND_STATIC, _COND_DYN, _COND_NUM,
              _COND_IMPULSE, _COND_AUX, _COND_FLAG)

    def __init__(self, row: RuleRowWidget, font: QFont, cond: dict | None = None) -> None:
        super().__init__(row)
        self._row = row
        self.setStyleSheet(_ITEM_FRAME_STYLE.format(cls="_CondItem"))
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(6, 2, 6, 4)

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
        editor_layout.setSpacing(2)
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
        self._type.addItem(tr("Переменная"), _COND_FLAG)
        # «Кэш переменная» — «Записан/Не записан КЭШ» по буферу 2
        # (отчёт мастера).
        self._type.addItem(tr("Кэш переменная"), _COND_CACHEVAR)
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
        st_layout.setSpacing(2)
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

        # Условие «Динамическая переменная» — имя + состояние из
        # таблицы привязки переменной (отчёт мастера: состояния не
        # показывались — был только вариант «Активно»). У «Численной»
        # остаются «Больше/Меньше/Равно» + порог.
        dyn_page = _DynCondPage(
            font, row._mark_dirty,
            get_tab=lambda: row._tab._variables_tab,
        )
        num_page = _CondVarPage(font, row._mark_dirty)
        impulse_page = _ImpulseVarPage(font, row._mark_dirty)
        self.imp_var = impulse_page.var
        self.dyn_var = dyn_page.var
        self.dyn_state = dyn_page.state
        self._dyn_page = dyn_page
        self.num_var = num_page.var
        self.num_op = num_page.op
        self.num_value = num_page.value

        aux_page = QWidget()
        aux_layout = QVBoxLayout(aux_page)
        aux_layout.setSpacing(2)
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

        # Условие «Переменная» — именованный бит ОЗУ/ПЗУ: имя +
        # состояние 1/0 — перед действием опрашивается текущее
        # состояние переменной (отчёт мастера).
        flag_page = QWidget()
        flag_layout = QVBoxLayout(flag_page)
        flag_layout.setSpacing(2)
        flag_layout.setContentsMargins(0, 0, 0, 0)
        flag_layout.addWidget(_small_label(tr("Переменная:"), font))
        self.flag_var = _VarCombo(font)
        flag_layout.addWidget(self.flag_var)
        flag_layout.addWidget(_small_label(tr("Состояние:"), font))
        self.flag_state = QComboBox()
        self.flag_state.setFont(font)
        self.flag_state.addItem("1", 1)
        self.flag_state.addItem("0", 0)
        flag_layout.addWidget(self.flag_state)
        flag_layout.addStretch()

        cachevar_page = _CacheVarCondPage(font, row._mark_dirty)
        self.cv_var = cachevar_page.var
        self.cv_state = cachevar_page.state

        for page in (
            none_page, static_page, dyn_page, num_page,
            impulse_page, aux_page, flag_page, cachevar_page
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
        self.dyn_state.currentTextChanged.connect(self._update_summary)
        self.num_var.currentIndexChanged.connect(self._update_summary)
        self.num_op.currentIndexChanged.connect(self._update_summary)
        self.num_value.textChanged.connect(self._update_summary)
        self.imp_var.currentIndexChanged.connect(self._update_summary)
        self.aux_channel.valueChanged.connect(self._update_summary)
        self.aux_state.currentIndexChanged.connect(self._update_summary)
        self.flag_var.currentIndexChanged.connect(self._update_summary)
        self.flag_state.currentIndexChanged.connect(self._update_summary)
        self.cv_var.currentIndexChanged.connect(self._update_summary)
        self.cv_state.currentIndexChanged.connect(self._update_summary)
        self.flag_var.currentIndexChanged.connect(row._mark_dirty)
        self.flag_state.currentIndexChanged.connect(row._mark_dirty)
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
        _auto_collapse(self)

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке разворачивает/сворачивает редактор."""
        _animate_editor_toggle(self)

    def is_complete(self) -> bool:
        """Условие настроено целиком — только тогда редактору можно
        сворачиваться в одну строку (отчёт мастера)."""
        ctype = self._type.currentData()
        if ctype == _COND_NONE:
            return False
        if ctype == _COND_STATIC:
            return bool(self.st_var.get_name())
        if ctype == _COND_DYN:
            return bool(self.dyn_var.get_name())
        if ctype == _COND_NUM:
            return bool(
                self.num_var.get_name() and self.num_value.text().strip()
            )
        if ctype == _COND_IMPULSE:
            return bool(self.imp_var.get_name())
        if ctype == _COND_FLAG:
            return bool(self.flag_var.get_name())
        if ctype == _COND_CACHEVAR:
            return bool(self.cv_var.get_name())
        return True  # «Доп канал» — все поля имеют значения по умолчанию

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
            # Состояние из таблицы привязки: «имя == состояние».
            # Пустое состояние — легаси «Активно»: истинно, пока
            # переменная в любом именованном состоянии.
            return {
                "type": _COND_DYN,
                "var": self.dyn_var.get_name(),
                "state": self.dyn_state.currentText().strip(),
            }
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
        if ctype == _COND_FLAG:
            return {
                "type": _COND_FLAG,
                "var": self.flag_var.get_name(),
                "state": self.flag_state.currentData(),
            }
        if ctype == _COND_CACHEVAR:
            return {
                "type": _COND_CACHEVAR,
                "var": self.cv_var.get_name(),
                "cache_op": self.cv_state.currentData(),
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
            self._dyn_page.refresh_binding_names()
            saved_state = str(cond.get("state", ""))
            if saved_state:
                sidx = self.dyn_state.findText(saved_state)
                if sidx >= 0:
                    self.dyn_state.setCurrentIndex(sidx)
                else:
                    self.dyn_state.setEditText(saved_state)
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
        elif ctype == _COND_FLAG:
            self.flag_var.set_name(str(cond.get("var", "")))
            sidx = self.flag_state.findData(int(cond.get("state", 1) or 0))
            self.flag_state.setCurrentIndex(sidx if sidx >= 0 else 0)
        elif ctype == _COND_CACHEVAR:
            self.cv_var.set_name(str(cond.get("var", "")))
            sidx = self.cv_state.findData(
                cond.get("cache_op", _CACHE_OP_FILLED)
            )
            self.cv_state.setCurrentIndex(sidx if sidx >= 0 else 0)

    def refresh_variables(self) -> None:
        tab = self._row._tab._variables_tab
        # Отдельные пункты: «Динамическая» (МК-кэш байтов),
        # «Численная» (график интерполяции), «Статическая»
        # (отчёт мастера).
        dyn = tab.variable_names("read", "dyn_cache") if tab else []
        num = tab.variable_names("read", "dynamic") if tab else []
        imp = tab.variable_names("read", "impulse") if tab else []
        st = tab.variable_names("read", "static") if tab else []
        flags = tab.variable_names("aux", "flag") if tab else []
        cache = tab.variable_names("read", "cache") if tab else []
        self.st_var.set_names(st, tr("— не выбрано —"))
        self.dyn_var.set_names(dyn, tr("— не выбрано —"))
        self.num_var.set_names(num, tr("— не выбрано —"))
        self.imp_var.set_names(imp, tr("— не выбрано —"))
        self.flag_var.set_names(flags, tr("— не выбрано —"))
        self.cv_var.set_names(cache, tr("— не выбрано —"))
        # Состояния «Динамической переменной» — из её таблицы привязки
        # (отчёт мастера: список состояний в условии был пуст).
        self._dyn_page.refresh_binding_names()

    def _update_summary(self, *_args) -> None:
        ctype = self._type.currentData()
        if ctype == _COND_STATIC:
            # «Дверь открыта 1» — имя + состояние (отчёт мастера).
            text = f"{self.st_var.get_name() or '—'} {self.st_state.currentData()}"
        elif ctype == _COND_DYN:
            # «Коробка — Драйв» — имя + состояние из таблицы привязки;
            # без состояния — «Активно» (отчёт мастера).
            name = self.dyn_var.get_name() or "—"
            state = self.dyn_state.currentText().strip()
            text = f"{name} — {state}" if state else f"{name} {tr('Активно')}"
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
        elif ctype == _COND_FLAG:
            # «Свет салона 1» — имя переменной + состояние.
            text = (
                f"{self.flag_var.get_name() or '—'} "
                f"{self.flag_state.currentData()}"
            )
        elif ctype == _COND_CACHEVAR:
            # «Записан КЭШ имя» / «Не записан КЭШ имя» (отчёт мастера).
            text = tr("{0} {1}").format(
                self.cv_state.currentText(),
                self.cv_var.get_name() or "—",
            )
        else:
            text = tr("Не выбрано")
        self._summary.set_unselected(ctype == _COND_NONE)
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

    _PAGES = (_ACT_NONE, _ACT_AUX, _ACT_VAR, _ACT_FLAG, _ACT_FRAME,
              _ACT_CACHE, _ACT_CACHEVAR)

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
        layout.setSpacing(2)
        layout.setContentsMargins(6, 2, 6, 4)

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
        editor_layout.setSpacing(2)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        # «Задержка» выполнения 0–999999 мс — ВЫШЕ выбора основного
        # действия (отчёт мастера: «паузу перед выполнением поставь
        # выше выбора основного действия»).
        delay_row = QHBoxLayout()
        delay_row.setContentsMargins(0, 0, 0, 0)
        delay_row.addWidget(_small_label(tr("Задержка"), font))
        self.exec_delay = QSpinBox()
        self.exec_delay.setFont(font)
        self.exec_delay.setRange(0, 999999)
        self.exec_delay.setSuffix(tr(" мс"))
        self.exec_delay.setFixedWidth(110)
        self.exec_delay.setToolTip(
            tr("Выполнить действие через указанную паузу")
        )
        delay_row.addWidget(self.exec_delay)
        delay_row.addStretch()
        editor_layout.addLayout(delay_row)

        self._type = QComboBox()
        self._type.setFont(font)
        # «Не выбрано» — позиция нового действия (отчёт мастера).
        self._type.addItem(tr("Не выбрано"), _ACT_NONE)
        self._type.addItem(tr("Доп канал"), _ACT_AUX)
        self._type.addItem(tr("Переменные управления"), _ACT_VAR)
        self._type.addItem(tr("Переменные"), _ACT_FLAG)
        self._type.addItem(tr("Отправить фрейм"), _ACT_FRAME)
        self._type.addItem(tr("Запись DATA в кэш"), _ACT_CACHE)
        # «Кэш переменная» — Записать/Отправить/Стереть буфер 2
        # (отчёт мастера).
        self._type.addItem(tr("Кэш переменная"), _ACT_CACHEVAR)
        self._type.currentIndexChanged.connect(self._on_type)
        editor_layout.addWidget(self._type)

        self._stack = QStackedWidget()
        none_label = _small_label(tr("— не выбрано —"), font)
        self._none = QWidget()
        QVBoxLayout(self._none).addWidget(none_label)
        self._stack.addWidget(self._none)
        self._build_aux_page(font, row._mark_dirty)
        self._build_var_page(font, row._mark_dirty)
        self._build_flag_page(font, row._mark_dirty)
        self._build_frame_page(font, row._mark_dirty)
        self._build_cache_page(font, row._mark_dirty)
        self._build_cachevar_page(font, row._mark_dirty)
        editor_layout.addWidget(self._stack)
        self._abort = _AbortEventEditor(
            font, row._mark_dirty,
            get_tab=lambda: row._tab._variables_tab,
        )
        editor_layout.addWidget(self._abort)
        self.exec_delay.valueChanged.connect(row._mark_dirty)
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
        _auto_collapse(self)

    # ---- страницы настроек -------------------------------------------

    def _build_aux_page(self, font: QFont, mark_dirty) -> None:
        """«Доп канал»: №, режим Вкл/Выкл/Импульсы/ШИМ, пауза до
        действия и под-страница параметров (отчёт мастера)."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(2)
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
        pulse_layout.setSpacing(2)
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
        pwm_layout.setSpacing(2)
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
        # «время включения» с галочкой (отчёт мастера): без галочки
        # ШИМ работает до команды «Выкл»; с галочкой — указанное
        # время 1–999999 мс и выключается сам.
        self.aux_pwm_time_en = QCheckBox(tr("время включения"))
        self.aux_pwm_time_en.setFont(font)
        self.aux_pwm_time_en.setToolTip(
            tr("Снята — ШИМ работает до команды «Выкл»; "
               "установлена — выключается через указанное время")
        )
        pwm_row.addWidget(self.aux_pwm_time_en)
        self.aux_pwm_time = QSpinBox()
        self.aux_pwm_time.setFont(font)
        self.aux_pwm_time.setRange(1, 999999)
        self.aux_pwm_time.setValue(100)
        self.aux_pwm_time.setSuffix(tr(" мс"))
        self.aux_pwm_time.setFixedWidth(96)
        self.aux_pwm_time.setEnabled(False)
        pwm_row.addWidget(self.aux_pwm_time)
        pwm_row.addStretch()
        pwm_layout.addLayout(pwm_row)
        self.aux_pwm_time_en.toggled.connect(self.aux_pwm_time.setEnabled)

        for sub in (plain, pulse, pwm):
            self._aux_stack.addWidget(sub)
        pl.addWidget(self._aux_stack)
        self._stack.addWidget(page)

        self.aux_channel.valueChanged.connect(mark_dirty)
        self.aux_mode.currentIndexChanged.connect(self._on_aux_mode)
        self.aux_mode.currentIndexChanged.connect(mark_dirty)
        self.aux_delay.valueChanged.connect(mark_dirty)
        self.aux_pwm_time_en.toggled.connect(mark_dirty)
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
        pl.setSpacing(2)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Команда:"), font))
        self.var = _VarCombo(font)
        row1.addWidget(self.var, 1)
        pl.addLayout(row1)
        pl.addStretch()
        self._stack.addWidget(page)

        self.var.currentIndexChanged.connect(mark_dirty)
        self.var.currentIndexChanged.connect(self._update_summary)

    def _build_flag_page(self, font: QFont, mark_dirty) -> None:
        """«Переменные»: выбор именованного бита (вид «Переменная»
        третьей колонки «Переменных») и операции «Включить» /
        «Включить на …мс» / «Выключить» (отчёт мастера)."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(2)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Переменная:"), font))
        self.flag_var = _VarCombo(font)
        row1.addWidget(self.flag_var, 1)
        pl.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(_small_label(tr("Действие:"), font))
        self.flag_mode = QComboBox()
        self.flag_mode.setFont(font)
        self.flag_mode.addItem(tr("Включить"), "on")
        self.flag_mode.addItem(tr("Включить на"), "pulse")
        self.flag_mode.addItem(tr("Выключить"), "off")
        row2.addWidget(self.flag_mode)
        self.flag_time = QSpinBox()
        self.flag_time.setFont(font)
        self.flag_time.setRange(1, 999999)
        self.flag_time.setValue(100)
        self.flag_time.setSuffix(tr(" мс"))
        self.flag_time.setFixedWidth(110)
        self.flag_time.setVisible(False)
        row2.addWidget(self.flag_time)
        row2.addStretch()
        pl.addLayout(row2)
        pl.addStretch()
        self._stack.addWidget(page)

        self.flag_var.currentIndexChanged.connect(mark_dirty)
        self.flag_var.currentIndexChanged.connect(self._update_summary)
        self.flag_mode.currentIndexChanged.connect(mark_dirty)
        self.flag_mode.currentIndexChanged.connect(
            lambda _i: self.flag_time.setVisible(
                self.flag_mode.currentData() == "pulse"
            )
        )
        self.flag_mode.currentIndexChanged.connect(self._update_summary)
        self.flag_time.valueChanged.connect(mark_dirty)

    def _build_frame_page(self, font: QFont, mark_dirty) -> None:
        """«Отправить фрейм»: канал/бит/ID/DLC/DATA (X — из кадра
        события) + пауза/кол-во/между — ручная рассылка оператором."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(2)
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
        _bind_id_width(self.fr_bit, self.fr_id)
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
            font, 8, edit_width=40, allow_x=True
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
        pl.setSpacing(2)
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
        _bind_id_width(self.cache_bit, self.cache_id)
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
            font, 8, edit_width=40, allow_x=True
        )
        pl.addWidget(from_widget)
        pl.addWidget(_small_label(tr("DATA до:"), font))
        self.cache_to, to_widget = create_data_field_widget(
            font, 8, edit_width=40, allow_x=True
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

    def _build_cachevar_page(self, font: QFont, mark_dirty) -> None:
        """«Кэш переменная» (отчёт мастера): «Записать КЭШ» — кадр из
        скрытого буфера 1 переносится в буфер 2, буфер 1 обнуляется;
        «Отправить КЭШ» — кадр буфера 2 уходит в CAN1/CAN2 заданное
        число раз с паузой; «Стереть КЭШ» — буфер 2 обнуляется."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setSpacing(2)
        pl.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row1.addWidget(_small_label(tr("Переменная:"), font))
        self.cv_var = _VarCombo(font)
        row1.addWidget(self.cv_var, 1)
        pl.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(_small_label(tr("Действие:"), font))
        self.cv_op = QComboBox()
        self.cv_op.setFont(font)
        self.cv_op.addItem(tr("Записать КЭШ"), _CACHE_OP_COMMIT)
        self.cv_op.addItem(tr("Отправить КЭШ"), _CACHE_OP_SEND)
        self.cv_op.addItem(tr("Стереть КЭШ"), _CACHE_OP_ERASE)
        row2.addWidget(self.cv_op)
        row2.addStretch()
        pl.addLayout(row2)
        # Параметры отправки — только у «Отправить КЭШ»
        # (отчёт мастера: куда, количество, пауза между).
        self.cv_send_row = QWidget()
        send_row = QHBoxLayout(self.cv_send_row)
        send_row.setContentsMargins(0, 0, 0, 0)
        send_row.addWidget(_small_label(tr("Канал"), font))
        self.cv_channel = QComboBox()
        self.cv_channel.setFont(font)
        self.cv_channel.addItems(["CAN1", "CAN2"])
        send_row.addWidget(self.cv_channel)
        send_row.addWidget(_small_label(tr("Кол-во"), font))
        self.cv_count = QSpinBox()
        self.cv_count.setFont(font)
        self.cv_count.setRange(1, 100)
        self.cv_count.setValue(1)
        self.cv_count.setFixedWidth(60)
        send_row.addWidget(self.cv_count)
        send_row.addWidget(_small_label(tr("Пауза"), font))
        self.cv_pause = QSpinBox()
        self.cv_pause.setFont(font)
        self.cv_pause.setRange(0, 9999)
        self.cv_pause.setSuffix(tr(" мс"))
        self.cv_pause.setFixedWidth(86)
        send_row.addWidget(self.cv_pause)
        send_row.addStretch()
        pl.addWidget(self.cv_send_row)
        pl.addStretch()
        self._stack.addWidget(page)

        self.cv_var.currentIndexChanged.connect(mark_dirty)
        self.cv_var.currentIndexChanged.connect(self._update_summary)
        self.cv_op.currentIndexChanged.connect(mark_dirty)
        self.cv_op.currentIndexChanged.connect(self._update_summary)
        self.cv_op.currentIndexChanged.connect(
            lambda _i: self.cv_send_row.setVisible(
                self.cv_op.currentData() == _CACHE_OP_SEND
            )
        )
        self.cv_channel.currentIndexChanged.connect(mark_dirty)
        self.cv_count.valueChanged.connect(mark_dirty)
        self.cv_pause.valueChanged.connect(mark_dirty)
        self.cv_send_row.setVisible(False)

    # ---- обработчики ---------------------------------------------------

    def _toggle_editor(self) -> None:
        """Клик по строке-сводке разворачивает/сворачивает редактор."""
        _animate_editor_toggle(self)

    def is_complete(self) -> bool:
        """Действие настроено целиком — только тогда редактору можно
        сворачиваться в одну строку (отчёт мастера)."""
        atype = self._type.currentData()
        if atype == _ACT_NONE:
            return False
        if atype == _ACT_VAR:
            return bool(self.var.get_name())
        if atype == _ACT_FLAG:
            return bool(self.flag_var.get_name())
        if atype == _ACT_FRAME:
            return hex_to_int(self.fr_id.text()) is not None
        if atype == _ACT_CACHE:
            return hex_to_int(self.cache_id.text()) is not None
        if atype == _ACT_CACHEVAR:
            return bool(self.cv_var.get_name())
        return True  # «Доп канал» — все поля имеют значения по умолчанию

    def _on_type(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._update_abort_visibility()
        self._update_summary()
        self._row._mark_dirty()

    def _on_aux_mode(self, *_args) -> None:
        mode = self.aux_mode.currentData()
        self._aux_stack.setCurrentIndex(
            {"on": 0, "off": 0, "pulse": 1, "pwm": 2}.get(mode, 0)
        )
        self._update_abort_visibility()
        self._update_summary()

    def _update_abort_visibility(self) -> None:
        """У «Выкл» доп канала настройки «Прервать если» не нужны —
        скрываются (отчёт мастера)."""
        abort = getattr(self, "_abort", None)
        if abort is None:
            return
        hidden = (
            self._type.currentData() == _ACT_AUX
            and self.aux_mode.currentData() == "off"
        )
        abort.setVisible(not hidden)

    def _refresh_pulse_graph(self, *_args) -> None:
        self.aux_pulse_graph.set_params(
            self.aux_pulse_on.value(),
            self.aux_pulse_off.value(),
            self.aux_pulse_count.value(),
        )

    def refresh_variables(self) -> None:
        tab = self._row._tab._variables_tab
        # «Переменные управления» — команды из раздела «Управление»;
        # «Переменные» — именованные биты третьей колонки; «Прервать
        # если» — те же списки событий (отчёт мастера).
        ctrl = tab.variable_names("control") if tab else []
        self.var.set_names(ctrl, tr("— не выбрано —"))
        flags = tab.variable_names("aux", "flag") if tab else []
        self.flag_var.set_names(flags, tr("— не выбрано —"))
        # «Кэш переменная» — имена кэш-переменных из «Чтения»
        # (отчёт мастера).
        cache = tab.variable_names("read", "cache") if tab else []
        self.cv_var.set_names(cache, tr("— не выбрано —"))
        self._abort.refresh_variables()

    # ---- схема -----------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """Плоский словарь с теми же ключами, что у старого единого
        блока «Действие» + маркер «type» — совместимо с исполнением."""
        atype = self._type.currentData()
        result: dict[str, Any]
        if atype == _ACT_VAR:
            result = {
                "type": _ACT_VAR,
                "var": self.var.get_name(),
            }
        elif atype == _ACT_FLAG:
            result = {
                "type": _ACT_FLAG,
                "flag_var": self.flag_var.get_name(),
                "flag_mode": self.flag_mode.currentData(),
                "flag_time": self.flag_time.value(),
            }
        elif atype == _ACT_FRAME:
            result = {
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
        elif atype == _ACT_AUX:
            result = {
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
                # Без галочки «время включения» — 0: ШИМ до «Выкл»
                # (отчёт мастера).
                "aux_pwm_time": (
                    self.aux_pwm_time.value()
                    if self.aux_pwm_time_en.isChecked() else 0
                ),
            }
        elif atype == _ACT_CACHE:
            result = {
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
        elif atype == _ACT_CACHEVAR:
            # «Кэш переменная»: Записать (буфер 1 → буфер 2) /
            # Отправить (буфер 2 → CAN) / Стереть (буфер 2 = 0)
            # (отчёт мастера). Ключи с префиксом cv_ — чтобы не
            # пересекаться с действием «Запись DATA в кэш».
            result = {
                "type": _ACT_CACHEVAR,
                "cachevar": self.cv_var.get_name(),
                "cv_op": self.cv_op.currentData(),
                "cv_channel": self.cv_channel.currentIndex(),
                "cv_count": self.cv_count.value(),
                "cv_pause": self.cv_pause.value(),
            }
        else:
            result = {"type": _ACT_NONE}
        # Общие параметры любого действия (отчёт мастера):
        # «Задержка» выполнения и событие-прерыватель «Прервать если».
        if self.exec_delay.value() > 0:
            result["exec_delay"] = self.exec_delay.value()
        # У «Выкл» доп канала прерывание не применяется — скрыто
        # и в схему не пишется (отчёт мастера).
        aux_off = (
            atype == _ACT_AUX
            and self.aux_mode.currentData() == "off"
        )
        if not aux_off:
            abort = self._abort.read()
            if abort.get("type") not in (None, "", _EVENT_NONE):
                result["abort"] = abort
        return result

    def write(self, action: dict[str, Any]) -> None:
        """action — словарь одного действия (с ключом «type») либо
        часть старого объединённого блока; тип угадывается по ключам."""
        atype = action.get("type")
        if atype not in self._PAGES:
            if action.get("cv_op"):
                atype = _ACT_CACHEVAR
            elif action.get("aux_enabled"):
                atype = _ACT_AUX
            elif action.get("frame_enabled"):
                atype = _ACT_FRAME
            elif action.get("cache_enabled"):
                atype = _ACT_CACHE
            elif action.get("flag_var"):
                atype = _ACT_FLAG
            elif action.get("var"):
                atype = _ACT_VAR
            else:
                atype = _ACT_NONE
        idx = self._type.findData(atype)
        self._type.setCurrentIndex(idx if idx >= 0 else 0)
        self._stack.setCurrentIndex(self._type.currentIndex())
        if atype == _ACT_VAR:
            self.var.set_name(str(action.get("var", "")))
        elif atype == _ACT_FLAG:
            self.flag_var.set_name(str(action.get("flag_var", "")))
            midx = self.flag_mode.findData(action.get("flag_mode", "on"))
            self.flag_mode.setCurrentIndex(midx if midx >= 0 else 0)
            self.flag_time.setValue(int(action.get("flag_time", 100) or 100))
            self.flag_time.setVisible(
                self.flag_mode.currentData() == "pulse"
            )
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
            pwm_time = int(action.get("aux_pwm_time", 0) or 0)
            self.aux_pwm_time_en.setChecked(pwm_time > 0)
            self.aux_pwm_time.setValue(max(1, pwm_time))
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
        elif atype == _ACT_CACHEVAR:
            self.cv_var.set_name(str(action.get("cachevar", "")))
            oidx = self.cv_op.findData(
                action.get("cv_op", _CACHE_OP_COMMIT)
            )
            self.cv_op.setCurrentIndex(oidx if oidx >= 0 else 0)
            self.cv_channel.setCurrentIndex(
                int(action.get("cv_channel", 0) or 0)
            )
            self.cv_count.setValue(
                max(1, int(action.get("cv_count", 1) or 1))
            )
            self.cv_pause.setValue(int(action.get("cv_pause", 0) or 0))
            self.cv_send_row.setVisible(
                self.cv_op.currentData() == _CACHE_OP_SEND
            )
        # Общие параметры действия (отчёт мастера).
        self.exec_delay.setValue(int(action.get("exec_delay", 0) or 0))
        self._abort.write(action.get("abort"))
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
        elif atype == _ACT_FLAG:
            name = self.flag_var.get_name() or "—"
            mode = self.flag_mode.currentData()
            if mode == "off":
                text = f"{name}: {tr('выключить')}"
            elif mode == "pulse":
                text = tr("{0}: включить на {1} мс").format(
                    name, self.flag_time.value()
                )
            else:
                text = f"{name}: {tr('включить')}"
        elif atype == _ACT_FRAME:
            text = tr("Фрейм")
        elif atype == _ACT_CACHE:
            text = tr("Кэш")
        elif atype == _ACT_CACHEVAR:
            # «Записать КЭШ имя» / «Отправить КЭШ имя» /
            # «Стереть КЭШ имя» (отчёт мастера).
            text = tr("{0} {1}").format(
                self.cv_op.currentText(),
                self.cv_var.get_name() or "—",
            )
        else:
            text = tr("Не выбрано")
        self._summary.set_unselected(atype == _ACT_NONE)
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
        self._focus_conn = None  # отписка — в _detach_item
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
        # ПОД списком событий (отчёт мастера).
        body_layout.addWidget(_connector(tr("ЕСЛИ")), 0, Qt.AlignmentFlag.AlignTop)
        ev_layout = QVBoxLayout(self._event_group)
        ev_layout.setSpacing(3)
        ev_layout.setContentsMargins(8, 4, 8, 4)
        self._events_layout = QVBoxLayout()
        self._events_layout.setSpacing(1)
        ev_layout.addLayout(self._events_layout, 1)
        ev_layout.addWidget(self._add_event_button)
        # Колонки прижаты к верху программы: короткая колонка не
        # растягивает рамку в высоту самой длинной (отчёт мастера —
        # «окно программы минимальное по высоте»).
        body_layout.addWidget(self._event_group, 1, Qt.AlignmentFlag.AlignTop)

        # ПРИ — Условия (И между условиями — выполняются все);
        # кнопка добавления под списком (отчёт мастера).
        body_layout.addWidget(_connector(tr("ПРИ")), 0, Qt.AlignmentFlag.AlignTop)
        cd_layout = QVBoxLayout(self._cond_group)
        cd_layout.setSpacing(3)
        cd_layout.setContentsMargins(8, 4, 8, 4)
        self._conds_layout = QVBoxLayout()
        self._conds_layout.setSpacing(1)
        cd_layout.addLayout(self._conds_layout, 1)
        cd_layout.addWidget(self._add_cond_button)
        body_layout.addWidget(self._cond_group, 1, Qt.AlignmentFlag.AlignTop)

        # ТО — Действия (выполняются все по порядку); кнопка
        # добавления под списком (отчёт мастера).
        body_layout.addWidget(_connector(tr("ТО")), 0, Qt.AlignmentFlag.AlignTop)
        act_layout = QVBoxLayout(self._action_group)
        act_layout.setSpacing(3)
        act_layout.setContentsMargins(8, 4, 8, 4)
        self._actions_layout = QVBoxLayout()
        self._actions_layout.setSpacing(1)
        act_layout.addLayout(self._actions_layout, 1)
        act_layout.addWidget(self._add_action_button)
        body_layout.addWidget(self._action_group, 1, Qt.AlignmentFlag.AlignTop)
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
        # Снимок РЕАЛЬНО исполняемых программ. Обновляется только
        # после успешной записи FLXH в МК (commit_runtime), вычитки
        # из устройства и загрузки конфига при старте — живые правки
        # полей сюда не попадают, программа отрабатывает по
        # записанной в камень версии (отчёт мастера).
        self._runtime_rules: list[dict[str, Any]] = []
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
        # «Переменные» — именованные биты ОЗУ/ПЗУ (вид «Переменная»
        # третьей колонки): имя → 0/1. _flag_edges — переходы текущего
        # тика для событий «Стала 1»/«Стала 0»; _flag_pulse_until —
        # время авто-гашения «включить на N мс» (отчёт мастера).
        self._flag_states: dict[str, int] = {}
        self._flag_edges: dict[str, int] = {}
        self._flag_pulse_until: dict[str, float] = {}
        # Отложенные действия с «Задержкой»/«Прервать если»: ждут
        # своего таймера, событие-прерыватель отменяет их.
        self._pending_actions: list[dict[str, Any]] = []
        self._abort_key_seq = 0
        # Кэш действий: индекс программы → последний подошедший кадр.
        self._fl_cache: dict[int, dict[str, Any]] = {}
        # «Автоматическая запись DATA в кэш» команд «Управления»:
        # имя команды → последний кадр, подошедший под её маску
        # (байты «X» во фреймах команды при отправке подставляются
        # из него — отчёт мастера).
        self._cmd_caches: dict[str, bytes] = {}
        # «Кэш переменная» (отчёт мастера): имя → два буфера.
        # buf1 — скрытый буфер ОЗУ: каждый кадр из диапазона ID
        # переменной перезаписывает его (оператор его не видит и не
        # настраивает). buf2 — операторский буфер (ОЗУ/ПЗУ — по
        # настройке переменной), заполняется действием «Записать
        # КЭШ», читается условиями «Записан/Не записан».
        # Буфер — {"id", "data", "extended", "channel"} или None.
        self._cache_bufs: dict[str, dict[str, Any]] = {}
        # Фронты кэш-событий текущего тика: (имя, канал) — приход
        # кадра в диапазон; (имя, "written"/"erased") — запись/стирание.
        self._cache_rx_edges: set[tuple[str, int]] = set()
        self._cache_edges: set[tuple[str, str]] = set()
        # Каналы, на которых события «Приход DATA КЭШ» ждут каждую
        # переменную — захват в буфер 1 ведётся только там (камень
        # «непрерывно анализирует» выбранные каналы — отчёт мастера).
        # Переменной без такого события — захват на любом канале.
        self._cache_watch: dict[str, set[int]] = {}
        # «Программа начала работать»: заголовок → номер тика, когда
        # программа прошла фазу событий; _program_event_seen —
        # (программа, событие, имя) → последний обработанный тик,
        # чтобы один старт не стрелял слушателю дважды.
        self._tick = 0
        self._program_started_seq: dict[str, int] = {}
        self._program_event_seen: dict[tuple[int, int, str], int] = {}
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
        self._restore_cache_bufs()
        self._refresh_variable_lists()

    def _program_names(self) -> list[str]:
        """Имена всех программ вкладки — список для события
        «Программа (имя) начала работать» (отчёт мастера)."""
        return [
            str(row._name_edit.text()).strip()
            for row in self._row_widgets
            if str(row._name_edit.text()).strip()
        ]

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
        # Исполняемый снимок: «flexible_rules_active» — то, что
        # реально записано/вычитано из МК; на старых конфигах ключа
        # нет — берём flexible_rules, как было до разделения
        # (отчёт мастера: программа отрабатывает только после
        # записи в камень).
        active = self._config.get("flexible_rules_active")
        self._runtime_rules = (
            list(active) if isinstance(active, list) else list(rules)
        )
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

    def set_config(
        self,
        rules: list[dict[str, Any]],
        *,
        suspend_execution: bool = False,
    ) -> None:
        """Загружает программы из импортированного профиля/вычитки МК.

        suspend_execution=True — файл только заполняет поля
        (импорт .kmc): исполняемый снимок не трогаем, программа
        начнёт работать после записи в МК. Вычитка с устройства и
        «Заводские настройки» вызывают без флага — там содержимое
        и есть то, что в камне."""
        self._config.set("flexible_rules", rules)
        if not suspend_execution:
            self._config.set("flexible_rules_active", list(rules))
        self._load_config()

    def commit_runtime(self) -> None:
        """Фиксирует снимок исполняемой программы — вызывается
        окном настроек только после успешной записи FLXH-блоба в МК.

        До этого момента живые правки полей меняют только
        «flexible_rules» (черновик), а исполняется последняя
        записанная в камень версия (отчёт мастера: ввод цифр в
        событии численной переменной не должен запускать программу
        до сохранения)."""
        self._save_config()
        self._runtime_rules = self._collect_rules()
        self._config.set("flexible_rules_active", self._runtime_rules)
        self._rules_dirty = True

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
        # Без setParent(None): репарент превращает строку в топлевел-окно
        # и рушит native-дерево виджетов прямо в обработчике clicked()
        # (отчёт мастера — вылет по крестику). Скрытие и удаление
        # отложены на следующий тик — release-событие кнопки-«крестика»
        # должно завершиться до DeferredDelete (повторный отчёт).
        widget.setEnabled(False)
        QTimer.singleShot(
            0, lambda w=widget: _detach_item(w, None)
        )
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

    def _capture_cache_vars(
        self, frame: dict[str, Any], frame_id: int, data: bytes
    ) -> None:
        """«Кэш переменная»: каждый кадр с ID в диапазоне «от–до»
        переменной перезаписывает её скрытый буфер 1 (всегда ОЗУ —
        оператор его не видит и не настраивает) — аналог аппаратного
        «камень непрерывно анализирует приходящие пакеты»
        (отчёт мастера). Захват ведётся на каналах, выбранных в
        событиях «Приход DATA КЭШ»; если таких событий нет — на
        любом канале."""
        channel = int(frame.get("channel", 1) or 1)
        extended = bool(frame.get("extended", frame_id > 0x7FF))
        for var in self._variable_defs():
            if var.get("type") != "cache":
                continue
            name = str(var.get("name", "")).strip()
            if not name:
                continue
            lo = hex_to_int(str(var.get("id_from", "")))
            hi = hex_to_int(str(var.get("id_to", "")))
            if lo is None:
                continue
            if hi is None:
                hi = lo
            if not (lo <= frame_id <= hi):
                continue
            if bool(var.get("extended")) != extended:
                continue
            watched = self._cache_watch.get(name)
            if watched is not None and channel not in watched:
                continue
            bufs = self._cache_bufs.setdefault(
                name, {"buf1": None, "buf2": None}
            )
            bufs["buf1"] = {
                "id": frame_id,
                "data": bytes(data),
                "extended": extended,
                "channel": channel,
            }
            # Фронт «Приход DATA КЭШ» по каналу + фронт «Записалась» —
            # запись в привязанный кэш произошла (отчёт мастера).
            self._cache_rx_edges.add((name, channel))
            self._cache_edges.add((name, _CACHE_OP_WRITTEN))

    def _cache_buf2_filled(self, name: str) -> bool:
        """Буфер 2 переменной содержит данные (не пуст и не нули) —
        условия «Записан/Не записан КЭШ» (отчёт мастера)."""
        buf = (self._cache_bufs.get(name) or {}).get("buf2")
        return bool(buf) and any(bytes(buf.get("data", b"")))

    def _run_cachevar_action(
        self, name: str, action: dict[str, Any]
    ) -> None:
        """Действия «Кэш переменная» (отчёт мастера): «Записать КЭШ»
        — кадр буфера 1 переносится в буфер 2, буфер 1 обнуляется;
        «Отправить КЭШ» — кадр буфера 2 уходит в CAN1/CAN2 N раз с
        паузой; «Стереть КЭШ» — буфер 2 обнуляется. Запись/стирание —
        фронты для событий «Записалась»/«Стирание»."""
        bufs = self._cache_bufs.setdefault(
            name, {"buf1": None, "buf2": None}
        )
        op = str(action.get("cv_op", _CACHE_OP_COMMIT))
        if op == _CACHE_OP_COMMIT:
            bufs["buf2"] = bufs["buf1"]
            bufs["buf1"] = None
            self._cache_edges.add((name, _CACHE_OP_WRITTEN))
            if bufs["buf2"] is None:
                # Записывать было нечего — буфер 2 фактически обнулился.
                self._cache_edges.add((name, _CACHE_OP_ERASED))
            self._persist_cache_bufs()
            self._fire_aux_events()
        elif op == _CACHE_OP_SEND:
            buf = bufs.get("buf2")
            if not buf:
                return
            count = max(1, int(action.get("cv_count", 1) or 1))
            pause = int(action.get("cv_pause", 0) or 0)
            channel = int(action.get("cv_channel", 0) or 0) + 1
            for n in range(count):
                self._send_frame(
                    channel, int(buf["id"]),
                    bytes(buf.get("data", b"")), n * pause,
                )
        elif op == _CACHE_OP_ERASE:
            bufs["buf2"] = None
            self._cache_edges.add((name, _CACHE_OP_ERASED))
            self._persist_cache_bufs()
            self._fire_aux_events()

    def _persist_cache_bufs(self) -> None:
        """Буфер 2 кэш-переменных с носителем «ПЗУ» сохраняется в
        общий конфиг — переживает перезапуск приложения и питание
        (отчёт мастера)."""
        if self._variables_tab is None:
            return
        saved: dict[str, dict[str, Any]] = {}
        for cfg in self._variables_tab.export_config().get("read") or []:
            if cfg.get("type") != "cache":
                continue
            name = str(cfg.get("name", "")).strip()
            if not name or cfg.get("storage") != "rom":
                continue
            buf = (self._cache_bufs.get(name) or {}).get("buf2")
            if buf:
                saved[name] = {
                    "id": int(buf["id"]),
                    "data": bytes(buf.get("data", b"")).hex(),
                    "extended": bool(buf.get("extended")),
                    "channel": int(buf.get("channel", 0) or 0),
                }
        self._config.set("cache_var_bufs", saved)

    def _restore_cache_bufs(self) -> None:
        """Поднимает сохранённые ПЗУ-буферы 2 кэш-переменных."""
        if self._variables_tab is None:
            return
        for cfg in self._variables_tab.export_config().get("read") or []:
            if cfg.get("type") != "cache" or cfg.get("storage") != "rom":
                continue
            name = str(cfg.get("name", "")).strip()
            saved = (self._config.get("cache_var_bufs") or {}).get(name)
            if not name or not isinstance(saved, dict):
                continue
            try:
                data = bytes.fromhex(str(saved.get("data", "")))
            except ValueError:
                continue
            self._cache_bufs.setdefault(name, {"buf1": None, "buf2": None})
            self._cache_bufs[name]["buf2"] = {
                "id": int(saved.get("id", 0) or 0),
                "data": data,
                "extended": bool(saved.get("extended")),
                "channel": int(saved.get("channel", 0) or 0),
            }

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
        """Формирует внутренний список активных программ.

        Источник — _runtime_rules (снимок последней записанной в МК
        конфигурации), а НЕ живые виджеты строк: правки полей не
        должны мгновенно менять исполняемую программу
        (отчёт мастера)."""
        self._internal_rules = []
        self._rule_counters = [0] * len(self._runtime_rules)
        # Каналы захвата «Кэш переменных»: событие «Приход DATA КЭШ»
        # выбирает CAN1/CAN2 — буфер 1 пополняется только на нём
        # (отчёт мастера).
        self._cache_watch = {}
        for rule_index, rule in enumerate(self._runtime_rules):
            if not rule.get("active", False):
                continue
            events = rule.get("events")
            if not isinstance(events, list) or not events:
                events = [rule.get("event") or {}]
            for event in events:
                if not isinstance(event, dict):
                    continue
                if (
                    event.get("type") == _EVENT_CACHEVAR
                    and event.get("cache_op", _CACHE_OP_RX) == _CACHE_OP_RX
                ):
                    name = str(event.get("var", "")).strip()
                    if name:
                        self._cache_watch.setdefault(name, set()).add(
                            int(event.get("channel", 1) or 1)
                        )
            self._internal_rules.append({"index": rule_index, "rule": rule})

    def _bump_rule_counter(self, rule_index: int) -> None:
        """Инкремент счётчика срабатываний программы и вывод его в
        шапку строки. Снимок _runtime_rules мог быть зафиксирован до
        добавления/удаления строк — виджет по индексу показываем
        только если он ещё существует."""
        while len(self._rule_counters) <= rule_index:
            self._rule_counters.append(0)
        self._rule_counters[rule_index] += 1
        if rule_index < len(self._row_widgets) and isValid(
            self._row_widgets[rule_index]
        ):
            self._row_widgets[rule_index].set_counter(
                self._rule_counters[rule_index]
            )

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
            # Состояние из таблицы привязки выбрано — истинно, пока
            # переменная находится именно в нём (отчёт мастера:
            # условия списком из таблицы привязки). Пустое состояние —
            # легаси «Активно»: переменная в любом именованном
            # состоянии.
            current = self._dyn_values.get(str(cond.get("var", "")))
            state = str(cond.get("state", "")).strip()
            if state:
                return str(current) == state
            return bool(current)
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
        if ctype == _COND_FLAG:
            # «Переменная N» — текущее состояние именованного бита
            # ОЗУ/ПЗУ (отчёт мастера).
            state = self._flag_states.get(str(cond.get("var", "")).strip(), 0)
            return state == int(cond.get("state", 1))
        if ctype == _COND_CACHEVAR:
            # «Записан/Не записан КЭШ» — по содержимому буфера 2
            # переменной (отчёт мастера).
            name = str(cond.get("var", "")).strip()
            filled = self._cache_buf2_filled(name)
            return (
                filled
                if str(cond.get("cache_op", _CACHE_OP_FILLED))
                == _CACHE_OP_FILLED
                else not filled
            )
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
        """Входная точка действия: «Задержка» + «Прервать если»
        (отчёт мастера). Без них — исполнение сразу; с ними —
        действие регистрируется как отложенное и событие-прерыватель
        может его отменить, пока идёт задержка."""
        delay = int(action.get("exec_delay", 0) or 0)
        abort = action.get("abort") or {}
        has_abort = abort.get("type") not in (None, "", _EVENT_NONE)
        if delay <= 0 and not has_abort:
            self._run_actions_now(rule_index, action, frame)
            return
        if has_abort and self._abort_level_true(abort):
            # Условие прерывания уже истинно — действие не запускаем.
            return
        self._abort_key_seq += 1
        entry: dict[str, Any] = {
            "key": self._abort_key_seq,
            "rule_index": rule_index,
            "action": dict(action),
            "frame": dict(frame),
            "abort": abort if has_abort else None,
            "aborted": False,
        }
        if has_abort:
            self._pending_actions.append(entry)
        if delay > 0:
            QTimer.singleShot(
                delay, lambda e=entry: self._execute_scheduled(e)
            )
        else:
            self._execute_scheduled(entry)

    def _execute_scheduled(self, entry: dict[str, Any]) -> None:
        """Таймер «Задержки» дотикал — проверяем прерыватель и
        исполняем."""
        if entry in self._pending_actions:
            self._pending_actions.remove(entry)
        if entry["aborted"]:
            logger.info("ГЛ: действие прервано событием «Прервать если»")
            return
        abort = entry["abort"]
        if abort and self._abort_level_true(abort):
            return
        self._run_actions_now(
            entry["rule_index"], entry["action"], entry["frame"]
        )

    def _abort_level_true(self, abort: dict[str, Any]) -> bool:
        """Уровневая проверка события-прерывателя прямо сейчас —
        для типов с состоянием (переменные, доп. каналы). События
        «фрейм»/«импульс» — фронтовые, уровня не имеют."""
        etype = abort.get("type")
        if etype == _EVENT_AUX:
            ch = int(abort.get("channel", 1) or 1)
            return self._aux_states.get(ch, 0) == int(
                abort.get("state", 1) or 0
            )
        if etype == _EVENT_FLAG:
            name = str(abort.get("var", "")).strip()
            return self._flag_states.get(name, 0) == int(
                abort.get("state", 1) or 0
            )
        if etype == _EVENT_STATIC:
            name = str(abort.get("var", "")).strip()
            state = self._static_states.get(name)
            if state is None:
                return False
            edge = abort.get("edge", "both")
            return (
                edge == "both"
                or (edge == "on" and state == 1)
                or (edge == "off" and state == 0)
            )
        if etype in (_EVENT_DYN, _EVENT_NUM):
            name = str(abort.get("var", "")).strip()
            current = self._dyn_values.get(name)
            if current is None:
                return False
            try:
                threshold = float(
                    str(abort.get("value", "")).replace(",", ".")
                )
            except (TypeError, ValueError):
                return str(current) == str(abort.get("value", "")).strip()
            if not isinstance(current, (int, float)):
                return False
            return (
                current > threshold
                if abort.get("dir", "gt") == "gt"
                else current < threshold
            )
        if etype == _EVENT_IMPULSE:
            name = str(abort.get("var", "")).strip()
            return time.monotonic() < self._impulse_until.get(name, 0.0)
        return False

    def _check_pending_aborts(
        self,
        frame: dict[str, Any],
        frame_data: bytes,
        changed_static: dict[str, int],
    ) -> None:
        """Проверяет события-прерыватели отложенных действий на
        текущем тике — сработавший прерыватель отменяет действие."""
        if not self._pending_actions:
            return
        for entry in list(self._pending_actions):
            abort = entry["abort"]
            if abort is None:
                continue
            if self._event_fired(
                -entry["key"], 0, abort, frame, frame_data, changed_static
            ):
                entry["aborted"] = True
                self._pending_actions.remove(entry)

    def _run_actions_now(
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

        # 1б. «Переменные» — именованный бит ОЗУ/ПЗУ: «включить»,
        #     «включить на N мс», «выключить» (отчёт мастера).
        flag_name = str(action.get("flag_var", "")).strip()
        if flag_name:
            flag_mode = str(action.get("flag_mode", "on"))
            pulse_ms = int(action.get("flag_time", 0) or 0)
            if flag_mode == "off":
                self._set_flag(flag_name, 0)
            elif flag_mode == "pulse":
                self._set_flag(flag_name, 1, pulse_ms=max(1, pulse_ms))
            else:
                self._set_flag(flag_name, 1)

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

        # 5. «Кэш переменная»: Записать (буфер 1 → буфер 2) /
        #    Отправить (буфер 2 → CAN) / Стереть (буфер 2 = 0)
        #    (отчёт мастера).
        if action.get("cv_op"):
            cv_name = str(action.get("cachevar", "")).strip()
            if cv_name:
                self._run_cachevar_action(cv_name, action)

    def _set_flag(
        self, name: str, value: int, pulse_ms: int = 0
    ) -> None:
        """Действие «Переменные»: перевод именованного бита ОЗУ/ПЗУ
        в 0/1. Переход — событие «Стала 1»/«Стала 0» для программ;
        биты с носителем «ПЗУ» сохраняются в конфиге (отчёт мастера)."""
        name = str(name).strip()
        if not name:
            return
        value = 1 if value else 0
        changed = self._flag_states.get(name, 0) != value
        self._flag_states[name] = value
        if pulse_ms > 0 and value == 1:
            self._flag_pulse_until[name] = (
                time.monotonic() + pulse_ms / 1000
            )
            QTimer.singleShot(
                pulse_ms, lambda n=name: self._flag_pulse_check(n)
            )
        if not changed:
            return
        self._persist_flag_states()
        if self._variables_tab is not None:
            self._variables_tab.set_flag_live(name, value)
        self._flag_edges[name] = value
        # Переход бита — событие «Стала 1/0»; там же проверяются
        # прерыватели отложенных действий (отчёт мастера).
        self._fire_aux_events()
        self._flag_edges.pop(name, None)

    def _flag_pulse_check(self, name: str) -> None:
        """Авто-гашение «включить на N мс»: время вышло — бит в 0."""
        if time.monotonic() >= self._flag_pulse_until.get(name, 0.0):
            self._set_flag(name, 0)

    def _persist_flag_states(self) -> None:
        """Биты «Переменная» с носителем «ПЗУ» сохраняются в общий
        конфиг — состояние переживает перезапуск приложения
        (отчёт мастера: бит фиксируется в ПЗУ)."""
        if self._variables_tab is None:
            return
        states: dict[str, int] = {}
        for cfg in self._variables_tab.export_config().get("aux") or []:
            if cfg.get("type") != "flag":
                continue
            name = str(cfg.get("name", "")).strip()
            if name and cfg.get("storage") == "flash":
                states[name] = self._flag_states.get(name, 0)
        self._config.set("flag_states", states)

    def _restore_flag_states(self) -> None:
        """Поднимает сохранённые ПЗУ-биты из конфига."""
        for name, state in (self._config.get("flag_states") or {}).items():
            if isinstance(state, int):
                self._flag_states[str(name)] = 1 if state else 0

    def notify_device_ready(self) -> None:
        """Устройство полностью загрузилось после подачи питания
        (опознано по USB) — событие «Включение устройства»
        (отчёт мастера): программы с таким событием стартуют раз."""
        if self._rules_dirty:
            self._build_internal_rules()
            self._rules_dirty = False
        self._restore_flag_states()
        # Отдельный тик: повторное «включение устройства» (реконнект)
        # должно заново стартовать слушателей «Программа начала
        # работать» — иначе seq совпал бы с прошлым и событие
        # отфильтровалось как уже увиденное.
        self._tick += 1
        dummy_frame = {"id": 0, "channel": 0, "data": b"", "extended": False}
        for internal in self._internal_rules:
            rule_index = internal["index"]
            rule = internal["rule"]
            events = rule.get("events")
            if not isinstance(events, list) or not events:
                events = [rule.get("event") or {}]
            if not any(
                e.get("type") == _EVENT_POWER for e in events
            ):
                continue
            title = str(rule.get("title", "")).strip()
            if title:
                # «Включение устройства» — событие прошло, программа
                # перешла к условиям: фиксируем для событий
                # «Программа начала работать» (отчёт мастера).
                self._program_started_seq[title] = self._tick
            conditions = rule.get("conditions")
            if not isinstance(conditions, list) or not conditions:
                conditions = [rule.get("condition") or {}]
            if not all(
                self._condition_passed(cond) for cond in conditions
            ):
                continue
            self._bump_rule_counter(rule_index)
            actions = rule.get("actions")
            if not isinstance(actions, list) or not actions:
                actions = [rule.get("action") or {}]
            for action in actions:
                self._run_actions(rule_index, action, dummy_frame)
            logger.info(
                "ГЛ: «Включение устройства» — программа «%s»",
                rule.get("title") or rule_index,
            )

    def _fire_aux_events(self) -> None:
        """Смена состояния доп. канала/бита «Переменная» — проход по
        программам с событиями «Доп канал»/«Переменная»: условия И →
        действия (ИЛИ по событиям). Реентерабельность ограничена:
        действие может само щёлкать каналом или битом — глубже одного
        уровня не идём, чтобы кольцевая программа не зациклила ПК."""
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
        # Отдельный тик: два подряд старта одной программы между
        # кадрами должны оба доходить до слушателей «Программа
        # начала работать».
        self._tick += 1
        dummy_frame = {"id": 0, "channel": 0, "data": b"", "extended": False}
        # События-прерыватели отложенных действий реагируют и на
        # смену состояний (доп. каналы, биты «Переменная»).
        self._check_pending_aborts(dummy_frame, b"", {})
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
                etype = event.get("type")
                # Здесь проходят только события-фронты состояний:
                # доп. каналы, биты «Переменная», запись/стирание
                # кэш-переменных и старт программ (отчёт мастера).
                # «Приход DATA КЭШ» и кадровые события привязаны к
                # реальному кадру — здесь не проверяются.
                if etype == _EVENT_CACHEVAR:
                    if event.get("cache_op") not in (
                        _CACHE_OP_WRITTEN, _CACHE_OP_ERASED
                    ):
                        continue
                elif etype not in (_EVENT_AUX, _EVENT_FLAG, _EVENT_PROGRAM):
                    continue
                if self._event_fired(
                    rule_index, sub_index, event, dummy_frame,
                    b"", {},
                ):
                    fired = True
                    break
            if not fired:
                continue
            title = str(rule.get("title", "")).strip()
            if title:
                self._program_started_seq[title] = self._tick
            if not all(
                self._condition_passed(cond) for cond in conditions
            ):
                continue
            self._bump_rule_counter(rule_index)
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
        if etype == _EVENT_FLAG:
            # «Переменная» (бит ОЗУ/ПЗУ): фронт «Стала 1»/«Стала 0»
            # зафиксирован в _flag_edges текущего тика (отчёт мастера).
            name = str(event.get("var", "")).strip()
            return self._flag_edges.get(name) == int(
                event.get("state", 1) or 0
            )
        if etype == _EVENT_POWER:
            # «Включение устройства» срабатывает отдельным вызовом
            # notify_device_ready() — по кадрам не проверяется.
            return False
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
        if etype == _EVENT_CACHEVAR:
            # «Кэш переменная» (отчёт мастера): фронты — приход кадра
            # в диапазон на выбранном канале / запись в привязанный
            # кэш / обнуление привязанного буфера.
            name = str(event.get("var", "")).strip()
            if not name:
                return False
            op = str(event.get("cache_op", _CACHE_OP_RX))
            if op == _CACHE_OP_RX:
                channel = int(event.get("channel", 1) or 1)
                return (name, channel) in self._cache_rx_edges
            return (name, op) in self._cache_edges
        if etype == _EVENT_PROGRAM:
            # «Программа (имя) начала работать»: целевая программа
            # прошла фазу событий на этом или прошлом тике — каждый
            # её старт доезжает до слушателя ровно один раз
            # (отчёт мастера). Сама себя программа не слушает.
            prog = str(event.get("var", "")).strip()
            if not prog:
                return False
            if 0 <= rule_index < len(self._runtime_rules):
                own = str(
                    self._runtime_rules[rule_index].get("title", "")
                ).strip()
                if own and own == prog:
                    return False
            seq = self._program_started_seq.get(prog)
            if seq is None:
                return False
            key = (rule_index, sub_index, prog)
            if self._program_event_seen.get(key, -1) >= seq:
                return False
            self._program_event_seen[key] = seq
            return True
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

        # Новый тик рантайма: фронты кэш-событий прошлого кадра
        # отработаны, список «программа начала работать» начинаем
        # заново (слушатели видят старт на этом же кадре; программа,
        # стоящая РАНЬШЕ целевой в списке, ловит старт на следующем
        # кадре через _program_event_seen — без двойных срабатываний).
        self._tick += 1
        self._cache_rx_edges.clear()
        self._cache_edges.clear()

        # Сначала состояние переменных — от него зависят и события,
        # и условия ПРИ.
        changed_static = self._update_variable_states(frame_id, frame_data)
        # Буфер 1 «Кэш переменных» — непрерывный захват кадров
        # диапазона, независимо от срабатывания программ
        # (отчёт мастера).
        self._capture_cache_vars(frame, frame_id, frame_data)
        # События-прерыватели отложенных действий («Прервать если» —
        # отчёт мастера) реагируют на этот кадр и смену состояний.
        self._check_pending_aborts(frame, frame_data, changed_static)
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
            # Программа перешла из «Событий» к «Условиям» — фиксируем
            # для событий «Программа (имя) начала работать»
            # (отчёт мастера). До проверки условий — переход уже
            # состоялся независимо от их результата.
            title = str(rule.get("title", "")).strip()
            if title:
                self._program_started_seq[title] = self._tick
            # Несколько условий объединены по И: должны
            # выполняться все (отчёт мастера).
            if not all(
                self._condition_passed(cond) for cond in conditions
            ):
                continue

            self._bump_rule_counter(rule_index)
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
