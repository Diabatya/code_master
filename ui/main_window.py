"""Главное окно приложения «Код Мастер»."""

import random
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
)
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


def _plus_icon(color: QColor, size: int = 96) -> QIcon:
    """Белый «+» той же толщины, что стрелка кнопки обновления —
    значок «загрузить/создать конфигурацию» (отчёт мастера)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(size * 0.12)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    c = size / 2
    span = size * 0.30
    p.drawLine(int(c - span), int(c), int(c + span), int(c))
    p.drawLine(int(c), int(c - span), int(c), int(c + span))
    p.end()
    return QIcon(pm)


def _kod_logo(dark: QColor, orange: QColor, width: int = 100,
              height: int = 42) -> QPixmap:
    """Векторный логотип «КОД» по ТЗ мастера: viewBox 1000×420,
    буквы К и Д + левая половина кольца «О» — основным цветом
    (на светлой теме чёрный, на тёмной — светлый, иначе не видно),
    правая половина «О» — оранжевым #E87A2A."""
    scale = 4  # суперсэмплинг для ровных краёв
    pm = QPixmap(width * scale, height * scale)
    pm.setDevicePixelRatio(1)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(width * scale / 1000.0, height * scale / 420.0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(dark)

    # Буква К
    k_path = QPainterPath()
    k_path.moveTo(50, 0)
    for x, y in ((130, 0), (130, 180), (260, 0), (340, 0), (200, 210),
                 (340, 420), (260, 420), (130, 240), (130, 420), (50, 420)):
        k_path.lineTo(x, y)
    k_path.closeSubpath()
    p.drawPath(k_path)

    # Буква О — кольцо R=130, r=70 с центром (500, 210).
    ring = QPainterPath()
    ring.setFillRule(Qt.FillRule.OddEvenFill)
    ring.addEllipse(500 - 130, 210 - 130, 260, 260)
    ring.addEllipse(500 - 70, 210 - 70, 140, 140)
    p.drawPath(ring)
    # Правая половина кольца — оранжевой.
    p.save()
    p.setClipRect(500, 0, 500, 420)
    p.setBrush(orange)
    p.drawPath(ring)
    p.restore()
    p.setBrush(dark)

    # Буква Д — по координатам из ТЗ мастера (turtle-холст 100×110):
    # верхняя фигурная часть + нижняя перекладина. Масштабируется
    # в слот 640–980 × 0–420 общего viewBox 1000×420.
    _D_TOP = (
        (35, 0), (75, 0), (75, 80), (55, 80), (55, 25), (35, 25),
        (30, 70), (20, 80), (5, 65), (5, 25), (15, 5),
    )
    _D_BOTTOM = ((0, 85), (95, 85), (95, 110), (0, 110))
    d_sx = (980.0 - 640.0) / 95.0
    d_sy = 420.0 / 110.0

    def _d_pt(pt: tuple[float, float]) -> tuple[float, float]:
        return 640.0 + pt[0] * d_sx, pt[1] * d_sy

    d_path = QPainterPath()
    d_path.moveTo(*_d_pt(_D_TOP[0]))
    for pt in _D_TOP[1:]:
        d_path.lineTo(*_d_pt(pt))
    d_path.closeSubpath()
    p.drawPath(d_path)
    d_base = QPainterPath()
    d_base.moveTo(*_d_pt(_D_BOTTOM[0]))
    for pt in _D_BOTTOM[1:]:
        d_base.lineTo(*_d_pt(pt))
    d_base.closeSubpath()
    p.drawPath(d_base)
    p.end()
    return pm.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class _MatrixBackground(QWidget):
    """Анимированный «матричный» фон по ТЗ мастера: 8 колонок
    падающих символов, у каждой своя скорость, прозрачность и
    размер шрифта (глубина). Нижний слой — не перехватывает
    события мыши (аналог pointer-events: none)."""

    # (длительность с, задержка с, прозрачность, размер шрифта px)
    # Скорость падения снижена ещё в 5 раз (отчёт мастера).
    _COL_STYLE = (
        (150.0, 0.0, 0.60, 34),   # передний план
        (275.0, 25.0, 0.25, 20),  # дальний план
        (210.0, 45.0, 0.45, 28),  # средний план
        (300.0, 10.0, 0.35, 24),  # средне-дальний
        (190.0, 65.0, 0.55, 32),  # передний план
    )
    _COLS = 8
    _ROWS = 16
    _REF_W = 1080.0
    _REF_H = 720.0

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        # Фон — нижний слой: мышь проходит насквозь (pointer-events: none).
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        rng = random.Random(0xC0DE)
        # Содержимое колонок генерируется один раз — цифровой «дождь».
        self._columns: list[list[str]] = [
            [
                " ".join(
                    str(rng.randrange(10))
                    for _ in range(rng.randrange(1, 6))
                )
                for _ in range(self._ROWS)
            ]
            for _ in range(self._COLS)
        ]
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(33)  # ~30 fps

    def paintEvent(self, _event) -> None:  # noqa: N802
        w, h = max(self.width(), 1), max(self.height(), 1)
        sx = w / self._REF_W
        sy = h / self._REF_H
        now = self._elapsed.elapsed() / 1000.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#8A8A8A")
        font = QFont("Courier New")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        # Полоса колонок центрируется на экране — по краям символов
        # быть не должно, дождь идёт в середине (отчёт мастера).
        col_step = 135 * sx
        x0 = (w - col_step * (self._COLS - 1)) / 2
        for c in range(self._COLS):
            duration, delay, opacity, fsize = self._COL_STYLE[c % len(self._COL_STYLE)]
            t = now - delay
            frac = (t % duration) / duration if t >= 0 else 0.0
            # Кадр из ТЗ: 0→0, 80%→+720, 81%→-720, 100%→0.
            y_off = (
                frac / 0.8 if frac < 0.8 else -1.0 + (frac - 0.8) / 0.2
            ) * self._REF_H * sy
            color.setAlphaF(opacity)
            p.setPen(color)
            font.setPixelSize(max(10, int(fsize * sy)))
            p.setFont(font)
            x = x0 + col_step * c
            for r, text in enumerate(self._columns[c]):
                p.drawText(int(x), int((60 + 45 * r) * sy + y_off), text)
        p.end()


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


class _ConfigMenuPopup(QWidget):
    """Анимированная выпадашка выбора конфигурации в стиле iOS:
    две строки-карточки в закруглённых «табличках», плавно выезжают
    вниз с fade-in (отчёт мастера)."""

    def __init__(
        self,
        anchor: QWidget,
        on_load_file,
        on_create,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        frame = QFrame(self)
        frame.setObjectName("menuFrame")
        frame.setStyleSheet(
            "QFrame#menuFrame {"
            " background: #26262F; border: 1px solid #3A7BD5;"
            " border-radius: 14px; }"
        )
        # Шире — вся фраза должна помещаться в строку-карточку
        # (отчёт мастера).
        frame.setMinimumWidth(430)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        items = (
            (
                "📂", tr("Загрузить конфигурацию из файла"),
                tr("Открыть сохранённую конфигурацию и выбрать устройство"),
                on_load_file,
            ),
            (
                "🛠", tr("Создать конфигурацию устройства"),
                tr("Новая конфигурация под выбранный тип устройства"),
                on_create,
            ),
        )
        for icon_text, title, sub, handler in items:
            card = QPushButton()
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setStyleSheet(
                "QPushButton {"
                " background: rgba(58,123,213,0.10);"
                " border: 1px solid #45455A; border-radius: 10px;"
                " text-align: left; padding: 10px 12px; color: #E8E8EF; }"
                "QPushButton:hover {"
                " background: rgba(58,123,213,0.28);"
                " border-color: #3A7BD5; }"
                "QPushButton:pressed { background: rgba(58,123,213,0.45); }"
            )
            inner = QHBoxLayout(card)
            inner.setContentsMargins(2, 2, 2, 2)
            inner.setSpacing(10)
            icon_label = QLabel(icon_text)
            icon_label.setFont(QFont("Segoe UI", 16))
            icon_label.setStyleSheet("background: transparent; border: none;")
            icon_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
            inner.addWidget(icon_label)
            text_col = QVBoxLayout()
            text_col.setSpacing(1)
            title_label = QLabel(title)
            title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            title_label.setWordWrap(True)
            sub_label = QLabel(sub)
            sub_label.setFont(QFont("Segoe UI", 8))
            sub_label.setWordWrap(True)
            sub_label.setStyleSheet("color: #9A9AA5;")
            for lbl in (title_label, sub_label):
                lbl.setStyleSheet(
                    lbl.styleSheet() + " background: transparent; border: none;"
                )
                lbl.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                text_col.addWidget(lbl)
            inner.addLayout(text_col, 1)
            # clicked() передаёт checked (bool) позиционно — глотаем
            # его отдельным параметром, иначе он подменяет handler
            # и «Создать/Загрузить конфигурацию» падает с
            # "'bool' object is not callable" (отчёт мастера).
            card.clicked.connect(
                lambda _checked=False, h=handler: (self.close(), h())
            )
            layout.addWidget(card)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self._frame = frame
        self._anchor = anchor

    def show_animated(self) -> None:
        """Выезжает из-под кнопки вниз с затухающим скольжением
        и fade-in — плавность «как у айфонов» (отчёт мастера)."""
        self.adjustSize()
        pos = self._anchor.mapToGlobal(self._anchor.rect().bottomLeft())
        final_y = pos.y() + 6
        self.move(pos.x(), final_y - 18)
        self.show()
        self.raise_()

        self._pos_anim = QPropertyAnimation(self, b"pos", self)
        self._pos_anim.setDuration(220)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(self.pos().__class__(pos.x(), final_y))
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fade)
        self._fade_anim = QPropertyAnimation(self._fade, b"opacity", self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._pos_anim.finished.connect(
            lambda: self.setGraphicsEffect(None)
        )
        self._pos_anim.start()
        self._fade_anim.start()


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

        # Общая голубая рамка с закруглением вокруг информации
        # и кнопок (отчёт мастера). Без WA_StyledBackground обычный
        # QWidget не рисует border/background из стиля — рамка
        # пропадала.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "_DeviceCard { border: 2px solid #3A7BD5; border-radius: 12px; }"
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
        # Строки информации плотно друг к другу (отчёт мастера).
        text_col.setSpacing(0)
        name_label = QLabel(name)
        name_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        text_col.addWidget(name_label)
        info_font = QFont("Segoe UI", 9)
        for line in (
            tr("ID: {0}").format(port or "—"),
            tr("Серийный номер: {0}").format(serial or "—"),
            # «Тип устройства» не выводим строкой — он уже показан
            # крупным именем карточки (отчёт мастера).
            tr("Версия ПО: {0}").format(version or "—"),
        ):
            label = QLabel(line)
            label.setFont(info_font)
            label.setStyleSheet("color: #9A9AA5;")
            # Любая строка выделяется курсором и копируется
            # (отчёт мастера).
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            text_col.addWidget(label)
        head.addLayout(text_col, 1)

        # Кнопки — горизонтально друг за другом, напротив строк
        # информации (вертикально по центру) — как в ранней версии
        # (отчёт мастера).
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
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
        head.addLayout(buttons)
        root.addLayout(head)

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

        # Логотип «КОД» по ТЗ мастера (вектор: К/Д + левая половина
        # кольца — тёмные, правая половина «О» — оранжевая), текст
        # «Код Мастер» рядом.
        self._logo_icon_label = QLabel()
        self._logo_icon_label.setFixedSize(120, 50)
        self._logo_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_logo()
        self._logo_label = QLabel(tr("Мастер"))
        # Название в 2 раза крупнее прежнего (отчёт мастера).
        self._logo_label.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        self._logo_label.setProperty("title", True)
        self._logo_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

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

        # Левая колонка значков: белый «+» — загрузка конфигурации из
        # файла или создание новой конфигурации устройства. Колонку
        # будем пополнять. Меню — кастомная анимированная выпадашка
        # в стиле iOS (отчёт мастера).
        self._fake_button = QPushButton()
        self._fake_button.setFixedSize(34, 34)
        self._fake_button.setIcon(_plus_icon(QColor("#DCE4FF")))
        self._fake_button.setIconSize(QSize(20, 20))
        self._fake_button.setFont(QFont("Segoe UI", 12))
        self._fake_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fake_button.setToolTip(
            tr("Загрузить или создать конфигурацию")
        )
        self._fake_button.clicked.connect(self._toggle_config_menu)
        self._config_menu: _ConfigMenuPopup | None = None

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
        # Viewport — отдельный виджет: без прозрачности он заливает
        # середину экрана и матрица видна только по периметру
        # (отчёт мастера).
        self._cards_scroll.viewport().setStyleSheet(
            "background: transparent;"
        )
        self._cards_box.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
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
        # Матричный фон — нижний слой стартовой страницы,
        # не перехватывает мышь (pointer-events: none, ТЗ мастера).
        self._matrix_bg = _MatrixBackground(self._startup_page)

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
        # Левая полоса-значки тянется от самого верха окна до самого
        # низа: корень окна — горизонтальный, полоса слева, всё
        # остальное (верхняя панель, страницы, статус-бар) — справа
        # (отчёт мастера).
        outer = QHBoxLayout(self.centralWidget())
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        icon_col = QVBoxLayout()
        icon_col.setSpacing(14)
        icon_col.setContentsMargins(8, 14, 8, 14)
        icon_col.addWidget(self._fake_button)
        icon_col.addWidget(self._update_check_button)
        icon_col.addStretch()
        self._icon_wrap = QWidget()
        self._icon_wrap.setLayout(icon_col)
        self._icon_wrap.setFixedWidth(50)
        self._icon_wrap.setStyleSheet(
            "background: palette(window);"
            " border: 3px solid #3A7BD5; border-radius: 10px;"
        )
        outer.addWidget(self._icon_wrap)

        right_box = QWidget()
        root = QVBoxLayout(right_box)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(right_box, 1)

        top_layout = QHBoxLayout(self._top_panel)
        top_layout.setContentsMargins(12, 0, 20, 0)
        top_layout.setSpacing(10)

        # Логотип «Код Мастер» переехал со стартовой панели в тело
        # окна — правее голубой полосы и ниже (отчёт мастера). Версия
        # приложения — в заголовке окна рядом с кнопками
        # свернуть/закрыть (setWindowTitle).

        top_layout.addStretch()
        top_layout.addSpacing(10)
        top_layout.addWidget(self._theme_button)
        top_layout.addWidget(self._logs_button)
        top_layout.addWidget(self._help_button)
        # Выбор языка — максимально справа (отчёт мастера).
        top_layout.addWidget(self._language_combo)
        root.addWidget(self._top_panel)

        startup_layout = QVBoxLayout(self._startup_page)
        startup_layout.setContentsMargins(0, 0, 0, 0)
        startup_layout.setSpacing(0)
        # Заголовки «Устройства / Подключенные адаптеры» убраны
        # полностью (отчёт мастера).

        body = QHBoxLayout()
        body.setSpacing(0)
        body.setContentsMargins(0, 0, 0, 0)
        # Левая полоса-значки вынесена в корень окна — тянется от
        # верхнего края до нижнего (см. начало _build_layout).

        # Правая часть стартового экрана: логотип + карточки +
        # нижние кнопки.
        right = QVBoxLayout()
        right.setContentsMargins(24, 20, 24, 16)
        right.setSpacing(12)
        # Логотип x2: иконка Porsche 911 + «Код Мастер» крупно,
        # с отступом от голубой линии и ниже верхнего края.
        logo_row = QHBoxLayout()
        logo_row.setSpacing(4)
        logo_row.addSpacing(18)
        logo_row.addWidget(self._logo_icon_label)
        logo_row.addWidget(
            self._logo_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        logo_row.addStretch()
        right.addSpacing(26)
        right.addLayout(logo_row)
        right.addSpacing(18)

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
        right.addWidget(self._select_hint)
        right.addWidget(self._cards_scroll, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        bottom.addStretch()
        bottom.addWidget(self._flash_button)
        bottom.addWidget(self._com_logger_button)
        bottom.addStretch()
        self._bottom_widget = QWidget()
        self._bottom_widget.setLayout(bottom)
        right.addWidget(self._bottom_widget)

        body.addLayout(right, 1)
        startup_layout.addLayout(body, 1)

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
        self._matrix_bg.lower()
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
        * объединённый образ (bootloader + application) прошивается
          через CDC: область загрузчика отрезается автоматически
          (trim_to_application_region в flash_firmware), сам
          загрузчик остаётся прежним — обновление больше не требует
          DFU (отчёт мастера);
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

        # Образ покрывает область загрузчика — через CDC шьём только
        # application: flash_firmware сам отрезает область < 0x08008000
        # (trim_to_application_region). Загрузчик остаётся прежним,
        # DFU для обновления не нужен (отчёт мастера).
        if base < APPLICATION_BASE_ADDR:
            logger.info(
                "Объединённый образ: область загрузчика будет пропущена, "
                "прошивается только application"
            )
            self._status_bar.showMessage(
                tr("Область загрузчика пропущена — прошивается приложение"),
                6000,
            )

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
                self._config.get("device_fw_version", ""),
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
        # ни карточек, ни подсказок (отчёт мастера).

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

    def _apply_logo(self) -> None:
        """Перерисовывает логотип «КОД» — тёмные элементы следуют
        теме: на тёмной теме они светлые, иначе их не видно."""
        light = bool(self._config.get("light_theme"))
        dark_color = QColor("#0A0A0A") if light else QColor("#E8EAF2")
        self._logo_icon_label.setPixmap(
            _kod_logo(dark_color, QColor("#E87A2A"), 120, 50)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Матричный фон тянется за стартовой страницей.
        self._matrix_bg.resize(self._startup_page.size())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._matrix_bg.resize(self._startup_page.size())

    def _set_dark_theme(self) -> None:
        """Устанавливает тёмную тему."""
        app = QApplication.instance()
        if app is None:
            return
        self._config.set("light_theme", False)
        self._config.set("theme", "dark")
        apply_theme(app, False)
        self._apply_logo()

    def _set_light_theme(self) -> None:
        """Устанавливает светлую тему."""
        app = QApplication.instance()
        if app is None:
            return
        self._config.set("light_theme", True)
        self._config.set("theme", "light")
        apply_theme(app, True)
        self._apply_logo()

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
        self._logo_label.setText(tr("Код Мастер"))
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
        self._fake_button.setToolTip(
            tr("Загрузить или создать конфигурацию")
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

    def _toggle_config_menu(self) -> None:
        """Показывает анимированную выпадашку «папки» (iOS-стиль):
        две строки-карточки — загрузить из файла / создать новую."""
        if self._config_menu is not None and self._config_menu.isVisible():
            self._config_menu.close()
            self._config_menu = None
            return
        self._config_menu = _ConfigMenuPopup(
            self._fake_button,
            self._on_load_config_file,
            self._on_create_config_clicked,
            self,
        )
        self._config_menu.show_animated()

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
        # Новая конфигурация стартует ПУСТОЙ: ключи-сидеры вкладок
        # затираем (прежняя программа остаётся в backup-снапшоте,
        # отчёт мастера — «демо подтягивало тестовый конфиг»).
        self._config.backup_snapshot("before_new_config")
        baud = int(self._config.get("baudrate", 115200) or 115200)
        self._config.set_bulk({
            "port": "FAKE",
            "emulation": True,
            "device_type_name": device_type,
            "device_name": "",
            "device_serial": "",
            "triggers": [],
            "gateway_rules": [],
            "gateway_ignore": [],
            "flexible_rules": [],
        })
        # Переменные живут в сессии окна настроек — чистим вкладку
        # у уже созданного окна, иначе в «пустой» демо-конфигурации
        # всплывали бы строки прошлой сессии (отчёт мастера).
        if self._settings_window is not None:
            self._settings_window._variables_tab.import_config({})
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
            self._logo_icon_label,
            self._logo_label,
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
        # Фоновый поллер телеметрии МК — гасим до разрыва порта,
        # иначе поток мог бы сидеть в read на закрытом дескрипторе.
        from ui.memory_indicator import MemoryIndicator
        MemoryIndicator.shutdown_poller()
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
