"""Главное окно приложения «Код Мастер»."""

import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
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
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
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
from core.firmware_utils import guess_firmware_base, load_firmware_bytes
from core.serial_manager import SerialManager
from core.stm32_info import (
    APPLICATION_BASE_ADDR,
    DEVICE_CONFIG_PAGE_ADDR,
    parse_device_config,
)
from core.update_checker import check_for_updates
from models.config import CONFIG_FILE_FILTER, Config
from models.logger import get_logger, get_log_dir
from models.translations import _ as tr, set_language
from models.version import VERSION
from ui.dark_theme import apply_theme
from ui.com_logger import ComLoggerWindow
from ui.firmware_page import BootloaderWorker, FirmwarePage
from ui.flash_dialog import FlashDialog
from ui.help_widget import show_help
from ui.settings_window import SettingsWindow

logger = get_logger(__name__)


def _up_arrow_icon(color: QColor, size: int = 96) -> QIcon:
    """Векторная «стрелка вверх» для кнопки обновления приложения
    (отчёт мастера: вместо круговой стрелки)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(size * 0.12)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    cx = size / 2
    top, bottom = size * 0.16, size * 0.84
    span = size * 0.30
    # Ствол
    p.drawLine(int(cx), int(top + size * 0.10), int(cx), int(bottom))
    # Наконечник: две дуги-усики от ствола вверх-наружу
    p.drawLine(int(cx - span), int(top + span + size * 0.10), int(cx), int(top))
    p.drawLine(int(cx), int(top), int(cx + span), int(top + span + size * 0.10))
    p.end()
    return QIcon(pm)


def _usb_icon(color: QColor, size: int = 96) -> QIcon:
    """Векторный значок USB-трезубца в стилистике карточки
    (отчёт мастера: вместо «планеты»)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(size * 0.075)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = size * 0.5
    # Ствол снизу-вверх
    y_root, y_tip = size * 0.86, size * 0.12
    p.drawLine(int(cx), int(y_root), int(cx), int(y_tip))
    # Левая ветвь с кружком
    y1, x1 = size * 0.58, size * 0.28
    p.drawLine(int(cx), int(size * 0.66), int(x1), int(y1))
    p.drawLine(int(x1), int(y1), int(x1), int(y1 - size * 0.10))
    # Правая ветвь с квадратом
    y2, x2 = size * 0.44, size * 0.72
    p.drawLine(int(cx), int(size * 0.50), int(x2), int(y2))
    p.drawLine(int(x2), int(y2), int(x2), int(y2 - size * 0.10))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    r = size * 0.085
    # Кружок слева, квадрат справа, стрелка сверху
    p.drawEllipse(int(x1 - r), int(y1 - size * 0.10 - 2 * r), int(2 * r), int(2 * r))
    sq = size * 0.13
    p.drawRect(int(x2 - sq / 2), int(y2 - size * 0.10 - sq), int(sq), int(sq))
    path = QPainterPath()
    path.moveTo(cx, y_tip)
    path.lineTo(cx - r * 1.3, y_tip + size * 0.14)
    path.lineTo(cx + r * 1.3, y_tip + size * 0.14)
    path.closeSubpath()
    p.drawPath(path)
    # Кружок в основании ствола
    p.drawEllipse(int(cx - r), int(y_root - r), int(2 * r), int(2 * r))
    p.end()
    return QIcon(pm)


# VID/PID нашего адаптера: приложение и bootloader (см. AGENTS.md).
_USB_VID = 0x0483
_USB_PID_APP = 0x5740
_USB_PID_BOOT = 0x5741


