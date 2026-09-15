"""Вкладка «Мониторинг CAN» с двумя каналами."""

import csv
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QDoubleValidator

from core.can_protocol import pack_can_frame
from core.dbc_manager import DBCManager
from core.dbc_parser import decode_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import bytes_to_hex_string, format_data_bytes, hex_to_int, int_to_hex, parse_data_bytes
from ui.filter_dialog import FilterDialog
from ui.hex_edit import create_data_field_widget
from ui.id_edit import IdPasteEdit
from ui.memory_indicator import MemoryIndicator
from ui.packet_clipboard import create_clipboard_buttons
from ui.ui_utils import setCheckableWithIndicator, setup_button

logger = get_logger(__name__)

MAX_TABLE_ROWS = 50_000

BIT_RATES = [tr("11 бит"), tr("29 бит")]


def _ascii_from_data(data: bytes) -> str:
    """Возвращает печатные ASCII-символы для байт, непечатные заменяются на '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


class _IdValidator:
    """Валидатор HEX ID с цветовой индикацией."""

    def __init__(self, edit: QLineEdit, bit_combo: QComboBox) -> None:
        self._edit = edit
        self._bit_combo = bit_combo
        self._edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,8}")))
        self._edit.textChanged.connect(self._validate)
        self._bit_combo.currentIndexChanged.connect(self._validate)

    def _validate(self) -> None:
        text = self._edit.text()
        upper = text.upper()
        if text != upper:
            self._edit.blockSignals(True)
            self._edit.setText(upper)
            self._edit.blockSignals(False)
            text = upper
        text = text.strip()
        if not text:
            self._edit.setStyleSheet("")
            return
        value = hex_to_int(text)
        if value is None:
            self._edit.setStyleSheet("color: #F44336;")
            return
        max_value = 0x1FFFFFFF if self._bit_combo.currentIndex() == 1 else 0x7FF
        self._edit.setStyleSheet("color: #4CAF50;" if value <= max_value else "color: #F44336;")


class DataVariantsDialog(QDialog):
    """Диалог со списком уникальных наборов данных для выбранного ID."""

    def __init__(self, can_id: int, variants: Set[bytes], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Варианты данных для ID {0}").format(int_to_hex(can_id, 8 if can_id > 0x7FF else 3)))
        self.resize(500, 300)
        layout = QVBoxLayout(self)
        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([tr("№"), tr("Данные"), tr("ASCII")])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, data in enumerate(sorted(variants)):
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            self._table.setItem(row, 1, QTableWidgetItem(" ".join(f"{b:02X}" for b in data)))
            self._table.setItem(row, 2, QTableWidgetItem(_ascii_from_data(data)))
        layout.addWidget(self._table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _data_percent(data: bytes, dlc: int, byte_index: Optional[int] = None) -> float:
    """DATA как процент заполнения: 00..00 → 0%, FF..FF → 100%.

    byte_index=None — весь DATA целиком; иначе — один байт (0x00→0%, 0xFF→100%).
    """
    dlc = max(0, min(int(dlc), len(data)))
    if dlc == 0:
        return 0.0
    if byte_index is not None:
        if byte_index >= dlc:
            return 0.0
        return data[byte_index] * 100.0 / 255.0
    value = int.from_bytes(data[:dlc], "big")
    maximum = (1 << (dlc * 8)) - 1
    return value * 100.0 / maximum if maximum else 0.0


class _PercentGraph(QWidget):
    """Живой график значения DATA в процентах: ось Y — %, ось X — время.

    Бегунок «развёртка» задаёт ширину окна в секундах, «инвертирование»
    переворачивает ось Y (FF..FF внизу, 00..00 вверху).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._samples: List[Tuple[float, float]] = []  # (time, percent)
        self._window_s = 30.0
        self._inverted = False
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_window(self, seconds: float) -> None:
        self._window_s = max(1.0, float(seconds))
        self.update()

    def set_inverted(self, inverted: bool) -> None:
        self._inverted = inverted
        self.update()

    def add_sample(self, t: float, percent: float) -> None:
        self._samples.append((t, percent))
        if len(self._samples) > 10_000:
            del self._samples[: len(self._samples) - 10_000]
        self.update()

    def set_samples(self, samples: List[Tuple[float, float]]) -> None:
        self._samples = list(samples)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#232338"))
        rect = self.rect().adjusted(38, 10, -10, -24)
        painter.setPen(QPen(QColor("#5A5A7A"), 1))
        painter.drawRect(rect)
        if rect.width() <= 0 or rect.height() <= 0:
            painter.end()
            return

        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        # Сетка и подписи процентов (инверсия переворачивает шкалу)
        for pct in (0, 25, 50, 75, 100):
            shown = 100 - pct if self._inverted else pct
            y = rect.bottom() - rect.height() * shown / 100.0
            painter.setPen(QPen(QColor("#3A3A5A"), 1, Qt.PenStyle.DotLine))
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            painter.setPen(QColor("#9E9E9E"))
            painter.drawText(2, int(y) + 4, f"{pct}%")

        now = time.time()
        start = now - self._window_s
        pts = [(t, v) for t, v in self._samples if t >= start]
        if len(pts) >= 2:
            painter.setPen(QPen(QColor("#4CAF50"), 2))
            prev = pts[0]
            for t, v in pts[1:]:
                # Линия должна следовать инверсии шкалы: FF..FF = 0%
                # внизу при включённом «Инвертировании», иначе 100% вверху.
                v1 = 100.0 - prev[1] if self._inverted else prev[1]
                v2 = 100.0 - v if self._inverted else v
                x1 = rect.left() + rect.width() * (prev[0] - start) / self._window_s
                y1 = rect.bottom() - rect.height() * v1 / 100.0
                x2 = rect.left() + rect.width() * (t - start) / self._window_s
                y2 = rect.bottom() - rect.height() * v2 / 100.0
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))
                prev = (t, v)
        painter.setPen(QColor("#9E9E9E"))
        painter.drawText(int(rect.left()), self.rect().bottom() - 6, f"-{int(self._window_s)}с")
        painter.drawText(int(rect.right()) - 24, self.rect().bottom() - 6, tr("сейчас"))
        painter.end()


