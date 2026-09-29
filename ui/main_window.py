"""Главное окно приложения «Код Мастер»."""

import subprocess
import sys
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)
from serial.tools.list_ports import comports
from shiboken6 import isValid

from core.dbc_manager import DBCManager
from core.serial_manager import SerialManager
from core.update_checker import check_for_updates
from models.config import CONFIG_FILE_FILTER, Config
from models.logger import get_logger, get_log_dir
from models.translations import _ as tr, set_language
from models.version import VERSION
from ui.dark_theme import apply_theme
from ui.com_logger import ComLoggerWindow
from ui.firmware_page import FirmwarePage
from ui.flash_dialog import FlashDialog
from ui.help_widget import show_help
from ui.settings_window import SettingsWindow

logger = get_logger(__name__)


def _reload_icon(color: QColor, size: int = 96) -> QIcon:
    """Жирный векторный значок «обновить»: дуга почти в полный круг
    со стрелкой-наконечником. Emoji «🔄» не реагирует на font-weight,
    а мастер просил стрелки жирнее — рисуем сами."""
    import math

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(size * 0.10)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    margin = size * 0.24
    rect = pm.rect().adjusted(margin, margin, -margin, -margin).toRectF()
    cx, cy = rect.center().x(), rect.center().y()
    r = rect.width() / 2
    # Дуга против часовой от θ=30° до θ=330° — разрыв справа, как у ↻.
    start_deg, end_deg = 30.0, 330.0
    p.drawArc(rect, int(start_deg * 16), int((end_deg - start_deg) * 16))
    # Наконечник в конце дуги: апекс чуть за точкой конца по касательной
    # (направление движения — вверх-вправо), основание перпендикулярно.
    th = math.radians(end_deg)
    ex, ey = cx + r * math.cos(th), cy - r * math.sin(th)
    tx, ty = -math.sin(th), -math.cos(th)          # касательная (CCW)
    nx, ny = -math.cos(th), math.sin(th)          # нормаль к центру
    head = size * 0.26
    ax, ay = ex + tx * head * 0.55, ey + ty * head * 0.55
    bx, by = ex - tx * head * 0.45, ey - ty * head * 0.45
    half = head * 0.42
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    path = QPainterPath()
    path.moveTo(ax, ay)
    path.lineTo(bx + nx * half, by + ny * half)
    path.lineTo(bx - nx * half, by - ny * half)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return QIcon(pm)


# VID/PID нашего адаптера: приложение и bootloader (см. AGENTS.md).
_USB_VID = 0x0483
_USB_PID_APP = 0x5740
_USB_PID_BOOT = 0x5741


class _DeviceCard(QWidget):
    """Карточка устройства на стартовом экране (по ТЗ мастера):

    [🌐]  Имя (bold)            [ОБНОВИТЬ] [НАСТРОИТЬ]
         ID / серийник / fw
         [синий баннер «подключите по USB», если порт не открыт]
    """

    def __init__(
        self,
        window: "MainWindow",
        port: str | None,
        name: str,
        serial: str,
        version: str,
        connected: bool,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._port = port
        font = QFont("Segoe UI", 10)

        self.setStyleSheet(
            "_DeviceCard { border: 1px solid #454552; border-radius: 12px;"
            " background: rgba(255,255,255,0.03); }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        icon = QLabel("🌐")
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            "border: 2px solid #3A7BD5; border-radius: 20px;"
            " font-size: 18px; background: rgba(58,123,213,0.12);"
        )
        head.addWidget(icon)
        head.addSpacing(10)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_label = QLabel(name)
        name_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        text_col.addWidget(name_label)
        info_font = QFont("Segoe UI", 9)
        for line in (
            tr("ID: {0}").format(port or "—"),
            tr("Серийный номер: {0}").format(serial or "—"),
            tr("Версия ПО: {0}").format(version or "—"),
        ):
            label = QLabel(line)
            label.setFont(info_font)
            label.setStyleSheet("color: #9A9AA5;")
            text_col.addWidget(label)
        head.addLayout(text_col, 1)
        root.addLayout(head)

        if not connected:
            banner = QLabel(tr(
                "Для обновления ПО и настройки подключите устройство "
                "по USB"
            ))
            banner.setFont(info_font)
            banner.setWordWrap(True)
            banner.setStyleSheet(
                "border: 1px solid #3A7BD5; border-radius: 6px;"
                " padding: 8px; color: #9CC3FF;"
                " background: rgba(58,123,213,0.08);"
            )
            root.addWidget(banner)

        buttons = QHBoxLayout()
        buttons.addStretch()
        update_btn = QPushButton(tr("Обновить"))
        update_btn.setFont(font)
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setStyleSheet(
            "QPushButton { border: 1px solid #3A7BD5; border-radius: 6px;"
            " color: #7C9EFF; padding: 6px 22px; background: transparent; }"
            "QPushButton:hover { background: rgba(58,123,213,0.15); }"
            "QPushButton:disabled { color: #666; border-color: #555; }"
        )
        update_btn.setEnabled(port is not None)
        update_btn.clicked.connect(self._on_update)
        configure_btn = QPushButton(tr("Настроить"))
        configure_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        configure_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        configure_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px;"
            " color: white; padding: 6px 22px; background: #3A7BD5; }"
            "QPushButton:hover { background: #4A8BE5; }"
        )
        configure_btn.clicked.connect(self._on_configure)
        buttons.addWidget(update_btn)
        buttons.addWidget(configure_btn)
        root.addLayout(buttons)

    def _on_update(self) -> None:
        self._window._card_action(self._port, flash=True)

    def _on_configure(self) -> None:
        self._window._card_action(self._port, flash=False)


