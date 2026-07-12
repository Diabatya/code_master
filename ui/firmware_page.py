"""Страница «Прошивка» с тремя столбцами: ПО блока, Автомобиль, Конфигурация."""

from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.bootloader import Bootloader, BootloaderError
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from ui.ui_utils import setup_button

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
        self._port = None

    def run(self) -> None:
        try:
            self._port = self._serial_manager._port
            if self._port is None:
                raise BootloaderError(tr("COM-порт не открыт"))

            bootloader = Bootloader(self._port, progress_callback=self.progress.emit)

            if self._mode == "diagnostics":
                info = bootloader.diagnostics()
                version = info.get("version", 0)
                device_id = info.get("device_id", 0)
                self.info_ready.emit(
                    tr("Версия bootloader: 0x{0:02X}, ID устройства: 0x{1:08X}").format(version, device_id)
                )
            elif self._mode == "flash":
                if not self._firmware_path or not Path(self._firmware_path).exists():
                    raise BootloaderError(tr("Файл прошивки не выбран или не существует"))
                bootloader.flash_firmware(self._firmware_path)
                self.finished_success.emit(tr("Прошивка завершена успешно"))
            else:
                raise BootloaderError(tr("Неизвестный режим: {0}").format(self._mode))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка bootloader")
            self.finished_error.emit(str(exc))

    def stop(self) -> None:
        self.requestInterruption()
        self.wait(2000)


