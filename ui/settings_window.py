"""Окно настроек и рабочего CAN-пространства."""

from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFileDialog,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import DEVICE_TYPE_ANALOG, DEVICE_TYPE_BASIC, DEVICE_TYPE_CAN_FD

from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr, get_all_translations
from ui.ui_utils import setup_button
from ui.analog_ports_tab import AnalogPortsTab
from ui.can_analyzer import CanAnalyzer
from ui.can_gateway_tab import CanGatewayTab
from ui.can_monitor_tab import CanMonitorTab
from ui.can_trigger_tab import CanTriggerTab
from ui.flexible_logic_tab import FlexibleLogicTab
from ui.hex_edit import HexDataEdit
from ui.library_browser import LibraryBrowser
from ui.memory_indicator import MemoryIndicator

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

        font = QFont("Segoe UI", 10)
        self._device_label = QLabel(tr("Устройство"))
        self._device_label.setFont(font)
        self._device_combo = QComboBox()
        self._device_combo.setFont(font)
        self._device_combo.setMinimumWidth(180)
        self._device_combo.addItem(tr("2 CAN"), DEVICE_TYPE_BASIC)
        self._device_combo.addItem(tr("2 CAN +"), DEVICE_TYPE_ANALOG)
        self._device_combo.addItem(tr("2 CAN FD"), DEVICE_TYPE_CAN_FD)
        self._device_combo.currentIndexChanged.connect(self._on_device_type_changed)
        device_layout = QHBoxLayout()
        device_layout.setSpacing(8)
        device_layout.addWidget(self._device_label)
        device_layout.addWidget(self._device_combo)
        device_layout.addStretch()
        self._device_layout = device_layout

        self._tabs = QTabWidget()
        self._tabs.setFont(QFont("Segoe UI", 10))

        self._trigger_tab = CanTriggerTab(self._serial_manager, self)
        self._monitor_tab = CanMonitorTab(self._serial_manager, self)
        self._gateway_tab = CanGatewayTab(self._serial_manager, self)
        self._flexible_tab = FlexibleLogicTab(self._serial_manager, self)
        self._library_tab = LibraryBrowser(self._trigger_tab, self._flexible_tab, self)
        self._analyzer_tab = CanAnalyzer(self._serial_manager, self)
        self._analog_tab: Optional[AnalogPortsTab] = None

        self._tabs.addTab(self._trigger_tab, "⚡ " + tr("Триггеры"))
        self._tabs.addTab(self._monitor_tab, "🔍 " + tr("Мониторинг"))
        self._tabs.addTab(self._gateway_tab, "🚦 " + tr("Шлюз"))
        self._tabs.addTab(self._flexible_tab, "🧩 " + tr("Гибкая логика"))
        self._tabs.addTab(self._library_tab, "📚 " + tr("Библиотека"))
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
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(15)
        self._completer.setCompletionRole(int(Qt.ItemDataRole.UserRole))
        self._search_edit.setCompleter(self._completer)
        self._completer.activated.connect(self._on_search_activated)

        self._search_model = QStandardItemModel(self)
        self._completer.setModel(self._search_model)

        self._highlight_timer: Optional[QTimer] = None
        self._highlighted_widget: Optional[QWidget] = None
        self._original_style: str = ""
        self._build_search_index()

        search_layout.addWidget(self._search_edit)
        search_layout.addStretch()
        layout.addLayout(self._device_layout)
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

    def _on_device_type_changed(self, index: int) -> None:
        """Сохраняет выбранный тип устройства в конфигурации."""
        device_type = self._device_combo.itemData(index)
        if device_type is not None:
            self._config.set("device_type", device_type)
            self._update_analog_tab()

    def _update_analog_tab(self) -> None:
        """Добавляет или удаляет вкладку аналоговых портов в зависимости от типа устройства."""
        analog_index = self._tabs.indexOf(self._analog_tab) if self._analog_tab else -1
        if self._config.get("device_type") == DEVICE_TYPE_ANALOG:
            if analog_index < 0:
                self._analog_tab = AnalogPortsTab(self)
                self._tabs.addTab(self._analog_tab, "🌊 " + tr("Аналоговые порты"))
                self._build_search_index()
        else:
            if analog_index >= 0:
                self._tabs.removeTab(analog_index)
                self._analog_tab = None
                self._build_search_index()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки окна настроек и всех вкладок."""
        self.setWindowTitle(tr("Настройки — Код Мастер"))
        self._device_label.setText(tr("Устройство"))
        current_type = self._device_combo.currentData()
        self._device_combo.clear()
        self._device_combo.addItem(tr("2 CAN"), DEVICE_TYPE_BASIC)
        self._device_combo.addItem(tr("2 CAN +"), DEVICE_TYPE_ANALOG)
        self._device_combo.addItem(tr("2 CAN FD"), DEVICE_TYPE_CAN_FD)
        if current_type is not None:
            index = self._device_combo.findData(current_type)
            if index >= 0:
                self._device_combo.setCurrentIndex(index)
        self._search_edit.setPlaceholderText(tr("Поиск по разделам..."))
        titles = {
            self._trigger_tab: "⚡ " + tr("Триггеры"),
            self._monitor_tab: "🔍 " + tr("Мониторинг"),
            self._gateway_tab: "🚦 " + tr("Шлюз"),
            self._flexible_tab: "🧩 " + tr("Гибкая логика"),
            self._library_tab: "📚 " + tr("Библиотека"),
            self._analyzer_tab: "🔬 " + tr("Трэйс"),
        }
        if self._analog_tab is not None:
            titles[self._analog_tab] = "🌊 " + tr("Аналоговые порты")
        for widget, title in titles.items():
            idx = self._tabs.indexOf(widget)
            if idx >= 0:
                self._tabs.setTabText(idx, title)
        self._build_search_index()
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
            self._analyzer_tab,
        ):
            if hasattr(tab, "retranslate_ui"):
                tab.retranslate_ui()

    def _connect_signals(self) -> None:
        self._serial_manager.new_can_frame.connect(self._trigger_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._gateway_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._monitor_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._flexible_tab.process_frame)
        self._serial_manager.new_can_frame.connect(self._analyzer_tab.process_frame)
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._monitor_tab.create_trigger_requested.connect(self._on_create_trigger)

    def _on_create_trigger(self, packet: dict) -> None:
        self._trigger_tab.create_trigger_from_packet(packet)
        self._tabs.setCurrentWidget(self._trigger_tab)

    def _build_search_index(self) -> None:
        """Строит индекс поиска по вкладкам и элементам интерфейса."""
        self._search_model.clear()

        for idx in range(self._tabs.count()):
            tab_widget = self._tabs.widget(idx)
            if tab_widget is None:
                continue
            tab_title = self._tabs.tabText(idx).strip()
            self._add_search_items_for_tab(idx, tab_widget, tab_title)

        self._add_manual_search_items()

    def _add_search_items_for_tab(self, tab_index: int, tab_widget: QWidget, tab_title: str) -> None:
        """Добавляет в индекс поиска элементы вкладки."""
        processed: set = set()
        for child in tab_widget.findChildren(QWidget):
            if child in processed:
                continue
            processed.add(child)
            if child is self._search_edit or child is self._tabs:
                continue
            if isinstance(child, (QTabWidget, QScrollArea, HexDataEdit, QSpinBox)):
                continue
            if isinstance(child, MemoryIndicator):
                continue
            if self._is_inside_memory_indicator(child):
                continue
            texts = self._extract_widget_texts(child)
            if not texts:
                continue
            self._add_search_items_for_widget(tab_index, tab_title, child, texts)

    def _is_inside_memory_indicator(self, widget: QWidget) -> bool:
        """Проверяет, что виджет находится внутри MemoryIndicator."""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, MemoryIndicator):
                return True
            parent = parent.parentWidget()
        return False

    def _add_search_items_for_widget(
        self,
        tab_index: int,
        tab_title: str,
        widget: QWidget,
        texts: List[str],
    ) -> None:
        """Добавляет один элемент поиска с несколькими текстами."""
        primary = texts[0]
        match_parts: List[str] = []
        for text in texts:
            match_parts.extend(self._collect_text_translations(text))
        match_parts.extend(self._collect_text_translations(tab_title))
        match_text = " ".join(dict.fromkeys(match_parts)).lower()

        context = self._get_widget_context(widget)
        display = f"{tab_title}: {primary}"
        if context:
            display = f"{display} — {context}"
        unique_match = f"{match_text} {id(widget)}"

        item = QStandardItem(display)
        item.setData(unique_match, Qt.ItemDataRole.UserRole)
        item.setData((tab_index, widget), int(Qt.ItemDataRole.UserRole) + 1)
        self._search_model.appendRow(item)

    def _get_widget_context(self, widget: QWidget) -> str:
        """Возвращает название ближайшего группового предка для уточнения поиска."""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QGroupBox):
                title = parent.title().strip()
                if title:
                    return title
            parent = parent.parentWidget()
        return ""

    def _add_manual_search_items(self) -> None:
        """Добавляет в индекс элементы, не имеющие собственных текстов."""
        # CAN1 / CAN2 в мониторинге
        if self._monitor_tab is not None:
            monitor_index = self._tabs.indexOf(self._monitor_tab)
            if monitor_index >= 0:
                monitor_title = self._tabs.tabText(monitor_index)
                for channel, monitor_widget in (("CAN1", getattr(self._monitor_tab, "_monitor1", None)),
                                                 ("CAN2", getattr(self._monitor_tab, "_monitor2", None))):
                    if monitor_widget is not None:
                        self._add_search_items_for_widget(
                            monitor_index, monitor_title, monitor_widget, [channel, tr("Канал")]
                        )

    def _is_noise_text(self, text: str) -> bool:
        """Проверяет, что текст не является поисковой меткой."""
        if text.isdigit() and len(text) <= 2:
            return True
        if text.endswith("%") and text[:-1].isdigit():
            return True
        if text.endswith("%") and text[:-1].replace(".", "", 1).isdigit():
            return True
        if len(text) <= 1:
            return True
        if text.startswith("D") and len(text) == 2 and text[1].isdigit() and 0 <= int(text[1]) <= 7:
            return True
        return False

    def _extract_widget_texts(self, widget: QWidget) -> List[str]:
        """Извлекает текстовые метки из виджета."""
        texts: List[str] = []
        if isinstance(widget, QTabWidget):
            return texts

        if hasattr(widget, "title") and callable(widget.title):
            title = widget.title().strip()
            if title:
                texts.append(title)
        if hasattr(widget, "text") and callable(widget.text):
            text = widget.text().strip()
            if text and text not in texts:
                if not self._is_noise_text(text):
                    texts.append(text)
        if hasattr(widget, "placeholderText") and callable(widget.placeholderText):
            placeholder = widget.placeholderText().strip()
            if placeholder and placeholder not in texts:
                if not self._is_noise_text(placeholder):
                    texts.append(placeholder)
        if hasattr(widget, "toolTip") and callable(widget.toolTip):
            tooltip = widget.toolTip().strip()
            if tooltip and tooltip not in texts:
                texts.append(tooltip)
        if hasattr(widget, "currentText") and callable(widget.currentText):
            current = widget.currentText().strip()
            if current and current not in texts:
                texts.append(current)
        if hasattr(widget, "suffix") and callable(widget.suffix):
            suffix = widget.suffix().strip()
            if suffix and suffix not in texts:
                texts.append(suffix)
        if hasattr(widget, "prefix") and callable(widget.prefix):
            prefix = widget.prefix().strip()
            if prefix and prefix not in texts:
                texts.append(prefix)
        return texts

    def _collect_text_translations(self, text: str) -> List[str]:
        """Возвращает список вариантов строки на всех доступных языках."""
        text = text.strip()
        if not text:
            return []

        cleaned = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()
        if not cleaned:
            cleaned = text

        result: set = set()
        for translation in get_all_translations(cleaned):
            if translation and translation not in result:
                result.add(translation)

        # Поддержка "Триггер 1", "Правило 1" и т.д.
        parts = cleaned.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            base, num = parts
            for tmpl in get_all_translations(f"{base} {{0}}"):
                if "{0}" in tmpl:
                    result.add(tmpl.replace("{0}", num))

        if not result:
            result.add(cleaned)
        return list(result)

    def _on_search_changed(self, text: str) -> None:
        """Показывает выпадающий список с найденными элементами."""
        if text.strip():
            self._completer.complete()

    def _on_search_activated(self, text: str) -> None:
        """Переключает вкладку, прокручивает и выделяет выбранный элемент."""
        for row in range(self._search_model.rowCount()):
            item = self._search_model.item(row)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole) == text:
                data = item.data(int(Qt.ItemDataRole.UserRole) + 1)
                if data is not None:
                    tab_index, widget = data
                    self._search_edit.blockSignals(True)
                    self._search_edit.setText(item.text())
                    self._search_edit.blockSignals(False)
                    self._tabs.setCurrentIndex(tab_index)
                    self._scroll_to_widget(widget)
                    self._highlight_widget(widget)
                break

    def _scroll_to_widget(self, widget: QWidget) -> None:
        """Прокручивает область к виджету."""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(widget)
                break
            parent = parent.parentWidget()
        widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _highlight_widget(self, widget: QWidget) -> None:
        """Кратковременно выделяет виджет рамкой."""
        self._reset_highlight()
        self._highlighted_widget = widget
        self._original_style = widget.styleSheet()
        class_name = widget.__class__.__name__
        widget.setStyleSheet(f"{class_name} {{ border: 2px solid #FFD700; }}")
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._reset_highlight)
        self._highlight_timer.start(1200)

    def _reset_highlight(self) -> None:
        """Снимает выделение с виджета."""
        if self._highlight_timer is not None:
            self._highlight_timer.stop()
            self._highlight_timer = None
        if self._highlighted_widget is not None:
            self._highlighted_widget.setStyleSheet(self._original_style)
            self._highlighted_widget = None

    def _on_serial_error(self, message: str) -> None:
        logger.error("Ошибка COM-порта: %s", message)

    def _device_name_for_filename(self) -> str:
        device_type = self._config.get("device_type", DEVICE_TYPE_BASIC)
        if device_type == DEVICE_TYPE_ANALOG:
            return "2CAN_Plus"
        if device_type == DEVICE_TYPE_CAN_FD:
            return "2CAN_FD"
        return "2CAN"

    def _save_config(self) -> None:
        default_name = f"{self._device_name_for_filename()}_config.cfg"
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить конфигурацию"),
            default_name,
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
        if not self._serial_manager.is_open() or not self._serial_manager.ping_device():
            QMessageBox.warning(self, tr("Внимание"), tr("Нет связи с устройством. Переподключите USB"))
            return
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
            self._analyzer_tab,
        ):
            if hasattr(tab, "set_dbc"):
                tab.set_dbc(dbc_manager)

    def closeEvent(self, event) -> None:  # noqa: N802
        logger.info("Закрыто окно настроек")
        if self.parent() is not None:
            self.parent().show()
        event.accept()