class MainWindow(QMainWindow):
    """Главное окно приложения «Код Мастер»."""

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        """Создаёт главное окно."""
        super().__init__(parent)
        install_exception_hook()
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

        self._settings_window: SettingsWindow | None = None
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._setup_shortcuts()
        self._set_theme_button_icon()
        self._set_language_combo()
        self._update_port_indicator()
        self._refresh_device_cards()

    def _create_widgets(self) -> None:
        """Создаёт виджеты главного окна."""
        font = QFont("Segoe UI", 10)

        # Верхняя панель
        self._top_panel = QWidget()
        self._top_panel.setObjectName("topPanel")
        self._top_panel.setFixedHeight(60)

        self._logo_label = QLabel("🛠️ " + tr("Код Мастер"))
        self._logo_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._logo_label.setProperty("title", True)

        self._slogan_label = QLabel(tr("Разработано «КОД МАСТЕР»"))
        self._slogan_label.setFont(QFont("Segoe UI", 8))

        self._port_indicator = QLabel("●")
        self._port_indicator.setFixedSize(20, 20)
        self._port_indicator.setStyleSheet("color: #666666; font-size: 14px; background: transparent;")
        self._port_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._port_indicator.setToolTip(tr("Индикатор подключения COM-порта"))

        self._theme_button = QPushButton(tr("Тема"))
        self._theme_button.setFixedSize(70, 28)
        self._theme_button.setFont(font)
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.setToolTip(tr("Выбор темы оформления"))
        self._theme_menu = QMenu(self._theme_button)
        self._dark_theme_action = self._theme_menu.addAction(tr("Тёмный"), self._set_dark_theme)
        self._light_theme_action = self._theme_menu.addAction(tr("Светлый"), self._set_light_theme)
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

        # Проверка обновлений — только векторная стрелка: emoji «🔄»
        # не реагирует на font-weight, значок рисуется штатно.
        self._update_check_button = QPushButton()
        self._update_check_button.setFixedSize(36, 28)
        self._update_check_button.setFont(font)
        self._update_check_button.setIcon(_reload_icon(QColor("#DCE4FF")))
        self._update_check_button.setIconSize(
            self._update_check_button.size() * 0.62
        )
        self._update_check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._update_check_button.clicked.connect(self._on_check_updates_clicked)

        # Левая колонка значков: «папка» — загрузка конфигурации из
        # файла и FAKE-настройки (эмулятор без устройства). Колонку
        # будем пополнять.
        self._fake_button = QPushButton("\U0001F4C2")
        self._fake_button.setFixedSize(48, 48)
        self._fake_button.setFont(QFont("Segoe UI", 16))
        self._fake_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fake_button.setToolTip(
            tr("Загрузить конфигурацию / FAKE-настройки")
        )
        self._fake_menu = QMenu(self._fake_button)
        self._load_config_action = self._fake_menu.addAction(
            tr("Загрузить конфигурацию из файла…"), self._on_load_config_file
        )
        self._fake_settings_action = self._fake_menu.addAction(
            tr("FAKE-настройки (эмулятор устройства)"), self._on_fake_clicked
        )
        self._fake_button.setMenu(self._fake_menu)

        # Список карточек обнаруженных устройств.
        self._cards_box = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_box)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch()
        self._cards_signature: tuple = ()
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setWidget(self._cards_box)
        self._cards_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        # Нижние служебные кнопки стартового экрана.
        self._flash_button = QPushButton(tr("Прошить МК"))
        self._flash_button.setFixedSize(220, 44)
        self._flash_button.setFont(QFont("Segoe UI", 11))
        self._flash_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flash_button.setToolTip(tr("Открыть окно прошивки"))
        self._flash_button.clicked.connect(self._on_flash_clicked)

        self._com_logger_button = QPushButton(tr("COM-логгер"))
        self._com_logger_button.setFixedSize(220, 44)
        self._com_logger_button.setFont(QFont("Segoe UI", 11))
        self._com_logger_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._com_logger_button.setToolTip(tr("Открыть COM-логгер"))
        self._com_logger_button.clicked.connect(self._on_com_logger_clicked)

        self._com_logger_window: ComLoggerWindow | None = None

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
        self._status_bar.showMessage(f"v{VERSION}")

        # Таймер heartbeat
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._reset_port_indicator)
        self._heartbeat_timer.timeout.connect(self._refresh_device_cards)
        self._heartbeat_timer.start(1500)
        # closeEvent выставляет True — дочерние окна по нему понимают,
        # что закрывается всё приложение, и не воскрешают это окно.
        self._closing_app = False

    def _build_layout(self) -> None:
        """Собирает компоновку главного окна."""
        root = QVBoxLayout(self.centralWidget())
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        top_layout = QHBoxLayout(self._top_panel)
        top_layout.setContentsMargins(12, 0, 20, 0)
        top_layout.setSpacing(10)

        self._brand_widget = QWidget()
        brand_layout = QVBoxLayout(self._brand_widget)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)
        brand_layout.addWidget(self._logo_label)
        brand_layout.addWidget(self._slogan_label)
        top_layout.addWidget(self._brand_widget)

        top_layout.addWidget(self._language_combo)
        top_layout.addStretch()
        top_layout.addWidget(self._port_indicator)
        top_layout.addSpacing(10)
        top_layout.addWidget(self._theme_button)
        top_layout.addWidget(self._logs_button)
        top_layout.addWidget(self._help_button)
        top_layout.addWidget(self._update_check_button)
        root.addWidget(self._top_panel)

        startup_layout = QVBoxLayout(self._startup_page)
        startup_layout.setContentsMargins(24, 16, 24, 16)
        startup_layout.setSpacing(12)
        self._startup_title = QLabel(tr("Устройства"))
        self._startup_title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self._startup_title.setProperty("title", True)
        startup_layout.addWidget(self._startup_title)
        self._startup_subtitle = QLabel(tr(
            "Подключенные адаптеры — выберите действие"
        ))
        self._startup_subtitle.setFont(QFont("Segoe UI", 10))
        self._startup_subtitle.setStyleSheet("color: #9A9AA5;")
        startup_layout.addWidget(self._startup_subtitle)

        body = QHBoxLayout()
        body.setSpacing(12)
        # Левая колонка значков (расширяемая).
        icon_col = QVBoxLayout()
        icon_col.setSpacing(8)
        icon_col.addWidget(self._fake_button)
        icon_col.addStretch()
        icon_wrap = QWidget()
        icon_wrap.setLayout(icon_col)
        icon_wrap.setFixedWidth(64)
        body.addWidget(icon_wrap)
        body.addWidget(self._cards_scroll, 1)
        startup_layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        bottom.addStretch()
        bottom.addWidget(self._flash_button)
        bottom.addWidget(self._com_logger_button)
        bottom.addStretch()
        startup_layout.addLayout(bottom)

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
        # Имя устройства приходит после опроса — обновить индикатор.
        self._serial_manager.device_identified.connect(
            lambda _info: self._update_port_indicator()
        )
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._serial_manager.critical_error.connect(self._on_critical_error)
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
                    block["recv"]["conds"][0]["id"].setFocus()
                    break

    def _detect_devices(self) -> list[dict[str, object]]:
        """Наши адаптеры среди COM-портов по USB VID/PID."""
        devices: list[dict[str, object]] = []
        try:
            for p in comports():
                if p.vid == _USB_VID and p.pid in (_USB_PID_APP, _USB_PID_BOOT):
                    devices.append({
                        "port": p.device,
                        "serial": (p.serial_number or "").strip(),
                        "bootloader": p.pid == _USB_PID_BOOT,
                    })
        except Exception:  # noqa: BLE001
            pass
        return devices

    def _connect_port(self, port: str) -> bool:
        """Открывает порт устройства (скорость для USB CDC не нужна)."""
        if (
            self._serial_manager.is_open()
            and self._serial_manager.current_port_name() == port
        ):
            return True
        baud = int(self._config.get("baudrate", 115200) or 115200)
        self._config.set_bulk({"port": port, "emulation": False})
        return bool(
            self._serial_manager.open_port(port, baud, auto_reconnect=True)
        )

    def _card_action(self, port: str | None, flash: bool) -> None:
        """Кнопки карточки: «Обновить» → страница прошивки,
        «Настроить» → окно настроек. Порт подключается сам."""
        if port is None:
            # Заглушка «устройств нет»: настройки доступны и офлайн.
            if not flash:
                self._open_settings_window()
            return
        if not self._connect_port(port):
            QMessageBox.warning(
                self, tr("Подключение"),
                tr("Не удалось подключиться к {0}").format(port),
            )
            return
        if flash:
            self._central_stack.setCurrentIndex(1)
            self._status_label.setText(tr("Страница прошивки"))
        else:
            self._open_settings_window()

    def _on_fake_clicked(self) -> None:
        """Значок «папки»: FAKE-настройки на эмуляторе устройства."""
        baud = int(self._config.get("baudrate", 115200) or 115200)
        self._config.set_bulk({"port": "FAKE", "emulation": True})
        if self._serial_manager.open_port(
            "FAKE", baud, emulation=True, auto_reconnect=True
        ):
            self._open_settings_window()
        else:
            QMessageBox.warning(
                self, tr("Эмулятор"),
                tr("Не удалось запустить эмулятор устройства"),
            )

    def _refresh_device_cards(self) -> None:
        """Перестраивает карточки устройств при смене набора портов."""
        if not isValid(self) or self._central_stack.currentIndex() != 0:
            return
        devices = self._detect_devices()
        connected_port = (
            self._serial_manager.current_port_name()
            if self._serial_manager.is_open() else None
        )
        signature = (
            tuple((d["port"], d["serial"], d["bootloader"]) for d in devices)
            + (
                connected_port,
                self._config.get("device_name", ""),
                self._config.get("device_serial", ""),
                self._config.get("device_version", 0),
            )
        )
        if signature == self._cards_signature:
            return
        self._cards_signature = signature
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        port_names = self._config.get("port_names", {}) or {}
        for d in devices:
            connected = connected_port == d["port"]
            name = (
                port_names.get(d["serial"], "")
                or (self._config.get("device_name", "") if connected else "")
                or (
                    tr("CodeMaster (режим прошивки)")
                    if d["bootloader"] else "CodeMaster"
                )
            )
            serial = (
                self._config.get("device_serial", "") if connected else ""
            ) or str(d["serial"])
            version = (
                str(self._config.get("device_version") or "—")
                if connected else "—"
            )
            card = _DeviceCard(
                self, str(d["port"]), name, serial, version, connected
            )
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, card
            )
        if not devices:
            # Устройства нет — карточку не рисуем: только текстовая
            # подсказка без рамки и кнопок (по отчёту мастера).
            hint = QLabel(tr("Подключите устройство по USB"))
            hint.setFont(QFont("Segoe UI", 10))
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #9A9AA5;")
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, hint
            )

    def _ensure_port_selected(self) -> bool:
        """Единственное видимое устройство подключается само — без
        таблицы выбора порта/скорости."""
        if self._serial_manager.is_open():
            return True
        devices = self._detect_devices()
        if len(devices) == 1:
            return self._connect_port(str(devices[0]["port"]))
        return bool(self._config.get("port"))

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

    def _on_com_logger_clicked(self) -> None:
        """Открывает отдельное окно COM-логгера."""
        if self._com_logger_window is None:
            self._com_logger_window = ComLoggerWindow(self._serial_manager)
        self._com_logger_window.show()
        self._com_logger_window.raise_()
        self._com_logger_window.activateWindow()
        self._status_label.setText(tr("Открыт COM-логгер"))

    def _on_configure_clicked(self) -> None:
        """«Настроить» без таблицы портов: единственное видимое
        устройство подключается само, дальше — окно настроек."""
        if self._serial_manager.is_open() or len(self._detect_devices()) == 1:
            self._ensure_port_selected()
        self._open_settings_window()

    def _open_settings_window(self) -> None:
        """Показывает окно настроек CAN."""
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self._serial_manager, main_window=self)
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
        self._slogan_label.setText(tr("Разработано «КОД МАСТЕР»"))
        self._port_indicator.setToolTip(tr("Индикатор подключения COM-порта"))
        self._theme_button.setText(tr("Тема"))
        self._theme_button.setToolTip(tr("Выбор темы оформления"))
        self._dark_theme_action.setText(tr("Тёмный"))
        self._light_theme_action.setText(tr("Светлый"))
        self._logs_button.setText("📄 " + tr("Логи"))
        self._help_button.setToolTip(tr("Помощь"))
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._flash_button.setText(tr("Прошить МК"))
        self._flash_button.setToolTip(tr("Открыть окно прошивки"))
        self._com_logger_button.setText(tr("COM-логгер"))
        self._com_logger_button.setToolTip(tr("Открыть COM-логгер"))
        self._firmware_page_back_button.setText(tr("← Назад"))
        self._startup_title.setText(tr("Устройства"))
        self._startup_subtitle.setText(
            tr("Подключенные адаптеры — выберите действие")
        )
        self._fake_button.setToolTip(
            tr("Загрузить конфигурацию / FAKE-настройки")
        )
        self._load_config_action.setText(
            tr("Загрузить конфигурацию из файла…")
        )
        self._fake_settings_action.setText(
            tr("FAKE-настройки (эмулятор устройства)")
        )
        self._cards_signature = ()
        self._refresh_device_cards()
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
        """Пульс активности — обновляет индикатор по состоянию соединения."""
        self._update_port_indicator()

    def _reset_port_indicator(self) -> None:
        """Сбрасывает индикатор порта в базовое состояние."""
        self._update_port_indicator()

    def _update_port_indicator(self) -> None:
        """Обновляет индикатор порта (без строки имени устройства)."""
        base_style = "font-size: 14px; background: transparent;"
        if self._serial_manager.is_open():
            self._port_indicator.setStyleSheet(f"color: #4CAF50; {base_style}")
        elif self._config.get("port"):
            self._port_indicator.setStyleSheet(f"color: #F44336; {base_style}")
        else:
            self._port_indicator.setStyleSheet(f"color: #666666; {base_style}")

    def _on_load_config_file(self) -> None:
        """Открывает диалог выбора файла и передаёт путь загрузчику
        окна настроек (кнопка «папка» на главном экране)."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить конфигурацию"),
            self._config.get("last_config_dir", "") or "",
            CONFIG_FILE_FILTER,
        )
        if not path:
            return
        self._open_settings_window()
        self._settings_window.load_config_from_path(path)

    def _on_serial_error(self, message: str) -> None:
        """Показывает ошибку COM-порта."""
        logger.error("Ошибка COM-порта: %s", message)
        self._status_label.setText(tr("Ошибка порта"))

    def _on_critical_error(self, message: str) -> None:
        """Поток чтения остановлен: соединение мертво, нужно переподключение."""
        logger.critical("Критическая ошибка COM-порта: %s", message)
        self._status_label.setText(tr("Связь потеряна"))
        if self._serial_manager.auto_reconnect_enabled:
            return  # автопереподключение само восстановит порт
        if self.isVisible():
            QMessageBox.critical(self, tr("Связь с устройством потеряна"), message)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Корректно закрывает приложение."""
        logger.info("Закрытие главного окна")
        # Дочерние окна в своём closeEvent зовут main_window.show() —
        # без флага это воскрешало главное окно посреди выхода.
        self._closing_app = True
        self._heartbeat_timer.stop()
        if self._settings_window is not None:
            self._settings_window.close()
        if self._com_logger_window is not None:
            self._com_logger_window.close()
        self._serial_manager.close_port()
        event.accept()


def show_exception_box(exc_type, exc_value, exc_tb) -> None:
    """Показывает QMessageBox при необработанном исключении."""
    message = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("Необработанное исключение: %s", message)
    try:
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                tr("Критическая ошибка"),
                tr("Произошла непредвиденная ошибка:\n{0}").format(exc_value),
            )
    except Exception:  # noqa: BLE001
        pass
    print(message, file=sys.stderr)


def install_exception_hook() -> None:
    """Устанавливает глобальный обработчик необработанных исключений."""
    sys.excepthook = show_exception_box