class IdHistoryDialog(QDialog):
    """История одного CAN ID: таблица «время → data» + живой график %."""

    MAX_ROWS = 2000

    def __init__(
        self,
        can_id: int,
        channel: int,
        samples: List[Tuple[float, bytes, bool, int]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.can_id = can_id
        self.setWindowTitle(tr("История ID 0x{0:X} — CAN{1}").format(can_id, channel))
        self.resize(640, 480)
        # Сырые сэмплы для пересчёта при смене источника графика
        # (весь DATA или конкретный байт) и инверсии.
        self._samples_raw: List[Tuple[float, bytes, bool, int]] = []
        self._byte_index: Optional[int] = None

        font = QFont("Segoe UI", 9)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([tr("Время"), tr("DLC"), tr("Data")])
        self._table.setFont(font)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self._percent_label = QLabel("0%")
        self._percent_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))

        self._graph = _PercentGraph(self)

        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(5, 300)
        self._zoom_slider.setValue(30)
        self._zoom_label = QLabel(tr("Развёртка: 30 с"))
        self._zoom_label.setFont(font)
        self._zoom_slider.valueChanged.connect(self._on_zoom_changed)

        self._invert_button = QPushButton(tr("Инвертирование"))
        self._invert_button.setFont(font)
        self._invert_button.setCheckable(True)
        self._invert_button.setToolTip(tr("FF..FF = 0% внизу, 00..00 = 100% вверху"))
        self._invert_button.toggled.connect(self._on_invert_toggled)

        # Выбор источника графика: весь DATA или один байт. Для поиска
        # «какой ID несёт обороты/скорость» удобно смотреть конкретный
        # байт — в 8-байтном значении его изменение почти не видно.
        self._source_combo = QComboBox()
        self._source_combo.setFont(font)
        self._source_combo.addItem(tr("Весь DATA"), None)
        for i in range(8):
            self._source_combo.addItem(tr("Байт {0}").format(i), i)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)

        bottom = QHBoxLayout()
        bottom.addWidget(self._percent_label)
        bottom.addSpacing(16)
        bottom.addWidget(self._source_combo)
        bottom.addWidget(self._zoom_label)
        bottom.addWidget(self._zoom_slider, 1)
        bottom.addWidget(self._invert_button)

        layout.addWidget(self._table, 1)
        layout.addWidget(self._graph)
        layout.addLayout(bottom)

        for sample in samples:
            self._append_row(*sample, repaint=False)
        # Счётчик процентов сразу показывает последнее известное значение,
        # а не «0%» до прихода нового кадра.
        if samples:
            _t, data, rtr, dlc = samples[-1]
            pct = _data_percent(data, dlc) if not rtr else 0.0
            self._percent_label.setText(f"{pct:.1f}%")
            self._table.scrollToBottom()
        self._graph.update()

        # Периодический repaint — «ползущее» окно времени
        self._repaint_timer = QTimer(self)
        self._repaint_timer.timeout.connect(self._graph.update)
        self._repaint_timer.start(250)

    def _current_pct(self, data: bytes, rtr: bool, dlc: int) -> float:
        return _data_percent(data, dlc, self._byte_index) if not rtr else 0.0

    def _append_row(self, t: float, data: bytes, rtr: bool, dlc: int, repaint: bool = True) -> None:
        if self._table.rowCount() >= self.MAX_ROWS:
            self._table.removeRow(0)
        row = self._table.rowCount()
        self._table.insertRow(row)
        timestamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
        data_text = "rtr" if rtr else " ".join(format_data_bytes(data))
        for col, text in enumerate((timestamp, str(dlc), data_text)):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, col, item)
        self._samples_raw.append((t, bytes(data), rtr, dlc))
        if len(self._samples_raw) > self.MAX_ROWS:
            del self._samples_raw[: len(self._samples_raw) - self.MAX_ROWS]
        pct = self._current_pct(data, rtr, dlc)
        self._graph.add_sample(t, pct)
        if repaint:
            shown = 100.0 - pct if self._invert_button.isChecked() else pct
            self._percent_label.setText(f"{shown:.1f}%")
            self._table.scrollToBottom()

    def add_sample(self, t: float, data: bytes, rtr: bool, dlc: int) -> None:
        """Вызывается монитором при новом фрейме с этим ID."""
        self._append_row(t, data, rtr, dlc)

    def _on_source_changed(self, _index: int) -> None:
        """Смена источника графика: весь DATA ↔ конкретный байт."""
        self._byte_index = self._source_combo.currentData()
        self._graph.set_samples(
            [
                (t, self._current_pct(data, rtr, dlc))
                for t, data, rtr, dlc in self._samples_raw
            ]
        )
        if self._samples_raw:
            _t, data, rtr, dlc = self._samples_raw[-1]
            pct = self._current_pct(data, rtr, dlc)
            shown = 100.0 - pct if self._invert_button.isChecked() else pct
            self._percent_label.setText(f"{shown:.1f}%")

    def _on_zoom_changed(self, value: int) -> None:
        self._zoom_label.setText(tr("Развёртка: {0} с").format(value))
        self._graph.set_window(float(value))

    def _on_invert_toggled(self, checked: bool) -> None:
        self._graph.set_inverted(checked)
        # Пересчитываем текущий процент и график с учётом инверсии
        if self._graph._samples:
            _t, pct = self._graph._samples[-1]
            self._percent_label.setText(f"{(100.0 - pct) if checked else pct:.1f}%")


