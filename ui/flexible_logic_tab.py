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
* «Доп канал» — заглушка (функция будет описана позже);
* «Фрейм» — ручной ввод CAN-пакета (канал, битность, ID, DLC,
  DATA по байтам с «X», «Сработок на DATA») — приход такого кадра
  запускает программу.

Условие — необязательная проверка текущего состояния перед действием:
статическая переменная (включена/выключена), динамическая
(больше/меньше/равно порогу), доп. канал (заглушка).

Действие — установка переменной из раздела «Управление», ручной
CAN-фрейм (байт «X» подставляется из кадра-события) и/или
«Автоматическая запись DATA в кэш» — по образцу триггеров: приходящий
кадр, подходящий под спецификацию источника, кэшируется, а при
срабатывании программы отправляется последний сохранённый кадр.

Сохранение — в общий конфиг приложения (ключ flexible_rules). Старый
формат правил (id/mask/condition_data → resp_*) автоматически
мигрирует в событие «Фрейм» + действие «Фрейм».
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QFont
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
from models.utils import hex_to_int, int_to_hex
from ui.can_monitor_tab import DbcSignalDialog
from ui.hex_edit import create_data_field_widget
from ui.ui_utils import setup_button
from ui.variables_tab import _HexIdEdit

logger = get_logger(__name__)

_EVENT_DYN = "dyn"
_EVENT_STATIC = "static"
_EVENT_AUX = "aux"
_EVENT_FRAME = "frame"

_COND_NONE = "none"
_COND_STATIC = "static"
_COND_DYN = "dyn"
_COND_AUX = "aux"

# Каналы в спецификациях фреймов ГЛ: 0 — CAN1, 1 — CAN2, 2 — любой.
_CHANNELS_ANY = ("CAN1", "CAN2", "")
_BIT_RATES = ("11 bit", "29 bit")


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


def _tokens_match(tokens: list[str], data: bytes) -> bool:
    """Проверка DATA по маске токенов: «»/«X» — байт не участвует."""
    for i, token in enumerate(tokens):
        if not token or token == "X":
            continue
        value = hex_to_int(token)
        if value is None:
            continue
        byte = data[i] if i < len(data) else 0
        if byte != value:
            return False
    return True


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
    """Событие «Динамическая переменная»: выбор переменной,
    «Стало больше/меньше», порог из графика переменной."""

    def __init__(self, font: QFont, mark_dirty, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_small_label(tr("Переменная:"), font))
        self.var = _VarCombo(font)
        layout.addWidget(self.var)

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
        layout.addStretch()

        self.var.currentIndexChanged.connect(mark_dirty)
        self.direction.currentIndexChanged.connect(mark_dirty)
        self.value.textChanged.connect(mark_dirty)

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_DYN,
            "var": self.var.get_name(),
            "dir": self.direction.currentData(),
            "value": self.value.text().strip(),
        }

    def write(self, event: dict[str, Any]) -> None:
        self.var.set_name(str(event.get("var", "")))
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
        self.edge.addItem(tr("Включилась и выключилась"), "both")
        self.edge.addItem(tr("Включилась (→1)"), "on")
        self.edge.addItem(tr("Выключилась (→0)"), "off")
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