class FirmwarePage(QWidget):
    """Страница управления прошивкой STM32."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._worker: Optional[BootloaderWorker] = None
        self._config = Config()
        self._create_widgets()
        self._build_layout()
        self._populate_fw_list()
        self._populate_car_list()

    def _make_browse_button(self, callback) -> QPushButton:
        """Создаёт чёткую кнопку выбора файла с крупным символом папки."""
        button = QPushButton("📂")
        button.setFixedSize(40, 40)
        button.setFont(QFont("Segoe UI", 24))
        button.setToolTip(tr("Выбрать файл с компьютера"))
        button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
        )
        button.clicked.connect(callback)
        return button

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Прошивка STM32"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        # Столбец 1: ПО блока
        self._fw_group = QGroupBox(tr("ПО блока"))
        self._fw_group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._fw_list = QListWidget()
        self._fw_list.setFont(font)
        self._fw_update_button = QPushButton(tr("Обновить"))
        setup_button(self._fw_update_button, height=30)
        self._fw_update_button.clicked.connect(self._on_fw_update)
        self._fw_browse_button = self._make_browse_button(self._on_fw_browse)

        # Столбец 2: Автомобиль
        self._car_group = QGroupBox(tr("Автомобиль"))
        self._car_group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._car_search = QLineEdit()
        self._car_search.setFont(font)
        self._car_search.setPlaceholderText(tr("Поиск по авто или версии"))
        self._car_search.textChanged.connect(self._on_car_search)
        self._car_list = QListWidget()
        self._car_list.setFont(font)
        self._car_update_button = QPushButton(tr("Обновить"))
        setup_button(self._car_update_button, height=30)
        self._car_update_button.clicked.connect(self._on_car_update)
        self._car_browse_button = self._make_browse_button(self._on_car_browse)

        # Столбец 3: Конфигурация
        self._config_group = QGroupBox(tr("Конфигурация"))
        self._config_group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._config_label = QLabel(tr("Конфигурация не загружена"))
        self._config_label.setFont(font)
        self._config_label.setWordWrap(True)
        self._config_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._config_update_button = QPushButton(tr("Обновить"))
        setup_button(self._config_update_button, height=30)
        self._config_update_button.clicked.connect(self._on_config_update)
        self._config_browse_button = self._make_browse_button(self._on_config_browse)

        # Общий прогресс и статус
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFont(font)

        self._status_label = QLabel("")
        self._status_label.setFont(font)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._title)

        columns = QHBoxLayout()
        columns.setSpacing(12)

        # ПО блока
        fw_layout = QVBoxLayout(self._fw_group)
        fw_layout.setSpacing(8)
        fw_layout.addWidget(self._fw_list, 1)
        fw_buttons = QHBoxLayout()
        fw_buttons.addWidget(self._fw_update_button)
        fw_buttons.addWidget(self._fw_browse_button)
        fw_layout.addLayout(fw_buttons)
        columns.addWidget(self._fw_group, 1)

        # Автомобиль
        car_layout = QVBoxLayout(self._car_group)
        car_layout.setSpacing(8)
        car_layout.addWidget(self._car_search)
        car_layout.addWidget(self._car_list, 1)
        car_buttons = QHBoxLayout()
        car_buttons.addWidget(self._car_update_button)
        car_buttons.addWidget(self._car_browse_button)
        car_layout.addLayout(car_buttons)
        columns.addWidget(self._car_group, 1)

        # Конфигурация
        config_layout = QVBoxLayout(self._config_group)
        config_layout.setSpacing(8)
        config_layout.addWidget(self._config_label, 1)
        config_buttons = QHBoxLayout()
        config_buttons.addWidget(self._config_update_button)
        config_buttons.addWidget(self._config_browse_button)
        config_layout.addLayout(config_buttons)
        columns.addWidget(self._config_group, 1)

        layout.addLayout(columns, 1)
        layout.addWidget(self._progress)
        layout.addWidget(self._status_label)

    def _populate_fw_list(self) -> None:
        self._fw_list.clear()
        for version in sorted(DEMO_FW_VERSIONS.keys()):
            display = self._display_name(version, DEMO_FW_VERSIONS[version])
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, DEMO_FW_VERSIONS[version])
            self._fw_list.addItem(item)
        if self._fw_list.count():
            self._fw_list.setCurrentRow(0)

    def _populate_car_list(self, filter_text: str = "") -> None:
        self._car_list.clear()
        text = filter_text.strip().lower()
        for name in sorted(DEMO_CARS.keys()):
            if text and text not in name.lower():
                continue
            display = self._display_name(name, DEMO_CARS[name])
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, DEMO_CARS[name])
            self._car_list.addItem(item)
        if self._car_list.count():
            self._car_list.setCurrentRow(0)

    def _display_name(self, name: str, path: str) -> str:
        if path:
            return name
        return f"{name} {tr('(загрузите файл)')}"

    def _selected_file(self, list_widget: QListWidget) -> Optional[str]:
        item = list_widget.currentItem()
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            return path
        return None

    def _browse_firmware(self) -> Optional[str]:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Выбор файла прошивки"),
            "",
            tr("Firmware files (*.bin *.hex);;All files (*.*)"),
        )
        return path if path else None

    def _flash_file(self, path: str) -> None:
        self._start_worker("flash", path)

    def _on_fw_update(self) -> None:
        path = self._selected_file(self._fw_list)
        if not path:
            QMessageBox.warning(self, tr("Внимание"), tr("Для выбранной версии ПО не выбран файл. Нажмите кнопку 📂."))
            return
        self._flash_file(path)

    def _on_fw_browse(self) -> None:
        path = self._browse_firmware()
        if not path:
            return
        item = self._fw_list.currentItem()
        if item is not None:
            base = item.text().replace(f" {tr('(загрузите файл)')}", "").strip()
            item.setText(base)
            item.setData(Qt.ItemDataRole.UserRole, path)
        self._flash_file(path)

    def _on_car_update(self) -> None:
        path = self._selected_file(self._car_list)
        if not path:
            QMessageBox.warning(self, tr("Внимание"), tr("Для выбранного автомобиля не выбран файл. Нажмите кнопку 📂."))
            return
        self._flash_file(path)

    def _on_car_browse(self) -> None:
        path = self._browse_firmware()
        if not path:
            return
        item = self._car_list.currentItem()
        if item is not None:
            base = item.text().replace(f" {tr('(загрузите файл)')}", "").strip()
            item.setText(base)
            item.setData(Qt.ItemDataRole.UserRole, path)
        self._flash_file(path)

    def _on_car_search(self, text: str) -> None:
        self._populate_car_list(text)

    def _on_config_update(self) -> None:
        path = self._config.label if hasattr(self._config, "label") else None
        # Демо-заглушка: берём путь из Config, если сохранён
        config_path = self._config.get("last_config_firmware", "")
        if not config_path:
            self._set_status(tr("Конфигурация не загружена"), error=True)
            return
        self._flash_file(config_path)

    def _on_config_browse(self) -> None:
        path = self._browse_firmware()
        if not path:
            return
        self._config.set("last_config_firmware", path)
        self._config_label.setText(tr("Конфигурация: {0}").format(Path(path).name))
        self._flash_file(path)

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status_label.setText(text)
        color = "#F44336" if error else "#4CAF50"
        self._status_label.setStyleSheet(f"color: {color};")

    def _start_worker(self, mode: str, firmware_path: str = "") -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(self, tr("Внимание"), tr("Операция уже выполняется"))
            return

        if not self._serial_manager.is_open():
            self._set_status(tr("COM-порт не открыт. Подключитесь в мастере подключения."), error=True)
            return

        self._progress.setValue(0)
        self._set_status(tr("Выполнение..."))
        self._set_buttons_enabled(False)

        self._worker = BootloaderWorker(self._serial_manager, mode, firmware_path, self)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished_success.connect(self._on_success)
        self._worker.finished_error.connect(self._on_error)
        self._worker.info_ready.connect(self._on_info_ready)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._fw_update_button.setEnabled(enabled)
        self._fw_browse_button.setEnabled(enabled)
        self._car_update_button.setEnabled(enabled)
        self._car_browse_button.setEnabled(enabled)
        self._config_update_button.setEnabled(enabled)
        self._config_browse_button.setEnabled(enabled)

    def _on_success(self, message: str) -> None:
        self._set_status(message)
        QMessageBox.information(self, tr("Готово"), message)

    def _on_error(self, message: str) -> None:
        self._set_status(message, error=True)
        QMessageBox.critical(self, tr("Ошибка"), message)

    def _on_info_ready(self, message: str) -> None:
        self._set_status(message)

    def _on_worker_finished(self) -> None:
        self._set_buttons_enabled(True)
        self._worker = None