class _DeviceCard(QWidget):
    """Карточка устройства на стартовом экране (рамка, как в ранних
    версиях — отчёт мастера):

    [USB]  Имя (bold)              [ОБНОВИТЬ] [НАСТРОИТЬ]
           ID / серийник / тип / fw

    В режиме выбора цели конфигурации обычные кнопки прячутся,
    появляется кнопка «Выбрать» — клик назначает устройство целью
    загружаемой конфигурации."""

    def __init__(
        self,
        window: "MainWindow",
        port: str | None,
        name: str,
        serial: str,
        dev_type: str,
        version: str,
        connected: bool,
        selecting: bool = False,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._port = port
        self._selecting = selecting
        if selecting:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        font = QFont("Segoe UI", 10)

        self.setStyleSheet(
            "_DeviceCard { border: 1px solid #454552; border-radius: 12px;"
            " background: rgba(255,255,255,0.03); }"
            + (
                "_DeviceCard { border-color: #3A7BD5; }"
                if selecting else ""
            )
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(_usb_icon(QColor("#3A7BD5"), 36).pixmap(36, 36))
        icon.setStyleSheet(
            "border: 2px solid #3A7BD5; border-radius: 20px;"
            " background: rgba(58,123,213,0.12);"
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
            tr("Тип устройства: {0}").format(dev_type or "—"),
            tr("Версия ПО: {0}").format(version or "—"),
        ):
            label = QLabel(line)
            label.setFont(info_font)
            label.setStyleSheet("color: #9A9AA5;")
            text_col.addWidget(label)
        head.addLayout(text_col, 1)
        root.addLayout(head)

        buttons = QHBoxLayout()
        buttons.addStretch()
        if selecting:
            select_btn = QPushButton(tr("Выбрать это устройство"))
            select_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            select_btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 6px;"
                " color: white; padding: 8px 26px; background: #3A7BD5; }"
                "QPushButton:hover { background: #4A8BE5; }"
            )
            select_btn.clicked.connect(self._on_select)
            buttons.addWidget(select_btn)
        else:
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

    def _on_select(self) -> None:
        self._window._select_config_target(self._port)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # В режиме выбора цели клик по любому месту карточки назначает
        # устройство получателем конфигурации (отчёт мастера).
        if self._selecting and self._port is not None:
            self._window._select_config_target(self._port)
            return
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    """Главное окно приложения «Код Мастер»."""

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        """Создаёт главное окно."""
        super().__init__(parent)
        install_exception_hook()
        self._serial_manager = serial_manager
        self._config = Config()
        set_language(self._config.get("language", "ru"))

        self.setWindowTitle(tr("Код Мастер") + f"  v{VERSION}")
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
        # Путь конфигурации, ждущей выбора устройства-цели.
        self._pending_config_path: str | None = None
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._setup_shortcuts()
        self._set_theme_button_icon()
        self._set_language_combo()
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

        # Цветная точка подключения убрана: флаг связи — сами данные
        # устройства на экране (отчёт мастера).

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

        # Обновление приложения — векторная «стрелка вверх» в левой
        # колонке значков под кнопкой загрузки конфигурации
        # (отчёт мастера: вместо круговой стрелки справа вверху).
        self._update_check_button = QPushButton()
        self._update_check_button.setFixedSize(34, 34)
        self._update_check_button.setFont(font)
        self._update_check_button.setIcon(_up_arrow_icon(QColor("#DCE4FF")))
        self._update_check_button.setIconSize(QSize(20, 20))
        self._update_check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_button.setToolTip(tr("Проверка обновлений"))
        self._update_check_button.clicked.connect(self._on_check_updates_clicked)

        # Левая колонка значков: «папка» — загрузка конфигурации из
        # файла или создание новой конфигурации устройства. Колонку
        # будем пополнять.
        self._fake_button = QPushButton("\U0001F4C2")
        self._fake_button.setFixedSize(34, 34)
        self._fake_button.setFont(QFont("Segoe UI", 12))
        self._fake_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fake_button.setToolTip(
            tr("Загрузить или создать конфигурацию")
        )
        self._fake_menu = QMenu(self._fake_button)
        self._load_config_action = self._fake_menu.addAction(
            tr("Загрузить конфигурацию из файла…"), self._on_load_config_file
        )
        self._fake_settings_action = self._fake_menu.addAction(
            tr("Создать конфигурацию устройства"), self._on_create_config_clicked
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

        # Статус-бар без постоянной подписи «Готов» (отчёт мастера):
        # сообщения всплывают на 4 с и гаснут.
        self._status_bar = QStatusBar()

        # Таймер heartbeat
        self._heartbeat_timer = QTimer(self)
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
        brand_layout = QHBoxLayout(self._brand_widget)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(8)
        brand_layout.addWidget(self._logo_label)
        # Версия приложения — в заголовке окна рядом с кнопками
        # свернуть/закрыть (setWindowTitle, отчёт мастера).
        top_layout.addWidget(self._brand_widget)

        top_layout.addStretch()
        top_layout.addSpacing(10)
        top_layout.addWidget(self._theme_button)
        top_layout.addWidget(self._logs_button)
        top_layout.addWidget(self._help_button)
        # Выбор языка — максимально справа (отчёт мастера).
        top_layout.addWidget(self._language_combo)
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
        # Левая колонка значков (расширяемая): конфигурация, обновление
        # приложения. Голубой фон — в тон «Обновить», разделитель —
        # заметно толще (отчёт мастера).
        icon_col = QVBoxLayout()
        icon_col.setSpacing(8)
        icon_col.addWidget(self._fake_button)
        icon_col.addWidget(self._update_check_button)
        icon_col.addStretch()
        self._icon_wrap = QWidget()
        self._icon_wrap.setLayout(icon_col)
        self._icon_wrap.setFixedWidth(50)
        self._icon_wrap.setStyleSheet(
            "background: #3A7BD5; border-radius: 8px;"
        )
        body.addWidget(self._icon_wrap)
        self._icon_line = QFrame()
        self._icon_line.setFrameShape(QFrame.Shape.VLine)
        self._icon_line.setFrameShadow(QFrame.Shadow.Plain)
        self._icon_line.setFixedWidth(4)
        self._icon_line.setStyleSheet("color: #5A5A68;")
        body.addWidget(self._icon_line)

        # Подсказка режима выбора цели конфигурации + отмена.
        self._select_hint = QWidget()
        select_hint_layout = QHBoxLayout(self._select_hint)
        select_hint_layout.setContentsMargins(8, 6, 8, 6)
        select_hint_layout.setSpacing(12)
        self._select_hint_label = QLabel(tr(
            "Куда загрузить конфигурацию? Кликните по устройству"
        ))
        self._select_hint_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        select_hint_layout.addWidget(self._select_hint_label)
        select_hint_layout.addStretch()
        self._select_cancel = QPushButton(tr("Отмена"))
        self._select_cancel.setFixedSize(90, 28)
        self._select_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_cancel.clicked.connect(self._cancel_config_target)
        select_hint_layout.addWidget(self._select_cancel)
        self._select_hint.setStyleSheet(
            "background: rgba(58,123,213,0.16); border: 1px solid #3A7BD5;"
            " border-radius: 8px;"
        )
        self._select_hint.setVisible(False)
        cards_side = QVBoxLayout()
        cards_side.setSpacing(8)
        cards_side.addWidget(self._select_hint)
        cards_side.addWidget(self._cards_scroll, 1)
        body.addLayout(cards_side, 1)
        startup_layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        bottom.addStretch()
        bottom.addWidget(self._flash_button)
        bottom.addWidget(self._com_logger_button)
        bottom.addStretch()
        self._bottom_widget = QWidget()
        self._bottom_widget.setLayout(bottom)
        startup_layout.addWidget(self._bottom_widget)

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
        self._serial_manager.connection_changed.connect(
            lambda *_a: self._refresh_device_cards()
        )
        # Имя устройства приходит после опроса — обновить карточки.
        self._serial_manager.device_identified.connect(
            lambda _info: self._refresh_device_cards()
        )
        self._serial_manager.error_occurred.connect(self._on_serial_error)
        self._serial_manager.critical_error.connect(self._on_critical_error)
        self._serial_manager.heartbeat.connect(
            lambda: self._refresh_device_cards()
        )

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
            self._status_bar.showMessage(tr("Настройки сохранены"), 4000)
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
            self._status_bar.showMessage(tr("Мониторинг остановлен"), 4000)
        else:
            monitor_tab._monitor1._start()
            monitor_tab._monitor2._start()
            self._status_bar.showMessage(tr("Мониторинг запущен"), 4000)

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
            self._update_device_via_cdc()
        else:
            self._open_settings_window()

    def _update_device_via_cdc(self) -> None:
        """«Обновить» на карточке устройства: выбор файла прошивки и
        заливка приложения через USB CDC (bootloader-протокол AN3155) —
        сам загрузчик при этом не переписывается.

        Проверки по отчёту мастера:
        * образ с областью загрузчика (< 0x08008000) через CDC не шьём —
          предупреждаем, что такую прошивку ставят через DFU;
        * если в образе есть страница конфигурации с именем устройства
          и оно не совпадает с подключённым — предупреждение с выбором
          «продолжить/отмена».
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Выбор прошивки"),
            self._config.get("last_fw_dir", "") or "",
            tr("Прошивки (*.hex *.bin);;Все файлы (*.*)"),
        )
        if not path:
            return
        self._config.set("last_fw_dir", str(Path(path).parent))

        try:
            data, base = load_firmware_bytes(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, tr("Ошибка"), tr("Не удалось открыть прошивку: {0}").format(exc)
            )
            return
        if base == 0:
            base = guess_firmware_base(data)

        # Образ покрывает область загрузчика — через CDC его не трогаем:
        # такую прошивку ставят полным образом через DFU.
        if base < APPLICATION_BASE_ADDR:
            QMessageBox.warning(
                self,
                tr("Обновление"),
                tr("Прошивка содержит область загрузчика — обновите "
                   "устройство через DFU (кнопка «Прошить МК», способ "
                   "«USB (DFU)»)."),
            )
            return

        # Сверка имени устройства с конфиг-страницей внутри прошивки.
        cfg_off = DEVICE_CONFIG_PAGE_ADDR - base
        fw_cfg = (
            parse_device_config(data[cfg_off : cfg_off + 32])
            if cfg_off >= 0 and cfg_off + 32 <= len(data)
            else None
        )
        if fw_cfg is not None and fw_cfg[0]:
            dev_name = self._config.get("device_name", "") or self._config.get(
                "device_type_name", ""
            )
            if dev_name and fw_cfg[0] != dev_name:
                answer = QMessageBox.warning(
                    self,
                    tr("Проверка прошивки"),
                    tr("Прошивка собрана для устройства «{0}», "
                       "подключено «{1}». Продолжить?").format(fw_cfg[0], dev_name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

        progress = QProgressDialog(
            tr("Обновление устройства…"), tr("Отмена"), 0, 100, self
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = BootloaderWorker(
            self._serial_manager, "flash", firmware_path=path, parent=self
        )
        progress.canceled.connect(worker.stop)
        worker.progress.connect(progress.setValue)
        result: dict[str, str | None] = {"message": None, "error": None}
        worker.finished_success.connect(
            lambda msg: (result.__setitem__("message", msg), progress.done(100))
        )
        worker.finished_error.connect(
            lambda msg: (result.__setitem__("error", msg), progress.done(100))
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        progress.exec()
        if progress.wasCanceled():
            worker.stop()
            return
        worker.wait(5000)
        if result["error"]:
            QMessageBox.critical(self, tr("Ошибка обновления"), result["error"])
        elif result["message"]:
            QMessageBox.information(self, tr("Обновление"), result["message"])

    def _refresh_device_cards(self) -> None:
        """Перестраивает карточки устройств при смене набора портов."""
        if not isValid(self) or self._central_stack.currentIndex() != 0:
            return
        devices = self._detect_devices()
        connected_port = (
            self._serial_manager.current_port_name()
            if self._serial_manager.is_open() else None
        )
        selecting = self._pending_config_path is not None
        signature = (
            tuple((d["port"], d["serial"], d["bootloader"]) for d in devices)
            + (
                connected_port,
                selecting,
                self._config.get("device_name", ""),
                self._config.get("device_serial", ""),
                self._config.get("device_type_name", ""),
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
            dev_type = (
                self._config.get("device_type_name", "")
                or self._config.get("device_name", "")
            ) if connected else ""
            version = (
                str(self._config.get("device_fw_version")
                    or self._config.get("device_version") or "—")
                if connected else "—"
            )
            card = _DeviceCard(
                self, str(d["port"]), name, serial, dev_type, version,
                connected, selecting=selecting,
            )
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, card
            )
        # Без подключённого камня центральная область пустая:
        # ни карточек, ни подсказок, ни заголовков (отчёт мастера).
        has_devices = bool(devices)
        self._startup_title.setVisible(has_devices)
        self._startup_subtitle.setVisible(has_devices)

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
        self._status_bar.showMessage(tr("Страница прошивки"), 4000)

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
        self._status_bar.showMessage(tr("Открыт COM-логгер"), 4000)

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
        self._status_bar.showMessage(tr("Открыто окно настроек"), 4000)

    def _show_startup_page(self) -> None:
        """Возвращает центральную область к главному меню."""
        self._central_stack.setCurrentIndex(0)
        self._status_bar.clearMessage()

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
        self.setWindowTitle(tr("Код Мастер") + f"  v{VERSION}")
        self._logo_label.setText("🛠️ " + tr("Код Мастер"))
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
            tr("Загрузить или создать конфигурацию")
        )
        self._load_config_action.setText(
            tr("Загрузить конфигурацию из файла…")
        )
        self._fake_settings_action.setText(
            tr("Создать конфигурацию устройства")
        )
        self._select_hint_label.setText(tr(
            "Куда загрузить конфигурацию? Кликните по устройству"
        ))
        self._select_cancel.setText(tr("Отмена"))
        self._cards_signature = ()
        self._refresh_device_cards()
        self._status_bar.clearMessage()

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
            self._status_bar.showMessage(tr("DBC загружен: {0}").format(path), 4000)
            if self._settings_window is not None:
                self._settings_window.set_dbc(dbc_manager)
        else:
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить DBC"))

    def _on_check_updates_clicked(self) -> None:
        """Проверяет обновления."""
        self._status_bar.showMessage(tr("Проверка обновлений"), 4000)
        available, message = check_for_updates()
        self._status_bar.clearMessage()
        if available:
            QMessageBox.information(self, tr("Доступно обновление"), message)
        else:
            QMessageBox.information(self, tr("Последняя версия"), message)

    def _on_help_clicked(self) -> None:
        """Открывает встроенную справку."""
        show_help(self)

    def _on_load_config_file(self) -> None:
        """«Загрузить конфигурацию из файла…»: выбор файла, затем —
        выбор устройства-цели (приложение затемняется, карточки
        остаются обычными). Запись в МК — только по «Сохранить» в окне
        настроек (отчёт мастера)."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить конфигурацию"),
            self._config.get("last_config_dir", "") or "",
            CONFIG_FILE_FILTER,
        )
        if not path:
            return
        self._config.set("last_config_dir", str(Path(path).parent))
        if not self._detect_devices():
            # Устройств нет — конфигурацию можно просмотреть и
            # поправить офлайн, но записать в МК всё равно не выйдет.
            self._open_settings_window()
            self._settings_window.load_config_from_path(path)
            return
        self._pending_config_path = path
        self._set_target_selection(True)

    def _on_create_config_clicked(self) -> None:
        """«Создать конфигурацию устройства»: таблица типов устройств
        (сейчас только «2 CAN», дальше будет больше — отчёт мастера).
        После выбора типа открывается окно настроек в офлайн-режиме
        (эмулятор) для сборки новой конфигурации."""
        from PySide6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QTableWidget,
            QTableWidgetItem,
        )

        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Создать конфигурацию устройства"))
        layout = QVBoxLayout(dialog)
        hint = QLabel(tr("Выберите тип устройства:"))
        hint.setFont(QFont("Segoe UI", 10))
        layout.addWidget(hint)
        table = QTableWidget(0, 1, dialog)
        table.setHorizontalHeaderLabels([tr("Тип устройства")])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setFixedHeight(120)
        for name in ("2 CAN",):
            row = table.rowCount()
            table.insertRow(row)
            item = QTableWidgetItem(name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 0, item)
        table.setCurrentCell(0, 0)
        layout.addWidget(table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        table.itemDoubleClicked.connect(lambda _i: dialog.accept())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        current = table.currentItem()
        device_type = current.text() if current is not None else "2 CAN"
        # Офлайн-редактор: порт-эмулятор, тип устройства — выбранный.
        baud = int(self._config.get("baudrate", 115200) or 115200)
        self._config.set_bulk({
            "port": "FAKE",
            "emulation": True,
            "device_type_name": device_type,
        })
        if self._serial_manager.open_port(
            "FAKE", baud, emulation=True, auto_reconnect=True
        ):
            self._open_settings_window()
        else:
            QMessageBox.warning(
                self, tr("Эмулятор"),
                tr("Не удалось запустить эмулятор устройства"),
            )

    def _set_target_selection(self, active: bool) -> None:
        """Затемняет приложение при выборе цели конфигурации —
        карточки устройств остаются обычными (отчёт мастера)."""
        self._top_panel.setEnabled(not active)
        self._icon_wrap.setEnabled(not active)
        self._bottom_widget.setEnabled(not active)
        for widget in (
            self._top_panel,
            self._icon_wrap,
            self._icon_line,
            self._bottom_widget,
            self._status_bar,
        ):
            widget.setGraphicsEffect(
                QGraphicsOpacityEffect(widget, opacity=0.35) if active else None
            )
        self._select_hint.setVisible(active)
        self._cards_signature = ()
        self._refresh_device_cards()

    def _cancel_config_target(self) -> None:
        """Отмена выбора цели — возвращаем обычный вид."""
        self._pending_config_path = None
        self._set_target_selection(False)

    def _select_config_target(self, port: str | None) -> None:
        """Клик по устройству в режиме выбора: подключить порт,
        открыть настройки, загрузить файл конфигурации."""
        path = self._pending_config_path
        self._pending_config_path = None
        self._set_target_selection(False)
        if port is not None and not self._connect_port(port):
            QMessageBox.warning(
                self, tr("Подключение"),
                tr("Не удалось подключиться к {0}").format(port),
            )
            return
        self._open_settings_window()
        self._settings_window.load_config_from_path(path)

    def _on_serial_error(self, message: str) -> None:
        """Показывает ошибку COM-порта."""
        logger.error("Ошибка COM-порта: %s", message)
        self._status_bar.showMessage(tr("Ошибка порта"), 4000)

    def _on_critical_error(self, message: str) -> None:
        """Поток чтения остановлен: соединение мертво, нужно переподключение."""
        logger.critical("Критическая ошибка COM-порта: %s", message)
        self._status_bar.showMessage(tr("Связь потеряна"), 4000)
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
