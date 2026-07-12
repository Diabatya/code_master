"""Окно настроек и рабочего CAN-пространства."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
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
from ui.ui_utils import setup_button
from ui.can_analyzer import CanAnalyzer
from ui.can_gateway_tab import CanGatewayTab
from ui.signal_graph_tab import SignalGraphTab
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
        self.resize(1100, 700)
        self.setMinimumSize(850, 500)

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
        self._graph_tab = SignalGraphTab(self._serial_manager, self)
        self._analyzer_tab = CanAnalyzer(self._serial_manager, self)

        self._tabs.addTab(self._trigger_tab, "⚡ " + tr("Триггеры"))
        self._tabs.addTab(self._monitor_tab, "🔍 " + tr("Мониторинг"))
        self._tabs.addTab(self._gateway_tab, "🚦 " + tr("Шлюз"))
        self._tabs.addTab(self._flexible_tab, "🧩 " + tr("Гибкая логика"))
        self._tabs.addTab(self._library_tab, "📚 " + tr("Библиотека"))
        self._tabs.addTab(self._graph_tab, "📈 " + tr("Графики"))
        self._tabs.addTab(self._analyzer_tab, "🔬 " + tr("Трэйс"))

        search_layout = QHBoxLayout()
        search_layout.setSpacing(8)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(tr("Поиск по разделам..."))
        self._search_edit.setFixedWidth(220)
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self._search_edit)
        search_layout.addStretch()
        layout.addLayout(search_layout)

        layout.addWidget(self._tabs, 1)

        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(8)

        self._save_button = QPushButton(tr("Сохранить"))
        setup_button(self._save_button, height=32)
        self._save_button.clicked.connect(self._save_current_config)

        self._save_config_button = QPushButton(tr("Сохранить конфигурацию"))
        setup_button(self._save_config_button, height=32)
        self._save_config_button.clicked.connect(self._save_config)

        self._factory_reset_button = QPushButton(tr("Заводские настройки"))
        setup_button(self._factory_reset_button, height=32)
        self._factory_reset_button.clicked.connect(self._factory_reset)

        self._back_button = QPushButton(tr("Назад"))
        setup_button(self._back_button, height=32)
        self._back_button.clicked.connect(self._on_back)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self._save_button)
        bottom_layout.addWidget(self._save_config_button)
        bottom_layout.addWidget(self._factory_reset_button)
        bottom_layout.addWidget(self._back_button)
        layout.addLayout(bottom_layout)

        self._connect_signals()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки окна настроек и всех вкладок."""
        self.setWindowTitle(tr("Настройки — Код Мастер"))
        self._search_edit.setPlaceholderText(tr("Поиск по разделам..."))
        self._tabs.setTabText(0, "⚡ " + tr("Триггеры"))
        self._tabs.setTabText(1, "🔍 " + tr("Мониторинг"))
        self._tabs.setTabText(2, "🚦 " + tr("Шлюз"))
        self._tabs.setTabText(3, "🧩 " + tr("Гибкая логика"))
        self._tabs.setTabText(4, "📚 " + tr("Библиотека"))
        self._tabs.setTabText(5, "📈 " + tr("Графики"))
        self._tabs.setTabText(6, "🔬 " + tr("Трэйс"))
        self._save_button.setText(tr("Сохранить"))
        self._save_config_button.setText(tr("Сохранить конфигурацию"))
        self._factory_reset_button.setText(tr("Заводские настройки"))
        self._back_button.setText(tr("Назад"))
        for tab in (
            self._trigger_tab,
            self._monitor_tab,
            self._gateway_tab,
            self._flexible_tab,
            self._library_tab,
            self._graph_tab,
            self._analyzer_tab,
        ):
            if hasattr(tab, "retranslate_ui"):
                tab.retranslate_ui()

    def _connect_signals(self) -> None:
        self._serial_manager.new_can_frame.connect(self._trigger_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._gateway_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._monitor_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._flexible_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._graph_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._analyzer_tab.process_frame)
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._monitor_tab.create_trigger_requested.connect(self._on_create_trigger)

    def _on_create_trigger(self, packet: dict) -> None:
        self._trigger_tab.create_trigger_from_packet(packet)
        self._tabs.setCurrentWidget(self._trigger_tab)

    def _on_search_changed(self, text: str) -> None:
        """Переключает вкладку по введённой подстроке (регистр не важен)."""
        query = text.strip().lower()
        if not query:
            return
        for i in range(self._tabs.count()):
            tab_text = self._tabs.tabText(i).lower()
            # Убираем эмодзи и пробелы для сравнения
            clean_text = "".join(ch for ch in tab_text if ch.isalnum() or ch.isspace()).strip()
            if query in clean_text or query in tab_text:
                self._tabs.setCurrentIndex(i)
                return

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

    def _save_current_config(self) -> None:
        """Сохраняет текущую конфигурацию в файл по умолчанию."""
        try:
            self._config.save()
            self._trigger_tab._save_config()
            self._flexible_tab._save_config()
            self._gateway_tab._save_config() if hasattr(self._gateway_tab, "_save_config") else None
            QMessageBox.information(self, tr("Готово"), tr("Настройки сохранены"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить: {0}").format(exc))

    def _factory_reset(self) -> None:
        """Сбрасывает настройки к заводским с подтверждением."""
        answer = QMessageBox.question(
            self,
            tr("Заводские настройки"),
            tr("Вернуть все настройки к значениям по умолчанию?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._config.reset_to_defaults()
            self._config.save()
            self._trigger_tab.set_config(self._config.get("triggers", []))
            self._flexible_tab.set_config(self._config.get("flexible_rules", []))
            QMessageBox.information(self, tr("Готово"), tr("Настройки сброшены"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сбросить: {0}").format(exc))

    def _on_back(self) -> None:
        """Закрывает окно настроек и возвращает главное окно."""
        self.close()

    def set_dbc(self, dbc_manager) -> None:
        """Уведомляет все вкладки о смене загруженного DBC."""
        for tab in (
            self._trigger_tab,
            self._gateway_tab,
            self._monitor_tab,
            self._flexible_tab,
            self._graph_tab,
            self._analyzer_tab,
        ):
            if hasattr(tab, "set_dbc"):
                tab.set_dbc(dbc_manager)

    def closeEvent(self, event) -> None:  # noqa: N802
        logger.info("Закрыто окно настроек")
        if self.parent() is not None:
            self.parent().show()
        event.accept()
