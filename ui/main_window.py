"""Главное окно приложения «Код Мастер»."""

import subprocess
import sys
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.serial_manager import SerialManager
from core.update_checker import check_for_updates
from models.config import Config
from models.logger import get_logger, get_log_dir
from models.translations import _ as tr, set_language
from ui.com_settings_dialog import ComSettingsDialog
from ui.dark_theme import apply_dark_theme, apply_light_theme
from ui.firmware_page import FirmwarePage
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
        self._set_lang_button_text()
        self._update_port_indicator()

    def _create_widgets(self) -> None:
        """Создаёт виджеты главного окна."""
        font = QFont("Segoe UI", 10)

        # Верхняя панель
        self._top_panel = QWidget()
        self._top_panel.setFixedHeight(48)
        self._top_panel.setStyleSheet("background-color: #252538; border: none;")

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

        self._theme_button = QPushButton("☀")
        self._theme_button.setFixedSize(36, 28)
        self._theme_button.setFont(font)
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.setToolTip(tr("Переключить светлую/тёмную тему"))
        self._theme_button.clicked.connect(self._on_theme_clicked)

        self._lang_button = QPushButton("EN")
        self._lang_button.setFixedSize(40, 28)
        self._lang_button.setFont(font)
        self._lang_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lang_button.setToolTip(tr("Переключить язык"))
        self._lang_button.clicked.connect(self._on_language_clicked)

        self._logs_button = QPushButton("📄 " + tr("Логи"))
        self._logs_button.setFixedSize(80, 28)
        self._logs_button.setFont(font)
        self._logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logs_button.clicked.connect(self._open_logs)

        self._update_check_button = QPushButton("🔄")
        self._update_check_button.setFixedSize(36, 28)
        self._update_check_button.setFont(font)
        self._update_check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._update_check_button.clicked.connect(self._on_check_updates_clicked)

        self._exit_button = QPushButton("✕ " + tr("Выход"))
        self._exit_button.setFixedSize(90, 28)
        self._exit_button.setFont(font)
        self._exit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._exit_button.clicked.connect(self.close)

        # Главное меню с двумя карточками
        self._central_stack = QStackedWidget()

        self._startup_page = QWidget()
        self._update_button = QPushButton("🔄 " + tr("Обновить"))
        self._update_button.setFixedSize(200, 80)
        self._update_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_button.clicked.connect(self._on_update_clicked)

        self._configure_button = QPushButton("⚙️ " + tr("Настроить"))
        self._configure_button.setFixedSize(200, 80)
        self._configure_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._configure_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._configure_button.clicked.connect(self._on_configure_clicked)

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
        top_layout.setContentsMargins(12, 0, 12, 0)
        top_layout.setSpacing(10)
        top_layout.addWidget(self._logo_label)
        top_layout.addStretch()
        top_layout.addWidget(self._port_indicator)
        top_layout.addWidget(self._port_label)
        top_layout.addSpacing(10)
        top_layout.addWidget(self._theme_button)
        top_layout.addWidget(self._lang_button)
        top_layout.addWidget(self._logs_button)
        top_layout.addWidget(self._update_check_button)
        top_layout.addWidget(self._exit_button)
        root.addWidget(self._top_panel)

        startup_layout = QVBoxLayout(self._startup_page)
        startup_layout.setContentsMargins(40, 40, 40, 40)
        startup_layout.setSpacing(30)
        startup_layout.addStretch(1)
        title = QLabel(tr("Код Мастер"))
        title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("title", True)
        startup_layout.addWidget(title)
        subtitle = QLabel(tr("Выберите режим работы"))
        subtitle.setFont(QFont("Segoe UI", 12))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        startup_layout.addWidget(subtitle)
        startup_layout.addStretch(1)
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(24)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._update_button)
        buttons_layout.addWidget(self._configure_button)
        buttons_layout.addStretch()
        startup_layout.addLayout(buttons_layout)
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
        QShortcut(QKeySequence(modifier | Qt.Key.Key_T), self, activated=self._on_theme_clicked)
        QShortcut(QKeySequence("Esc"), self, activated=self.setFocus)

    def _ensure_port_selected(self) -> bool:
        """Если порт не выбран, открывает диалог подключения."""
        if self._serial_manager.is_open():
            return True
        dialog = ComSettingsDialog(self._serial_manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_port_indicator()
            return self._serial_manager.is_open()
        return False

    def _on_update_clicked(self) -> None:
        """Открывает страницу прошивки после проверки порта."""
        if not self._ensure_port_selected():
            return
        self._central_stack.setCurrentIndex(1)
        self._status_label.setText(tr("Страница прошивки"))

    def _on_configure_clicked(self) -> None:
        """Открывает окно настроек CAN после проверки порта."""
        if not self._ensure_port_selected():
            return
        if self._settings_window is None or not self._settings_window.isVisible():
            self._settings_window = SettingsWindow(self._serial_manager, self)
            self._settings_window.show()
        if self._settings_window is not None:
            self._settings_window.raise_()
            self._settings_window.activateWindow()
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

    def _on_theme_clicked(self) -> None:
        """Переключает светлую/тёмную тему."""
        app = QApplication.instance()
        if app is None:
            return
        light = not self._config.get("light_theme", False)
        self._config.set("light_theme", light)
        if light:
            apply_light_theme(app)
            self._theme_button.setText("☾")
        else:
            apply_dark_theme(app)
            self._theme_button.setText("☀")

    def _on_language_clicked(self) -> None:
        """Переключает язык."""
        lang = "en" if self._config.get("language", "ru") == "ru" else "ru"
        self._config.set("language", lang)
        set_language(lang)
        self._set_lang_button_text()
        QMessageBox.information(
            self,
            tr("Переключить язык"),
            tr("Перезапустите приложение, чтобы применить новый язык.") if lang == "ru" else "Restart the application to apply the new language.",
        )

    def _set_lang_button_text(self) -> None:
        """Обновляет текст кнопки языка."""
        self._lang_button.setText("RU" if self._config.get("language", "ru") == "en" else "EN")

    def _set_theme_button_icon(self) -> None:
        """Обновляет иконку кнопки темы."""
        self._theme_button.setText("☾" if self._config.get("light_theme", False) else "☀")

    def _on_check_updates_clicked(self) -> None:
        """Проверяет обновления."""
        self._status_label.setText(tr("Проверка обновлений"))
        available, message = check_for_updates()
        self._status_label.setText(tr("Готов"))
        if available:
            QMessageBox.information(self, tr("Доступно обновление"), message)
        else:
            QMessageBox.information(self, tr("Последняя версия"), message)

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
        QMessageBox.critical(None, tr("Критическая ошибка"), tr("Произошла непредвиденная ошибка:\n{0}").format(exc_value))
    except Exception:  # noqa: BLE001
        pass


sys.excepthook = show_exception_box