class _AuxEventPage(QWidget):
    """Событие «Доп канал» — заглушка: функция будет описана позже."""

    def __init__(self, font: QFont, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(tr("Доп канал — скоро"))
        label.setFont(font)
        label.setStyleSheet("color: #9A9AA5;")
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch()

    def read(self) -> dict[str, Any]:
        return {"type": _EVENT_AUX}

    def write(self, event: dict[str, Any]) -> None:
        pass


class _FrameEventPage(QWidget):
    """Событие «Фрейм»: ручной ввод CAN-пакета — настройки как
    в триггерах (канал, битность, ID, DLC, DATA по байтам с «X»,
    «Сработок на DATA»)."""

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
            self.channel.addItem(name or tr("Любой"), i)
        row1.addWidget(self.channel)
        row1.addWidget(_small_label(tr("Бит"), font))
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItems(_BIT_RATES)
        self.bit.setFixedWidth(70)
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
        self.from_dbc = QPushButton(tr("Из DBC"))
        self.from_dbc.setFont(font)
        row2.addWidget(self.from_dbc)
        row2.addStretch()
        layout.addLayout(row2)

        layout.addWidget(_small_label("DATA (X — любой байт)", font))
        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=32, allow_x=True
        )
        layout.addWidget(data_widget)

        row3 = QHBoxLayout()
        row3.addWidget(_small_label(tr("Сработок на DATA"), font))
        self.fire_limit = QSpinBox()
        self.fire_limit.setFont(font)
        self.fire_limit.setRange(1, 99)
        self.fire_limit.setValue(1)
        self.fire_limit.setFixedWidth(64)
        self.fire_limit.setToolTip(tr(
            "Сколько раз программа срабатывает, пока DATA кадра "
            "не изменится. По умолчанию 1 — одинаковые пакеты "
            "не перезапускают программу."
        ))
        row3.addWidget(self.fire_limit)
        row3.addStretch()
        layout.addLayout(row3)
        layout.addStretch()

        self.channel.currentIndexChanged.connect(mark_dirty)
        self.bit.currentIndexChanged.connect(mark_dirty)
        self.can_id.textChanged.connect(mark_dirty)
        self.dlc.valueChanged.connect(mark_dirty)
        self.fire_limit.valueChanged.connect(mark_dirty)
        for edit in self.data:
            edit.textChanged.connect(mark_dirty)
        self.from_dbc.clicked.connect(self._on_from_dbc)

    def _on_from_dbc(self) -> None:
        dialog = DbcSignalDialog(self)
        if dialog.exec() != 1:
            return
        result = dialog.get_result()
        if result is None:
            return
        can_id, data = result
        self.can_id.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        self.bit.setCurrentIndex(1 if can_id > 0x7FF else 0)
        for i, edit in enumerate(self.data):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")

    def read(self) -> dict[str, Any]:
        return {
            "type": _EVENT_FRAME,
            "channel": self.channel.currentData(),
            "extended": self.bit.currentIndex() == 1,
            "id": self.can_id.text().strip(),
            "dlc": self.dlc.value(),
            "data": _tokens_to_text(self.data),
            "fire_limit": self.fire_limit.value(),
        }

    def write(self, event: dict[str, Any]) -> None:
        ch = int(event.get("channel", 2))
        cidx = self.channel.findData(ch)
        self.channel.setCurrentIndex(cidx if cidx >= 0 else 2)
        self.bit.setCurrentIndex(1 if event.get("extended") else 0)
        self.can_id.setText(str(event.get("id", "")))
        self.dlc.setValue(int(event.get("dlc", 8)))
        _text_to_tokens(self.data, event.get("data"))
        self.fire_limit.setValue(int(event.get("fire_limit", 1)))


