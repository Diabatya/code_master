"""Страница «Прошивка» с тремя столбцами: ПО блока, Автомобиль, Конфигурация."""

from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.bootloader import Bootloader, BootloaderError
from core.can_protocol import (
    DEVICE_TYPE_BASIC as FIRMWARE_DEVICE_TYPE_2_CAN,
    DEVICE_TYPE_ANALOG as FIRMWARE_DEVICE_TYPE_2_CAN_PLUS,
    DEVICE_TYPE_CAN_FD as FIRMWARE_DEVICE_TYPE_2_CAN_FD,
)
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr

# Новые типы устройств для выбора в окне прошивки
# 2 CAN = 0x00, 2 CAN+ (с аналоговыми портами) = 0x01, 2 CAN FD = 0x02

try:
    from serial.tools.list_ports import comports
except Exception:  # noqa: BLE001
    def comports() -> list:
        return []

logger = get_logger(__name__)

DEMO_FW_VERSIONS: Dict[str, str] = {
    "v1.0.0": "",
    "v1.1.0": "",
    "v1.2.5": "",
    "v2.0.0": "",
    "v2.1.3": "",
}

DEMO_CARS: Dict[str, str] = {
    "Toyota Camry — v2.1.3": "",
    "Ford Focus — v1.2.5": "",
    "BMW E46 — v2.0.0": "",
    "Audi A4 — v1.1.0": "",
    "VW Golf — v2.1.3": "",
}


class BootloaderWorker(QThread):
    """Фоновый поток для операций bootloader."""

    progress = Signal(int)
    finished_success = Signal(str)
    finished_error = Signal(str)
    info_ready = Signal(str)

    def __init__(
        self,
        serial_manager: SerialManager,
        mode: str,
        firmware_path: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._mode = mode
        self._firmware_path = firmware_path
        self._config = Config()
        self._port: Optional[Any] = None
        self._bootloader: Optional[Bootloader] = None
        self._was_open = False
        self._port_name = ""
        self._baudrate = 115200

    def _open_bootloader_port(self) -> Any:
        """Открывает COM-порт напрямую, освобождая его от SerialManager."""
        import serial as serial_module

        self._port_name = self._serial_manager.current_port_name() or self._config.get("port", "")
        self._baudrate = self._config.get("baudrate", 115200)
        if not self._port_name:
            raise BootloaderError(tr("COM-порт не указан"))

        # Освобождаем порт от SerialManager/SerialReader
        self._was_open = self._serial_manager.is_open()
        if self._was_open:
            self._serial_manager.close_port()

        return serial_module.Serial(
            self._port_name,
            self._baudrate,
            bytesize=serial_module.EIGHTBITS,
            parity=serial_module.PARITY_EVEN,
            stopbits=serial_module.STOPBITS_ONE,
            timeout=1,
        )

    def _close_and_restore(self) -> None:
        """Закрывает bootloader-порт и восстанавливает SerialManager."""
        try:
            if self._port is not None:
                self._port.close()
        except Exception:  # noqa: S110
            pass
        if self._was_open and self._port_name:
            try:
                self._serial_manager.open_port(self._port_name, self._baudrate)
            except Exception:  # noqa: S110
                pass

    def run(self) -> None:
        try:
            self._port = self._open_bootloader_port()
            self._bootloader = Bootloader(self._port, progress_callback=self.progress.emit)

            if self._mode == "diagnostics":
                info = self._bootloader.diagnostics()
                version = info.get("version", 0)
                device_id = info.get("device_id", 0)
                self.info_ready.emit(
                    tr("Версия bootloader: 0x{0:02X}, ID устройства: 0x{1:08X}").format(version, device_id)
                )
            elif self._mode == "flash":
                if not self._firmware_path or not Path(self._firmware_path).exists():
                    raise BootloaderError(tr("Файл прошивки не выбран или не существует"))
                self._bootloader.flash_firmware(self._firmware_path)
                self.finished_success.emit(tr("Прошивка завершена успешно"))
            else:
                raise BootloaderError(tr("Неизвестный режим: {0}").format(self._mode))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка bootloader")
            self.finished_error.emit(str(exc))
        finally:
            self._close_and_restore()

    def stop(self) -> None:
        self.requestInterruption()
        if self._bootloader is not None:
            self._bootloader.request_stop()
        self.wait(2000)


class FirmwarePage(QWidget):
    """Страница выбора типа устройства для прошивки."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._update_device_info()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Прошивка STM32"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._device_type_label = QLabel(tr("Устройство"))
        self._device_type_label.setFont(font)

        self._device_type_combo = QComboBox()
        self._device_type_combo.setFont(font)
        self._device_type_combo.setMinimumWidth(160)
        self._device_type_combo.addItem(tr("2 CAN"), FIRMWARE_DEVICE_TYPE_2_CAN)
        self._device_type_combo.addItem(tr("2 CAN +"), FIRMWARE_DEVICE_TYPE_2_CAN_PLUS)
        self._device_type_combo.addItem(tr("2 CAN FD"), FIRMWARE_DEVICE_TYPE_2_CAN_FD)
        self._device_type_combo.currentIndexChanged.connect(self._on_device_type_changed)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._title)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        row.addWidget(self._device_type_label)
        row.addWidget(self._device_type_combo)
        row.addStretch()
        layout.addLayout(row)

        layout.addStretch(1)

    def _connect_signals(self) -> None:
        """Подключает сигналы SerialManager к UI."""
        self._serial_manager.device_identified.connect(self._update_device_info)

    def _update_device_info(self, device_type: int = 0, device_version: int = 0) -> None:
        """Обновляет выпадающий список типа устройства из конфига."""
        _ = device_version
        device_type = self._config.get("device_type", device_type)
        index = self._device_type_combo.findData(device_type)
        if index < 0:
            index = 0
        self._device_type_combo.blockSignals(True)
        self._device_type_combo.setCurrentIndex(index)
        self._device_type_combo.blockSignals(False)

    def _on_device_type_changed(self, index: int) -> None:
        """Сохраняет выбранный тип устройства в конфиг."""
        device_type = self._device_type_combo.itemData(index)
        if device_type is not None:
            self._config.set("device_type", device_type)

    def retranslate_ui(self) -> None:
        """Обновляет статические строки страницы."""
        self._title.setText(tr("Прошивка STM32"))
        self._device_type_label.setText(tr("Устройство"))
        current_type = self._device_type_combo.currentData()
        self._device_type_combo.clear()
        self._device_type_combo.addItem(tr("2 CAN"), FIRMWARE_DEVICE_TYPE_2_CAN)
        self._device_type_combo.addItem(tr("2 CAN +"), FIRMWARE_DEVICE_TYPE_2_CAN_PLUS)
        self._device_type_combo.addItem(tr("2 CAN FD"), FIRMWARE_DEVICE_TYPE_2_CAN_FD)
        if current_type is not None:
            index = self._device_type_combo.findData(current_type)
            if index >= 0:
                self._device_type_combo.setCurrentIndex(index)