class BitmapDialog(QDialog):
    """Диалог с битовой картой 8×8 для последнего кадра ID."""

    def __init__(self, can_id: int, data: bytes, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Битовая карта ID {0}").format(int_to_hex(can_id, 8 if can_id > 0x7FF else 3)))
        layout = QGridLayout(self)
        for byte_idx in range(8):
            byte = data[byte_idx] if byte_idx < len(data) else 0
            for bit in range(8):
                label = QLabel("1" if (byte >> bit) & 1 else "0")
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setStyleSheet(
                    f"background-color: {'#4A6A8A' if (byte >> bit) & 1 else '#2B2B2B'}; "
                    "border: 1px solid #555; min-width: 22px; min-height: 22px;"
                )
                layout.addWidget(label, byte_idx, 7 - bit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons, 8, 0, 1, 8)


class DbcSignalDialog(QDialog):
    """Диалог выбора сообщения и сигнала из DBC для автозаполнения."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Выбор сигнала из DBC"))
        self.resize(360, 180)
        self._dbc_manager = DBCManager()
        self._result: Optional[Any] = None
        layout = QVBoxLayout(self)
        self._message_combo = QComboBox()
        self._signal_combo = QComboBox()
        self._message_combo.currentIndexChanged.connect(self._on_message_changed)
        layout.addWidget(QLabel(tr("Сообщение:")))
        layout.addWidget(self._message_combo)
        layout.addWidget(QLabel(tr("Сигнал:")))
        layout.addWidget(self._signal_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._populate()

    def _populate(self) -> None:
        db = self._dbc_manager.get_cantools_db()
        self._message_combo.clear()
        if db is None:
            return
        for message in db.messages:
            self._message_combo.addItem(message.name, message)
        self._on_message_changed(0)

    def _on_message_changed(self, index: int) -> None:
        self._signal_combo.clear()
        message = self._message_combo.itemData(index)
        if message is None:
            return
        for signal in message.signals:
            self._signal_combo.addItem(signal.name, signal)

    def _on_ok(self) -> None:
        message = self._message_combo.currentData()
        signal = self._signal_combo.currentData()
        if message is None or signal is None:
            self.reject()
            return
        try:
            encoded = message.encode({signal.name: 0})
            self._result = (message.frame_id, list(encoded))
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка кодирования сигнала DBC: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось закодировать сигнал: {0}").format(exc))
            return
        self.accept()

    def get_result(self) -> Optional[Any]:
        return self._result


class CanChannelMonitor(QWidget):
    """Панель мониторинга одного CAN-канала."""

    create_trigger_requested = Signal(dict)
    monitoring_state_changed = Signal(int, bool)

    def __init__(self, channel: int, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._channel_byte = channel
        self._serial_manager = serial_manager
        self._config = Config()
        self._running = False
        self._received_count = 0
        self._sent_count = 0
        self._packet_times: deque[float] = deque()
        self._last_packet_time: Optional[float] = None
        self._cyclic_frame: Optional[bytes] = None
        self._dbc_manager = DBCManager()

        self._id_to_row: Dict[int, int] = {}
        self._id_stats: Dict[int, Dict[str, Any]] = {}
        self._id_data_variants: Dict[int, Set[bytes]] = {}
        # История фреймов по каждому ID (время, data, rtr, dlc) — для
        # диалога «История ID» из контекстного меню таблицы.
        self._id_history: Dict[int, deque] = {}
        self._history_dialogs: List[IdHistoryDialog] = []
        self._highlight_timers: Dict[int, QTimer] = {}
        self._ignored_ids: set[int] = set()
        # Предыдущие значения счётчиков ошибок — для визуальных предупреждений
        # (чек-лист 4.2): bus-off/рост потерь подсвечивают строку статуса.
        self._prev_busoff = 0
        self._prev_lost = 0
        self._warn_level = 0  # 0=ok, 1=lost, 2=bus-off

        self._create_widgets()
        self._layout_widgets()
        self._setup_timers()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)
        self._font = font

        compact_font = QFont("Segoe UI", 9)

        self._start_button = QPushButton(tr("Запустить"))
        self._start_button.setFixedSize(80, 28)
        self._start_button.setFont(compact_font)
        self._start_button.clicked.connect(self._start)

        self._stop_button = QPushButton(tr("Остановить"))
        self._stop_button.setFixedSize(90, 28)
        self._stop_button.setFont(compact_font)
        self._stop_button.clicked.connect(self._stop)

        self._clear_button = QPushButton(tr("Очистить"))
        setup_button(self._clear_button, height=28)
        self._clear_button.setFont(compact_font)
        self._clear_button.clicked.connect(self._clear)

        self._update_monitor_buttons()

        self._search_edit = QLineEdit()
        self._search_edit.setFixedWidth(160)
        self._search_edit.setFont(font)
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._search_edit.textChanged.connect(self._apply_search)

        self._filter_rules: List[Dict[str, Any]] = []
        self._filter_enabled = False
        self._highlight_interval_ms = 500

        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("Счётчик"), tr("ASCII"), tr("Пояснение")]
        )
        self._table.setFont(font)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        # Двойной правый клик по строке ID → сразу «История ID» (без меню).
        # Штатный MouseButtonDblClick до нас не доезжает: первый правый
        # клик уже открыл контекстное меню, и второй уходит в него.
        # Поэтому считаем два правых клика по одной строке сами — в
        # _show_context_menu второй клик за 600 мс открывает историю
        # вместо меню.
        self._last_rclick_time = 0.0
        self._last_rclick_row = -1
        self._table.viewport().installEventFilter(self)
        # Двойной клик ЛЕВОЙ кнопкой по строке — та же история ID
        # (таблица логирования, онлайн-график %, развёртка, инверсия).
        self._table.cellDoubleClicked.connect(
            lambda row, _col: self._show_id_history(row)
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 210)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 80)
        self._table.setColumnWidth(5, 90)
        self._table.setColumnWidth(6, 170)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(200)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._send_bit_combo = QComboBox()
        self._send_bit_combo.setFont(font)
        self._send_bit_combo.addItems(BIT_RATES)
        self._send_bit_combo.setFixedWidth(90)

        self._send_id_edit = IdPasteEdit()
        self._send_id_edit.setFixedWidth(90)
        self._send_id_edit.setMaxLength(8)
        self._send_id_edit.setFont(font)
        self._send_id_edit.setPlaceholderText("ID")
        self._send_id_validator = _IdValidator(self._send_id_edit, self._send_bit_combo)
        self._send_id_edit.set_fill_callback(self._fill_send_from_packet)

        self._send_dlc_spin = QSpinBox()
        self._send_dlc_spin.setRange(1, 8)
        self._send_dlc_spin.setValue(8)
        self._send_dlc_spin.setFont(font)
        self._send_dlc_spin.setFixedWidth(45)

        self._send_data_edits, self._send_data_widget = create_data_field_widget(font, 8, edit_width=46)

        self._send_copy_paste = create_clipboard_buttons(
            self, self._send_id_edit, self._send_dlc_spin, self._send_data_edits, self._send_bit_combo
        )

        self._send_period_spin = QSpinBox()
        self._send_period_spin.setRange(0, 9999)
        self._send_period_spin.setValue(1000)
        self._send_period_spin.setSuffix(tr(" мс"))
        self._send_period_spin.setFont(font)
        self._send_period_spin.setMinimumWidth(90)

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setMinimumWidth(100)
        self._send_button.setFixedHeight(28)
        self._send_button.setFont(font)
        self._send_button.clicked.connect(self._send_manual)

        self._cyclic_button = QPushButton("∞")
        self._cyclic_button.setFixedSize(56, 36)
        self._cyclic_button.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        self._cyclic_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )
        self._cyclic_button.setToolTip(tr("Циклически"))
        setCheckableWithIndicator(self._cyclic_button)
        self._cyclic_button.toggled.connect(self._on_cyclic_toggled)

        self._rtr_button = QPushButton(tr("RTR"))
        self._rtr_button.setFixedSize(50, 36)
        self._rtr_button.setFont(QFont("Arial", 7, QFont.Weight.Bold))
        self._rtr_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )
        self._rtr_button.setToolTip(tr("Remote Transmission Request"))
        setCheckableWithIndicator(self._rtr_button)
        self._rtr_button.toggled.connect(self._on_rtr_toggled)

        self._cyclic_timer = QTimer(self)
        self._cyclic_timer.timeout.connect(self._send_cyclic_frame)

        self._send_dlc_spin.valueChanged.connect(self._on_send_dlc_changed)
        self._on_send_dlc_changed(self._send_dlc_spin.value())

        self._stats_label = QLabel(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._stats_label.setFont(font)

    def _layout_widgets(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        layout.setContentsMargins(4, 4, 4, 4)

        control_layout = QHBoxLayout()
        control_layout.setSpacing(4)
        control_layout.addWidget(self._start_button)
        control_layout.addWidget(self._stop_button)
        control_layout.addWidget(self._clear_button)
        control_layout.addWidget(QLabel(tr("Поиск:")))
        control_layout.addWidget(self._search_edit)
        control_layout.addStretch()
        layout.addLayout(control_layout)

        layout.addWidget(self._table, 1)

        send_layout = QVBoxLayout()
        send_layout.setSpacing(4)

        send_top = QHBoxLayout()
        send_top.setSpacing(4)
        send_top.addWidget(QLabel(tr("Бит")))
        send_top.addWidget(self._send_bit_combo)
        send_top.addWidget(QLabel(tr("ID")))
        send_top.addWidget(self._send_id_edit)
        send_top.addWidget(QLabel(tr("DLC")))
        send_top.addWidget(self._send_dlc_spin)
        send_top.addWidget(QLabel(tr("Data")))
        send_top.addWidget(self._send_data_widget)
        send_top.addWidget(self._send_copy_paste)
        send_top.addStretch()
        self._send_top_layout = send_top

        send_bottom = QHBoxLayout()
        send_bottom.setSpacing(4)
        send_bottom.addWidget(QLabel(tr("Период")))
        send_bottom.addWidget(self._send_period_spin)
        send_bottom.addWidget(self._send_button)
        send_bottom.addWidget(self._cyclic_button)
        send_bottom.addWidget(self._rtr_button)
        send_bottom.addStretch()

        self._sent_label = QLabel(tr("Отправлено: 0"))
        self._sent_label.setFont(self._font)

        send_layout.addLayout(send_top)
        send_layout.addLayout(send_bottom)
        send_layout.addWidget(self._sent_label)
        layout.addLayout(send_layout)

        layout.addWidget(self._stats_label)

    def _setup_timers(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_stats)
        self._timer.start(1000)

    def _on_send_dlc_changed(self, value: int) -> None:
        rtr = self._rtr_button.isChecked()
        for i, edit in enumerate(self._send_data_edits):
            if i >= value or rtr:
                edit.setText("")
                edit.setEnabled(False)
            else:
                edit.setEnabled(True)

    def _fill_send_from_packet(self, parsed: Dict[str, Any]) -> None:
        """Заполняет панель отправки из распарсенного пакета."""
        can_id = parsed.get("id")
        if can_id is None:
            return
        self._send_bit_combo.setCurrentIndex(1 if can_id > 0x7FF else 0)
        self._send_id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        dlc = max(1, min(8, parsed.get("dlc", 8)))
        self._send_dlc_spin.setValue(dlc)
        data = parsed.get("data", [])
        for i, edit in enumerate(self._send_data_edits):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")
        self._on_send_dlc_changed(dlc)

    def _on_cyclic_toggled(self, checked: bool) -> None:
        if checked:
            self._cyclic_button.setStyleSheet("background-color: #4CAF50; color: #FFFFFF;")
            self._send_manual()
        else:
            self._cyclic_button.setStyleSheet("")
            self._stop_cyclic_timer()

    def _on_rtr_toggled(self, checked: bool) -> None:
        if checked:
            self._rtr_button.setStyleSheet(
                "QPushButton { background-color: #FF9800; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #F57C00; }"
            )
            self._send_data_widget.setEnabled(False)
            opacity = QGraphicsOpacityEffect(self._send_data_widget)
            opacity.setOpacity(0.35)
            self._send_data_widget.setGraphicsEffect(opacity)
        else:
            self._rtr_button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )
            self._send_data_widget.setEnabled(True)
            self._send_data_widget.setGraphicsEffect(None)

    def _start(self) -> None:
        if not self._serial_manager.is_open():
            port = self._config.get("port", "")
            baudrate = self._config.get("baudrate", 115200)
            emulation = self._config.get("emulation", False)
            error_probability = self._config.get("error_probability", 0)
            if not port:
                QMessageBox.warning(self, tr("Внимание"), tr("Устройство не подключено"))
                return
            if not self._serial_manager.open_port(
                port, baudrate, emulation, auto_reconnect=True,
                error_probability=error_probability,
            ):
                QMessageBox.warning(self, tr("Внимание"), tr("Устройство не подключено"))
                return
        self._running = True
        self._update_monitor_buttons()
        self.monitoring_state_changed.emit(self._channel, True)
        logger.info("Мониторинг CAN%d запущен", self._channel)

    def _stop(self) -> None:
        self._running = False
        self._cyclic_button.setChecked(False)
        self._stop_cyclic_timer()
        self._update_monitor_buttons()
        self.monitoring_state_changed.emit(self._channel, False)
        logger.info("Мониторинг CAN%d остановлен", self._channel)

    def _update_monitor_buttons(self) -> None:
        """Визуально отображает текущее состояние мониторинга."""
        if self._running:
            self._start_button.setText(tr("Запущено"))
            self._start_button.setEnabled(False)
            self._start_button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: #FFFFFF; border: none; border-radius: 4px; }"
            )
            self._stop_button.setText(tr("Остановить"))
            self._stop_button.setEnabled(True)
            self._stop_button.setStyleSheet("")
        else:
            self._start_button.setText(tr("Запустить"))
            self._start_button.setEnabled(True)
            self._start_button.setStyleSheet("")
            self._stop_button.setText(tr("Остановлено"))
            self._stop_button.setEnabled(False)
            self._stop_button.setStyleSheet(
                "QPushButton { background-color: #F44336; color: #FFFFFF; border: none; border-radius: 4px; }"
            )

    def _clear(self) -> None:
        self._table.setRowCount(0)
        self._id_to_row.clear()
        self._id_stats.clear()
        self._id_data_variants.clear()
        self._id_history.clear()
        for timer in self._highlight_timers.values():
            timer.stop()
        self._highlight_timers.clear()
        self._received_count = 0
        self._sent_count = 0
        self._packet_times.clear()
        self._last_packet_time = None
        self._prev_busoff = 0
        self._prev_lost = 0
        self._warn_level = 0
        self._stats_label.setStyleSheet("")
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._update_sent_label()

    def _apply_search(self, text: str = "") -> None:
        query = text.strip().lower()
        for row in range(self._table.rowCount()):
            hidden = bool(query)
            if query:
                for col in range(self._table.columnCount()):
                    item = self._table.item(row, col)
                    if item is not None and query in item.text().lower():
                        hidden = False
                        break
            self._table.setRowHidden(row, hidden)

    def _update_sent_label(self) -> None:
        self._sent_label.setText(tr("Отправлено: {0}").format(self._sent_count))

    def _send_manual(self) -> None:
        can_id = hex_to_int(self._send_id_edit.text())
        if can_id is None:
            return
        dlc = self._send_dlc_spin.value()
        data = self._data_from_send_edits(dlc)
        rtr = self._rtr_button.isChecked() if self._rtr_button is not None else False
        self._cyclic_frame = pack_can_frame(self._channel_byte, can_id, data, rtr=rtr, dlc=dlc)
        if self._send_cyclic_frame():
            if self._cyclic_button.isChecked():
                self._start_cyclic_timer()

    def _data_from_send_edits(self, dlc: int) -> bytes:
        values = [edit.text() for edit in self._send_data_edits[:dlc]]
        parsed = parse_data_bytes(values)
        return bytes(parsed[:dlc])

    def _start_cyclic_timer(self) -> None:
        interval_ms = max(10, self._send_period_spin.value())
        self._cyclic_timer.start(interval_ms)

    def _stop_cyclic_timer(self) -> None:
        if self._cyclic_timer.isActive():
            self._cyclic_timer.stop()

    def _send_cyclic_frame(self) -> bool:
        if self._cyclic_frame is None:
            return False
        result = self._serial_manager.send_data(self._cyclic_frame)
        if result:
            self._sent_count += 1
            self._update_sent_label()
        return result

    def _update_stats(self) -> None:
        now = time.time()
        while self._packet_times and now - self._packet_times[0] > 1.0:
            self._packet_times.popleft()
        speed = len(self._packet_times)
        text = tr("Принято: {0} | Скорость: {1} пак/с").format(self._received_count, speed)
        try:
            if self._serial_manager.is_open() and not self._config.get("emulation", False):
                device = self._serial_manager.read_can_stats(self._channel)
                ready = tr("OK") if device.get("ready") else tr("INIT FAIL")
                text += tr(" | Ready: {0} | RX: {1} TX: {2} Потеряно: {3} Errors: {4} Bus-off: {5} Recovery: {6}").format(
                    ready,
                    device["rx_count"],
                    device["tx_count"],
                    device["lost_count"],
                    device["error_count"],
                    device["busoff_count"],
                    device["recovery_count"],
                )
                usb = self._serial_manager.read_usb_stats()
                text += tr(" USB dropped: {0}").format(usb["tx_dropped"])
                self._update_error_warnings(device)
        except Exception:  # noqa: BLE001
            pass
        if self._warn_level == 2:
            text += tr("  ⚠ BUS-OFF — проверьте шину/терминацию")
        elif self._warn_level == 1:
            text += tr("  ⚠ Потеряны кадры CAN")
        self._stats_label.setText(text)

    def _update_error_warnings(self, device: Dict[str, int]) -> None:
        """Подсвечивает строку статуса при bus-off или росте потерь кадров."""
        busoff = int(device.get("busoff_count", 0))
        lost = int(device.get("lost_count", 0))
        if busoff > self._prev_busoff:
            self._warn_level = 2
            logger.warning("CAN%d: bus-off (всего %d)", self._channel, busoff)
        elif lost > self._prev_lost and self._warn_level < 2:
            self._warn_level = 1
            logger.warning("CAN%d: потеряны кадры (всего %d)", self._channel, lost)
        self._prev_busoff = busoff
        self._prev_lost = lost
        if self._warn_level == 2:
            self._stats_label.setStyleSheet("color: #FFFFFF; background-color: #C62828;")
        elif self._warn_level == 1:
            self._stats_label.setStyleSheet("color: #FFFFFF; background-color: #E65100;")
        else:
            self._stats_label.setStyleSheet("")

    def _format_signals(self, can_id: int, data: bytes) -> str:
        db = self._dbc_manager.get_cantools_db()
        if db is None:
            return ""
        decoded = decode_frame(db, can_id, data)
        if decoded is None:
            return ""
        parts = []
        for name, info in list(decoded.items())[:3]:
            if isinstance(info, dict):
                parts.append(f"{name}={info['value']:.2f}{info.get('unit', '')}")
            else:
                parts.append(f"{name}={info}")
        return " | ".join(parts)

    def _format_period(self, can_id: int, now: float) -> str:
        stats = self._id_stats.get(can_id)
        if stats is None or stats.get("last_time") is None:
            return ""
        period_ms = int((now - stats["last_time"]) * 1000)
        return f"{period_ms} ms"

    def _build_row_items(
        self, frame_id: int, dlc: int, data: bytes, rtr: bool, timestamp: str, period: str, count: int
    ) -> List[str]:
        id_width = 8 if frame_id > 0x7FF else 3
        signals = "" if rtr else self._format_signals(frame_id, data)
        return [
            int_to_hex(frame_id, id_width),
            str(dlc),
            "rtr" if rtr else " ".join(format_data_bytes(data)),
            period,
            str(count),
            "" if rtr else _ascii_from_data(data),
            signals,
        ]

    def add_frame(self, frame: Dict[str, object]) -> None:
        if not self._running:
            return
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        rtr = bool(frame.get("rtr", False))
        dlc = int(frame.get("dlc", len(data)))
        if self._filter_enabled and self._matches_filter(frame_id, data):
            return

        self._received_count += 1
        self._packet_times.append(time.time())
        self._last_packet_time = time.time()

        now = time.time()
        stats = self._id_stats.setdefault(frame_id, {"count": 0, "last_time": None, "last_data": b"", "last_receive_time": None})
        stats["count"] += 1
        period = self._format_period(frame_id, now)
        prev_data = stats.get("last_data")
        stats["last_time"] = now

        timestamp = time.strftime("%H:%M:%S") + f".{int((now % 1) * 1000):03d}"
        items = self._build_row_items(frame_id, dlc, data, rtr, timestamp, period, stats["count"])

        tooltip = ""
        if not rtr and self._dbc_manager.is_loaded():
            tooltip = self._dbc_manager.describe_frame(frame_id, data)

        if frame_id in self._id_to_row:
            row = self._id_to_row[frame_id]
            for col, text in enumerate(items):
                item = self._table.item(row, col)
                if item is None:
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._table.setItem(row, col, item)
                else:
                    item.setText(text)
                if tooltip:
                    item.setToolTip(tooltip)
            # Подсветка изменившихся данных: DATA того же ID изменилась →
            # фон ячейки Data светлее на «Интервал подсветки» сверху.
            if prev_data is not None and prev_data != data and self._highlight_interval_ms > 0:
                self._highlight_data_cell(row)
        else:
            if self._table.rowCount() >= MAX_TABLE_ROWS:
                last_row = self._table.rowCount() - 1
                id_item = self._table.item(last_row, 0)
                if id_item is not None:
                    fid = hex_to_int(id_item.text())
                    if fid is not None:
                        self._id_to_row.pop(fid, None)
                        self._id_stats.pop(fid, None)
                self._table.removeRow(last_row)
                for fid, r in list(self._id_to_row.items()):
                    if r >= last_row:
                        self._id_to_row[fid] = r - 1
            row = self._find_insert_row(frame_id)
            self._table.insertRow(row)
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if tooltip:
                    item.setToolTip(tooltip)
                self._table.setItem(row, col, item)
            for fid, r in list(self._id_to_row.items()):
                if r >= row:
                    self._id_to_row[fid] = r + 1
            self._id_to_row[frame_id] = row

        stats["last_receive_time"] = now
        stats["last_data"] = data
        self._id_data_variants.setdefault(frame_id, set()).add(data)
        self._id_history.setdefault(frame_id, deque(maxlen=2000)).append((now, data, rtr, dlc))
        for dialog in self._history_dialogs:
            if dialog.can_id == frame_id:
                dialog.add_sample(now, data, rtr, dlc)

        self._table.scrollToBottom()

    def _matches_filter(self, frame_id: int, data: bytes) -> bool:
        if frame_id in self._ignored_ids:
            return True

        allow_rules = [r for r in self._filter_rules if r.get("mode") == "show"]
        hide_rules = [r for r in self._filter_rules if r.get("mode") != "show"]

        if allow_rules:
            if not self._rule_matches(allow_rules, frame_id, data):
                return True

        if self._rule_matches(hide_rules, frame_id, data):
            return True

        return False

    def _rule_matches(self, rules: List[Dict[str, Any]], frame_id: int, data: bytes) -> bool:
        for rule in rules:
            id_from = rule.get("id_from")
            id_to = rule.get("id_to")
            if id_from is not None and id_to is not None:
                if not (id_from <= frame_id <= id_to):
                    continue
            elif id_from is not None and frame_id != id_from:
                continue

            from_bytes = rule.get("data_from", b"")
            to_bytes = rule.get("data_to", b"")
            length = min(len(data), len(from_bytes), len(to_bytes))
            if length == 0 and (len(from_bytes) > 0 or len(to_bytes) > 0):
                continue
            match = True
            for i in range(length):
                if not (from_bytes[i] <= data[i] <= to_bytes[i]):
                    match = False
                    break
            if match:
                return True
        return False

    def _find_insert_row(self, frame_id: int) -> int:
        for row in range(self._table.rowCount()):
            id_item = self._table.item(row, 0)
            if id_item is None:
                continue
            existing = hex_to_int(id_item.text())
            if existing is not None and existing < frame_id:
                return row
        return self._table.rowCount()

    def _highlight_data_cell(self, row: int) -> None:
        if row in self._highlight_timers:
            self._highlight_timers[row].stop()
            del self._highlight_timers[row]
        data_item = self._table.item(row, 2)
        if data_item is None:
            return
        data_item.setBackground(QColor("#464672"))
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda r=row: self._reset_data_background(r))
        # Длительность — из поля «Интервал подсветки» сверху, а не
        # жёсткие 500 мс (поле раньше ни на что не влияло).
        timer.start(max(50, self._highlight_interval_ms))
        self._highlight_timers[row] = timer

    def _reset_data_background(self, row: int) -> None:
        data_item = self._table.item(row, 2)
        if data_item is not None:
            data_item.setBackground(QColor())
        self._highlight_timers.pop(row, None)

    def _show_filter_dialog(self) -> None:
        """Переключено на глобальное управление фильтром в CanMonitorTab."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Двойной клик ПКМ по строке таблицы открывает историю ID."""
        from PySide6.QtCore import QEvent

        if (
            watched is self._table.viewport()
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.RightButton
        ):
            row = self._table.rowAt(event.position().toPoint().y())
            if row >= 0:
                self._show_id_history(row)
                return True
        return super().eventFilter(watched, event)

    def _show_context_menu(self, position) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        # Второй правый клик подряд по той же строке — это ТЗ-шный
        # «двойной ПКМ»: открываем историю ID вместо меню.
        now = time.monotonic()
        if row == self._last_rclick_row and now - self._last_rclick_time < 0.6:
            self._last_rclick_row = -1
            self._show_id_history(row)
            return
        self._last_rclick_time = now
        self._last_rclick_row = row
        menu = QMenu(self)
        menu.addAction(tr("Копировать ID"), lambda: self._copy_selected_id(row))
        menu.addAction(tr("Копировать данные"), lambda: self._copy_selected_data(row))
        menu.addAction(tr("Копировать всю строку"), lambda: self._copy_selected_row(row))
        menu.addAction(tr("Создать триггер"), lambda: self._create_trigger_from_row(row))
        menu.addAction(tr("Показать варианты данных"), lambda: self._show_data_variants(row))
        menu.addAction(tr("Битовая карта"), lambda: self._show_bitmap(row))
        menu.addAction(tr("История ID…"), lambda: self._show_id_history(row))
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _copy_selected_id(self, row: int) -> None:
        item = self._table.item(row, 0)
        if item is not None:
            QApplication.clipboard().setText(item.text())

    def _copy_selected_data(self, row: int) -> None:
        item = self._table.item(row, 2)
        if item is not None:
            QApplication.clipboard().setText(item.text())

    def _copy_selected_row(self, row: int) -> None:
        values = [self._table.item(row, col).text() if self._table.item(row, col) is not None else "" for col in range(self._table.columnCount())]
        QApplication.clipboard().setText("  ".join(values))

    def _create_trigger_from_row(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        data_item = self._table.item(row, 2)
        if id_item is None:
            return
        data_values = data_item.text().split() if data_item is not None else []
        self.create_trigger_requested.emit({"id": id_item.text(), "data": data_values})

    def _show_data_variants(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        variants = self._id_data_variants.get(can_id, set())
        if not variants:
            QMessageBox.information(self, tr("Информация"), tr("Нет вариантов данных для этого ID"))
            return
        dialog = DataVariantsDialog(can_id, variants, self)
        dialog.exec()

    def _show_id_history(self, row: int) -> None:
        """Открывает диалог истории по ID из строки таблицы."""
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        samples = list(self._id_history.get(can_id, ()))
        dialog = IdHistoryDialog(can_id, self._channel, samples, self)
        self._history_dialogs.append(dialog)
        dialog.finished.connect(
            lambda *_a, d=dialog: self._history_dialogs.remove(d)
            if d in self._history_dialogs else None
        )
        dialog.show()

    def _show_bitmap(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        data_item = self._table.item(row, 2)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        data_values = data_item.text().split() if data_item is not None else []
        data = bytes(parse_data_bytes(data_values))
        dialog = BitmapDialog(can_id, data, self)
        dialog.exec()

    def set_dbc(self, dbc) -> None:
        """Уведомляет канал о смене DBC."""
        self._apply_search(self._search_edit.text())

    def set_filter(self, enabled: bool, rules: List[Dict[str, Any]], ignored_ids: List[int], interval_ms: int) -> None:
        """Устанавливает правила фильтрации и интервал подсветки."""
        self._filter_enabled = enabled
        self._filter_rules = rules
        self._ignored_ids = set(ignored_ids)
        self._highlight_interval_ms = max(0, interval_ms)

    def get_known_ids(self) -> List[int]:
        """Возвращает список ID, которые уже были получены в канале."""
        return list(self._id_to_row.keys())

    def retranslate_ui(self) -> None:
        """Обновляет статические строки панели мониторинга канала."""
        self._clear_button.setText(tr("Очистить"))
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("Счётчик"), tr("ASCII"), tr("Пояснение")]
        )
        self._send_button.setText(tr("Отправить"))
        self._cyclic_button.setToolTip(tr("Циклически"))
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._sent_label.setText(tr("Отправлено: 0"))
        self._update_monitor_buttons()


class CanMonitorTab(QWidget):
    """Вкладка мониторинга CAN с двумя каналами."""

    create_trigger_requested = Signal(dict)

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._recording = False
        self._csv_file: Optional[TextIO] = None
        self._csv_writer: Optional[csv.writer] = None
        self._csv_path: Optional[Path] = None
        self._dbc_manager = DBCManager()
        self._memory_indicator = MemoryIndicator(self)
        self._create_widgets()
        self._layout_widgets()

    def _create_widgets(self) -> None:
        compact_font = QFont("Segoe UI", 9)

        self._filter_button = QPushButton(tr("Фильтр"))
        self._filter_button.setFixedSize(80, 28)
        self._filter_button.setFont(compact_font)
        self._filter_button.clicked.connect(self._show_filter_dialog)

        self._highlight_interval_spin = QSpinBox()
        self._highlight_interval_spin.setRange(0, 9999)
        self._highlight_interval_spin.setValue(500)
        self._highlight_interval_spin.setSuffix(tr(" мс"))
        self._highlight_interval_spin.setFont(compact_font)
        self._highlight_interval_spin.valueChanged.connect(self._on_highlight_interval_changed)

        self._can1_speed_label = QLabel(tr("Скорость CAN1"))
        self._can1_speed_label.setFont(compact_font)
        self._can1_speed_combo = QComboBox()
        self._can1_speed_combo.setFont(compact_font)
        self._can1_speed_combo.setEditable(True)
        self._can1_speed_combo.setFixedWidth(100)
        for preset in ["33.3", "50", "100", "125", "250", "500", "800", "1000"]:
            self._can1_speed_combo.addItem(preset)
        self._can1_speed_combo.setMaxVisibleItems(12)
        self._can1_speed_combo.lineEdit().setValidator(QDoubleValidator(0.1, 10000.0, 1, self))
        self._can1_speed_combo.lineEdit().setPlaceholderText(tr("кбит/с"))

        self._can1_speed_button = QPushButton()
        self._can1_speed_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        self._fit_speed_button(self._can1_speed_button, self._can1_speed_combo)
        self._can1_speed_button.setToolTip(tr("Выбрать из списка"))
        self._can1_speed_button.clicked.connect(self._can1_speed_combo.showPopup)

        self._can1_terminator_check = QPushButton(tr("120 Ом"))
        self._can1_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can1_terminator_check.setFont(compact_font)
        self._can1_terminator_check.setCheckable(True)
        self._can1_terminator_check.setChecked(self._config.get("can1_terminator", False))
        self._can1_terminator_check.toggled.connect(self._on_can1_terminator_toggled)
        self._update_terminator_style(self._can1_terminator_check)

        self._can2_speed_label = QLabel(tr("Скорость CAN2"))
        self._can2_speed_label.setFont(compact_font)
        self._can2_speed_combo = QComboBox()
        self._can2_speed_combo.setFont(compact_font)
        self._can2_speed_combo.setEditable(True)
        self._can2_speed_combo.setFixedWidth(100)
        for preset in ["33.3", "50", "100", "125", "250", "500", "800", "1000"]:
            self._can2_speed_combo.addItem(preset)
        self._can2_speed_combo.setMaxVisibleItems(12)
        self._can2_speed_combo.lineEdit().setValidator(QDoubleValidator(0.1, 10000.0, 1, self))
        self._can2_speed_combo.lineEdit().setPlaceholderText(tr("кбит/с"))

        self._can2_speed_button = QPushButton()
        self._can2_speed_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        self._fit_speed_button(self._can2_speed_button, self._can2_speed_combo)
        self._can2_speed_button.setToolTip(tr("Выбрать из списка"))
        self._can2_speed_button.clicked.connect(self._can2_speed_combo.showPopup)

        self._can2_terminator_check = QPushButton(tr("120 Ом"))
        self._can2_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can2_terminator_check.setFont(compact_font)
        self._can2_terminator_check.setCheckable(True)
        self._can2_terminator_check.setChecked(self._config.get("can2_terminator", False))
        self._can2_terminator_check.toggled.connect(self._on_can2_terminator_toggled)
        self._update_terminator_style(self._can2_terminator_check)

        self._sleep_mode_label = QLabel(tr("Переход в режим сна"))
        self._sleep_mode_label.setFont(compact_font)
        self._sleep_time_spin = QSpinBox()
        self._sleep_time_spin.setRange(0, 9999)
        self._sleep_time_spin.setSuffix(tr(" с"))
        self._sleep_time_spin.setFont(compact_font)
        self._sleep_time_spin.setFixedWidth(80)
        self._sleep_time_spin.setValue(self._config.get("sleep_time", 0))
        self._sleep_time_spin.valueChanged.connect(self._on_sleep_time_changed)

        self._sleep_mode_combo = QComboBox()
        self._sleep_mode_combo.setFont(compact_font)
        self._sleep_mode_combo.setFixedWidth(140)
        self._sleep_mode_combo.addItems([
            tr("Не переходить в сон"),
            tr("Слип мод"),
            tr("Стоп мод"),
        ])
        self._sleep_mode_combo.setCurrentIndex(self._config.get("sleep_mode", 0))
        self._sleep_mode_combo.currentIndexChanged.connect(self._on_sleep_mode_changed)
        self._update_sleep_time_state(self._sleep_mode_combo.currentIndex())

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._monitor1 = CanChannelMonitor(1, self._serial_manager, self)
        self._monitor2 = CanChannelMonitor(2, self._serial_manager, self)
        self._monitor1.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor2.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor1.monitoring_state_changed.connect(self._on_monitor_state_changed)
        self._monitor2.monitoring_state_changed.connect(self._on_monitor_state_changed)
        self._splitter.addWidget(self._monitor1)
        self._splitter.addWidget(self._monitor2)
        self._splitter.setSizes([450, 450])
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)

        self._can1_speed_combo.setCurrentText(self._format_speed(self._config.get("can1_speed", 500000)))
        self._can1_speed_combo.currentIndexChanged.connect(self._on_can1_speed_changed)
        self._can1_speed_combo.lineEdit().editingFinished.connect(self._on_can1_speed_changed)

        self._can2_speed_combo.setCurrentText(self._format_speed(self._config.get("can2_speed", 500000)))
        self._can2_speed_combo.currentIndexChanged.connect(self._on_can2_speed_changed)
        self._can2_speed_combo.lineEdit().editingFinished.connect(self._on_can2_speed_changed)

    def _layout_widgets(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self._filter_button)
        buttons_layout.addWidget(QLabel(tr("Интервал подсветки")))
        buttons_layout.addWidget(self._highlight_interval_spin)
        buttons_layout.addWidget(self._can1_speed_label)
        buttons_layout.addWidget(self._can1_speed_combo)
        buttons_layout.addWidget(self._can1_speed_button)
        buttons_layout.addWidget(self._can1_terminator_check)
        buttons_layout.addWidget(self._can2_speed_label)
        buttons_layout.addWidget(self._can2_speed_combo)
        buttons_layout.addWidget(self._can2_speed_button)
        buttons_layout.addWidget(self._can2_terminator_check)
        buttons_layout.addSpacing(16)
        buttons_layout.addWidget(self._sleep_mode_label)
        buttons_layout.addWidget(self._sleep_time_spin)
        buttons_layout.addWidget(self._sleep_mode_combo)
        buttons_layout.addStretch()
        layout.addLayout(buttons_layout)
        layout.addWidget(self._splitter)
        layout.addWidget(self._memory_indicator)

    def _show_filter_dialog(self) -> None:
        """Открывает диалог фильтра и применяет настройки к обоим каналам."""
        first = self._monitor1._filter_rules
        enabled = self._monitor1._filter_enabled
        ignored = list(self._monitor1._ignored_ids)
        dialog = FilterDialog(first, enabled, ignored, self._get_known_ids, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_result()
        if result is None:
            return
        interval = self._highlight_interval_spin.value()
        self._monitor1.set_filter(result["enabled"], result["rules"], result["ignored_ids"], interval)
        self._monitor2.set_filter(result["enabled"], result["rules"], result["ignored_ids"], interval)
        self._refresh_memory_indicator()

    def _refresh_memory_indicator(self) -> None:
        """«Память» показывает долю Flash-страницы триггеров — как во
        вкладке «Триггеры», чтобы в мониторинге не было ложного 0%."""
        from core.trigger_protocol import count_configured_triggers

        self._memory_indicator.show_trigger_usage(
            count_configured_triggers(self._config.get("triggers", []))
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_memory_indicator()

    def _get_known_ids(self) -> List[int]:
        return list(set(self._monitor1.get_known_ids() + self._monitor2.get_known_ids()))

    def _on_highlight_interval_changed(self, value: int) -> None:
        self._monitor1._highlight_interval_ms = value
        self._monitor2._highlight_interval_ms = value

    def _format_speed(self, speed_bps: int) -> str:
        """Форматирует скорость в кбит/с для отображения в комбобоксе."""
        if speed_bps == 33300:
            return "33.3"
        if speed_bps and speed_bps % 1000 == 0:
            return str(speed_bps // 1000)
        if speed_bps:
            return f"{speed_bps / 1000:.1f}"
        return "500"

    def _on_can1_speed_changed(self) -> None:
        try:
            speed_kbps = float(self._can1_speed_combo.currentText().strip() or "500")
        except ValueError:
            speed_kbps = 500.0
        self._config.set("can1_speed", max(1000, int(round(speed_kbps * 1000))))

    def _on_can2_speed_changed(self) -> None:
        try:
            speed_kbps = float(self._can2_speed_combo.currentText().strip() or "500")
        except ValueError:
            speed_kbps = 500.0
        self._config.set("can2_speed", max(1000, int(round(speed_kbps * 1000))))

    @staticmethod
    def _fit_speed_button(button: QPushButton, combo: QComboBox) -> None:
        """Кнопка списка скоростей со значком «стрелка вниз»: чуть ниже
        поля ввода, ширина с запасом под иконку."""
        height = combo.sizeHint().height()
        if height <= 0:
            height = 26
        height = max(18, height - 6)
        button.setFixedSize(height + 8, height)

    @staticmethod
    def _update_terminator_style(button: QPushButton) -> None:
        """Цветовая индикация включённого терминатора 120 Ом — зелёная кнопка."""
        if button.isChecked():
            button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: #FFFFFF; border: none; border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #45A049; }"
            )
        else:
            button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )

    def _on_can1_terminator_toggled(self, checked: bool) -> None:
        self._config.set("can1_terminator", checked)
        self._update_terminator_style(self._can1_terminator_check)
        self._apply_can_mode(1)

    def _on_can2_terminator_toggled(self, checked: bool) -> None:
        self._config.set("can2_terminator", checked)
        self._update_terminator_style(self._can2_terminator_check)
        self._apply_can_mode(2)

    def _on_monitor_state_changed(self, channel: int, running: bool) -> None:
        """При запуске мониторинга применяет текущий режим и терминатор."""
        if running:
            self._apply_can_mode(channel)

    def _apply_can_mode(self, channel: int) -> None:
        """Отправляет в МК режим Normal и состояние терминатора для канала."""
        if not self._serial_manager.is_open():
            return
        term = bool(self._config.get(f"can{channel}_terminator", False))
        try:
            self._serial_manager.set_can_mode(channel, 0, term)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось установить режим CAN%d: %s", channel, exc)

    def _on_sleep_time_changed(self, value: int) -> None:
        self._config.set("sleep_time", value)

    def _on_sleep_mode_changed(self, index: int) -> None:
        self._config.set("sleep_mode", index)
        self._update_sleep_time_state(index)

    def _update_sleep_time_state(self, index: int) -> None:
        """Активирует/деактивирует поле времени сна в зависимости от режима."""
        enabled = index != 0
        self._sleep_time_spin.setEnabled(enabled)
        effect = self._sleep_time_spin.graphicsEffect()
        if enabled:
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(1.0)
            self._sleep_time_spin.setStyleSheet("")
        else:
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(self._sleep_time_spin)
                self._sleep_time_spin.setGraphicsEffect(effect)
            effect.setOpacity(0.5)
            self._sleep_time_spin.setStyleSheet("QSpinBox { color: #888888; }")

    def process_frame(self, frame: Dict[str, object]) -> None:
        channel = int(frame["channel"])
        if channel == 1:
            self._monitor1.add_frame(frame)
        elif channel == 2:
            self._monitor2.add_frame(frame)
        self._write_frame_to_csv(frame)

    def set_dbc(self, dbc) -> None:
        """Уведомляет вкладку о смене DBC."""
        for monitor in (self._monitor1, self._monitor2):
            monitor.set_dbc(dbc)

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки мониторинга."""
        self._filter_button.setText(tr("Фильтр"))
        self._can1_speed_label.setText(tr("Скорость CAN1"))
        self._can1_terminator_check.setText(tr("120 Ом"))
        self._can1_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can2_speed_label.setText(tr("Скорость CAN2"))
        self._can2_terminator_check.setText(tr("120 Ом"))
        self._can2_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._sleep_mode_label.setText(tr("Переход в режим сна"))
        self._sleep_time_spin.setSuffix(tr(" с"))
        index = self._sleep_mode_combo.currentIndex()
        self._sleep_mode_combo.clear()
        self._sleep_mode_combo.addItems([
            tr("Не переходить в сон"),
            tr("Слип мод"),
            tr("Стоп мод"),
        ])
        self._sleep_mode_combo.setCurrentIndex(index)
        self._monitor1.retranslate_ui()
        self._monitor2.retranslate_ui()

    def _start_recording(self, path: str) -> None:
        try:
            self._csv_path = Path(path)
            self._csv_file = open(self._csv_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["timestamp", "channel", "id", "dlc", "data"])
            self._recording = True
            logger.info("Потоковая запись CAN начата: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка открытия CSV: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть файл: {0}").format(exc))

    def _stop_recording(self) -> None:
        self._recording = False
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка закрытия CSV: %s", exc)
            finally:
                self._csv_file = None
                self._csv_writer = None

    def _write_frame_to_csv(self, frame: Dict[str, object]) -> None:
        if not self._recording or self._csv_writer is None:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        self._csv_writer.writerow([timestamp, frame["channel"], int_to_hex(frame_id, 8), len(data), bytes_to_hex_string(data)])
