"""Вкладка «Мониторинг CAN» с двумя каналами."""

import csv
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.dbc_manager import DBCManager
from core.dbc_parser import decode_frame
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import bytes_to_hex_string, format_data_bytes, hex_to_int, int_to_hex, parse_data_bytes
from ui.ui_utils import setup_button

logger = get_logger(__name__)

MAX_TABLE_ROWS = 50_000
HIGHLIGHT_COLOR = "#4A6A8A"
HIGHLIGHT_MS = 2000

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

    def __init__(self, channel: int, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._channel_byte = channel
        self._serial_manager = serial_manager
        self._running = False
        self._paused = False
        self._received_count = 0
        self._packet_times: deque[float] = deque()
        self._last_packet_time: Optional[float] = None
        self._cyclic_frame: Optional[bytes] = None
        self._dbc_manager = DBCManager()

        self._id_to_row: Dict[int, int] = {}
        self._id_stats: Dict[int, Dict[str, Any]] = {}
        self._id_data_variants: Dict[int, Set[bytes]] = {}
        self._highlight_timers: Dict[int, QTimer] = {}

        self._create_widgets()
        self._layout_widgets()
        self._setup_timers()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        compact_font = QFont("Segoe UI", 9)

        self._start_button = QPushButton(tr("Старт"))
        self._start_button.setFixedSize(80, 28)
        self._start_button.setFont(compact_font)
        self._start_button.clicked.connect(self._start)

        self._stop_button = QPushButton(tr("Стоп"))
        self._stop_button.setFixedSize(80, 28)
        self._stop_button.setFont(compact_font)
        self._stop_button.clicked.connect(self._stop)

        self._clear_button = QPushButton(tr("Очистить"))
        self._clear_button.setFixedSize(80, 28)
        self._clear_button.setFont(compact_font)
        self._clear_button.clicked.connect(self._clear)

        self._filter_button = QPushButton(tr("Фильтр"))
        self._filter_button.setFixedSize(80, 28)
        self._filter_button.setFont(compact_font)
        self._filter_button.clicked.connect(self._show_filter_stub)

        self._filter_from = QLineEdit()
        self._filter_from.setFixedWidth(70)
        self._filter_from.setMaxLength(8)
        self._filter_from.setFont(font)
        self._filter_from.setPlaceholderText(tr("ID от"))

        self._filter_to = QLineEdit()
        self._filter_to.setFixedWidth(70)
        self._filter_to.setMaxLength(8)
        self._filter_to.setFont(font)
        self._filter_to.setPlaceholderText(tr("ID до"))

        self._exclude_edit = QLineEdit()
        self._exclude_edit.setFixedWidth(120)
        self._exclude_edit.setFont(font)
        self._exclude_edit.setPlaceholderText(tr("Исключить ID"))

        self._pause_check = QCheckBox(tr("Приостановить"))
        self._pause_check.setFont(font)
        self._pause_check.stateChanged.connect(self._on_pause_changed)

        self._search_edit = QLineEdit()
        self._search_edit.setFixedWidth(160)
        self._search_edit.setFont(font)
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._search_edit.textChanged.connect(self._apply_search)

        self._filter_from.textChanged.connect(self._apply_filters)
        self._filter_to.textChanged.connect(self._apply_filters)
        self._exclude_edit.textChanged.connect(self._apply_filters)

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
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 220)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 80)
        self._table.setColumnWidth(5, 90)
        self._table.setColumnWidth(6, 150)

        self._send_bit_combo = QComboBox()
        self._send_bit_combo.setFont(font)
        self._send_bit_combo.addItems(BIT_RATES)
        self._send_bit_combo.setFixedWidth(90)

        self._send_id_edit = QLineEdit()
        self._send_id_edit.setFixedWidth(90)
        self._send_id_edit.setMaxLength(8)
        self._send_id_edit.setFont(font)
        self._send_id_edit.setPlaceholderText("ID")
        self._send_id_validator = _IdValidator(self._send_id_edit, self._send_bit_combo)

        self._send_dlc_spin = QSpinBox()
        self._send_dlc_spin.setRange(1, 8)
        self._send_dlc_spin.setValue(8)
        self._send_dlc_spin.setFont(font)
        self._send_dlc_spin.setFixedWidth(45)

        self._send_data_edits: List[QLineEdit] = []
        for i in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(38)
            edit.setFont(font)
            edit.setMaxLength(2)
            edit.setPlaceholderText(f"D{i}")
            edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,2}")))
            edit.textChanged.connect(lambda text, e=edit: self._on_data_edit_changed(e, text))
            self._send_data_edits.append(edit)

        self._send_period_spin = QSpinBox()
        self._send_period_spin.setRange(0, 9999)
        self._send_period_spin.setValue(1000)
        self._send_period_spin.setSuffix(tr(" мс"))
        self._send_period_spin.setFont(font)
        self._send_period_spin.setFixedWidth(80)

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setFixedSize(80, 28)
        self._send_button.setFont(font)
        self._send_button.clicked.connect(self._send_manual)

        self._cyclic_button = QPushButton("∞")
        self._cyclic_button.setFixedSize(40, 40)
        self._cyclic_button.setFont(QFont("Segoe UI", 24))
        self._cyclic_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
        )
        self._cyclic_button.setToolTip(tr("Циклически"))
        self._cyclic_button.setCheckable(True)
        self._cyclic_button.toggled.connect(self._on_cyclic_toggled)

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

        control_layout = QGridLayout()
        control_layout.setSpacing(4)
        control_layout.addWidget(self._start_button, 0, 0)
        control_layout.addWidget(self._stop_button, 0, 1)
        control_layout.addWidget(self._clear_button, 0, 2)
        control_layout.addWidget(self._filter_button, 0, 3)
        control_layout.addWidget(QLabel(tr("ID от")), 0, 4)
        control_layout.addWidget(self._filter_from, 0, 5)
        control_layout.addWidget(QLabel(tr("до")), 0, 6)
        control_layout.addWidget(self._filter_to, 0, 7)
        control_layout.addWidget(QLabel(tr("Искл.")), 0, 8)
        control_layout.addWidget(self._exclude_edit, 0, 9)
        control_layout.addWidget(self._pause_check, 1, 0, 1, 2)
        control_layout.addWidget(QLabel(tr("Поиск:")), 1, 4)
        control_layout.addWidget(self._search_edit, 1, 5, 1, 3)
        layout.addLayout(control_layout)

        layout.addWidget(self._table, 1)

        send_layout = QVBoxLayout()
        send_layout.setSpacing(4)

        send_top = QHBoxLayout()
        send_top.setSpacing(4)
        send_top.addWidget(QLabel(tr("Бит")))
        send_top.addWidget(self._send_bit_combo)
        send_top.addWidget(QLabel("ID"))
        send_top.addWidget(self._send_id_edit)
        send_top.addWidget(QLabel("DLC"))
        send_top.addWidget(self._send_dlc_spin)
        send_top.addWidget(QLabel(tr("Data")))
        for edit in self._send_data_edits:
            send_top.addWidget(edit)
        send_top.addStretch()

        send_bottom = QHBoxLayout()
        send_bottom.setSpacing(4)
        send_bottom.addWidget(QLabel(tr("Период")))
        send_bottom.addWidget(self._send_period_spin)
        send_bottom.addWidget(self._send_button)
        send_bottom.addWidget(self._cyclic_button)
        send_bottom.addStretch()

        send_layout.addLayout(send_top)
        send_layout.addLayout(send_bottom)
        layout.addLayout(send_layout)

        layout.addWidget(self._stats_label)

    def _setup_timers(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_stats)
        self._timer.start(1000)

    def _show_filter_stub(self) -> None:
        QMessageBox.information(self, tr("Фильтр"), tr("Функция в разработке"))

    def _on_send_dlc_changed(self, value: int) -> None:
        for i, edit in enumerate(self._send_data_edits):
            edit.setEnabled(i < value)

    def _on_data_edit_changed(self, edit: QLineEdit, text: str) -> None:
        """Автоматически приводит введённые HEX-символы к верхнему регистру."""
        upper = text.upper()
        if text != upper:
            edit.blockSignals(True)
            edit.setText(upper)
            edit.blockSignals(False)

    def _on_cyclic_toggled(self, checked: bool) -> None:
        if checked:
            self._cyclic_button.setStyleSheet("background-color: #4CAF50; color: #FFFFFF;")
            self._send_manual()
        else:
            self._cyclic_button.setStyleSheet("")
            self._stop_cyclic_timer()

    def _start(self) -> None:
        self._running = True
        logger.info("Мониторинг CAN%d запущен", self._channel)

    def _stop(self) -> None:
        self._running = False
        self._cyclic_button.setChecked(False)
        self._stop_cyclic_timer()
        logger.info("Мониторинг CAN%d остановлен", self._channel)

    def _clear(self) -> None:
        self._table.setRowCount(0)
        self._id_to_row.clear()
        self._id_stats.clear()
        self._id_data_variants.clear()
        for timer in self._highlight_timers.values():
            timer.stop()
        self._highlight_timers.clear()
        self._received_count = 0
        self._packet_times.clear()
        self._last_packet_time = None
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))

    def _on_pause_changed(self, state: int) -> None:
        self._paused = state == Qt.CheckState.Checked.value

    def _filter_range(self) -> tuple[Optional[int], Optional[int]]:
        return (hex_to_int(self._filter_from.text()), hex_to_int(self._filter_to.text()))

    def _exclude_ids(self) -> Set[int]:
        ids: Set[int] = set()
        for token in self._exclude_edit.text().replace(",", " ").split():
            value = hex_to_int(token)
            if value is not None:
                ids.add(value)
        return ids

    def _apply_filters(self) -> None:
        f_from, f_to = self._filter_range()
        exclude = self._exclude_ids()
        for row in range(self._table.rowCount()):
            hidden = False
            id_item = self._table.item(row, 0)
            if id_item is not None:
                can_id = hex_to_int(id_item.text())
                if can_id is not None:
                    if f_from is not None and can_id < f_from:
                        hidden = True
                    if f_to is not None and can_id > f_to:
                        hidden = True
                    if can_id in exclude:
                        hidden = True
            self._table.setRowHidden(row, hidden)

    def _apply_search(self, text: str = "") -> None:
        query = text.strip().lower()
        if not query:
            for row in range(self._table.rowCount()):
                self._table.setRowHidden(row, False)
            self._apply_filters()
            return
        for row in range(self._table.rowCount()):
            hidden = True
            for col in range(self._table.columnCount()):
                item = self._table.item(row, col)
                if item is not None and query in item.text().lower():
                    hidden = False
                    break
            self._table.setRowHidden(row, hidden)

    def _send_manual(self) -> None:
        can_id = hex_to_int(self._send_id_edit.text())
        if can_id is None:
            return
        dlc = self._send_dlc_spin.value()
        data = self._data_from_send_edits(dlc)
        self._cyclic_frame = pack_can_frame(self._channel_byte, can_id, data)
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
        return self._serial_manager.send_data(self._cyclic_frame)

    def _update_stats(self) -> None:
        now = time.time()
        while self._packet_times and now - self._packet_times[0] > 1.0:
            self._packet_times.popleft()
        speed = len(self._packet_times)
        self._stats_label.setText(tr("Принято: {0} | Скорость: {1} пак/с").format(self._received_count, speed))

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

    def _build_row_items(self, frame_id: int, data: bytes, timestamp: str, period: str, count: int) -> List[str]:
        id_width = 8 if frame_id > 0x7FF else 3
        signals = self._format_signals(frame_id, data)
        return [
            int_to_hex(frame_id, id_width),
            str(len(data)),
            " ".join(format_data_bytes(data)),
            period,
            str(count),
            _ascii_from_data(data),
            signals,
        ]

    def _reset_row_highlight(self, row: int) -> None:
        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None:
                item.setBackground(QColor())

    def _highlight_changed_bytes(self, row: int, new_data: bytes, old_data: bytes) -> None:
        if row in self._highlight_timers:
            self._highlight_timers[row].stop()
        color = QColor(HIGHLIGHT_COLOR)
        data_text = " ".join(format_data_bytes(new_data))
        old_text = " ".join(format_data_bytes(old_data))
        data_item = self._table.item(row, 2)
        if data_item is not None:
            data_item.setBackground(color)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda r=row: self._reset_row_highlight(r))
        timer.start(HIGHLIGHT_MS)
        self._highlight_timers[row] = timer

    def add_frame(self, frame: Dict[str, object]) -> None:
        if not self._running or self._paused:
            return
        frame_id = int(frame["id"])
        f_from, f_to = self._filter_range()
        exclude = self._exclude_ids()
        if f_from is not None and frame_id < f_from:
            return
        if f_to is not None and frame_id > f_to:
            return
        if frame_id in exclude:
            return

        self._received_count += 1
        self._packet_times.append(time.time())
        self._last_packet_time = time.time()

        data = bytes(frame["data"])
        now = time.time()
        stats = self._id_stats.setdefault(frame_id, {"count": 0, "last_time": None, "last_data": b""})
        stats["count"] += 1
        period = self._format_period(frame_id, now)
        stats["last_time"] = now

        timestamp = time.strftime("%H:%M:%S") + f".{int((now % 1) * 1000):03d}"
        items = self._build_row_items(frame_id, data, timestamp, period, stats["count"])

        tooltip = ""
        if self._dbc_manager.is_loaded():
            tooltip = self._dbc_manager.describe_frame(frame_id, data)

        if frame_id in self._id_to_row:
            row = self._id_to_row[frame_id]
            old_data = stats["last_data"]
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
            self._highlight_changed_bytes(row, data, old_data)
        else:
            if self._table.rowCount() >= MAX_TABLE_ROWS:
                self._table.removeRow(0)
                self._id_to_row = {}
                for r in range(self._table.rowCount()):
                    id_item = self._table.item(r, 0)
                    if id_item is not None:
                        fid = hex_to_int(id_item.text())
                        if fid is not None:
                            self._id_to_row[fid] = r
            row = self._table.rowCount()
            self._table.insertRow(row)
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if tooltip:
                    item.setToolTip(tooltip)
                self._table.setItem(row, col, item)
            self._id_to_row[frame_id] = row

        stats["last_data"] = data
        self._id_data_variants.setdefault(frame_id, set()).add(data)

        self._table.scrollToBottom()

    def _show_context_menu(self, position) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        menu = QMenu(self)
        menu.addAction(tr("Копировать ID"), lambda: self._copy_selected_id(row))
        menu.addAction(tr("Копировать данные"), lambda: self._copy_selected_data(row))
        menu.addAction(tr("Копировать всю строку"), lambda: self._copy_selected_row(row))
        menu.addAction(tr("Создать триггер"), lambda: self._create_trigger_from_row(row))
        menu.addAction(tr("Показать варианты данных"), lambda: self._show_data_variants(row))
        menu.addAction(tr("Битовая карта"), lambda: self._show_bitmap(row))
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
        self._apply_filters()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки панели мониторинга канала."""
        self._start_button.setText(tr("Старт"))
        self._stop_button.setText(tr("Стоп"))
        self._clear_button.setText(tr("Очистить"))
        self._filter_button.setText(tr("Фильтр"))
        self._filter_from.setPlaceholderText(tr("ID от"))
        self._filter_to.setPlaceholderText(tr("ID до"))
        self._exclude_edit.setPlaceholderText(tr("Исключить ID"))
        self._pause_check.setText(tr("Приостановить"))
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("Счётчик"), tr("ASCII"), tr("Пояснение")]
        )
        self._send_button.setText(tr("Отправить"))
        self._cyclic_button.setToolTip(tr("Циклически"))
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))


class CanMonitorTab(QWidget):
    """Вкладка мониторинга CAN с двумя каналами."""

    create_trigger_requested = Signal(dict)

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._recording = False
        self._csv_file: Optional[TextIO] = None
        self._csv_writer: Optional[csv.writer] = None
        self._csv_path: Optional[Path] = None
        self._dbc_manager = DBCManager()
        self._create_widgets()
        self._layout_widgets()

    def _create_widgets(self) -> None:
        compact_font = QFont("Segoe UI", 9)

        self._start_all_button = QPushButton(tr("Запустить оба"))
        self._start_all_button.setFixedSize(80, 28)
        self._start_all_button.setFont(compact_font)
        self._start_all_button.clicked.connect(self._start_all)

        self._stop_all_button = QPushButton(tr("Остановить оба"))
        self._stop_all_button.setFixedSize(80, 28)
        self._stop_all_button.setFont(compact_font)
        self._stop_all_button.clicked.connect(self._stop_all)

        self._clear_all_button = QPushButton(tr("Очистить всё"))
        self._clear_all_button.setFixedSize(80, 28)
        self._clear_all_button.setFont(compact_font)
        self._clear_all_button.clicked.connect(self._clear_all)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._monitor1 = CanChannelMonitor(1, self._serial_manager, self)
        self._monitor2 = CanChannelMonitor(2, self._serial_manager, self)
        self._monitor1.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor2.create_trigger_requested.connect(self.create_trigger_requested)
        self._splitter.addWidget(self._monitor1)
        self._splitter.addWidget(self._monitor2)
        self._splitter.setSizes([450, 450])

    def _layout_widgets(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self._start_all_button)
        buttons_layout.addWidget(self._stop_all_button)
        buttons_layout.addWidget(self._clear_all_button)
        buttons_layout.addStretch()
        layout.addLayout(buttons_layout)
        layout.addWidget(self._splitter)

    def _start_all(self) -> None:
        self._monitor1._start()
        self._monitor2._start()
        logger.info("Запущен мониторинг обоих CAN-каналов")

    def _stop_all(self) -> None:
        self._monitor1._stop()
        self._monitor2._stop()
        logger.info("Остановлен мониторинг обоих CAN-каналов")

    def _clear_all(self) -> None:
        self._monitor1._clear()
        self._monitor2._clear()

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
        self._start_all_button.setText(tr("Запустить оба"))
        self._stop_all_button.setText(tr("Остановить оба"))
        self._clear_all_button.setText(tr("Очистить всё"))
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
