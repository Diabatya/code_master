"""Окно настроек и рабочего CAN-пространства."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from ui.can_analyzer import CanAnalyzer
from ui.can_gateway_tab import CanGatewayTab
from ui.can_graph_tab import CanGraphTab
from ui.can_monitor_tab import CanMonitorTab
from ui.can_trigger_tab import CanTriggerTab
from ui.flexible_logic_tab import FlexibleLogicTab
from ui.library_browser import LibraryBrowser

logger = get_logger(__name__)


class SettingsWindow(QMainWindow):
    """Окно настроек с вкладками для работы с CAN."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self.setWindowTitle(tr("Настройки — Код Мастер"))
        self.resize(900, 650)
        self.setMinimumSize(700, 500)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._tabs = QTabWidget()
        self._tabs.setFont(QFont("Segoe UI", 10))

        self._trigger_tab = CanTriggerTab(self._serial_manager, self)
        self._monitor_tab = CanMonitorTab(self._serial_manager, self)
        self._gateway_tab = CanGatewayTab(self._serial_manager, self)
        self._flexible_tab = FlexibleLogicTab(self._serial_manager, self)
        self._library_tab = LibraryBrowser(self._trigger_tab, self._flexible_tab, self)
        self._graph_tab = CanGraphTab(self._serial_manager, self)
        self._analyzer_tab = CanAnalyzer(self._serial_manager, self)

        self._tabs.addTab(self._trigger_tab, "⚡ " + tr("Триггеры"))
        self._tabs.addTab(self._monitor_tab, "🔍 " + tr("Мониторинг"))
        self._tabs.addTab(self._gateway_tab, "🚦 " + tr("Шлюз"))
        self._tabs.addTab(self._flexible_tab, "🧩 " + tr("Гибкая логика"))
        self._tabs.addTab(self._library_tab, "📚 " + tr("Библиотека"))
        self._tabs.addTab(self._graph_tab, "📈 " + tr("График"))
        self._tabs.addTab(self._analyzer_tab, "🔬 " + tr("Анализатор"))

        layout.addWidget(self._tabs, 1)

        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(8)
        self._close_button = QPushButton(tr("Закрыть"))
        self._close_button.setFixedSize(110, 32)
        self._close_button.clicked.connect(self.close)

        self._save_config_button = QPushButton(tr("Сохранить конфигурацию"))
        self._save_config_button.setFixedSize(160, 32)
        self._save_config_button.clicked.connect(self._save_config)

        self._load_config_button = QPushButton(tr("Загрузить конфигурацию"))
        self._load_config_button.setFixedSize(160, 32)
        self._load_config_button.clicked.connect(self._load_config)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self._save_config_button)
        bottom_layout.addWidget(self._load_config_button)
        bottom_layout.addWidget(self._close_button)
        layout.addLayout(bottom_layout)

        self._connect_signals()

    def _connect_signals(self) -> None:
        self._serial_manager.new_can_frame.connect(self._trigger_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._monitor_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._flexible_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._graph_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._analyzer_tab.process_frame)
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._monitor_tab.create_trigger_requested.connect(self._on_create_trigger)

    def _on_create_trigger(self, packet: dict) -> None:
        self._trigger_tab.create_trigger_from_packet(packet)
        self._tabs.setCurrentWidget(self._trigger_tab)

    def _on_serial_error(self, message: str) -> None:
        logger.error("Ошибка COM-порта: %s", message)

    def _save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить конфигурацию"),
            "",
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            self._config.save_to_file(path)
            QMessageBox.information(self, tr("Готово"), tr("Конфигурация сохранена"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить: {0}").format(exc))

    def _load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить конфигурацию"),
            "",
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            self._config.load_from_file(path)
            self._trigger_tab.set_config(self._config.get("triggers", []))
            self._flexible_tab.set_config(self._config.get("flexible_rules", []))
            QMessageBox.information(self, tr("Готово"), tr("Конфигурация загружена"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить: {0}").format(exc))

    def closeEvent(self, event) -> None:  # noqa: N802
        logger.info("Закрыто окно настроек")
        event.accept()