class RuleRowWidget(QWidget):
    """Одна программа: шапка (галочка, имя, ✕) + колонки
    ЕСЛИ «Событие» / ПРИ «Условие» / ТО «Действие»."""

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

        # Шапка программы: вкл/выкл, имя, свёртка, удаление.
        self._active_check = QCheckBox()
        self._active_check.setFont(font)
        self._active_check.setToolTip(tr("Включить/выключить программу"))
        self._active_check.toggled.connect(self._mark_dirty)
        self._name_edit = QLineEdit()
        self._name_edit.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._name_edit.setPlaceholderText(tr("Программа"))
        self._name_edit.setClearButtonEnabled(True)
        self._name_edit.textChanged.connect(self._mark_dirty)
        self._collapse_button = QPushButton("▾")
        self._collapse_button.setFont(font)
        self._collapse_button.setFixedWidth(30)
        self._collapse_button.setToolTip(tr("Свернуть программу"))
        self._collapse_button.clicked.connect(self._toggle_collapsed)
        self._remove_button = QPushButton("✕")
        self._remove_button.setFont(font)
        self._remove_button.setFixedSize(26, 26)
        self._remove_button.setToolTip(tr("Удалить программу"))
        self._remove_button.clicked.connect(self._on_remove)
        self._summary_label = QLabel()
        self._summary_label.setFont(font)
        self._summary_label.setVisible(False)
        self._counter_label = QLabel("0")
        self._counter_label.setFont(font)
        self._counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter_label.setFixedWidth(36)
        self._counter_label.setToolTip(tr("Срабатываний"))

        # ---- Событие ---------------------------------------------------
        self._event_group = QGroupBox(tr("Событие"))
        self._event_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._event_type = QComboBox()
        self._event_type.setFont(font)
        self._event_type.addItem(tr("Динамическая переменная"), _EVENT_DYN)
        self._event_type.addItem(tr("Статическая переменная"), _EVENT_STATIC)
        self._event_type.addItem(tr("Доп канал"), _EVENT_AUX)
        self._event_type.addItem(tr("Фрейм"), _EVENT_FRAME)
        self._event_type.currentIndexChanged.connect(self._on_event_type)
        self._event_stack = QStackedWidget()
        self._ev_dyn = _DynEventPage(font, self._mark_dirty)
        self._ev_static = _StaticEventPage(font, self._mark_dirty)
        self._ev_aux = _AuxEventPage(font)
        self._ev_frame = _FrameEventPage(font, self._mark_dirty)
        for page in (self._ev_dyn, self._ev_static, self._ev_aux, self._ev_frame):
            self._event_stack.addWidget(page)

        # ---- Условие ----------------------------------------------------
        self._cond_group = QGroupBox(tr("Условие"))
        self._cond_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._cond_type = QComboBox()
        self._cond_type.setFont(font)
        self._cond_type.addItem(tr("Нет"), _COND_NONE)
        self._cond_type.addItem(tr("Статическая переменная"), _COND_STATIC)
        self._cond_type.addItem(tr("Динамическая переменная"), _COND_DYN)
        self._cond_type.addItem(tr("Доп канал"), _COND_AUX)
        self._cond_type.currentIndexChanged.connect(self._on_cond_type)

        self._cond_stack = QStackedWidget()
        none_page = QWidget()
        none_layout = QVBoxLayout(none_page)
        none_layout.setContentsMargins(0, 0, 0, 0)
        none_label = QLabel(tr("Без проверки — действие сразу"))
        none_label.setFont(font)
        none_label.setStyleSheet("color: #9A9AA5;")
        none_layout.addWidget(none_label)
        none_layout.addStretch()

        static_page = QWidget()
        st_layout = QVBoxLayout(static_page)
        st_layout.setSpacing(4)
        st_layout.setContentsMargins(0, 0, 0, 0)
        st_layout.addWidget(_small_label(tr("Переменная:"), font))
        self._cd_st_var = _VarCombo(font)
        st_layout.addWidget(self._cd_st_var)
        st_layout.addWidget(_small_label(tr("Состояние:"), font))
        self._cd_st_state = QComboBox()
        self._cd_st_state.setFont(font)
        self._cd_st_state.addItem(tr("Включена (1)"), 1)
        self._cd_st_state.addItem(tr("Выключена (0)"), 0)
        st_layout.addWidget(self._cd_st_state)
        st_layout.addStretch()

        dyn_page = QWidget()
        dyn_layout = QVBoxLayout(dyn_page)
        dyn_layout.setSpacing(4)
        dyn_layout.setContentsMargins(0, 0, 0, 0)
        dyn_layout.addWidget(_small_label(tr("Переменная:"), font))
        self._cd_dyn_var = _VarCombo(font)
        dyn_layout.addWidget(self._cd_dyn_var)
        dyn_row = QHBoxLayout()
        self._cd_dyn_op = QComboBox()
        self._cd_dyn_op.setFont(font)
        self._cd_dyn_op.addItem(tr("Больше"), "gt")
        self._cd_dyn_op.addItem(tr("Меньше"), "lt")
        self._cd_dyn_op.addItem(tr("Равно"), "eq")
        dyn_row.addWidget(self._cd_dyn_op)
        self._cd_dyn_value = QLineEdit()
        self._cd_dyn_value.setFont(font)
        self._cd_dyn_value.setPlaceholderText(tr("значение"))
        self._cd_dyn_value.setFixedWidth(80)
        dyn_row.addWidget(self._cd_dyn_value)
        dyn_row.addStretch()
        dyn_layout.addLayout(dyn_row)
        dyn_layout.addStretch()

        aux_page = QWidget()
        aux_layout = QVBoxLayout(aux_page)
        aux_layout.setContentsMargins(0, 0, 0, 0)
        aux_label = QLabel(tr("Доп канал — скоро"))
        aux_label.setFont(font)
        aux_label.setStyleSheet("color: #9A9AA5;")
        aux_layout.addWidget(aux_label)
        aux_layout.addStretch()

        for page in (none_page, static_page, dyn_page, aux_page):
            self._cond_stack.addWidget(page)

        self._cd_st_var.currentIndexChanged.connect(self._mark_dirty)
        self._cd_st_state.currentIndexChanged.connect(self._mark_dirty)
        self._cd_dyn_var.currentIndexChanged.connect(self._mark_dirty)
        self._cd_dyn_op.currentIndexChanged.connect(self._mark_dirty)
        self._cd_dyn_value.textChanged.connect(self._mark_dirty)

        # ---- Действие ----------------------------------------------------
        self._action_group = QGroupBox(tr("Действие"))
        self._action_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))

        # Переменная из «Управление».
        self._act_var = _VarCombo(font)
        self._act_var.currentIndexChanged.connect(self._on_act_var_changed)
        self._act_var_value = QComboBox()
        self._act_var_value.setFont(font)
        self._act_var_value.addItem(tr("→ 1"), 1)
        self._act_var_value.addItem(tr("→ 0"), 0)
        self._act_var_value.currentIndexChanged.connect(self._mark_dirty)
        self._act_var_num = QLineEdit()
        self._act_var_num.setFont(font)
        self._act_var_num.setPlaceholderText(tr("значение"))
        self._act_var_num.setFixedWidth(80)
        self._act_var_num.textChanged.connect(self._mark_dirty)

        # Ручной фрейм (канал — здесь, по отчёту мастера).
        self._act_frame_box = QGroupBox(tr("Фрейм"))
        self._act_frame_box.setFont(font)
        self._act_frame_box.setCheckable(True)
        self._act_frame_box.setChecked(False)
        self._act_frame_box.toggled.connect(self._mark_dirty)
        self._act_channel = QComboBox()
        self._act_channel.setFont(font)
        self._act_channel.addItems(["CAN1", "CAN2"])
        self._act_channel.currentIndexChanged.connect(self._mark_dirty)
        self._act_bit = QComboBox()
        self._act_bit.setFont(font)
        self._act_bit.addItems(_BIT_RATES)
        self._act_bit.setFixedWidth(70)
        self._act_bit.currentIndexChanged.connect(self._mark_dirty)
        self._act_id = _HexIdEdit(font)
        self._act_id.textChanged.connect(self._mark_dirty)
        self._act_dlc = QSpinBox()
        self._act_dlc.setFont(font)
        self._act_dlc.setRange(1, 8)
        self._act_dlc.setValue(8)
        self._act_dlc.setFixedWidth(54)
        self._act_dlc.valueChanged.connect(self._mark_dirty)
        self._act_data, self._act_data_widget = create_data_field_widget(
            font, 8, edit_width=32, allow_x=True
        )
        for edit in self._act_data:
            edit.textChanged.connect(self._mark_dirty)
        self._act_delay = QSpinBox()
        self._act_delay.setRange(0, 9999)
        self._act_delay.setSuffix(tr(" мс"))
        self._act_delay.setFont(font)
        self._act_delay.setFixedWidth(86)
        self._act_delay.valueChanged.connect(self._mark_dirty)
        self._act_count = QSpinBox()
        self._act_count.setRange(1, 100)
        self._act_count.setValue(1)
        self._act_count.setFont(font)
        self._act_count.setFixedWidth(60)
        self._act_count.valueChanged.connect(self._mark_dirty)
        self._act_between = QSpinBox()
        self._act_between.setRange(0, 9999)
        self._act_between.setSuffix(tr(" мс"))
        self._act_between.setFont(font)
        self._act_between.setFixedWidth(86)
        self._act_between.valueChanged.connect(self._mark_dirty)

        # Автоматическая запись DATA в кэш — по образцу триггеров.
        self._act_cache_box = QGroupBox(tr("Автоматическая запись DATA в кэш"))
        self._act_cache_box.setFont(font)
        self._act_cache_box.setCheckable(True)
        self._act_cache_box.setChecked(False)
        self._act_cache_box.toggled.connect(self._mark_dirty)
        self._cache_channel = QComboBox()
        self._cache_channel.setFont(font)
        for i, name in enumerate(_CHANNELS_ANY):
            self._cache_channel.addItem(name or tr("Любой"), i)
        self._cache_channel.currentIndexChanged.connect(self._mark_dirty)
        self._cache_bit = QComboBox()
        self._cache_bit.setFont(font)
        self._cache_bit.addItems(_BIT_RATES)
        self._cache_bit.setFixedWidth(70)
        self._cache_bit.currentIndexChanged.connect(self._mark_dirty)
        self._cache_id = _HexIdEdit(font)
        self._cache_id.textChanged.connect(self._mark_dirty)
        self._cache_dlc = QSpinBox()
        self._cache_dlc.setFont(font)
        self._cache_dlc.setRange(1, 8)
        self._cache_dlc.setValue(8)
        self._cache_dlc.setFixedWidth(54)
        self._cache_dlc.valueChanged.connect(self._mark_dirty)
        self._cache_from, self._cache_from_widget = create_data_field_widget(
            font, 8, edit_width=30, allow_x=True
        )
        self._cache_to, self._cache_to_widget = create_data_field_widget(
            font, 8, edit_width=30, allow_x=True
        )
        for edit in (*self._cache_from, *self._cache_to):
            edit.textChanged.connect(self._mark_dirty)
        self._cache_tx_channel = QComboBox()
        self._cache_tx_channel.setFont(font)
        self._cache_tx_channel.addItems(["CAN1", "CAN2"])
        self._cache_tx_channel.currentIndexChanged.connect(self._mark_dirty)
        self._cache_delay = QSpinBox()
        self._cache_delay.setRange(0, 9999)
        self._cache_delay.setSuffix(tr(" мс"))
        self._cache_delay.setFont(font)
        self._cache_delay.setFixedWidth(86)
        self._cache_delay.valueChanged.connect(self._mark_dirty)
        self._cache_count = QSpinBox()
        self._cache_count.setRange(1, 100)
        self._cache_count.setValue(1)
        self._cache_count.setFont(font)
        self._cache_count.setFixedWidth(60)
        self._cache_count.valueChanged.connect(self._mark_dirty)

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

        # ЕСЛИ — Событие
        body_layout.addWidget(_connector(tr("ЕСЛИ")), 0)
        ev_layout = QVBoxLayout(self._event_group)
        ev_layout.setSpacing(4)
        ev_layout.setContentsMargins(8, 8, 8, 8)
        ev_layout.addWidget(self._event_type)
        ev_layout.addWidget(self._event_stack, 1)
        body_layout.addWidget(self._event_group, 1)

        # ПРИ — Условие
        body_layout.addWidget(_connector(tr("ПРИ")), 0)
        cd_layout = QVBoxLayout(self._cond_group)
        cd_layout.setSpacing(4)
        cd_layout.setContentsMargins(8, 8, 8, 8)
        cd_layout.addWidget(self._cond_type)
        cd_layout.addWidget(self._cond_stack, 1)
        body_layout.addWidget(self._cond_group, 1)

        # ТО — Действие
        body_layout.addWidget(_connector(tr("ТО")), 0)
        act_layout = QVBoxLayout(self._action_group)
        act_layout.setSpacing(4)
        act_layout.setContentsMargins(8, 8, 8, 8)

        var_row = QHBoxLayout()
        var_row.addWidget(_small_label(tr("Переменная:"), font))
        var_row.addWidget(self._act_var, 1)
        var_row.addWidget(self._act_var_value)
        var_row.addWidget(self._act_var_num)
        act_layout.addLayout(var_row)

        frame_layout = QVBoxLayout(self._act_frame_box)
        frame_layout.setSpacing(4)
        fr1 = QHBoxLayout()
        fr1.addWidget(_small_label(tr("Канал"), font))
        fr1.addWidget(self._act_channel)
        fr1.addWidget(_small_label(tr("Бит"), font))
        fr1.addWidget(self._act_bit)
        fr1.addWidget(_small_label("ID", font))
        fr1.addWidget(self._act_id)
        fr1.addWidget(_small_label("DLC", font))
        fr1.addWidget(self._act_dlc)
        fr1.addStretch()
        frame_layout.addLayout(fr1)
        frame_layout.addWidget(
            _small_label(tr("DATA (X — из кадра события)"), font)
        )
        frame_layout.addWidget(self._act_data_widget)
        fr2 = QHBoxLayout()
        fr2.addWidget(_small_label(tr("Пауза"), font))
        fr2.addWidget(self._act_delay)
        fr2.addWidget(_small_label(tr("Кол-во"), font))
        fr2.addWidget(self._act_count)
        fr2.addWidget(_small_label(tr("Между"), font))
        fr2.addWidget(self._act_between)
        fr2.addStretch()
        frame_layout.addLayout(fr2)
        act_layout.addWidget(self._act_frame_box)

        cache_layout = QVBoxLayout(self._act_cache_box)
        cache_layout.setSpacing(4)
        cr1 = QHBoxLayout()
        cr1.addWidget(_small_label(tr("Канал"), font))
        cr1.addWidget(self._cache_channel)
        cr1.addWidget(_small_label(tr("Бит"), font))
        cr1.addWidget(self._cache_bit)
        cr1.addWidget(_small_label("ID", font))
        cr1.addWidget(self._cache_id)
        cr1.addWidget(_small_label("DLC", font))
        cr1.addWidget(self._cache_dlc)
        cr1.addStretch()
        cache_layout.addLayout(cr1)
        cache_layout.addWidget(_small_label(tr("DATA от:"), font))
        cache_layout.addWidget(self._cache_from_widget)
        cache_layout.addWidget(_small_label(tr("DATA до:"), font))
        cache_layout.addWidget(self._cache_to_widget)
        cr2 = QHBoxLayout()
        cr2.addWidget(_small_label(tr("Отправить в"), font))
        cr2.addWidget(self._cache_tx_channel)
        cr2.addWidget(_small_label(tr("Пауза"), font))
        cr2.addWidget(self._cache_delay)
        cr2.addWidget(_small_label(tr("Кол-во"), font))
        cr2.addWidget(self._cache_count)
        cr2.addStretch()
        cache_layout.addLayout(cr2)
        act_layout.addWidget(self._act_cache_box)

        act_layout.addStretch()
        body_layout.addWidget(self._action_group, 1)
        layout.addWidget(self._body)

    # ---- переменные из вкладки «Переменные» ----------------------------

    def refresh_variables(self) -> None:
        """Обновляет списки переменных в комбобоксах из вкладки
        «Переменные» (события/условия — колонка «Чтение», действия —
        «Управление»). Выбранное имя сохраняется."""
        tab = self._tab._variables_tab
        dyn_read = tab.variable_names("read", "dynamic") if tab else []
        st_read = tab.variable_names("read", "static") if tab else []
        ctrl = tab.variable_names("control") if tab else []

        def refill(combo: _VarCombo, names: list[str]) -> None:
            combo.set_names(names, tr("— не выбрано —"))

        refill(self._ev_dyn.var, dyn_read)
        refill(self._ev_static.var, st_read)
        refill(self._cd_st_var, st_read)
        refill(self._cd_dyn_var, dyn_read)
        refill(self._act_var, ctrl)
        self._on_act_var_changed()

    def _on_event_type(self, index: int) -> None:
        self._event_stack.setCurrentIndex(index)
        self._mark_dirty()

    def _on_cond_type(self, index: int) -> None:
        self._cond_stack.setCurrentIndex(index)
        self._mark_dirty()

    def _on_act_var_changed(self, *_args) -> None:
        """Для статической переменной — выбор →1/→0, для динамической —
        числовое поле."""
        is_dynamic = False
        tab = self._tab._variables_tab
        if tab is not None:
            name = self._act_var.get_name()
            for cfg in tab._ctrl_col.configs():
                if cfg.get("name", "").strip() == name:
                    is_dynamic = cfg.get("type") == "dynamic"
                    break
        self._act_var_value.setVisible(not is_dynamic)
        self._act_var_num.setVisible(is_dynamic)
        self._mark_dirty()

    # ---- правило ---------------------------------------------------------

    def _migrate_legacy(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Старый формат (id/mask/condition_data → resp_*) — в новую
        схему: событие «Фрейм» + действие «Фрейм». Байт маски 0x00
        становится «X», ответная маска 0x00 — подстановкой из кадра."""
        if "event" in rule:
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

        event = rule.get("event") or {}
        etype = event.get("type", _EVENT_FRAME)
        eidx = self._event_type.findData(etype)
        self._event_type.setCurrentIndex(eidx if eidx >= 0 else 3)
        self._event_stack.setCurrentIndex(self._event_type.currentIndex())
        page = {
            _EVENT_DYN: self._ev_dyn,
            _EVENT_STATIC: self._ev_static,
            _EVENT_AUX: self._ev_aux,
            _EVENT_FRAME: self._ev_frame,
        }[etype if eidx >= 0 else _EVENT_FRAME]
        page.write(event)

        cond = rule.get("condition") or {}
        ctype = cond.get("type", _COND_NONE)
        cidx = self._cond_type.findData(ctype)
        self._cond_type.setCurrentIndex(cidx if cidx >= 0 else 0)
        self._cond_stack.setCurrentIndex(self._cond_type.currentIndex())
        if ctype == _COND_STATIC:
            self._cd_st_var.set_name(str(cond.get("var", "")))
            sidx = self._cd_st_state.findData(int(cond.get("state", 1)))
            self._cd_st_state.setCurrentIndex(sidx if sidx >= 0 else 0)
        elif ctype == _COND_DYN:
            self._cd_dyn_var.set_name(str(cond.get("var", "")))
            oidx = self._cd_dyn_op.findData(cond.get("op", "gt"))
            self._cd_dyn_op.setCurrentIndex(oidx if oidx >= 0 else 0)
            self._cd_dyn_value.setText(str(cond.get("value", "")))

        action = rule.get("action") or {}
        self._act_var.set_name(str(action.get("var", "")))
        vidx = self._act_var_value.findData(int(action.get("var_value", 1) or 0))
        self._act_var_value.setCurrentIndex(vidx if vidx >= 0 else 0)
        self._act_var_num.setText(str(action.get("var_num", "")))
        self._act_frame_box.setChecked(bool(action.get("frame_enabled", False)))
        self._act_channel.setCurrentIndex(int(action.get("channel", 0) or 0))
        self._act_bit.setCurrentIndex(1 if action.get("extended") else 0)
        self._act_id.setText(str(action.get("id", "")))
        self._act_dlc.setValue(int(action.get("dlc", 8) or 8))
        _text_to_tokens(self._act_data, action.get("data"))
        self._act_delay.setValue(int(action.get("delay", 0) or 0))
        self._act_count.setValue(int(action.get("count", 1) or 1))
        self._act_between.setValue(int(action.get("between", 0) or 0))
        self._act_cache_box.setChecked(bool(action.get("cache_enabled", False)))
        ch = int(action.get("cache_channel", 2) or 0)
        cidx2 = self._cache_channel.findData(ch)
        self._cache_channel.setCurrentIndex(cidx2 if cidx2 >= 0 else 2)
        self._cache_bit.setCurrentIndex(1 if action.get("cache_extended") else 0)
        self._cache_id.setText(str(action.get("cache_id", "")))
        self._cache_dlc.setValue(int(action.get("cache_dlc", 8) or 8))
        _text_to_tokens(self._cache_from, action.get("cache_from"))
        _text_to_tokens(self._cache_to, action.get("cache_to"))
        self._cache_tx_channel.setCurrentIndex(
            int(action.get("cache_tx_channel", 0) or 0)
        )
        self._cache_delay.setValue(int(action.get("cache_delay", 0) or 0))
        self._cache_count.setValue(int(action.get("cache_count", 1) or 1))

        if rule.get("collapsed"):
            self._toggle_collapsed()

    def _on_remove(self) -> None:
        self._tab._remove_row(self)

    def _toggle_collapsed(self) -> None:
        """Сворачивает тело программы до шапки со сводкой и обратно."""
        collapsed = not self._body.isHidden()
        self._body.setVisible(not collapsed)
        self._summary_label.setVisible(collapsed)
        self._collapse_button.setText("▸" if collapsed else "▾")
        self._collapse_button.setToolTip(
            tr("Развернуть программу") if collapsed
            else tr("Свернуть программу")
        )
        if collapsed:
            rule = self.get_rule()
            event = rule.get("event") or {}
            src = (
                event.get("id") or "—"
                if event.get("type") == _EVENT_FRAME
                else event.get("var") or "—"
            )
            action = rule.get("action") or {}
            dst = action.get("var") or action.get("id") or "—"
            self._summary_label.setText(
                tr("{0} → {1}").format(src or "—", dst or "—")
            )
        self._mark_dirty()

    def get_rule(self) -> dict[str, Any]:
        """Собирает программу из полей строки."""
        pages = (self._ev_dyn, self._ev_static, self._ev_aux, self._ev_frame)
        event = pages[self._event_type.currentIndex()].read()

        cond: dict[str, Any] = {"type": _COND_NONE}
        ctype = self._cond_type.currentData()
        if ctype == _COND_STATIC:
            cond = {
                "type": _COND_STATIC,
                "var": self._cd_st_var.get_name(),
                "state": self._cd_st_state.currentData(),
            }
        elif ctype == _COND_DYN:
            cond = {
                "type": _COND_DYN,
                "var": self._cd_dyn_var.get_name(),
                "op": self._cd_dyn_op.currentData(),
                "value": self._cd_dyn_value.text().strip(),
            }
        elif ctype == _COND_AUX:
            cond = {"type": _COND_AUX}

        action: dict[str, Any] = {
            "var": self._act_var.get_name(),
            "var_value": self._act_var_value.currentData(),
            "var_num": self._act_var_num.text().strip(),
            "frame_enabled": self._act_frame_box.isChecked(),
            "channel": self._act_channel.currentIndex(),
            "extended": self._act_bit.currentIndex() == 1,
            "id": self._act_id.text().strip(),
            "dlc": self._act_dlc.value(),
            "data": _tokens_to_text(self._act_data),
            "delay": self._act_delay.value(),
            "count": self._act_count.value(),
            "between": self._act_between.value(),
            "cache_enabled": self._act_cache_box.isChecked(),
            "cache_channel": self._cache_channel.currentData(),
            "cache_extended": self._cache_bit.currentIndex() == 1,
            "cache_id": self._cache_id.text().strip(),
            "cache_dlc": self._cache_dlc.value(),
            "cache_from": _tokens_to_text(self._cache_from),
            "cache_to": _tokens_to_text(self._cache_to),
            "cache_tx_channel": self._cache_tx_channel.currentIndex(),
            "cache_delay": self._cache_delay.value(),
            "cache_count": self._cache_count.value(),
        }
        return {
            "title": self._name_edit.text().strip(),
            "active": self._active_check.isChecked(),
            "event": event,
            "condition": cond,
            "action": action,
            "collapsed": self._body.isHidden(),
        }

    def set_counter(self, value: int) -> None:
        self._counter_label.setText(str(value))

    def retranslate_ui(self) -> None:
        self._event_group.setTitle(tr("Событие"))
        self._cond_group.setTitle(tr("Условие"))
        self._action_group.setTitle(tr("Действие"))
        self._act_frame_box.setTitle(tr("Фрейм"))
        self._act_cache_box.setTitle(tr("Автоматическая запись DATA в кэш"))
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
        # Признак «условие дин. события уже истинно» — событие «Стало
        # больше/меньше» — фронт булева состояния, а не каждый кадр.
        self._dyn_flags: dict[tuple[int, str], bool] = {}
        # Кэш действий: индекс программы → последний подошедший кадр.
        self._fl_cache: dict[int, dict[str, Any]] = {}
        # «Сработок на DATA» событий-фреймов: индекс → (last_data, n).
        self._event_fire: dict[int, tuple[bytes | None, int]] = {}
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
        self.mark_dirty()

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
        for var in self._variable_defs():
            name = var.get("name", "").strip()
            if not name:
                continue
            if var.get("type") == "static":
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
            else:
                fid = hex_to_int(str(var.get("id", "")))
                if fid is None or fid != frame_id:
                    continue
                lo = str(var.get("from", "")).split()
                hi = str(var.get("to", "")).split()
                if not _tokens_range_match(lo, hi, data):
                    continue
                raw = 0
                for pos in var.get("bytes") or []:
                    if 0 <= pos < len(data):
                        raw = (raw << 8) | data[pos]
                self._dyn_values[name] = _interpolate(
                    var.get("points"), raw
                )
        return changed

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
        tokens = str(event.get("data", "")).split()
        return _tokens_match(tokens, bytes(frame["data"]))

    def _condition_passed(self, cond: dict[str, Any]) -> bool:
        """ПРИ: проверка текущего состояния перед действием."""
        ctype = cond.get("type", _COND_NONE)
        if ctype == _COND_STATIC:
            state = self._static_states.get(str(cond.get("var", "")), 0)
            return state == int(cond.get("state", 1))
        if ctype == _COND_DYN:
            value = self._dyn_values.get(str(cond.get("var", "")))
            if value is None:
                return False
            try:
                threshold = float(str(cond.get("value", "")).replace(",", "."))
            except ValueError:
                return False
            op = cond.get("op", "gt")
            if op == "lt":
                return value < threshold
            if op == "eq":
                return value == threshold
            return value > threshold
        return True  # «Нет» и «Доп канал» (заглушка) — не блокируют.

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

    def _send_frame(self, channel: int, can_id: int, data: bytes, delay_ms: int = 0) -> None:
        packed = pack_can_frame(channel, can_id, data)
        if delay_ms > 0:
            QTimer.singleShot(
                delay_ms, lambda p=packed: self._serial_manager.send_data(p)
            )
        else:
            self._serial_manager.send_data(packed)

    def _run_actions(
        self, rule_index: int, action: dict[str, Any], frame: dict[str, Any]
    ) -> None:
        # 1. Переменная из «Управление»: статическая — шлём её фрейм
        #    с нужным значением, динамическая — фрейм с сырым значением
        #    из обратной интерполяции графика.
        var_name = str(action.get("var", "")).strip()
        if var_name:
            for var in self._variable_defs():
                if var.get("name", "").strip() != var_name:
                    continue
                if var.get("type") == "static":
                    want = int(action.get("var_value", 1) or 0)
                    self._static_states[var_name] = want
                    for fdef in var.get("frames") or []:
                        if int(fdef.get("value", 1) or 0) != want:
                            continue
                        fid = hex_to_int(str(fdef.get("id", "")))
                        if fid is None:
                            continue
                        tokens = str(fdef.get("data", "")).split()
                        payload = bytearray(8)
                        for i, t in enumerate(tokens[:8]):
                            v = hex_to_int(t)
                            payload[i] = v if v is not None and t != "X" else 0
                        self._send_frame(
                            int(action.get("channel", 0)) + 1, fid,
                            bytes(payload[: int(fdef.get("dlc", 8) or 8)]),
                        )
                else:
                    try:
                        want = float(str(action.get("var_num", "")).replace(",", "."))
                    except ValueError:
                        break
                    fid = hex_to_int(str(var.get("id", "")))
                    if fid is None:
                        break
                    raw = int(round(_inverse_interpolate(var.get("points"), want)))
                    positions = sorted(p for p in (var.get("bytes") or []) if 0 <= p < 8)
                    payload = bytearray(8)
                    for order, pos in enumerate(reversed(positions)):
                        payload[pos] = (raw >> (8 * order)) & 0xFF
                    self._dyn_values[var_name] = want
                    self._send_frame(
                        int(action.get("channel", 0)) + 1, fid,
                        bytes(payload[: int(var.get("dlc", 8) or 8)]),
                    )
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

        # 3. Кэш: уходит последний сохранённый кадр (как в триггерах —
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

    def set_dbc(self, dbc_manager) -> None:
        """Обновляет логику при смене DBC (заглушка)."""

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

        for internal in self._internal_rules:
            rule_index = internal["index"]
            rule = internal["rule"]
            event = rule.get("event") or {}
            action = rule.get("action") or {}

            # Кэш пополняется независимо от срабатывания программы
            # (как строки кэша триггеров).
            self._update_cache(rule_index, action, frame)

            etype = event.get("type", _EVENT_FRAME)
            fired = False
            if etype == _EVENT_FRAME:
                if self._frame_event_matches(event, frame):
                    # «Сработок на DATA»: одинаковый поток не
                    # перезапускает программу; новая DATA — заново.
                    limit = max(1, int(event.get("fire_limit", 1) or 1))
                    last, count = self._event_fire.get(rule_index, (None, 0))
                    if last != frame_data:
                        last, count = frame_data, 0
                    if count < limit:
                        count += 1
                        fired = True
                    self._event_fire[rule_index] = (last, count)
            elif etype == _EVENT_STATIC:
                name = str(event.get("var", "")).strip()
                if name in changed_static:
                    edge = event.get("edge", "both")
                    fired = (
                        edge == "both"
                        or (edge == "on" and changed_static[name] == 1)
                        or (edge == "off" and changed_static[name] == 0)
                    )
            elif etype == _EVENT_DYN:
                name = str(event.get("var", "")).strip()
                if name in self._dyn_values:
                    try:
                        threshold = float(
                            str(event.get("value", "")).replace(",", ".")
                        )
                    except ValueError:
                        threshold = None
                    if threshold is not None:
                        flag = (
                            self._dyn_values[name] > threshold
                            if event.get("dir", "gt") == "gt"
                            else self._dyn_values[name] < threshold
                        )
                        key = (rule_index, name)
                        # Фронт истинности: «стало больше/меньше»
                        # стреляет один раз на переход через порог.
                        if flag and not self._dyn_flags.get(key, False):
                            fired = True
                        self._dyn_flags[key] = flag
            # _EVENT_AUX — заглушка, не срабатывает.

            if not fired:
                continue
            if not self._condition_passed(rule.get("condition") or {}):
                continue

            self._rule_counters[rule_index] += 1
            self._row_widgets[rule_index].set_counter(
                self._rule_counters[rule_index]
            )
            self._run_actions(rule_index, action, frame)
            logger.info(
                "Сработала программа ГЛ «%s» (событие %s)",
                rule.get("title") or rule_index,
                etype,
            )

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки."""
        self._add_button.setText(tr("＋ Добавить программу"))
        for row in self._row_widgets:
            row.retranslate_ui()
