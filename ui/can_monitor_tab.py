"""Вкладка «Мониторинг CAN» для отображения и ручной отправки CAN-пакетов."""

import csv
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import MARKER_TX_EXT, pack_can_frame
from core.dbc_manager import DBCManager
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import bytes_to_hex_string, format_data_bytes, hex_to_int, int_to_hex, parse_data_bytes

logger = get_logger(__name__)

MAX_TABLE_ROWS = 50_000


class CanChannelMonitor(QWidget):
    """Панель мониторинга одного CAN-канала."""

    create_trigger_requested = Signal(dict)

    def __init__(self, channel: int, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт панель мониторинга для одного канала.

        Args:
            channel: Номер канала (1 или 2).
            serial_manager: Менеджер COM-порта.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._channel = channel
        self._channel_byte = channel
        self._serial_manager = serial_manager
        self._running = False
        self._paused = False
        self._received_count = 0
        self._packet_times: deque[float] = deque()
        self._manual_send_ids: set[int] = set()
        self._last_packet_time: Optional[float] = None
        self._send_history: List[Dict[str, object]] = []

        self._create_widgets()
        self._layout_widgets()
        self._setup_timer()
        self._setup_activity_timer()

    def _create_widgets(self) -> None:
        """Создаёт элементы управления каналом."""
        self._start_button = QPushButton(tr("Старт"))
        self._start_button.setFixedSize(60, 24)
        self._start_button.setFont(QFont("Segoe UI", 9))
        self._start_button.clicked.connect(self._start)

        self._stop_button = QPushButton(tr("Стоп"))
        self._stop_button.setFixedSize(60, 24)
        self._stop_button.setFont(QFont("Segoe UI", 9))
        self._stop_button.clicked.connect(self._stop)

        self._clear_button = QPushButton(tr("Очистить"))
        self._clear_button.setFixedSize(70, 24)
        self._clear_button.setFont(QFont("Segoe UI", 9))
        self._clear_button.clicked.connect(self._clear)

        self._filter_edit = QLineEdit()
        self._filter_edit.setFixedWidth(110)
        self._filter_edit.setMaxLength(8)
        self._filter_edit.setFont(QFont("Segoe UI", 9))
        self._filter_edit.setPlaceholderText(tr("ID HEX"))

        self._pause_check = QCheckBox(tr("Приостановить"))
        self._pause_check.setFont(QFont("Segoe UI", 9))
        self._pause_check.stateChanged.connect(self._on_pause_changed)

        self._search_edit = QLineEdit()
        self._search_edit.setFixedWidth(180)
        self._search_edit.setFont(QFont("Segoe UI", 9))
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._search_edit.textChanged.connect(self._apply_search)

        self._table = QTableWidget()
        self._table.setColumnCount(12)
        self._table.setHorizontalHeaderLabels(
            [tr("Время"), "ID", "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "DLC", tr("Описание")]
        )
        self._table.setFont(QFont("Segoe UI", 9))
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        self._send_id_edit = QLineEdit()
        self._send_id_edit.setFixedWidth(110)
        self._send_id_edit.setMaxLength(8)
        self._send_id_edit.setFont(QFont("Segoe UI", 9))
        self._send_id_edit.setPlaceholderText("ID")

        self._send_data_edits: List[QLineEdit] = []
        for i in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(40)
            edit.setFont(QFont("Segoe UI", 9))
            edit.setPlaceholderText(f"D{i}")
            self._send_data_edits.append(edit)

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setFixedSize(80, 28)
        self._send_button.setFont(QFont("Segoe UI", 9))
        self._send_button.clicked.connect(self._send_manual)

        self._send_history_combo = QComboBox()
        self._send_history_combo.setFixedWidth(160)
        self._send_history_combo.setFont(QFont("Segoe UI", 9))
        self._send_history_combo.activated.connect(self._on_history_activated)
        self._rebuild_history_combo()

        self._cyclic_check = QCheckBox(tr("Циклически"))
        self._cyclic_check.setFont(QFont("Segoe UI", 9))
        self._cyclic_check.stateChanged.connect(self._on_cyclic_changed)

        self._cyclic_interval_edit = QLineEdit()
        self._cyclic_interval_edit.setFixedWidth(60)
        self._cyclic_interval_edit.setMaxLength(5)
        self._cyclic_interval_edit.setFont(QFont("Segoe UI", 9))
        self._cyclic_interval_edit.setPlaceholderText("1000")
        self._cyclic_interval_edit.setText("1000")

        self._cyclic_timer = QTimer(self)
        self._cyclic_timer.timeout.connect(self._send_cyclic_frame)
        self._cyclic_frame: Optional[bytes] = None

        self._stats_label = QLabel(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._stats_label.setFont(QFont("Segoe UI", 9))

    def _layout_widgets(self) -> None:
        """Располагает элементы панели канала."""
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)

        control_layout = QHBoxLayout()
        control_layout.addWidget(self._start_button)
        control_layout.addWidget(self._stop_button)
        control_layout.addWidget(self._clear_button)
        control_layout.addWidget(QLabel(tr("Фильтр ID:")))
        control_layout.addWidget(self._filter_edit)
        control_layout.addWidget(self._pause_check)
        control_layout.addStretch()
        control_layout.addWidget(QLabel(tr("Поиск:")))
        control_layout.addWidget(self._search_edit)
        layout.addLayout(control_layout)

        layout.addWidget(self._table)

        send_layout = QHBoxLayout()
        send_layout.addWidget(QLabel(tr("Отправить:")))
        send_layout.addWidget(self._send_id_edit)
        for edit in self._send_data_edits:
            send_layout.addWidget(edit)
        send_layout.addWidget(self._send_button)
        send_layout.addWidget(self._send_history_combo)
        send_layout.addWidget(self._cyclic_check)
        send_layout.addWidget(QLabel(tr("интервал мс:")))
        send_layout.addWidget(self._cyclic_interval_edit)
        send_layout.addStretch()
        layout.addLayout(send_layout)

        layout.addWidget(self._stats_label)

    def _setup_timer(self) -> None:
        """Запускает таймер обновления статистики."""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_stats)
        self._timer.start(1000)

    def _setup_activity_timer(self) -> None:
        """Запускает таймер проверки активности канала."""
        self._activity_timer = QTimer(self)
        self._activity_timer.timeout.connect(self._update_activity_color)
        self._activity_timer.start(500)

    def _update_activity_color(self) -> None:
        """Обновляет фон панели в зависимости от активности канала."""
        if self._last_packet_time is None:
            color = "#2B2B2B"  # Серый/нейтральный, нет пакетов с момента запуска
        elif time.time() - self._last_packet_time > 1.0:
            color = "#3A2020"  # Красноватый, нет пакетов более 1 сек
        else:
            color = "#1A3A1A"  # Зеленоватый, пакеты идут
        self.setStyleSheet(f"CanChannelMonitor {{ background-color: {color}; border-radius: 6px; }}")

    def _update_dbc_descriptions(self) -> None:
        """Обновляет описания и подсказки для уже отображённых строк."""
        dbc_manager = DBCManager()
        if not dbc_manager.is_loaded():
            return
        for row in range(self._table.rowCount()):
            id_item = self._table.item(row, 1)
            if id_item is None:
                continue
            can_id = hex_to_int(id_item.text())
            if can_id is None:
                continue
            data_values = []
            for col in range(2, 10):
                item = self._table.item(row, col)
                data_values.append(item.text() if item is not None else "")
            data = bytes(parse_data_bytes(data_values))
            message = dbc_manager.get_message(can_id)
            description = message.get("name", "") if message else ""
            desc_item = self._table.item(row, 11)
            if desc_item is None:
                desc_item = QTableWidgetItem(description)
                desc_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(row, 11, desc_item)
            else:
                desc_item.setText(description)
            tooltip = dbc_manager.describe_frame(can_id, data)
            if tooltip:
                for col in range(self._table.columnCount()):
                    item = self._table.item(row, col)
                    if item is not None:
                        item.setToolTip(tooltip)

    def _start(self) -> None:
        """Запускает отображение пакетов канала."""
        self._running = True
        logger.info("Мониторинг CAN%d запущен", self._channel)

    def _stop(self) -> None:
        """Останавливает отображение пакетов канала."""
        self._running = False
        self._stop_cyclic_timer()
        logger.info("Мониторинг CAN%d остановлен", self._channel)

    def _clear(self) -> None:
        """Очищает таблицу канала."""
        self._table.setRowCount(0)
        self._received_count = 0
        self._packet_times.clear()
        self._last_packet_time = None
        self._stats_label.setText("Принято: 0 | Скорость: 0 пак/с")
        self._update_activity_color()

    def _on_pause_changed(self, state: int) -> None:
        """Обрабатывает изменение чекбокса паузы."""
        self._paused = state == Qt.CheckState.Checked.value

    def _apply_search(self, text: str = "") -> None:
        """Скрывает строки таблицы, не содержащие поисковую подстроку."""
        query = text.strip().lower()
        if not query:
            for row in range(self._table.rowCount()):
                self._table.setRowHidden(row, False)
            return
        for row in range(self._table.rowCount()):
            match = False
            for col in range(1, 12):  # ID + D0..D7 + Описание
                item = self._table.item(row, col)
                if item is not None and query in item.text().lower():
                    match = True
                    break
            self._table.setRowHidden(row, not match)

    def _filter_id(self) -> Optional[int]:
        """Возвращает ID фильтра или None."""
        return hex_to_int(self._filter_edit.text())

    def _send_manual(self) -> None:
        """Отправляет вручную сформированный пакет."""
        can_id = hex_to_int(self._send_id_edit.text())
        if can_id is None:
            return
        data = parse_data_bytes([edit.text() for edit in self._send_data_edits])
        self._cyclic_frame = pack_can_frame(self._channel_byte, can_id, bytes(data))
        if self._send_cyclic_frame():
            self._add_to_history(can_id, data)
        if self._cyclic_check.isChecked():
            self._start_cyclic_timer()

    def _add_to_history(self, can_id: int, data: List[int]) -> None:
        """Добавляет успешную ручную отправку в историю (максимум 10 записей)."""
        entry = {"id": can_id, "data": data[:8]}
        # Удаляем дубликат, если есть
        self._send_history = [e for e in self._send_history if not (e["id"] == can_id and e["data"] == entry["data"])]
        self._send_history.insert(0, entry)
        self._send_history = self._send_history[:10]
        self._rebuild_history_combo()

    def _rebuild_history_combo(self) -> None:
        """Перезаполняет выпадающий список истории."""
        self._send_history_combo.blockSignals(True)
        self._send_history_combo.clear()
        self._send_history_combo.addItem(tr("— История —"), None)
        for entry in self._send_history:
            id_text = int_to_hex(entry["id"], 8 if entry["id"] > 0x7FF else 3)
            data_text = " ".join(int_to_hex(b, 2) for b in entry["data"])
            label = f"ID: {id_text} Data: {data_text}"
            self._send_history_combo.addItem(label, entry)
        self._send_history_combo.blockSignals(False)

    def _on_history_activated(self, index: int) -> None:
        """Заполняет поля ID и Data из выбранной истории."""
        entry = self._send_history_combo.itemData(index)
        if not isinstance(entry, dict):
            return
        can_id = entry.get("id")
        data = entry.get("data", [])
        if can_id is None:
            return
        self._send_id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        for i, edit in enumerate(self._send_data_edits):
            edit.setText(int_to_hex(data[i], 2) if i < len(data) else "")

    def _start_cyclic_timer(self) -> None:
        """Запускает таймер циклической отправки."""
        try:
            interval_ms = int(self._cyclic_interval_edit.text().strip() or "1000")
        except ValueError:
            interval_ms = 1000
        interval_ms = max(10, interval_ms)
        self._cyclic_timer.start(interval_ms)
        logger.info("Циклическая отправка CAN%d запущена с интервалом %d мс", self._channel, interval_ms)

    def _stop_cyclic_timer(self) -> None:
        """Останавливает таймер циклической отправки."""
        if self._cyclic_timer.isActive():
            self._cyclic_timer.stop()
            logger.info("Циклическая отправка CAN%d остановлена", self._channel)

    def _send_cyclic_frame(self) -> bool:
        """Отправляет сохранённый кадр и помечает его как ручную отправку."""
        if self._cyclic_frame is None:
            return False
        if self._serial_manager.send_data(self._cyclic_frame):
            can_id = int.from_bytes(self._cyclic_frame[2:6], "little") if self._cyclic_frame[0] == MARKER_TX_EXT else (self._cyclic_frame[2] | (self._cyclic_frame[3] << 8))
            self._manual_send_ids.add(can_id)
            logger.info("Циклическая отправка CAN%d: ID=0x%s", self._channel, int_to_hex(can_id, 8))
            return True
        return False

    def _on_cyclic_changed(self, state: int) -> None:
        """Обрабатывает изменение чекбокса циклической отправки."""
        if state == Qt.CheckState.Checked.value:
            if self._cyclic_frame is not None:
                self._start_cyclic_timer()
        else:
            self._stop_cyclic_timer()

    def _update_stats(self) -> None:
        """Обновляет статистику скорости приёма."""
        now = time.time()
        while self._packet_times and now - self._packet_times[0] > 1.0:
            self._packet_times.popleft()
        speed = len(self._packet_times)
        self._stats_label.setText(tr("Принято: {0} | Скорость: {1} пак/с").format(self._received_count, speed))

    def add_frame(self, frame: Dict[str, object]) -> None:
        """Добавляет принятый кадр в таблицу канала.

        Args:
            frame: Распакованный CAN-кадр.
        """
        if not self._running or self._paused:
            return

        frame_id = int(frame["id"])
        filter_id = self._filter_id()
        if filter_id is not None and frame_id != filter_id:
            return

        self._received_count += 1
        self._packet_times.append(time.time())
        self._last_packet_time = time.time()

        row = self._table.rowCount()
        if row >= MAX_TABLE_ROWS:
            self._table.removeRow(0)
            row = MAX_TABLE_ROWS
        self._table.insertRow(row)

        timestamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        id_width = 8 if frame.get("extended") else 3
        items = [timestamp, int_to_hex(frame_id, id_width)]
        data = bytes(frame["data"])
        data_hex = format_data_bytes(data) + [""] * (8 - len(data))
        items.extend(data_hex[:8])
        items.append(str(len(data)))

        dbc_manager = DBCManager()
        message = dbc_manager.get_message(frame_id)
        description = message.get("name", "") if message else ""
        items.append(description)
        tooltip = dbc_manager.describe_frame(frame_id, data) if dbc_manager.is_loaded() else ""

        for col, text in enumerate(items):
            table_item = QTableWidgetItem(text)
            table_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if tooltip:
                table_item.setToolTip(tooltip)
            self._table.setItem(row, col, table_item)

        if frame_id in self._manual_send_ids:
            highlight_color = QColor("#4A6A8A")
            for col in range(self._table.columnCount()):
                item = self._table.item(row, col)
                if item is not None:
                    item.setBackground(highlight_color)

        self._table.scrollToBottom()
        if self._search_edit.text().strip():
            self._apply_search(self._search_edit.text())

    def _show_context_menu(self, position) -> None:
        """Показывает контекстное меню для выбранной строки таблицы."""
        row = self._table.currentRow()
        if row < 0:
            return

        menu = QMenu(self)
        copy_id_action = QAction(tr("Копировать ID"), self)
        copy_id_action.triggered.connect(lambda: self._copy_selected_id(row))
        copy_data_action = QAction(tr("Копировать данные"), self)
        copy_data_action.triggered.connect(lambda: self._copy_selected_data(row))
        copy_row_action = QAction(tr("Копировать всю строку"), self)
        copy_row_action.triggered.connect(lambda: self._copy_selected_row(row))
        create_trigger_action = QAction(tr("Создать триггер из пакета"), self)
        create_trigger_action.triggered.connect(lambda: self._create_trigger_from_row(row))
        menu.addAction(copy_id_action)
        menu.addAction(copy_data_action)
        menu.addAction(copy_row_action)
        menu.addAction(create_trigger_action)
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _copy_selected_id(self, row: int) -> None:
        """Копирует ID выбранной строки в буфер обмена."""
        item = self._table.item(row, 1)
        if item is not None:
            QApplication.clipboard().setText(item.text())

    def _copy_selected_data(self, row: int) -> None:
        """Копирует данные D0-D7 выбранной строки в буфер обмена."""
        values = []
        for col in range(2, 10):
            item = self._table.item(row, col)
            if item is not None and item.text():
                values.append(item.text())
        QApplication.clipboard().setText(" ".join(values))

    def _copy_selected_row(self, row: int) -> None:
        """Копирует всю строку таблицы в формате лога."""
        values = []
        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            values.append(item.text() if item is not None else "")
        # Формат: Время ID D0-D7 DLC
        QApplication.clipboard().setText("  ".join(values))

    def _create_trigger_from_row(self, row: int) -> None:
        """Создаёт триггер из выбранного пакета и запрашивает открытие вкладки CAN Тригер."""
        id_item = self._table.item(row, 1)
        if id_item is None:
            return
        data_values = []
        for col in range(2, 10):
            item = self._table.item(row, col)
            data_values.append(item.text() if item is not None else "")
        self.create_trigger_requested.emit({"id": id_item.text(), "data": data_values})


class CanMonitorTab(QWidget):
    """Вкладка мониторинга CAN с двумя каналами."""

    create_trigger_requested = Signal(dict)

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт вкладку мониторинга.

        Args:
            serial_manager: Менеджер COM-порта.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._recording = False
        self._csv_file: Optional[TextIO] = None
        self._csv_writer: Optional[csv.writer] = None
        self._csv_path: Optional[Path] = None

        self._create_widgets()
        self._layout_widgets()

    def _create_widgets(self) -> None:
        """Создаёт элементы вкладки."""
        self._start_all_button = QPushButton(tr("Запустить оба"))
        self._start_all_button.setFixedSize(110, 28)
        self._start_all_button.setFont(QFont("Segoe UI", 9))
        self._start_all_button.clicked.connect(self._start_all)

        self._stop_all_button = QPushButton(tr("Остановить оба"))
        self._stop_all_button.setFixedSize(110, 28)
        self._stop_all_button.setFont(QFont("Segoe UI", 9))
        self._stop_all_button.clicked.connect(self._stop_all)

        self._clear_all_button = QPushButton(tr("Очистить всё"))
        self._clear_all_button.setFixedSize(110, 28)
        self._clear_all_button.setFont(QFont("Segoe UI", 9))
        self._clear_all_button.clicked.connect(self._clear_all)

        self._record_button = QPushButton(tr("Записать в CSV"))
        self._record_button.setFixedSize(110, 28)
        self._record_button.setFont(QFont("Segoe UI", 9))
        self._record_button.clicked.connect(self._toggle_recording)

        self._dbc_button = QPushButton(tr("Загрузить DBC"))
        self._dbc_button.setFixedSize(110, 28)
        self._dbc_button.setFont(QFont("Segoe UI", 9))
        self._dbc_button.clicked.connect(self._on_load_dbc)
        self._dbc_manager = DBCManager()

        # Панель отправки и триггера
        self._send_id_edit = QLineEdit()
        self._send_id_edit.setFixedWidth(90)
        self._send_id_edit.setMaxLength(32)
        self._send_id_edit.setFont(QFont("Segoe UI", 9))
        self._send_id_edit.setPlaceholderText(tr("ID HEX или имя DBC"))
        self._send_id_edit.editingFinished.connect(self._autofill_send_from_dbc)

        self._send_data_edit = QLineEdit()
        self._send_data_edit.setFixedWidth(140)
        self._send_data_edit.setFont(QFont("Segoe UI", 9))
        self._send_data_edit.setPlaceholderText(tr("D0 D1 D2 ..."))

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 100)
        self._repeat_spin.setValue(1)
        self._repeat_spin.setFixedWidth(70)
        self._repeat_spin.setFont(QFont("Segoe UI", 9))

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 1000)
        self._interval_spin.setValue(10)
        self._interval_spin.setSuffix(" мс")
        self._interval_spin.setFixedWidth(90)
        self._interval_spin.setFont(QFont("Segoe UI", 9))

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setFixedSize(90, 28)
        self._send_button.setFont(QFont("Segoe UI", 9))
        self._send_button.clicked.connect(self._on_send_clicked)

        self._trigger_check = QCheckBox(tr("Триггер"))
        self._trigger_check.setFont(QFont("Segoe UI", 9))
        self._trigger_check.setToolTip(tr("Автоматический ответ на входящий пакет с указанным ID"))
        self._trigger_check.stateChanged.connect(self._on_trigger_mode_changed)

        self._mask_edit = QLineEdit()
        self._mask_edit.setFixedWidth(90)
        self._mask_edit.setFont(QFont("Segoe UI", 9))
        self._mask_edit.setPlaceholderText(tr("Маска HEX"))
        self._mask_edit.setEnabled(False)
        self._mask_edit.setToolTip(tr("Маска входящих данных для сравнения (опционально)"))

        self._send_status_label = QLabel("")
        self._send_status_label.setFont(QFont("Segoe UI", 9))

        self._repeat_timer = QTimer(self)
        self._repeat_timer.timeout.connect(self._send_one_repeated_frame)
        self._repeat_remaining = 0
        self._repeat_frame: bytes = b""
        self._trigger_response_data: List[int] = []
        self._trigger_response_mask: List[int] = []

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._monitor1 = CanChannelMonitor(1, self._serial_manager, self)
        self._monitor2 = CanChannelMonitor(2, self._serial_manager, self)
        self._monitor1.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor2.create_trigger_requested.connect(self.create_trigger_requested)
        self._splitter.addWidget(self._monitor1)
        self._splitter.addWidget(self._monitor2)
        self._splitter.setSizes([450, 450])

    def _layout_widgets(self) -> None:
        """Располагает элементы вкладки."""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self._start_all_button)
        buttons_layout.addWidget(self._stop_all_button)
        buttons_layout.addWidget(self._clear_all_button)
        buttons_layout.addWidget(self._record_button)
        buttons_layout.addWidget(self._dbc_button)
        buttons_layout.addStretch()
        layout.addLayout(buttons_layout)

        send_layout = QHBoxLayout()
        send_layout.setSpacing(6)
        send_layout.addWidget(QLabel(tr("ID:")))
        send_layout.addWidget(self._send_id_edit)
        send_layout.addWidget(QLabel(tr("Данные:")))
        send_layout.addWidget(self._send_data_edit)
        send_layout.addWidget(QLabel(tr("Повторы:")))
        send_layout.addWidget(self._repeat_spin)
        send_layout.addWidget(QLabel(tr("Интервал:")))
        send_layout.addWidget(self._interval_spin)
        send_layout.addWidget(self._send_button)
        send_layout.addWidget(self._trigger_check)
        send_layout.addWidget(self._mask_edit)
        send_layout.addStretch()
        layout.addLayout(send_layout)

        layout.addWidget(self._send_status_label)
        layout.addWidget(self._splitter)

    def _start_all(self) -> None:
        """Запускает мониторинг обоих каналов."""
        self._monitor1._start()
        self._monitor2._start()
        logger.info("Запущен мониторинг обоих CAN-каналов")

    def _stop_all(self) -> None:
        """Останавливает мониторинг обоих каналов."""
        self._monitor1._stop()
        self._monitor2._stop()
        logger.info("Остановлен мониторинг обоих CAN-каналов")

    def _clear_all(self) -> None:
        """Очищает таблицы обоих каналов."""
        self._monitor1._clear()
        self._monitor2._clear()

    def _on_trigger_mode_changed(self, state: int) -> None:
        """Включает/отключает маску в режиме триггера."""
        self._mask_edit.setEnabled(state == Qt.CheckState.Checked.value)

    def _parse_global_data(self) -> List[int]:
        """Парсит строку данных из глобального поля в список байт."""
        text = self._send_data_edit.text().strip()
        if not text:
            return []
        return parse_data_bytes(text.split())

    def _autofill_send_from_dbc(self) -> None:
        """Автоматически подставляет ID и данные по имени сообщения из DBC."""
        text = self._send_id_edit.text().strip()
        if not text or hex_to_int(text) is not None:
            return
        if not self._dbc_manager.is_loaded():
            return
        message = self._dbc_manager.get_message_by_name(text)
        can_id: Optional[int] = None
        if message is None:
            for cid, msg in self._dbc_manager.get_all().items():
                for signal in msg.get("signals", []):
                    if signal.get("name") == text:
                        message = msg
                        can_id = cid
                        break
                if message is not None:
                    break
        if message is None or can_id is None:
            can_id = self._dbc_manager.get_id_by_name(text)
            if can_id is None:
                return
        dlc = int(message.get("dlc", 8)) if message else 8
        self._send_id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        self._send_data_edit.setText(" ".join(["00"] * dlc))
        self._send_status_label.setText(tr("Автозаполнено из DBC: {0}").format(text))

    def _on_send_clicked(self) -> None:
        """Отправляет пакет с учётом повторов и интервала."""
        if self._trigger_check.isChecked():
            # В режиме триггера кнопка просто сохраняет настройки
            self._trigger_response_data = self._parse_global_data()
            mask_text = self._mask_edit.text().strip()
            self._trigger_response_mask = parse_data_bytes(mask_text.split()) if mask_text else []
            self._send_status_label.setText(tr("Триггер активирован"))
            logger.info("Триггер мониторинга активирован: ID=%s", self._send_id_edit.text())
            return

        can_id = hex_to_int(self._send_id_edit.text())
        if can_id is None:
            self._send_status_label.setText(tr("Ошибка: неверный ID"))
            return
        data = self._parse_global_data()
        self._repeat_frame = pack_can_frame(0x01, can_id, bytes(data))
        self._repeat_remaining = self._repeat_spin.value()
        interval_ms = self._interval_spin.value()
        self._send_status_label.setText(tr("Отправка {0} пакетов...").format(self._repeat_remaining))
        self._send_one_repeated_frame()
        if self._repeat_remaining > 0:
            self._repeat_timer.start(interval_ms)

    def _send_one_repeated_frame(self) -> None:
        """Отправляет один пакет из серии повторов."""
        if self._repeat_remaining <= 0:
            self._repeat_timer.stop()
            self._send_status_label.setText(tr("Отправка завершена"))
            return
        if self._serial_manager.send_data(self._repeat_frame):
            self._repeat_remaining -= 1
        else:
            self._repeat_timer.stop()
            self._send_status_label.setText(tr("Ошибка отправки"))

    def _check_trigger_response(self, frame: Dict[str, object]) -> None:
        """Проверяет входящий кадр на условие триггера и отправляет ответ."""
        if not self._trigger_check.isChecked():
            return
        trigger_id = hex_to_int(self._send_id_edit.text())
        if trigger_id is None:
            return
        frame_id = int(frame["id"])
        if frame_id != trigger_id:
            return

        frame_data = bytes(frame["data"])
        if self._trigger_response_mask:
            if len(frame_data) < len(self._trigger_response_mask):
                return
            for i, mask_byte in enumerate(self._trigger_response_mask):
                if (frame_data[i] & mask_byte) != (self._trigger_response_data[i] & mask_byte if i < len(self._trigger_response_data) else 0):
                    return

        channel = int(frame["channel"])
        resp_data = bytes(self._trigger_response_data[:8])
        resp_frame = pack_can_frame(channel, frame_id, resp_data)
        self._serial_manager.send_data(resp_frame)
        self._send_status_label.setText(tr("Автоответ: ID {0}").format(int_to_hex(frame_id, 8)))
        logger.info("Автоответ мониторинга: ID=0x%s в канал %d", int_to_hex(frame_id, 8), channel)

    def process_frame(self, frame: Dict[str, object]) -> None:
        """Распределяет CAN-кадр по соответствующему каналу и записывает в CSV при необходимости.

        Args:
            frame: Распакованный CAN-кадр.
        """
        channel = int(frame["channel"])
        if channel == 1:
            self._monitor1.add_frame(frame)
        elif channel == 2:
            self._monitor2.add_frame(frame)
        self._check_trigger_response(frame)
        self._write_frame_to_csv(frame)

    def _toggle_recording(self) -> None:
        """Запускает или останавливает запись CAN-трафика в CSV."""
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Выбирает файл и начинает запись CSV."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить запись CAN-трафика"),
            "",
            tr("CSV files (*.csv)"),
        )
        if not path:
            return
        try:
            self._csv_path = Path(path)
            self._csv_file = open(self._csv_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["timestamp", "channel", "id", "dlc", "data"])
            self._recording = True
            self._record_button.setText(tr("Остановить запись"))
            logger.info("Запись CAN-трафика начата: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка открытия CSV: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть файл: {0}").format(exc))

    def _stop_recording(self) -> None:
        """Останавливает запись и закрывает CSV-файл."""
        self._recording = False
        if self._csv_file is not None:
            try:
                self._csv_file.close()
                logger.info("Запись CAN-трафика остановлена: %s", self._csv_path)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка закрытия CSV: %s", exc)
            finally:
                self._csv_file = None
                self._csv_writer = None
        self._record_button.setText(tr("Записать в CSV"))

    def _on_load_dbc(self) -> None:
        """Загружает DBC-файл и активирует описания в таблице."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить DBC"),
            "",
            "DBC files (*.dbc)",
        )
        if not path:
            return
        if self._dbc_manager.load_dbc(path):
            QMessageBox.information(self, tr("Готово"), tr("DBC загружен: {0}").format(path))
            self._update_dbc_columns()
        else:
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить DBC"))

    def _update_dbc_columns(self) -> None:
        """Пересчитывает описания в таблицах каналов после загрузки DBC."""
        for monitor in (self._monitor1, self._monitor2):
            monitor._update_dbc_descriptions()

    def _write_frame_to_csv(self, frame: Dict[str, object]) -> None:
        """Записывает один CAN-кадр в CSV-файл.

        Args:
            frame: Распакованный CAN-кадр.
        """
        if not self._recording or self._csv_writer is None:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        self._csv_writer.writerow([timestamp, frame["channel"], int_to_hex(frame_id, 8), len(data), bytes_to_hex_string(data)])
