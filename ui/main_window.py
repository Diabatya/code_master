"""Главное окно приложения «Код Мастер»."""

import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.dbc_manager import DBCManager
from core.serial_manager import SerialManager
from core.update_checker import check_for_updates
from models.config import Config
from models.logger import get_logger, get_log_dir
from models.translations import _ as tr, set_language
from ui.com_settings_dialog import ComSettingsDialog
from ui.dark_theme import apply_theme
from ui.firmware_page import FirmwarePage
from ui.flash_dialog import FlashDialog
from ui.help_widget import show_help
from ui.settings_window import SettingsWindow

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """Главное окно приложения «Код Мастер»."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт главное окно."""
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        set_language(self._config.get("language", "ru"))

        self.setWindowTitle(tr("Код Мастер"))
        self.resize(800, 600)
        self.setMinimumSize(640, 480)
        self.setWindowFlags(
            Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        central = QWidget(self)
        self.setCentralWidget(central)

        self._settings_window: Optional[SettingsWindow] = None
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._setup_shortcuts()
        self._set_theme_button_icon()
        self._set_language_combo()
        self._update_port_indicator()

    def _create_widgets(self) -> None:
        """Создаёт виджеты главного окна."""
        font = QFont("Segoe UI", 10)

        # Верхняя панель
        self._top_panel = QWidget()
        self._top_panel.setObjectName("topPanel")
        self._top_panel.setFixedHeight(48)

        self._logo_label = QLabel("🛠️ " + tr("Код Мастер"))
        self._logo_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._logo_label.setProperty("title", True)

        self._port_indicator = QLabel("●")
        self._port_indicator.setFixedSize(20, 20)
        self._port_indicator.setStyleSheet("color: #666666; font-size: 14px; background: transparent;")
        self._port_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._port_indicator.setToolTip(tr("Индикатор подключения COM-порта"))

        self._port_label = QLabel(tr("Нет подключения"))
        self._port_label.setFont(font)

        self._theme_button = QPushButton(tr("Тема"))
        self._theme_button.setFixedSize(70, 28)
        self._theme_button.setFont(font)
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.setToolTip(tr("Выбор темы оформления"))
        self._theme_menu = QMenu(self._theme_button)
        self._dark_theme_action = self._theme_menu.addAction(tr("Тёмный"), self._set_dark_theme)
        self._light_theme_action = self._theme_menu.addAction(tr("Светлый"), self._set_light_theme)
        self._starline_theme_action = self._theme_menu.addAction(tr("StarLine"), self._set_starline_theme)
        self._theme_button.setMenu(self._theme_menu)

        self._language_combo = QComboBox()
        self._language_combo.setFixedSize(110, 28)
        self._language_combo.setFont(font)
        self._language_combo.addItem(tr("Русский"), "ru")
        self._language_combo.addItem(tr("English"), "en")
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)

        self._logs_button = QPushButton("📄 " + tr("Логи"))
        self._logs_button.setFixedSize(80, 28)
        self._logs_button.setFont(font)
        self._logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logs_button.clicked.connect(self._open_logs)

        self._help_button = QPushButton("?")
        self._help_button.setFixedSize(36, 28)
        self._help_button.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._help_button.setToolTip(tr("Помощь"))
        self._help_button.clicked.connect(self._on_help_clicked)

        self._update_check_button = QPushButton("🔄")
        self._update_check_button.setFixedSize(36, 28)
        self._update_check_button.setFont(font)
        self._update_check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._update_check_button.clicked.connect(self._on_check_updates_clicked)

        # Главные кнопки в теле окна
        self._update_button = QPushButton("🔄 " + tr("Обновить"))
        self._update_button.setFixedSize(240, 100)
        self._update_button.setFont(QFont("Segoe UI", 16))
        self._update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_button.clicked.connect(self._on_update_clicked)

        self._configure_button = QPushButton("⚙️ " + tr("Настроить"))
        self._configure_button.setFixedSize(240, 100)
        self._configure_button.setFont(QFont("Segoe UI", 16))
        self._configure_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._configure_button.clicked.connect(self._on_configure_clicked)

        self._flash_button = QPushButton(tr("Прошить\nмикроконтроллер"))
        self._flash_button.setFixedSize(260, 110)
        self._flash_button.setFont(QFont("Segoe UI", 13))
        self._flash_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flash_button.setToolTip(tr("Открыть окно прошивки"))
        self._flash_button.clicked.connect(self._on_flash_clicked)

        # Главное меню
        self._central_stack = QStackedWidget()

        self._startup_page = QWidget()

        # Страница прошивки
        self._firmware_page = FirmwarePage(self._serial_manager, self)
        self._firmware_page_back_button = QPushButton(tr("← Назад"))
        self._firmware_page_back_button.setFixedSize(100, 30)
        self._firmware_page_back_button.clicked.connect(self._show_startup_page)

        # Статус-бар
        self._status_bar = QStatusBar()
        self._status_label = QLabel(tr("Готов"))
        self._status_label.setFont(font)
        self._status_bar.addWidget(self._status_label)
        self._status_bar.showMessage("v1.0.0")

        # Таймер heartbeat
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._reset_port_indicator)
        self._heartbeat_timer.start(1500)

    def _build_layout(self) -> None:
        """Собирает компоновку главного окна."""
        root = QVBoxLayout(self.centralWidget())
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        top_layout = QHBoxLayout(self._top_panel)
        top_layout.setContentsMargins(12, 0, 20, 0)
        top_layout.setSpacing(10)
        top_layout.addWidget(self._logo_label)
        top_layout.addWidget(self._language_combo)
        top_layout.addStretch()
        top_layout.addWidget(self._port_indicator)
        top_layout.addWidget(self._port_label)
        top_layout.addSpacing(10)
        top_layout.addWidget(self._theme_button)
        top_layout.addWidget(self._logs_button)
        top_layout.addWidget(self._help_button)
        top_layout.addWidget(self._update_check_button)
        root.addWidget(self._top_panel)

        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(20, 20, 20, 0)
        action_layout.setSpacing(16)
        action_layout.addStretch()
        action_layout.addWidget(self._update_button)
        action_layout.addWidget(self._configure_button)
        action_layout.addStretch()
        root.addLayout(action_layout)

        flash_layout = QHBoxLayout()
        flash_layout.setContentsMargins(20, 16, 20, 0)
        flash_layout.setSpacing(16)
        flash_layout.addStretch()
        flash_layout.addWidget(self._flash_button)
        flash_layout.addStretch()
        root.addLayout(flash_layout)

        startup_layout = QVBoxLayout(self._startup_page)
        startup_layout.setContentsMargins(40, 40, 40, 40)
        startup_layout.setSpacing(30)
        startup_layout.addStretch(1)
        self._startup_title = QLabel(tr("Код Мастер"))
        self._startup_title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        self._startup_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._startup_title.setProperty("title", True)
        startup_layout.addWidget(self._startup_title)
        self._startup_subtitle = QLabel(tr("Выберите режим работы"))
        self._startup_subtitle.setFont(QFont("Segoe UI", 12))
        self._startup_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        startup_layout.addWidget(self._startup_subtitle)
        startup_layout.addStretch(2)

        firmware_container = QWidget()
        firmware_layout = QVBoxLayout(firmware_container)
        firmware_layout.setContentsMargins(8, 8, 8, 8)
        firmware_layout.setSpacing(8)
        back_layout = QHBoxLayout()
        back_layout.addWidget(self._firmware_page_back_button)
        back_layout.addStretch()
        firmware_layout.addLayout(back_layout)
        firmware_layout.addWidget(self._firmware_page, 1)

        self._central_stack.addWidget(self._startup_page)
        self._central_stack.addWidget(firmware_container)
        root.addWidget(self._central_stack, 1)
        root.addWidget(self._status_bar)

    def _connect_signals(self) -> None:
        """Подключает сигналы SerialManager к UI."""
        self._serial_manager.connection_changed.connect(self._update_port_indicator)
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._serial_manager.heartbeat.connect(self._on_heartbeat)

    def _setup_shortcuts(self) -> None:
        """Настраивает горячие клавиши с учётом платформы."""
        modifier = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
        QShortcut(QKeySequence(modifier | Qt.Key.Key_O), self, activated=self._on_update_clicked)
        QShortcut(QKeySequence(modifier | Qt.Key.Key_M), self, activated=self._on_configure_clicked)
        QShortcut(QKeySequence("Esc"), self, activated=self.setFocus)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._on_save_settings)
        QShortcut(QKeySequence("Ctrl+Shift+P"), self, activated=self._on_flash_clicked)
        QShortcut(QKeySequence("F5"), self, activated=self._on_toggle_monitoring)
        QShortcut(QKeySequence("Ctrl+Shift+T"), self, activated=self._on_add_trigger_hotkey)

    def _on_save_settings(self) -> None:
        """Сохраняет текущие настройки."""
        try:
            self._config.save()
            self._status_label.setText(tr("Настройки сохранены"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить настройки: {0}").format(exc))

    def _on_toggle_monitoring(self) -> None:
        """Запускает или останавливает CAN-мониторинг."""
        if self._settings_window is None or not self._settings_window.isVisible():
            self._on_configure_clicked()
            if self._settings_window is None:
                return
        monitor_tab = self._settings_window._monitor_tab
        if not hasattr(monitor_tab, "_monitor1") or not hasattr(monitor_tab, "_monitor2"):
            return
        any_running = monitor_tab._monitor1._running or monitor_tab._monitor2._running
        if any_running:
            monitor_tab._monitor1._stop()
            monitor_tab._monitor2._stop()
            self._status_label.setText(tr("Мониторинг остановлен"))
        else:
            monitor_tab._monitor1._start()
            monitor_tab._monitor2._start()
            self._status_label.setText(tr("Мониторинг запущен"))

    def _on_add_trigger_hotkey(self) -> None:
        """Открывает вкладку триггеров и активирует первый свободный блок."""
        if self._settings_window is None or not self._settings_window.isVisible():
            self._on_configure_clicked()
            if self._settings_window is None:
                return
        tabs = self._settings_window._tabs
        trigger_tab = self._settings_window._trigger_tab
        tabs.setCurrentWidget(trigger_tab)
        if hasattr(trigger_tab, "_blocks"):
            for block in trigger_tab._blocks:
                if not block["group"].isChecked():
                    block["group"].setChecked(True)
                    block["recv"]["id"].setFocus()
                    break

    def _ensure_port_selected(self) -> bool:
        """Если порт не выбран, открывает диалог подключения."""
        if self._serial_manager.is_open() or self._config.get("port"):
            return True
        dialog = ComSettingsDialog(self._serial_manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_port_indicator()
            return self._serial_manager.is_open() or bool(self._config.get("port"))
        return False

    def _on_update_clicked(self) -> None:
        """Открывает страницу прошивки после проверки порта."""
        if not self._ensure_port_selected():
            return
        self._central_stack.setCurrentIndex(1)
        self._status_label.setText(tr("Страница прошивки"))

    def _on_flash_clicked(self) -> None:
        """Открывает полноценный диалог прошивки микроконтроллера."""
        dialog = FlashDialog(self._serial_manager, self)
        dialog.exec()

    def _on_configure_clicked(self) -> None:
        """Открывает окно настроек CAN, скрывая главное окно."""
        if not self._ensure_port_selected():
            return
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self._serial_manager, self)
            self._settings_window.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()
        self.hide()
        self._status_label.setText(tr("Открыто окно настроек"))

    def _show_startup_page(self) -> None:
        """Возвращает центральную область к главному меню."""
        self._central_stack.setCurrentIndex(0)
        self._status_label.setText(tr("Готов"))

    def _open_logs(self) -> None:
        """Открывает папку с логами с учётом платформы."""
        folder = str(get_log_dir())
        try:
            if sys.platform == "win32":
                import os
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", folder], check=False)
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось открыть папку с логами: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть папку с логами: {0}").format(exc))

    def _set_dark_theme(self) -> None:
        """Устанавливает тёмную тему."""
        app = QApplication.instance()
        if app is None:
            return
        self._config.set("light_theme", False)
        self._config.set("theme", "dark")
        apply_theme(app, False)

    def _set_light_theme(self) -> None:
        """Устанавливает светлую тему."""
        app = QApplication.instance()
        if app is None:
            return
        self._config.set("light_theme", True)
        self._config.set("theme", "light")
        apply_theme(app, True)

    def _set_starline_theme(self) -> None:
        """Устанавливает тему «StarLine»."""
        from ui.dark_theme import apply_starline_theme
        app = QApplication.instance()
        if app is None:
            return
        self._config.set("light_theme", False)
        self._config.set("theme", "starline")
        apply_starline_theme(app)

    def _on_language_changed(self, index: int) -> None:
        """Переключает язык через выпадающий список и обновляет открытые окна."""
        lang = self._language_combo.itemData(index)
        if lang is None:
            return
        self._config.set("language", lang)
        set_language(lang)
        self.retranslate_ui()
        if self._settings_window is not None:
            self._settings_window.retranslate_ui()
        if self._firmware_page is not None and hasattr(self._firmware_page, "retranslate_ui"):
            self._firmware_page.retranslate_ui()

    def _set_language_combo(self) -> None:
        """Устанавливает текущий язык в выпадающем списке."""
        current = self._config.get("language", "ru")
        for idx in range(self._language_combo.count()):
            if self._language_combo.itemData(idx) == current:
                self._language_combo.setCurrentIndex(idx)
                break

    def retranslate_ui(self) -> None:
        """Обновляет все статические строки главного окна."""
        self.setWindowTitle(tr("Код Мастер"))
        self._logo_label.setText("🛠️ " + tr("Код Мастер"))
        self._port_indicator.setToolTip(tr("Индикатор подключения COM-порта"))
        if not self._serial_manager.is_open() and not self._config.get("port"):
            self._port_label.setText(tr("Нет подключения"))
        self._theme_button.setText(tr("Тема"))
        self._theme_button.setToolTip(tr("Выбор темы оформления"))
        self._dark_theme_action.setText(tr("Тёмный"))
        self._light_theme_action.setText(tr("Светлый"))
        self._starline_theme_action.setText(tr("StarLine"))
        self._logs_button.setText("📄 " + tr("Логи"))
        self._help_button.setToolTip(tr("Помощь"))
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._update_button.setText("🔄 " + tr("Обновить"))
        self._configure_button.setText("⚙️ " + tr("Настроить"))
        self._flash_button.setText(tr("Прошить\nмикроконтроллер"))
        self._flash_button.setToolTip(tr("Открыть окно прошивки"))
        self._firmware_page_back_button.setText(tr("← Назад"))
        self._startup_title.setText(tr("Код Мастер"))
        self._startup_subtitle.setText(tr("Выберите режим работы"))
        self._status_label.setText(tr("Готов"))

    def _set_theme_button_icon(self) -> None:
        """Оставляет текст кнопки темы без изменений."""
        pass

    def _on_load_dbc(self) -> None:
        """Загружает DBC-файл и обновляет интерфейсы."""
        path, _ = QFileDialog.getOpenFileName(self, tr("Загрузить DBC"), "", "DBC files (*.dbc)")
        if not path:
            return
        dbc_manager = DBCManager()
        if dbc_manager.load_dbc(path):
            self._status_label.setText(tr("DBC загружен: {0}").format(path))
            if self._settings_window is not None:
                self._settings_window.set_dbc(dbc_manager)
        else:
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить DBC"))

    def _on_check_updates_clicked(self) -> None:
        """Проверяет обновления."""
        self._status_label.setText(tr("Проверка обновлений"))
        available, message = check_for_updates()
        self._status_label.setText(tr("Готов"))
        if available:
            QMessageBox.information(self, tr("Доступно обновление"), message)
        else:
            QMessageBox.information(self, tr("Последняя версия"), message)

    def _on_help_clicked(self) -> None:
        """Открывает встроенную справку."""
        show_help(self)

    def _on_heartbeat(self) -> None:
        """Индикатор порта становится зелёным при активности."""
        self._port_indicator.setStyleSheet("color: #4CAF50; font-size: 14px; background: transparent;")

    def _reset_port_indicator(self) -> None:
        """Сбрасывает индикатор порта в базовое состояние."""
        self._update_port_indicator()

    def _update_port_indicator(self) -> None:
        """Обновляет индикатор и текст порта."""
        base_style = "font-size: 14px; background: transparent;"
        if self._serial_manager.is_open():
            self._port_indicator.setStyleSheet(f"color: #4CAF50; {base_style}")
            self._port_label.setText(self._serial_manager.current_port_name())
        elif self._config.get("port"):
            self._port_indicator.setStyleSheet(f"color: #F44336; {base_style}")
            self._port_label.setText(tr("Порт не открыт"))
        else:
            self._port_indicator.setStyleSheet(f"color: #666666; {base_style}")
            self._port_label.setText(tr("Нет подключения"))

    def _on_serial_error(self, message: str) -> None:
        """Показывает ошибку COM-порта."""
        logger.error("Ошибка COM-порта: %s", message)
        self._status_label.setText(tr("Ошибка порта"))

    def closeEvent(self, event) -> None:  # noqa: N802
        """Корректно закрывает приложение."""
        logger.info("Закрытие главного окна")
        self._heartbeat_timer.stop()
        if self._settings_window is not None:
            self._settings_window.close()
        self._serial_manager.close_port()
        event.accept()


def show_exception_box(exc_type, exc_value, exc_tb) -> None:
    """Показывает QMessageBox при необработанном исключении."""
    message = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("Необработанное исключение: %s", message)
    try:
        if QApplication.instance() is not None:
            QMessageBox.critical(None, tr("Критическая ошибка"), tr("Произошла непредвиденная ошибка:\n{0}").format(exc_value))
    except Exception:  # noqa: BLE001
        pass
    print(message, file=sys.stderr)


sys.excepthook = show_exception_box
