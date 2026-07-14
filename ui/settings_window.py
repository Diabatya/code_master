"""Окно настроек и рабочего CAN-пространства."""

from typing import Optional

from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCompleter,
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
from ui.analog_ports_tab import AnalogPortsTab
from ui.can_analyzer import CanAnalyzer
from ui.can_gateway_tab import CanGatewayTab
from ui.signal_graph_tab import SignalGraphTab
from ui.can_monitor_tab import CanMonitorTab
from ui.can_trigger_tab import CanTriggerTab
from ui.flexible_logic_tab import FlexibleLogicTab
from ui.library_browser import LibraryBrowser
from ui.logic_tab import LogicTab

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
        self._logic_tab = LogicTab(self._serial_manager, self)
        self._library_tab = LibraryBrowser(self._trigger_tab, self._flexible_tab, self)
        self._graph_tab = SignalGraphTab(self._serial_manager, self)
        self._analyzer_tab = CanAnalyzer(self._serial_manager, self)
        self._analog_tab: Optional[AnalogPortsTab] = None

        self._tabs.addTab(self._trigger_tab, "⚡ " + tr("Триггеры"))
        self._tabs.addTab(self._monitor_tab, "🔍 " + tr("Мониторинг"))
        self._tabs.addTab(self._gateway_tab, "🚦 " + tr("Шлюз"))
        self._tabs.addTab(self._logic_tab, "🧩 " + tr("Логика"))
        self._tabs.addTab(self._flexible_tab, "🧩 " + tr("Гибкая логика"))
        self._tabs.addTab(self._library_tab, "📚 " + tr("Библиотека"))
        self._tabs.addTab(self._graph_tab, "📈 " + tr("Графики"))
        self._tabs.addTab(self._analyzer_tab, "🔬 " + tr("Трэйс"))
        self._update_analog_tab()
        self._serial_manager.device_identified.connect(self._update_analog_tab)

        search_layout = QHBoxLayout()
        search_layout.setSpacing(8)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(tr("Поиск по разделам..."))
        self._search_edit.setFixedWidth(220)
        self._search_edit.textChanged.connect(self._on_search_changed)

        self._completer = QCompleter(self._search_edit)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._search_edit.setCompleter(self._completer)
        self._completer.activated.connect(self._on_search_activated)

        self._search_map: Dict[str, int] = {}
        self._build_search_keywords()

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

    def _update_analog_tab(self) -> None:
        """Добавляет или удаляет вкладку аналоговых портов в зависимости от типа устройства."""
        analog_index = self._tabs.indexOf(self._analog_tab) if self._analog_tab else -1
        if self._config.get("device_type") == 0x01:
            if analog_index < 0:
                self._analog_tab = AnalogPortsTab(self)
                self._tabs.addTab(self._analog_tab, "🌊 " + tr("Аналоговые порты"))
                self._build_search_keywords()
        else:
            if analog_index >= 0:
                self._tabs.removeTab(analog_index)
                self._analog_tab = None
                self._build_search_keywords()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки окна настроек и всех вкладок."""
        self.setWindowTitle(tr("Настройки — Код Мастер"))
        self._search_edit.setPlaceholderText(tr("Поиск по разделам..."))
        titles = {
            self._trigger_tab: "⚡ " + tr("Триггеры"),
            self._monitor_tab: "🔍 " + tr("Мониторинг"),
            self._gateway_tab: "🚦 " + tr("Шлюз"),
            self._logic_tab: "🧩 " + tr("Логика"),
            self._flexible_tab: "🧩 " + tr("Гибкая логика"),
            self._library_tab: "📚 " + tr("Библиотека"),
            self._graph_tab: "📈 " + tr("Графики"),
            self._analyzer_tab: "🔬 " + tr("Трэйс"),
        }
        if self._analog_tab is not None:
            titles[self._analog_tab] = "🌊 " + tr("Аналоговые порты")
        for widget, title in titles.items():
            idx = self._tabs.indexOf(widget)
            if idx >= 0:
                self._tabs.setTabText(idx, title)
        self._build_search_keywords()
        self._save_button.setText(tr("Сохранить"))
        self._save_config_button.setText(tr("Сохранить конфигурацию"))
        self._factory_reset_button.setText(tr("Заводские настройки"))
        self._back_button.setText(tr("Назад"))
        for tab in (
            self._trigger_tab,
            self._monitor_tab,
            self._gateway_tab,
            self._logic_tab,
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

    def _build_search_keywords(self) -> None:
        """Строит словарь ключевых слов для быстрого поиска вкладок."""
        keywords: Dict[QWidget, List[str]] = {
            self._trigger_tab: ["trigger", "триггеры", "триггер", "кэш", "cache", "ответ", "response", "frame", "фрейм", "data", "данные"],
            self._monitor_tab: ["monitor", "monitoring", "мониторинг", "фильтр", "filter", "канал", "channel", "can1", "can2", "поиск", "search", "send", "отправить"],
            self._gateway_tab: ["gateway", "шлюз", "rule", "правило", "ignore", "игнорировать", "replace", "подмена"],
            self._logic_tab: ["logic", "логика", "events", "события", "conditions", "условия", "actions", "действия", "chain", "цепочка"],
            self._flexible_tab: ["flexible logic", "гибкая логика", "logic", "логика", "rules", "правила"],
            self._library_tab: ["library", "библиотека", "dbc", "id", "make", "марка", "model", "модель", "database", "база"],
            self._graph_tab: ["graph", "график", "signal", "сигнал", "chart", "plot"],
            self._analyzer_tab: ["trace", "трейс", "analyzer", "анализатор", "log", "лог"],
        }
        if self._analog_tab is not None:
            keywords[self._analog_tab] = ["analog", "аналоговые порты", "ports", "порты", "adc"]

        self._search_map = {}
        for widget, words in keywords.items():
            idx = self._tabs.indexOf(widget)
            if idx < 0:
                continue
            title = self._tabs.tabText(idx).strip().lower()
            if title:
                self._search_map[title] = idx
                clean = "".join(ch for ch in title if ch.isalnum() or ch.isspace()).strip()
                if clean:
                    self._search_map[clean] = idx
                for part in title.split():
                    self._search_map[part] = idx
            for word in words:
                self._search_map[word.lower()] = idx

        self._completer.setModel(QStringListModel(sorted(self._search_map.keys())))

    def _on_search_changed(self, text: str) -> None:
        """Переключает вкладку по введённой подстроке (регистр не важен)."""
        query = text.strip().lower()
        if not query:
            return
        if query in self._search_map:
            self._tabs.setCurrentIndex(self._search_map[query])
            return
        for keyword, idx in self._search_map.items():
            if query in keyword:
                self._tabs.setCurrentIndex(idx)
                return

    def _on_search_activated(self, text: str) -> None:
        """Переключает вкладку при выборе пункта из выпадающего списка."""
        query = text.strip().lower()
        if query in self._search_map:
            self._tabs.setCurrentIndex(self._search_map[query])

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
