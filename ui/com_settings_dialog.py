"""Модальный диалог выбора и подключения COM-порта."""

from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr

try:
    from serial import Serial
    from serial.tools.list_ports import comports
except Exception:  # noqa: BLE001
    def comports() -> list:
        return []


logger = get_logger(__name__)



class BaudRateDetector(QThread):
    """Фоновый поток автоопределения скорости COM-порта по bootloader sync."""

    baud_found = Signal(int)
    finished_no_result = Signal()

    def __init__(self, port_name: str, parent: Optional[QDialog] = None) -> None:
        super().__init__(parent)
        self._port_name = port_name
        self._baud_rates = [9600, 19200, 38400, 57600, 115200]
        self._timeout = 0.5

    def run(self) -> None:
        for baud in self._baud_rates:
            if self.isInterruptionRequested():
                break
            try:
                with Serial(self._port_name, baud, timeout=self._timeout) as port:
                    port.write_timeout = 0.5
                    port.reset_input_buffer()
                    port.reset_output_buffer()
                    port.write(bytes([0x7F]))
                    response = port.read(1)
                    if response == bytes([0x79]):
                        self.baud_found.emit(baud)
                        return
            except Exception:  # noqa: BLE001
                continue
        self.finished_no_result.emit()


class ComSettingsDialog(QDialog):
    """Диалог выбора COM-порта и подключения."""

    connected = Signal()

    def __init__(self, serial_manager: SerialManager, parent: Optional[QDialog] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self.setWindowTitle(tr("Настройка подключения"))
        self.setModal(True)
        self.setMinimumWidth(360)
        self.setStyleSheet("background-color: #252538;")
        self._create_widgets()
        self._build_layout()
        self._load_defaults()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._port_label = QLabel(tr("COM-порт:"))
        self._port_label.setFont(font)

        self._port_combo = QComboBox()
        self._port_combo.setFont(font)
        self._port_combo.setMinimumWidth(240)

        self._baud_label = QLabel(tr("Скорость:"))
        self._baud_label.setFont(font)

        self._baud_combo = QComboBox()
        self._baud_combo.setFont(font)
        self._baud_combo.addItems(["9600", "19200", "38400", "57600", "115200", "230400", "460800"])

        self._auto_baud_button = QPushButton(tr("Автоопределить"))
        self._auto_baud_button.setFixedSize(130, 30)
        self._auto_baud_button.setFont(font)
        self._auto_baud_button.clicked.connect(self._on_auto_baudrate)

        self._status_label = QLabel("")
        self._status_label.setFont(font)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._connect_button = QPushButton(tr("Подключить"))
        self._connect_button.setFixedSize(120, 34)
        self._connect_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._connect_button.clicked.connect(self._on_connect)

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._button_box.rejected.connect(self.reject)
        self._button_box.addButton(self._connect_button, QDialogButtonBox.ButtonRole.AcceptRole)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(self._port_label)
        layout.addWidget(self._port_combo)
        layout.addWidget(self._baud_label)
        baud_layout = QHBoxLayout()
        baud_layout.addWidget(self._baud_combo, 1)
        baud_layout.addWidget(self._auto_baud_button)
        layout.addLayout(baud_layout)
        layout.addSpacing(8)
        layout.addWidget(self._status_label)
        layout.addStretch()
        layout.addWidget(self._button_box)

    def _load_defaults(self) -> None:
        self._refresh_ports()
        saved_port = self._config.get("port", "")
        if saved_port:
            index = self._port_combo.findText(saved_port)
            if index < 0:
                self._port_combo.addItem(saved_port)
                index = self._port_combo.count() - 1
            self._port_combo.setCurrentIndex(index)

        saved_baud = str(self._config.get("baudrate", 115200))
        index = self._baud_combo.findText(saved_baud)
        if index >= 0:
            self._baud_combo.setCurrentIndex(index)

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentText()
        self._port_combo.clear()
        self._port_combo.addItem(tr("FAKE (эмулятор)"))
        for port_info in comports():
            self._port_combo.addItem(port_info.device)
        if current:
            index = self._port_combo.findText(current)
            if index >= 0:
                self._port_combo.setCurrentIndex(index)
            else:
                self._port_combo.setCurrentIndex(0)

    def _on_auto_baudrate(self) -> None:
        port_text = self._port_combo.currentText()
        if not port_text or port_text.startswith("FAKE"):
            self._set_status(tr("Выберите реальный COM-порт"), error=True)
            return
        if self._serial_manager.is_open():
            self._serial_manager.close_port()

        self._auto_baud_button.setEnabled(False)
        self._set_status(tr("Определение скорости..."), error=False)

        self._baud_detector = BaudRateDetector(port_text, self)
        self._baud_detector.baud_found.connect(self._on_baud_found)
        self._baud_detector.finished_no_result.connect(self._on_baud_not_found)
        self._baud_detector.start()

    def _on_baud_found(self, baud: int) -> None:
        self._auto_baud_button.setEnabled(True)
        index = self._baud_combo.findText(str(baud))
        if index >= 0:
            self._baud_combo.setCurrentIndex(index)
        self._set_status(tr("Скорость определена: {0}").format(baud), error=False)

    def _on_baud_not_found(self) -> None:
        self._auto_baud_button.setEnabled(True)
        self._set_status(tr("Не удалось определить скорость"), error=True)

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status_label.setText(text)
        color = "#F44336" if error else "#4CAF50"
        self._status_label.setStyleSheet(f"color: {color};")

    def _on_connect(self) -> None:
        port_text = self._port_combo.currentText()
        port_name = "FAKE" if port_text.startswith("FAKE") else port_text
        baudrate = int(self._baud_combo.currentText())
        emulation = port_text.startswith("FAKE")

        if not port_name:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите COM-порт"))
            return

        self._config.set_bulk({
            "port": port_name,
            "baudrate": baudrate,
            "emulation": emulation,
        })
        self._set_status(tr("Выбран {0}").format(port_name), error=False)
        logger.info("Выбран порт через диалог: %s", port_name)
        self.connected.emit()
        self.accept()
