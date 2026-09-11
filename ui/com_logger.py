"""Отдельное COM-логгер окно с мониторингом и отправкой байт."""

import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import serial
from serial.tools.list_ports import comports

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import parse_all_frames
from core.listen_only import CanPacket, ListenOnlyMode
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger, get_log_dir
from models.translations import _ as tr

logger = get_logger(__name__)

BAUDRATES: List[int] = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

MAX_ROWS = 10000


class ComLoggerReader(QThread):
    """Поток чтения/записи для COM-логгера.

    Порт держится открытым только пока запущен мониторинг: это единственный
    способ надёжно получать асинхронные данные. Для работы других программ
    мониторинг нужно остановить кнопкой «Отключить».
    """

    data_received = Signal(bytes, float)
    error = Signal(str)
    connection_changed = Signal(bool)
    state_changed = Signal(str)

    def __init__(self, port_name: str, baudrate: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._port_name = port_name
        self._baudrate = baudrate
        self._running = True
        self._port_lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None

    def _open_port(self) -> Optional[serial.Serial]:
        """Открывает порт с минимальным воздействием на линии DTR/RTS."""
        try:
            ser = serial.Serial(
                port=self._port_name,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
                write_timeout=1,
            )
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:  # noqa: S110
                pass
            return ser
        except Exception as exc:  # noqa: BLE001
            logger.warning("COM-логгер: не удалось открыть %s: %s", self._port_name, exc)
            return None

    def run(self) -> None:
        logger.info("COM-логгер: запуск мониторинга %s @ %d", self._port_name, self._baudrate)
        with self._port_lock:
            self._ser = self._open_port()
        if self._ser is None:
            self.error.emit(tr("Не удалось открыть порт") + f" {self._port_name}")
            self.connection_changed.emit(False)
            return
        self.connection_changed.emit(True)
        self.state_changed.emit(tr("Мониторинг (порт занят)"))
        try:
            while self._running:
                try:
                    chunk = self._ser.read(self._ser.in_waiting or 1)
                    if chunk:
                        logger.debug("COM-логгер: прочитано %d байт из %s", len(chunk), self._port_name)
                        self.data_received.emit(chunk, time.time())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("COM-логгер: ошибка чтения %s: %s", self._port_name, exc)
                    self.error.emit(str(exc))
                    self.msleep(100)
        finally:
            with self._port_lock:
                if self._ser is not None:
                    try:
                        self._ser.close()
                    except Exception:  # noqa: S110
                        pass
                    self._ser = None
            self.connection_changed.emit(False)
            self.state_changed.emit(tr("Отключено"))
            logger.info("COM-логгер: мониторинг %s остановлен", self._port_name)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)

    def write(self, data: bytes) -> bool:
        with self._port_lock:
            ser = self._ser
        if ser is None or not ser.is_open:
            self.error.emit(tr("Порт не подключен"))
            return False
        try:
            ser.write(data)
            ser.flush()
            logger.info("COM-логгер: отправлено %d байт в %s", len(data), self._port_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("COM-логгер: ошибка отправки в %s: %s", self._port_name, exc)
            self.error.emit(str(exc))
            return False


class ComLoggerWindow(QDialog):
    """Отдельное окно COM-логгера."""

    def __init__(self, serial_manager: Optional[SerialManager] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("COM логгер"))
        self.resize(950, 700)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._config = Config()
        self._serial_manager = serial_manager
        self._reader: Optional[ComLoggerReader] = None
        self._main_listener: bool = False
        self._listen_mode = ListenOnlyMode(self)
        self._listen_mode.packet_ready.connect(self._on_packet)
        self._listen_mode.raw_chunk_ready.connect(self._on_raw_chunk)
        self._listen_mode.is_active_changed.connect(self._set_connected)
        # Буферы склейки для фильтра CAN: кадр может быть разрезан
        # между двумя чанками, поэтому хвост храним до следующей порции.
        self._filter_buf_rx = bytearray()
        self._filter_buf_tx = bytearray()

        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._refresh_ports()
        self._load_defaults()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._port_label = QLabel(tr("Порт"))
        self._port_label.setFont(font)
        self._port_combo = QComboBox()
        self._port_combo.setFont(font)
        self._shrinkable(self._port_combo)

        self._virtual_port_label = QLabel(tr("Виртуальный порт"))
        self._virtual_port_label.setFont(font)
        self._virtual_port_combo = QComboBox()
        self._virtual_port_combo.setFont(font)
        self._shrinkable(self._virtual_port_combo)

        self._refresh_button = QPushButton(tr("Обновить список"))
        self._refresh_button.setFont(font)

        self._baud_label = QLabel(tr("Скорость порта"))
        self._baud_label.setFont(font)
        self._baud_combo = QComboBox()
        self._baud_combo.setFont(font)
        for b in BAUDRATES:
            self._baud_combo.addItem(str(b), b)
        self._shrinkable(self._baud_combo)

        self._open_button = QPushButton(tr("Подключить"))
        self._open_button.setFont(font)

        self._main_checkbox = QCheckBox(tr("Режим прокси (com0com)"))
        self._main_checkbox.setFont(font)
        self._main_checkbox.setToolTip(tr("Логер встаёт между реальным портом и виртуальной парой"))
        if self._serial_manager is None:
            pass

        self._status_label = QLabel(tr("Отключено"))
        self._status_label.setFont(font)
        # Длинный статус (путь к CSV) не должен раздвигать окно по горизонтали
        self._status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._table = QTableWidget()
        self._table.setFont(QFont("Consolas", 10))
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([tr("Время"), tr("Направление"), tr("Данные (HEX)"), tr("ASCII")])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self._hex_checkbox = QCheckBox(tr("HEX"))
        self._hex_checkbox.setFont(font)
        self._hex_checkbox.setChecked(True)

        self._autoscroll_button = QPushButton(tr("Автоскролл"))
        self._autoscroll_button.setFont(font)
        self._autoscroll_button.setCheckable(True)
        self._autoscroll_button.setChecked(False)
        self._autoscroll_button.setToolTip(
            tr("Автоматически прокручивать таблицу к последнему сообщению")
        )
        self._autoscroll_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
            "QPushButton:checked { background-color: #4CAF50; color: #FFFFFF; }"
        )
        self._autoscroll_button.toggled.connect(self._on_autoscroll_toggled)

        self._filter_button = QPushButton(tr("Фильтр"))
        self._filter_button.setFont(font)
        self._filter_button.setCheckable(True)
        self._filter_button.setChecked(False)
        self._filter_button.setToolTip(
            tr("Показывать только CAN-кадры, скрыть служебный обмен")
        )
        self._filter_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
            "QPushButton:checked { background-color: #2196F3; color: #FFFFFF; }"
        )
        self._filter_button.toggled.connect(self._on_filter_toggled)

        self._send_input = QLineEdit()
        self._send_input.setFont(QFont("Consolas", 10))
        self._send_input.setPlaceholderText(tr("Введите HEX: 01 02 03 или текст"))
        self._send_input.returnPressed.connect(self._on_send)
        self._send_input.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setFont(font)
        self._send_button.setEnabled(False)

        self._clear_button = QPushButton(tr("Очистить"))
        self._clear_button.setFont(font)

        self._export_button = QPushButton(tr("Сохранить лог"))
        self._export_button.setFont(font)
        self._export_button.setToolTip(tr("Выгрузить COM-лог в текстовый файл"))

    @staticmethod
    def _shrinkable(widget: QWidget) -> None:
        """Разрешает виджету сжиматься, чтобы окно можно было сузить."""
        widget.setMinimumWidth(70)
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Порт и виртуальный порт — верхний ряд
        ports = QHBoxLayout()
        ports.setSpacing(8)
        ports.addWidget(self._port_label)
        ports.addWidget(self._port_combo, 1)
        ports.addWidget(self._virtual_port_label)
        ports.addWidget(self._virtual_port_combo, 1)
        ports.addWidget(self._refresh_button)
        root.addLayout(ports)

        # Скорость — отдельный ряд, без чекбокса, чтобы чекбокс не
        # перекрывал выбор скорости.
        speed = QHBoxLayout()
        speed.setSpacing(8)
        speed.addWidget(self._baud_label)
        speed.addWidget(self._baud_combo)
        speed.addStretch()
        root.addLayout(speed)

        # Режим прокси, кнопка подключения и статус — следующий ряд
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self._main_checkbox)
        actions.addSpacing(12)
        actions.addWidget(self._open_button)
        actions.addWidget(self._status_label, 1)
        root.addLayout(actions)

        root.addWidget(self._table, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self._hex_checkbox)
        bottom.addWidget(self._autoscroll_button)
        bottom.addWidget(self._filter_button)
        bottom.addWidget(self._send_input, 1)
        bottom.addWidget(self._send_button)
        bottom.addWidget(self._clear_button)
        bottom.addWidget(self._export_button)
        root.addLayout(bottom)

    def _connect_signals(self) -> None:
        self._refresh_button.clicked.connect(self._refresh_ports)
        self._open_button.clicked.connect(self._on_open_close)
        self._send_button.clicked.connect(self._on_send)
        self._clear_button.clicked.connect(self._clear_table)
        self._export_button.clicked.connect(self._on_export_log)
        self._port_combo.currentIndexChanged.connect(self._on_port_changed)
        self._virtual_port_combo.currentIndexChanged.connect(self._on_virtual_port_changed)
        self._main_checkbox.stateChanged.connect(self._on_main_toggled)

    def _load_defaults(self) -> None:
        baud = self._config.get("com_logger_baud", 115200)
        idx = self._baud_combo.findData(baud)
        if idx >= 0:
            self._baud_combo.setCurrentIndex(idx)
        saved_port = self._config.get("com_logger_port", "")
        if saved_port:
            idx = self._port_combo.findData(saved_port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
        saved_virtual = self._config.get("com_logger_virtual_port", "")
        if saved_virtual:
            idx = self._virtual_port_combo.findData(saved_virtual)
            if idx >= 0:
                self._virtual_port_combo.setCurrentIndex(idx)
        proxy_enabled = self._config.get("com_logger_proxy", False)
        self._main_checkbox.setChecked(proxy_enabled)
        self._on_main_toggled(self._main_checkbox.checkState().value)

    def _on_port_changed(self) -> None:
        port = self._port_combo.currentData()
        if port:
            self._config.set("com_logger_port", port)

    def _on_virtual_port_changed(self) -> None:
        port = self._virtual_port_combo.currentData()
        if port:
            self._config.set("com_logger_virtual_port", port)

    def _refresh_ports(self) -> None:
        for combo in (self._port_combo, self._virtual_port_combo):
            current = combo.currentData()
            combo.clear()
            combo.addItem(tr("-- выберите порт --"), "")
            for p in comports():
                text = f"{p.device}"
                if p.description:
                    text += f" — {p.description}"
                combo.addItem(text, p.device)
            if current:
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def _on_open_close(self) -> None:
        if self._main_listener:
            self._disconnect_main()
        elif self._reader is not None and self._reader.isRunning():
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        if self._main_checkbox.isChecked():
            self._connect_proxy()
            return
        port = self._port_combo.currentData()
        if not port:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите COM-порт"))
            return
        baud = self._baud_combo.currentData()
        if baud is None:
            baud = 115200

        self._reader = ComLoggerReader(port, baud, self)
        self._reader.data_received.connect(self._on_data_received)
        self._reader.error.connect(self._on_reader_error)
        self._reader.connection_changed.connect(self._on_connection_changed)
        self._reader.state_changed.connect(self._status_label.setText)
        self._reader.start()

        self._config.set_bulk({"com_logger_port": port, "com_logger_baud": baud})

    def _connect_proxy(self) -> None:
        real = self._port_combo.currentData()
        virtual = self._virtual_port_combo.currentData()
        if not real:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите реальный COM-порт"))
            return
        if not virtual:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите виртуальный COM-порт (com0com)"))
            return
        if real == virtual:
            QMessageBox.warning(self, tr("Внимание"), tr("Реальный и виртуальный порты должны различаться"))
            return
        baud = self._baud_combo.currentData()
        if baud is None:
            baud = 115200

        device_name = self._config.get("device_type_name", "")
        ok = self._listen_mode.enable(
            log_dir=get_log_dir(),
            device_name=device_name,
            mode="proxy",
            real_port=real,
            virtual_port=virtual,
            baudrate=baud,
        )
        if not ok:
            QMessageBox.warning(self, tr("Внимание"), tr("Не удалось запустить режим 'Только слушать'"))
            return
        self._main_listener = True
        self._set_connected(True)
        log_path = self._listen_mode.log_path
        short = Path(log_path).name if log_path else "-"
        self._status_label.setText(tr("CSV: {0}").format(short))
        self._status_label.setToolTip(log_path or "-")

    def _disconnect(self) -> None:
        if self._reader is not None:
            self._reader.stop()
            self._reader = None
        self._set_connected(False)

    def _disconnect_main(self) -> None:
        self._listen_mode.disable()
        self._main_listener = False
        self._set_connected(False)
        self._status_label.setText(tr("Отключено"))

    def _on_connection_changed(self, connected: bool) -> None:
        if not connected and self._reader is not None:
            self._reader = None
        self._set_connected(connected)

    def _on_main_toggled(self, state: int) -> None:
        checked = state == Qt.CheckState.Checked.value
        self._config.set("com_logger_proxy", checked)
        if checked:
            self._main_checkbox.setStyleSheet("QCheckBox { background-color: #4CAF50; color: #FFFFFF; padding: 4px 8px; border-radius: 4px; }")
            self._port_label.setText(tr("Реальный порт"))
            self._virtual_port_label.setVisible(True)
            self._virtual_port_combo.setVisible(True)
            self._port_combo.setEnabled(not self._main_listener)
            self._virtual_port_combo.setEnabled(not self._main_listener)
            self._baud_combo.setEnabled(not self._main_listener)
            self._refresh_button.setEnabled(not self._main_listener)
        else:
            self._main_checkbox.setStyleSheet("")
            self._port_label.setText(tr("Порт"))
            self._virtual_port_label.setVisible(False)
            self._virtual_port_combo.setVisible(False)
            self._port_combo.setEnabled(not self._main_listener)
            self._baud_combo.setEnabled(not self._main_listener)
            self._refresh_button.setEnabled(True)

    def _on_main_connection_changed(self, connected: bool) -> None:
        if not connected and self._main_listener:
            self._disconnect_main()

    def _set_connected(self, connected: bool) -> None:
        self._open_button.setText(tr("Отключить") if connected else tr("Подключить"))
        self._send_button.setEnabled(connected and not self._main_listener)
        if not self._main_checkbox.isChecked():
            self._port_combo.setEnabled(not connected)
            self._baud_combo.setEnabled(not connected)
        else:
            self._port_combo.setEnabled(not connected)
            self._virtual_port_combo.setEnabled(not connected)
            self._baud_combo.setEnabled(not connected)
            self._refresh_button.setEnabled(not connected)
        if not connected:
            self._main_listener = False
            self._reader = None
            self._status_label.setText(tr("Отключено"))

    def _on_data_received(self, data: bytes, timestamp: float) -> None:
        if self._filter_button.isChecked():
            self._log_filtered_chunk(True, data, timestamp)
            return
        self._add_row(tr("RX"), data, timestamp, self._rx_color())

    def _on_packet(self, pkt: CanPacket) -> None:
        prefix = tr("МК") if pkt.is_rx else tr("Конф")
        direction = f"{prefix} 0x{pkt.can_id:04X} ({pkt.dlc})"
        self._add_row(direction, pkt.data, time.time(), self._rx_color() if pkt.is_rx else self._tx_color())

    def _on_raw_chunk(self, is_rx: bool, data: bytes) -> None:
        if self._filter_button.isChecked():
            self._log_filtered_chunk(is_rx, data, time.time())
            return
        direction = tr("МК") if is_rx else tr("Конф")
        self._add_row(direction, data, time.time(), self._rx_color() if is_rx else self._tx_color())

    def _log_filtered_chunk(self, is_rx: bool, data: bytes, timestamp: float) -> None:
        """Показывает только CAN-кадры из сырого потока (режим «Фильтр»).

        Служебный обмен приложения и МК (опрос статуса, ответы команд)
        пропускается; полные CAN-кадры вырезаются из потока и показываются
        отдельными строками — в том числе разрезанные между чанками.
        """
        buf = self._filter_buf_rx if is_rx else self._filter_buf_tx
        buf += data
        frames, leftover = parse_all_frames(bytes(buf), tx=not is_rx)
        buf[:] = leftover
        if len(buf) > 4096:
            del buf[:-256]
        color = self._rx_color() if is_rx else self._tx_color()
        direction = tr("МК") if is_rx else tr("Конф")
        for frame in frames:
            self._add_row(direction, frame["raw"], timestamp, color)

    def _on_send(self) -> None:
        if self._main_listener:
            if self._serial_manager is None or not self._serial_manager.is_open():
                QMessageBox.warning(self, tr("Внимание"), tr("Основной порт не подключён"))
                return
        elif self._reader is None or not self._reader.isRunning():
            QMessageBox.warning(self, tr("Внимание"), tr("Порт не подключен"))
            return
        raw = self._send_input.text().strip()
        if not raw:
            return
        if self._hex_checkbox.isChecked():
            try:
                data = self._parse_hex_string(raw)
            except ValueError as exc:
                QMessageBox.warning(self, tr("Ошибка"), str(exc))
                return
        else:
            try:
                data = raw.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, tr("Ошибка"), str(exc))
                return
        if self._main_listener:
            ok = self._serial_manager.send_data(data)
        else:
            ok = self._reader.write(data)
        if ok:
            self._add_row(tr("TX"), data, time.time(), self._tx_color())

    def _on_autoscroll_toggled(self, checked: bool) -> None:
        """Визуально отображает состояние автопрокрутки."""
        if checked:
            self._autoscroll_button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #45A049; }"
            )
        else:
            self._autoscroll_button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )

    def _on_filter_toggled(self, checked: bool) -> None:
        """Визуально отображает состояние фильтра CAN-кадров."""
        if checked:
            self._filter_button.setStyleSheet(
                "QPushButton { background-color: #2196F3; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #1976D2; }"
            )
        else:
            self._filter_button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )

    @staticmethod
    def _parse_hex_string(text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        # Поддержка форматов: "01 02 03", "0x01 0x02", "010203"
        normalized = text.replace("0x", " ").replace("0X", " ")
        parts = re.split(r"[\s,]+", normalized)
        parts = [p for p in parts if p]
        if len(parts) == 1 and len(parts[0]) > 2:
            # возможно строка вида 010203
            hex_str = parts[0]
            if len(hex_str) % 2 != 0:
                raise ValueError(tr("Нечётное количество HEX-символов"))
            parts = [hex_str[i : i + 2] for i in range(0, len(hex_str), 2)]
        result = bytearray()
        for part in parts:
            try:
                result.append(int(part, 16))
            except ValueError as exc:
                raise ValueError(tr("Неверный HEX-байт: {0}").format(part)) from exc
        return bytes(result)

    def _add_row(self, direction: str, data: bytes, timestamp: float, color: QColor) -> None:
        if not data:
            return
        row = self._table.rowCount()
        if row >= MAX_ROWS:
            self._table.removeRow(0)
            row = MAX_ROWS - 1
        self._table.insertRow(row)

        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
        hex_str = " ".join(f"{b:02X}" for b in data)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in data)

        items = [
            QTableWidgetItem(time_str),
            QTableWidgetItem(direction),
            QTableWidgetItem(hex_str),
            QTableWidgetItem(ascii_str),
        ]
        for item in items:
            item.setBackground(color)
            item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            if self._is_dark_theme():
                item.setForeground(QColor(255, 255, 255))
        self._table.setItem(row, 0, items[0])
        self._table.setItem(row, 1, items[1])
        self._table.setItem(row, 2, items[2])
        self._table.setItem(row, 3, items[3])

        if self._autoscroll_button.isChecked():
            self._table.scrollToBottom()

    def _tx_color(self) -> QColor:
        return QColor(30, 130, 76) if self._is_dark_theme() else QColor(200, 230, 201)

    def _rx_color(self) -> QColor:
        return QColor(25, 100, 140) if self._is_dark_theme() else QColor(187, 222, 251)

    def _is_dark_theme(self) -> bool:
        try:
            base = QApplication.palette().base().color()
            return base.lightness() < 128
        except Exception:  # noqa: BLE001
            return False

    def _on_reader_error(self, message: str) -> None:
        logger.error("Ошибка COM-логгера: %s", message)
        self._status_label.setText(tr("Ошибка: {0}").format(message))

    def _clear_table(self) -> None:
        self._table.setRowCount(0)

    def _on_export_log(self) -> None:
        """Сохраняет содержимое таблицы COM-логгера в текстовый файл."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить COM-лог"),
            "",
            tr("Текстовые файлы (*.txt);;Все файлы (*.*)"),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{tr('Время')}\t{tr('Направление')}\t{tr('Данные (HEX)')}\t{tr('ASCII')}\n")
                f.write("-" * 80 + "\n")
                for row in range(self._table.rowCount()):
                    time_text = self._table.item(row, 0).text() if self._table.item(row, 0) else ""
                    dir_text = self._table.item(row, 1).text() if self._table.item(row, 1) else ""
                    hex_text = self._table.item(row, 2).text() if self._table.item(row, 2) else ""
                    ascii_text = self._table.item(row, 3).text() if self._table.item(row, 3) else ""
                    f.write(f"{time_text}\t{dir_text}\t{hex_text}\t{ascii_text}\n")
            self._status_label.setText(tr("Лог сохранён: {0}").format(path))
            logger.info("COM-лог сохранён: %s", path)
        except OSError as exc:
            logger.error("Не удалось сохранить COM-лог: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить лог:\n{0}").format(exc))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._disconnect()
        event.accept()
