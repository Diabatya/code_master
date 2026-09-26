"""Вкладка «Мониторинг CAN» с двумя каналами."""

import csv
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from PySide6.QtCore import QPointF, QRect, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QDoubleValidator

from core.can_protocol import pack_can_frame
from core.dbc_manager import DBCManager
from core.dbc_parser import decode_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import bytes_to_hex_string, format_data_bytes, hex_to_int, int_to_hex, parse_data_bytes
from ui.filter_dialog import FilterDialog
from ui.hex_edit import create_data_field_widget
from ui.id_edit import IdPasteEdit
from ui.memory_indicator import MemoryIndicator
from ui.packet_clipboard import create_clipboard_buttons
from ui.toast import show_toast
from ui.ui_utils import setCheckableWithIndicator, setup_button

logger = get_logger(__name__)


class CanSettingsReadbackMismatch(Exception):
    """Устройство сообщает не те CAN-настройки, что были записаны:
    «Сохранить» прерывается без отметки об успехе — оператор видит
    расхождение и может повторить запись."""

MAX_TABLE_ROWS = 50_000

BIT_RATES = [tr("11 бит"), tr("29 бит")]


def _ascii_from_data(data: bytes) -> str:
    """Возвращает печатные ASCII-символы для байт, непечатные заменяются на '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


class _IdValidator:
    """Валидатор HEX ID с цветовой индикацией."""

    def __init__(self, edit: QLineEdit, bit_combo: QComboBox) -> None:
        self._edit = edit
        self._bit_combo = bit_combo
        self._edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,8}")))
        self._edit.textChanged.connect(self._validate)
        self._bit_combo.currentIndexChanged.connect(self._validate)

    def _validate(self) -> None:
        text = self._edit.text()
        upper = text.upper()
        if text != upper:
            self._edit.blockSignals(True)
            self._edit.setText(upper)
            self._edit.blockSignals(False)
            text = upper
        text = text.strip()
        if not text:
            self._edit.setStyleSheet("")
            return
        value = hex_to_int(text)
        if value is None:
            self._edit.setStyleSheet("color: #F44336;")
            return
        max_value = 0x1FFFFFFF if self._bit_combo.currentIndex() == 1 else 0x7FF
        self._edit.setStyleSheet("color: #4CAF50;" if value <= max_value else "color: #F44336;")


class DataVariantsDialog(QDialog):
    """Диалог со списком уникальных наборов данных для выбранного ID."""

    def __init__(self, can_id: int, variants: set[bytes], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Варианты данных для ID {0}").format(int_to_hex(can_id, 8 if can_id > 0x7FF else 3)))
        self.resize(500, 300)
        layout = QVBoxLayout(self)
        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([tr("№"), tr("Данные"), tr("ASCII")])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, data in enumerate(sorted(variants)):
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            self._table.setItem(row, 1, QTableWidgetItem(" ".join(f"{b:02X}" for b in data)))
            self._table.setItem(row, 2, QTableWidgetItem(_ascii_from_data(data)))
        layout.addWidget(self._table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _id_row_color(row_index: int, palette_size: int) -> QColor:
    """Цвет ячейки ID по позиции строки: palette_size равномерно
    разнесённых оттенков повторяются по кругу — глаз цепляется за цвет
    соседних строк, а не за hex. Та же приглушённая гамма (S80/L40)."""
    n = max(1, palette_size)
    hue = int((row_index % n) * 360 / n)
    return QColor.fromHsl(hue, 80, 40)


def _is_dark_theme() -> bool:
    """Тёмная ли сейчас палитра приложения."""
    try:
        return QApplication.palette().base().color().lightness() < 128
    except Exception:  # noqa: BLE001
        return False


def _tx_echo_colors() -> tuple[QColor, QColor]:
    """Фон и текст строки кадра, отправленного самим МК (tx_echo):
    оранжевый ШРИФТ на общем фоне — заливка путалась с подсветкой
    смены данных (отчёт мастера); теперь оранжевый цвет текста
    однозначно значит «кадр отправил МК»."""
    if _is_dark_theme():
        return _row_base_bg(), QColor("#FF8C00")
    return _row_base_bg(), QColor("#E65100")


def _row_base_bg() -> QColor:
    """Фон обычной строки таблицы приёма — как общий фон окна, а не
    более тёмный фон виджета-таблицы (в тёмной теме строки выглядели
    почти чёрными — отчёт мастера)."""
    return QApplication.palette().color(QPalette.ColorRole.Window)


def _row_base_fg() -> QColor:
    """Шрифт строк приёма: в тёмной теме явно белый (отчёт мастера),
    в светлой — палитра."""
    if _is_dark_theme():
        return QColor("#FFFFFF")
    return QApplication.palette().color(QPalette.ColorRole.Text)


# Роль item-данных: множество индексов байтов DATA, которые делегат
# рисует жёлтым шрифтом (подсветка «смена DATA» побайтно).
_DATA_HL_ROLE = Qt.ItemDataRole.UserRole + 101


class SendPacketsDialog(QDialog):
    """Отправка выделенных строк истории ID: разово с паузой между
    пакетами или циклически — до «Стоп» либо заданное число кругов."""

    def __init__(
        self,
        can_id: int,
        packets: list[tuple[bytes, int, bool]],
        send_cb,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            tr("Отправка ID {0}").format(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        )
        self._can_id = can_id
        self._packets = list(packets)
        self._send_cb = send_cb
        self._pos = 0
        self._round = 0
        self._sent = 0

        font = QFont("Segoe UI", 9)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(tr("Выделено пакетов: {0}").format(len(packets))))

        pause_row = QHBoxLayout()
        pause_row.addWidget(QLabel(tr("Пауза между пакетами, мс")))
        self._pause_spin = QSpinBox()
        self._pause_spin.setRange(0, 60000)
        self._pause_spin.setValue(10)
        self._pause_spin.setFont(font)
        pause_row.addWidget(self._pause_spin)
        pause_row.addStretch()
        layout.addLayout(pause_row)

        self._once_radio = QRadioButton(tr("Отправить разово"))
        self._once_radio.setChecked(True)
        self._loop_radio = QRadioButton(tr("Циклически до «Стоп»"))
        self._rounds_radio = QRadioButton(tr("Кол-во кругов:"))
        self._rounds_spin = QSpinBox()
        self._rounds_spin.setRange(1, 9999)
        self._rounds_spin.setValue(1)
        for w in (self._once_radio, self._loop_radio, self._rounds_radio,
                  self._rounds_spin):
            w.setFont(font)
        layout.addWidget(self._once_radio)
        layout.addWidget(self._loop_radio)
        rounds_row = QHBoxLayout()
        rounds_row.addWidget(self._rounds_radio)
        rounds_row.addWidget(self._rounds_spin)
        rounds_row.addStretch()
        layout.addLayout(rounds_row)

        self._status_label = QLabel(tr("Отправлено: 0"))
        self._status_label.setFont(font)
        layout.addWidget(self._status_label)

        buttons = QHBoxLayout()
        self._start_button = QPushButton(tr("Старт"))
        self._stop_button = QPushButton(tr("Стоп"))
        self._stop_button.setEnabled(False)
        close_button = QPushButton(tr("Закрыть"))
        for b in (self._start_button, self._stop_button, close_button):
            b.setFont(font)
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._stop_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._start_button.clicked.connect(self._start_sending)
        self._stop_button.clicked.connect(self._stop_sending)
        close_button.clicked.connect(self.reject)

    def _start_sending(self) -> None:
        if not self._packets:
            return
        self._pos = 0
        self._round = 0
        self._sent = 0
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._send_one()
        # «Разово» из одной строки заканчивается прямо в _send_one —
        # таймер запускаем, только если отправка не завершена.
        if self._stop_button.isEnabled():
            self._timer.start(max(1, self._pause_spin.value()))

    def _stop_sending(self) -> None:
        self._timer.stop()
        self._start_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        if self._sent:
            self._status_label.setText(
                tr("Отправлено: {0} — остановлено").format(self._sent)
            )

    def _send_one(self) -> None:
        data, dlc, rtr = self._packets[self._pos]
        if self._send_cb is not None:
            self._send_cb(self._can_id, bytes(data), dlc, rtr)
        self._sent += 1
        self._status_label.setText(tr("Отправлено: {0}").format(self._sent))
        self._pos += 1
        if self._pos >= len(self._packets):
            self._pos = 0
            self._round += 1
            if self._once_radio.isChecked() or (
                self._rounds_radio.isChecked()
                and self._round >= self._rounds_spin.value()
            ):
                self._timer.stop()
                self._start_button.setEnabled(True)
                self._stop_button.setEnabled(False)
                self._status_label.setText(
                    tr("Отправлено: {0} — готово").format(self._sent)
                )

    def _tick(self) -> None:
        self._send_one()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)


class CalculatorDialog(QDialog):
    """Мини-калькулятор bin/dec/hex/char: ввод в любое поле сразу
    пересчитывает остальные (значение — беззнаковое до 64 бит)."""

    MAX_VALUE = (1 << 64) - 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Калькулятор"))
        font = QFont("Segoe UI", 9)
        mono = QFont("Consolas", 10)
        grid = QGridLayout(self)
        grid.setSpacing(6)

        self._edits: dict[str, QLineEdit] = {}
        rows = [
            ("bin", "BIN"),
            ("dec", "DEC"),
            ("hex", "HEX"),
            ("char", "CHAR"),
        ]
        for row, (key, name) in enumerate(rows):
            label = QLabel(name)
            label.setFont(font)
            edit = QLineEdit()
            edit.setFont(mono)
            edit.setMinimumWidth(220)
            edit.textChanged.connect(lambda _t, k=key: self._recalc(k))
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            self._edits[key] = edit
        self._edits["bin"].setPlaceholderText("0101…")
        self._edits["hex"].setPlaceholderText("FF…")
        self._edits["char"].setMaxLength(8)
        self._edits["char"].setPlaceholderText(tr("символы"))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        grid.addWidget(buttons, len(rows), 0, 1, 2)

    def _parse(self, key: str) -> int | None:
        """Текст поля → int или None при ошибке/пустом вводе."""
        text = self._edits[key].text().strip()
        if not text:
            return None
        try:
            if key == "bin":
                value = int(text.replace(" ", ""), 2)
            elif key == "dec":
                value = int(text, 10)
            elif key == "hex":
                value = int(text, 16)
            else:  # char
                raw = text.encode("latin-1", "replace")[:8]
                value = int.from_bytes(raw, "big")
            if value < 0 or value > self.MAX_VALUE:
                return None
            return value
        except (ValueError, OverflowError):
            return None

    def _recalc(self, source: str) -> None:
        edit = self._edits[source]
        value = self._parse(source)
        if value is None:
            # Пустое поле — сброс остальных; мусор — красный фон.
            edit.setStyleSheet("" if not edit.text().strip() else "color: #F44336;")
            if not edit.text().strip():
                for k, e in self._edits.items():
                    if k != source:
                        e.blockSignals(True)
                        e.setText("")
                        e.setStyleSheet("")
                        e.blockSignals(False)
            return
        edit.setStyleSheet("")
        width = max(1, (value.bit_length() + 7) // 8)
        texts = {
            "bin": format(value, "b"),
            "dec": str(value),
            "hex": format(value, "X"),
            "char": "".join(
                chr(b) if 32 <= b < 127 else "."
                for b in value.to_bytes(width, "big")
            ),
        }
        for key, e in self._edits.items():
            if key == source:
                continue
            e.blockSignals(True)
            e.setText(texts[key])
            e.setStyleSheet("")
            e.blockSignals(False)


class _DataByteDelegate(QStyledItemDelegate):
    """DATA-ячейка таблицы приёма: рисует байты по одному, чтобы
    подсветка «смена DATA» меняла цвет шрифта конкретного байта
    (жёлтый), а не заливку фона строки (отчёт мастера). Индексы байтов
    лежат в item.data(_DATA_HL_ROLE); пустое значение — обычная
    отрисовка базовым классом."""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        highlighted = index.data(_DATA_HL_ROLE)
        if not highlighted:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text or ""
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget
        )

        fm = opt.fontMetrics
        rect = opt.rect
        total_w = fm.horizontalAdvance(text)
        x = rect.x() + max(0, (rect.width() - total_w) // 2)  # AlignCenter
        y = rect.y() + (rect.height() + fm.ascent() - fm.descent()) // 2
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        if isinstance(fg, QBrush):
            base = fg.color()
        elif isinstance(fg, QColor):
            base = fg
        else:
            base = opt.palette.color(QPalette.ColorRole.Text)
        # Изменившийся байт — КРАСНЫЙ шрифт: оранжевый зарезервирован за
        # tx_echo (кадры, отправленные самим МК) — отчёт мастера.
        hl = QColor("#FF5252") if _is_dark_theme() else QColor("#D32F2F")
        painter.save()
        painter.setFont(opt.font)
        for i in range((len(text) + 2) // 3):
            seg = text[i * 3:i * 3 + 3]
            painter.setPen(hl if i in highlighted else base)
            painter.drawText(x, y, seg)
            x += fm.horizontalAdvance(seg)
        painter.restore()


def _data_percent(data: bytes, dlc: int, byte_indices=None) -> float:
    """DATA как процент: сумма выбранных байт от их максимума
    (255 × N). byte_indices=None — все байты DLC; иначе — iterable
    индексов (чекбоксы «Байт N» в истории ID)."""
    dlc = max(0, min(int(dlc), len(data)))
    if dlc == 0:
        return 0.0
    if byte_indices is None:
        byte_indices = range(dlc)
    indices = [i for i in byte_indices if 0 <= i < dlc]
    if not indices:
        return 0.0
    return sum(data[i] for i in indices) * 100.0 / (255.0 * len(indices))


def _data_sum(data: bytes, dlc: int, byte_indices=None) -> int:
    """Десятичная сумма выбранных байт — мгновенное значение рядом с %."""
    dlc = max(0, min(int(dlc), len(data)))
    if byte_indices is None:
        byte_indices = range(dlc)
    return sum(data[i] for i in byte_indices if 0 <= i < dlc)


class _PercentGraph(QWidget):
    """Живой график значения DATA в процентах: ось Y — %, ось X — время.

    Бегунок «развёртка» задаёт ширину окна в секундах, «инвертирование»
    переворачивает ось Y (FF..FF внизу, 00..00 вверху).
    """

    LINE_COLOR = QColor("#4CAF50")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: list[tuple[float, float]] = []  # (time, percent)
        self._window_s = 30.0
        self._inverted = False
        self._hover_x: float | None = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def set_window(self, seconds: float) -> None:
        self._window_s = max(1.0, float(seconds))
        self.update()

    def set_inverted(self, inverted: bool) -> None:
        self._inverted = inverted
        self.update()

    def add_sample(self, t: float, percent: float) -> None:
        self._samples.append((t, percent))
        if len(self._samples) > 10_000:
            del self._samples[: len(self._samples) - 10_000]
        self.update()

    def set_samples(self, samples: list[tuple[float, float]]) -> None:
        self._samples = list(samples)
        self.update()

    # ---- геометрия -----------------------------------------------------

    def _plot_rect(self) -> "QRect":
        return self.rect().adjusted(38, 10, -10, -24)

    def _to_point(self, rect, t: float, v: float, start: float) -> QPointF:
        """(время, %) → точка на канве с учётом инверсии оси Y."""
        shown = 100.0 - v if self._inverted else v
        x = rect.left() + rect.width() * (t - start) / self._window_s
        y = rect.bottom() - rect.height() * shown / 100.0
        return QPointF(x, y)

    @staticmethod
    def _smooth_path(points: list[QPointF]) -> QPainterPath:
        """Сглаживание Catmull-Rom → кубические Bezier (плавная кривая)."""
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        n = len(points)
        for i in range(n - 1):
            p0 = points[i - 1] if i > 0 else points[0]
            p1, p2 = points[i], points[i + 1]
            p3 = points[i + 2] if i + 2 < n else p2
            c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                         p1.y() + (p2.y() - p0.y()) / 6.0)
            c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                         p2.y() - (p3.y() - p1.y()) / 6.0)
            path.cubicTo(c1, c2, p2)
        return path

    # ---- мышь ----------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = event.position().x()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    # ---- отрисовка -----------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#232338"))
        rect = self._plot_rect()
        painter.setPen(QPen(QColor("#5A5A7A"), 1))
        painter.drawRect(rect)
        if rect.width() <= 0 or rect.height() <= 0:
            painter.end()
            return

        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        # Сетка и подписи процентов (инверсия переворачивает шкалу)
        for pct in (0, 25, 50, 75, 100):
            shown = 100 - pct if self._inverted else pct
            y = rect.bottom() - rect.height() * shown / 100.0
            painter.setPen(QPen(QColor("#3A3A5A"), 1, Qt.PenStyle.DotLine))
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            painter.setPen(QColor("#9E9E9E"))
            painter.drawText(2, int(y) + 4, f"{pct}%")

        now = time.time()
        start = now - self._window_s
        pts = [(t, v) for t, v in self._samples if t >= start]
        coords = [self._to_point(rect, t, v, start) for t, v in pts]

        # Кривая: сглаженная линия + градиентная заливка под ней
        if len(coords) >= 2:
            curve = self._smooth_path(coords)
            fill = QPainterPath(curve)
            fill.lineTo(coords[-1].x(), rect.bottom())
            fill.lineTo(coords[0].x(), rect.bottom())
            fill.closeSubpath()
            gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            top = QColor(self.LINE_COLOR)
            top.setAlpha(90)
            bottom = QColor(self.LINE_COLOR)
            bottom.setAlpha(5)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, bottom)
            painter.fillPath(fill, gradient)
            painter.setPen(QPen(self.LINE_COLOR, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(curve)

            # Светящаяся точка последнего значения + подпись
            last = coords[-1]
            glow = QColor(self.LINE_COLOR)
            glow.setAlpha(60)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(last, 7, 7)
            painter.setBrush(self.LINE_COLOR)
            painter.drawEllipse(last, 3, 3)
            painter.setPen(QColor("#E0E0E0"))
            label = f"{pts[-1][1]:.1f}%"
            lx = min(last.x() + 10, rect.right() - 34)
            painter.drawText(int(lx), int(last.y()) - 8, label)

        # Метки времени на оси X (5 делений)
        painter.setPen(QColor("#9E9E9E"))
        for i in range(5):
            t = start + self._window_s * i / 4.0
            x = rect.left() + rect.width() * i / 4.0
            text = time.strftime("%H:%M:%S", time.localtime(t))
            tx = int(x) - 20 if 0 < i < 4 else int(x) - (2 if i == 0 else 40)
            painter.drawText(tx, self.rect().bottom() - 6, text)

        # Hover-курсор: вертикальная линия + значение ближайшей точки
        if self._hover_x is not None and pts:
            x = min(max(self._hover_x, rect.left()), rect.right())
            t_hover = start + (x - rect.left()) / rect.width() * self._window_s
            t_near, v_near = min(pts, key=lambda p: abs(p[0] - t_hover))
            pt = self._to_point(rect, t_near, v_near, start)
            painter.setPen(QPen(QColor("#AAAAAA"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(int(pt.x()), int(rect.top()), int(pt.x()), int(rect.bottom()))
            painter.setBrush(QColor("#FFFFFF"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(pt, 3, 3)
            ts = time.strftime("%H:%M:%S", time.localtime(t_near))
            ts += f".{int((t_near % 1) * 1000):03d}"
            shown = 100.0 - v_near if self._inverted else v_near
            info = f"{ts} — {shown:.1f}%"
            ix = min(pt.x() + 8, rect.right() - 110)
            painter.setPen(QColor("#E0E0E0"))
            painter.drawText(int(ix), int(rect.top()) + 12, info)

        painter.end()


class IdHistoryDialog(QDialog):
    """История одного CAN ID: таблица «время → data» + живой график %."""

    MAX_ROWS = 2000

    def __init__(
        self,
        can_id: int,
        channel: int,
        samples: list[tuple[float, bytes, bool, int]],
        parent: QWidget | None = None,
        send_callback: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self.can_id = can_id
        # Разовая отправка строки на шину для тестирования — колбэк
        # монитора (can_id, data, dlc, rtr). None — без устройства.
        self._send_callback = send_callback
        self.setWindowTitle(tr("История ID 0x{0:X} — CAN{1}").format(can_id, channel))
        # Окно анализа — сразу на весь доступный экран: таблица и
        # график читаются без прокрутки, кнопки остаются видимыми.
        self.setWindowState(Qt.WindowState.WindowMaximized)
        # Сырые сэмплы для пересчёта при смене набора байт графика
        # (чекбоксы «Байт N», «весь DATA» = все отмечены) и инверсии.
        self._samples_raw: list[tuple[float, bytes, bool, int]] = []

        font = QFont("Segoe UI", 9)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([tr("Время"), tr("DLC"), tr("Data")])
        self._table.setFont(font)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # Мультивыбор строк — «Отправить …» шлёт все выделенные пакеты.
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_table_menu)

        self._percent_label = QLabel("0%")
        self._percent_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        # Мгновенная десятичная сумма выбранных байт — рядом с %.
        self._value_label = QLabel("0")
        self._value_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._value_label.setToolTip(
            tr("Сумма выбранных байт DATA в десятичной системе")
        )

        self._graph = _PercentGraph(self)

        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(5, 300)
        self._zoom_slider.setValue(30)
        self._zoom_label = QLabel(tr("Развёртка: 30 с"))
        self._zoom_label.setFont(font)
        self._zoom_slider.valueChanged.connect(self._on_zoom_changed)

        self._invert_button = QPushButton(tr("Инвертирование"))
        self._invert_button.setFont(font)
        self._invert_button.setCheckable(True)
        self._invert_button.setToolTip(tr("FF..FF = 0% внизу, 00..00 = 100% вверху"))
        self._invert_button.toggled.connect(self._on_invert_toggled)

        # Выбор байтов источника графика: выпадающее меню с галочками
        # «Байт 0..7». Процент и график считаются от СУММЫ отмеченных
        # байт; ни одного — трактуем как «весь DATA» (все байты).
        self._byte_actions: list[Any] = []
        self._source_button = QPushButton(tr("Весь DATA ▾"))
        self._source_button.setFont(font)
        self._source_button.setToolTip(
            tr("Байты DATA для графика и процентов — отметьте нужные")
        )
        source_menu = QMenu(self._source_button)
        for i in range(8):
            action = source_menu.addAction(tr("Байт {0}").format(i))
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(lambda _c, idx=i: self._on_bytes_changed())
            self._byte_actions.append(action)
        self._source_button.setMenu(source_menu)

        self._export_button = QPushButton(tr("Экспорт CSV"))
        self._export_button.setFont(font)
        self._export_button.clicked.connect(self._export_csv)

        # Выбранная строка истории → буфер обмена в формате пакета
        # (ID=.. DLC=.. DATA=..) — вставляется прямо в поля триггера —
        # и разовая отправка этой строки на шину для проверки реакции.
        self._copy_row_button = QPushButton(tr("Копировать строку"))
        self._copy_row_button.setFont(font)
        self._copy_row_button.setToolTip(tr("ID/DLC/Data выбранной строки — для вставки в триггер"))
        self._copy_row_button.clicked.connect(self._copy_selected_packet)
        self._send_row_button = QPushButton(tr("Отправить …"))
        self._send_row_button.setFont(font)
        self._send_row_button.setToolTip(
            tr("Отправка выделенных строк: разово с паузой или циклически")
        )
        self._send_row_button.clicked.connect(self._open_send_dialog)
        self._send_row_button.setEnabled(self._send_callback is not None)

        self._calc_button = QPushButton(tr("Калькулятор"))
        self._calc_button.setFont(font)
        self._calc_button.setToolTip(tr("BIN/DEC/HEX/CHAR — ввод в любое поле пересчитывает остальные"))
        self._calc_button.clicked.connect(lambda: CalculatorDialog(self).exec())

        bottom = QHBoxLayout()
        bottom.addWidget(self._percent_label)
        bottom.addWidget(QLabel("="))
        bottom.addWidget(self._value_label)
        bottom.addSpacing(16)
        bottom.addWidget(self._source_button)
        bottom.addWidget(self._zoom_label)
        bottom.addWidget(self._zoom_slider, 1)
        bottom.addWidget(self._invert_button)
        bottom.addWidget(self._calc_button)
        bottom.addWidget(self._copy_row_button)
        bottom.addWidget(self._send_row_button)
        bottom.addWidget(self._export_button)

        layout.addWidget(self._table, 1)
        layout.addWidget(self._graph)
        layout.addLayout(bottom)

        for sample in samples:
            self._append_row(*sample, repaint=False)
        # Счётчик процентов сразу показывает последнее известное значение,
        # а не «0%» до прихода нового кадра.
        if samples:
            _t, data, rtr, dlc = samples[-1]
            pct = _data_percent(data, dlc, self._selected_bytes()) if not rtr else 0.0
            self._percent_label.setText(f"{pct:.1f}%")
            self._value_label.setText(
                str(_data_sum(data, dlc, self._selected_bytes()) if not rtr else 0)
            )
            self._table.scrollToBottom()
        self._graph.update()

        # Периодический repaint — «ползущее» окно времени
        self._repaint_timer = QTimer(self)
        self._repaint_timer.timeout.connect(self._graph.update)
        self._repaint_timer.start(250)

    def _selected_bytes(self) -> list[int] | None:
        """Индексы отмеченных байт DATA; все/ни одного — None (=весь)."""
        sel = [i for i, a in enumerate(self._byte_actions) if a.isChecked()]
        return sel if 0 < len(sel) < 8 else None

    def _current_pct(self, data: bytes, rtr: bool, dlc: int) -> float:
        return _data_percent(data, dlc, self._selected_bytes()) if not rtr else 0.0

    def _current_sum(self, data: bytes, rtr: bool, dlc: int) -> int:
        return _data_sum(data, dlc, self._selected_bytes()) if not rtr else 0

    def _append_row(self, t: float, data: bytes, rtr: bool, dlc: int, repaint: bool = True) -> None:
        # Автопрокрутка — только при бегунке внизу: иначе в потоке кадров
        # таблицу невозможно прокрутить вверх.
        scrollbar = self._table.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        if self._table.rowCount() >= self.MAX_ROWS:
            self._table.removeRow(0)
        row = self._table.rowCount()
        self._table.insertRow(row)
        timestamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
        data_text = "rtr" if rtr else " ".join(format_data_bytes(data))
        for col, text in enumerate((timestamp, str(dlc), data_text)):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, col, item)
        self._samples_raw.append((t, bytes(data), rtr, dlc))
        if len(self._samples_raw) > self.MAX_ROWS:
            del self._samples_raw[: len(self._samples_raw) - self.MAX_ROWS]
        pct = self._current_pct(data, rtr, dlc)
        self._graph.add_sample(t, pct)
        if repaint:
            shown = 100.0 - pct if self._invert_button.isChecked() else pct
            self._percent_label.setText(f"{shown:.1f}%")
            self._value_label.setText(str(self._current_sum(data, rtr, dlc)))
            if was_at_bottom:
                self._table.scrollToBottom()

    def add_sample(self, t: float, data: bytes, rtr: bool, dlc: int) -> None:
        """Вызывается монитором при новом фрейме с этим ID."""
        self._append_row(t, data, rtr, dlc)

    def _show_table_menu(self, position) -> None:
        row = self._table.rowAt(position.y())
        if row < 0 or row >= len(self._samples_raw):
            return
        self._table.setCurrentCell(row, self._table.currentColumn())
        menu = QMenu(self)
        menu.addAction(tr("Копировать строку (для триггера)"), lambda: self._copy_row_packet(row))
        if self._send_callback is not None:
            menu.addAction(tr("Отправить разово"), lambda: self._send_row(row))
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _packet_text(self, row: int) -> str:
        """Строка ID=.. DLC=.. DATA=.. для вставки в поля триггера."""
        _t, data, rtr, dlc = self._samples_raw[row]
        dlc = max(0, min(dlc, len(data)))
        data_str = "" if rtr else " ".join(f"{b:02X}" for b in data[:dlc])
        return f"ID=0x{self.can_id:X} DLC={dlc} DATA={data_str}"

    def _copy_row_packet(self, row: int) -> None:
        QApplication.clipboard().setText(self._packet_text(row))

    def _copy_selected_packet(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._samples_raw):
            self._copy_row_packet(row)
        else:
            show_toast(self, tr("Выберите строку истории"), success=False)

    def _send_row(self, row: int) -> None:
        if self._send_callback is None:
            return
        _t, data, rtr, dlc = self._samples_raw[row]
        self._send_callback(self.can_id, bytes(data), dlc, rtr)

    def _open_send_dialog(self) -> None:
        """«Отправить …»: выделенные строки → разово с паузой или
        циклически (до «Стоп»/N кругов по кругу)."""
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        if not rows:
            row = self._table.currentRow()
            if 0 <= row < len(self._samples_raw):
                rows = [row]
        packets = [
            (bytes(data), dlc, rtr)
            for r in rows
            if 0 <= r < len(self._samples_raw)
            for _t, data, rtr, dlc in (self._samples_raw[r],)
        ]
        if not packets:
            show_toast(self, tr("Выберите строку истории"), success=False)
            return
        SendPacketsDialog(self.can_id, packets, self._send_callback, self).exec()

    def _on_bytes_changed(self) -> None:
        """Смена набора байт графика — чекбоксы «Байт N» в меню."""
        sel = [i for i, a in enumerate(self._byte_actions) if a.isChecked()]
        if len(sel) == 8 or not sel:
            self._source_button.setText(tr("Весь DATA ▾"))
        else:
            self._source_button.setText(
                tr("Байты {0} ▾").format(",".join(str(i) for i in sel))
            )
        self._graph.set_samples(
            [
                (t, self._current_pct(data, rtr, dlc))
                for t, data, rtr, dlc in self._samples_raw
            ]
        )
        if self._samples_raw:
            _t, data, rtr, dlc = self._samples_raw[-1]
            pct = self._current_pct(data, rtr, dlc)
            shown = 100.0 - pct if self._invert_button.isChecked() else pct
            self._percent_label.setText(f"{shown:.1f}%")
            self._value_label.setText(str(self._current_sum(data, rtr, dlc)))

    def _on_zoom_changed(self, value: int) -> None:
        self._zoom_label.setText(tr("Развёртка: {0} с").format(value))
        self._graph.set_window(float(value))

    def _on_invert_toggled(self, checked: bool) -> None:
        self._graph.set_inverted(checked)
        # Пересчитываем текущий процент и график с учётом инверсии
        if self._graph._samples:
            _t, pct = self._graph._samples[-1]
            self._percent_label.setText(f"{(100.0 - pct) if checked else pct:.1f}%")

    def _export_csv(self) -> None:
        """Выгружает историю ID в CSV: время, DLC, DATA, процент."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Экспорт истории ID 0x{0:X}").format(self.can_id),
            f"id_{self.can_id:X}_history.csv",
            tr("CSV files (*.csv)"),
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "dlc", "data", "percent"])
                for t, data, rtr, dlc in self._samples_raw:
                    ts = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
                    data_text = "rtr" if rtr else " ".join(format_data_bytes(data))
                    writer.writerow([ts, dlc, data_text, f"{self._current_pct(data, rtr, dlc):.1f}"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта истории ID: %s", exc)


class BitmapDialog(QDialog):
    """Диалог с битовой картой 8×8 для последнего кадра ID."""

    def __init__(self, can_id: int, data: bytes, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Битовая карта ID {0}").format(int_to_hex(can_id, 8 if can_id > 0x7FF else 3)))
        layout = QGridLayout(self)
        for byte_idx in range(8):
            byte = data[byte_idx] if byte_idx < len(data) else 0
            for bit in range(8):
                label = QLabel("1" if (byte >> bit) & 1 else "0")
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setStyleSheet(
                    f"background-color: {'#4A6A8A' if (byte >> bit) & 1 else '#2B2B2B'}; "
                    "color: #FFFFFF; border: 1px solid #555; min-width: 22px; min-height: 22px;"
                )
                layout.addWidget(label, byte_idx, 7 - bit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons, 8, 0, 1, 8)


class DbcSignalDialog(QDialog):
    """Диалог выбора сообщения и сигнала из DBC для автозаполнения."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Выбор сигнала из DBC"))
        self.resize(360, 180)
        self._dbc_manager = DBCManager()
        self._result: Any | None = None
        layout = QVBoxLayout(self)
        self._message_combo = QComboBox()
        self._signal_combo = QComboBox()
        self._message_combo.currentIndexChanged.connect(self._on_message_changed)
        layout.addWidget(QLabel(tr("Сообщение:")))
        layout.addWidget(self._message_combo)
        layout.addWidget(QLabel(tr("Сигнал:")))
        layout.addWidget(self._signal_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._populate()

    def _populate(self) -> None:
        db = self._dbc_manager.get_cantools_db()
        self._message_combo.clear()
        if db is None:
            return
        for message in db.messages:
            self._message_combo.addItem(message.name, message)
        self._on_message_changed(0)

    def _on_message_changed(self, index: int) -> None:
        self._signal_combo.clear()
        message = self._message_combo.itemData(index)
        if message is None:
            return
        for signal in message.signals:
            self._signal_combo.addItem(signal.name, signal)

    def _on_ok(self) -> None:
        message = self._message_combo.currentData()
        signal = self._signal_combo.currentData()
        if message is None or signal is None:
            self.reject()
            return
        try:
            encoded = message.encode({signal.name: 0})
            self._result = (message.frame_id, list(encoded))
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка кодирования сигнала DBC: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось закодировать сигнал: {0}").format(exc))
            return
        self.accept()

    def get_result(self) -> Any | None:
        return self._result


class CanChannelMonitor(QWidget):
    """Панель мониторинга одного CAN-канала."""

    create_trigger_requested = Signal(dict)
    monitoring_state_changed = Signal(int, bool)

    def __init__(self, channel: int, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._channel_byte = channel
        self._serial_manager = serial_manager
        self._config = Config()
        self._running = False
        self._received_count = 0
        self._sent_count = 0
        self._packet_times: deque[tuple[float, int]] = deque()  # (t, бит кадра)
        self._last_packet_time: float | None = None
        self._cyclic_frame: bytes | None = None
        self._dbc_manager = DBCManager()

        self._id_to_row: dict[int, int] = {}
        self._id_stats: dict[int, dict[str, Any]] = {}
        self._id_data_variants: dict[int, set[bytes]] = {}
        # Последнее направление кадра по каждому ID: True — кадр
        # отправлен самим МК (tx_echo), строка подсвечивается цветом
        # направления (тёмная тема — оранжевый, светлая — чёрный).
        self._id_tx_echo: dict[int, bool] = {}
        # История фреймов по каждому ID (время, data, rtr, dlc) — для
        # диалога «История ID» из контекстного меню таблицы.
        self._id_history: dict[int, deque] = {}
        self._history_dialogs: list[IdHistoryDialog] = []
        self._highlight_timers: dict[int, QTimer] = {}
        self._ignored_ids: set[int] = set()
        # Входная очередь кадров: при потоке с двух CAN один кадр = одно
        # обновление таблицы — это тысячи setItem/с и главный источник
        # фризов UI. Кадры копятся и сливаются до ~25 обновлений/с на ID.
        self._pending_rx: list[dict[str, Any]] = []
        self._rx_flush_timer = QTimer(self)
        self._rx_flush_timer.setSingleShot(True)
        self._rx_flush_timer.setInterval(40)
        self._rx_flush_timer.timeout.connect(self._flush_rx)
        # «Скрыть выделенное»: ID строк, скрытых кнопкой у поиска.
        self._hidden_ids: set[int] = set()
        # Фон ячейки ID кодирует позицию строки в окне — количество
        # различимых цветов задаёт оператор («кол-во строк» у RTR).
        self._row_color_count = max(1, int(self._config.get("monitor_row_colors", 15) or 15))
        # Предыдущие значения счётчиков ошибок — для визуальных предупреждений
        # (чек-лист 4.2): bus-off/рост потерь подсвечивают строку статуса.
        self._prev_busoff = 0
        self._prev_lost = 0
        self._warn_level = 0  # 0=ok, 1=lost, 2=bus-off

        self._create_widgets()
        self._layout_widgets()
        self._setup_timers()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)
        self._font = font

        compact_font = QFont("Segoe UI", 9)

        self._start_button = QPushButton(tr("Запустить"))
        self._start_button.setFixedSize(80, 28)
        self._start_button.setFont(compact_font)
        self._start_button.clicked.connect(self._start)

        self._stop_button = QPushButton(tr("Остановить"))
        self._stop_button.setFixedSize(90, 28)
        self._stop_button.setFont(compact_font)
        self._stop_button.clicked.connect(self._stop)

        self._clear_button = QPushButton(tr("Очистить"))
        setup_button(self._clear_button, height=28)
        self._clear_button.setFont(compact_font)
        self._clear_button.clicked.connect(self._clear)

        self._update_monitor_buttons()

        self._search_edit = QLineEdit()
        self._search_edit.setFixedWidth(160)
        self._search_edit.setFont(font)
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._search_edit.textChanged.connect(self._apply_search)

        # «Скрыть выделенное»: пока зажата — строки выделенных ID не
        # показываются (и новые кадры этих ID не воскрешают строку).
        self._hide_sel_button = QPushButton(tr("Скрыть выделенное"))
        self._hide_sel_button.setFont(compact_font)
        self._hide_sel_button.setFixedHeight(26)
        self._hide_sel_button.setCheckable(True)
        self._hide_sel_button.setToolTip(
            tr("Скрыть строки выделенных ID, пока кнопка нажата")
        )
        self._hide_sel_button.toggled.connect(self._on_hide_selected_toggled)

        self._filter_rules: list[dict[str, Any]] = []
        self._filter_enabled = False
        self._highlight_interval_ms = 500

        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("Счётчик"), tr("ASCII"), tr("Пояснение")]
        )
        # Таблица чуть компактнее остального UI: шрифт 8 pt и уменьшенная
        # высота строк (13 px — в 1.5 раза ниже обычных 20) — в плотном
        # потоке влезает больше кадров на экран.
        self._table.setFont(QFont("Segoe UI", 8))
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(13)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        # Двойной правый клик по строке ID → сразу «История ID» (без меню).
        # Штатный MouseButtonDblClick до нас не доезжает: первый правый
        # клик уже открыл контекстное меню, и второй уходит в него.
        # Поэтому считаем два правых клика по одной строке сами — в
        # _show_context_menu второй клик за 600 мс открывает историю
        # вместо меню.
        self._last_rclick_time = 0.0
        self._last_rclick_row = -1
        self._table.viewport().installEventFilter(self)
        # Двойной клик ЛЕВОЙ кнопкой по строке — та же история ID
        # (таблица логирования, онлайн-график %, развёртка, инверсия).
        self._table.cellDoubleClicked.connect(
            lambda row, _col: self._show_id_history(row)
        )
        # DATA колонка — делегат побайтовой подсветки (жёлтый шрифт
        # изменившегося байта, а не заливка строки — отчёт мастера).
        self._table.setItemDelegateForColumn(2, _DataByteDelegate(self._table))
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 210)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 80)
        self._table.setColumnWidth(5, 90)
        self._table.setColumnWidth(6, 170)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(200)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._send_bit_combo = QComboBox()
        self._send_bit_combo.setFont(font)
        self._send_bit_combo.addItems(BIT_RATES)
        self._send_bit_combo.setFixedWidth(90)

        self._send_id_edit = IdPasteEdit()
        self._send_id_edit.setFixedWidth(90)
        self._send_id_edit.setMaxLength(8)
        self._send_id_edit.setFont(font)
        self._send_id_edit.setPlaceholderText("ID")
        self._send_id_validator = _IdValidator(self._send_id_edit, self._send_bit_combo)
        self._send_id_edit.set_fill_callback(self._fill_send_from_packet)

        self._send_dlc_spin = QSpinBox()
        self._send_dlc_spin.setRange(1, 8)
        self._send_dlc_spin.setValue(8)
        self._send_dlc_spin.setFont(font)
        self._send_dlc_spin.setFixedWidth(45)

        self._send_data_edits, self._send_data_widget = create_data_field_widget(font, 8, edit_width=46)

        self._send_copy_paste = create_clipboard_buttons(
            self, self._send_id_edit, self._send_dlc_spin, self._send_data_edits, self._send_bit_combo
        )

        self._send_period_spin = QSpinBox()
        self._send_period_spin.setRange(0, 9999)
        self._send_period_spin.setValue(1000)
        self._send_period_spin.setSuffix(tr(" мс"))
        self._send_period_spin.setFont(font)
        self._send_period_spin.setMinimumWidth(90)

        self._send_button = QPushButton(tr("Отправить"))
        self._send_button.setMinimumWidth(100)
        self._send_button.setFixedHeight(28)
        self._send_button.setFont(font)
        self._send_button.clicked.connect(self._send_manual)

        self._cyclic_button = QPushButton("∞")
        self._cyclic_button.setFixedSize(56, 36)
        self._cyclic_button.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        self._cyclic_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )
        self._cyclic_button.setToolTip(tr("Циклически"))
        setCheckableWithIndicator(self._cyclic_button)
        self._cyclic_button.toggled.connect(self._on_cyclic_toggled)

        self._rtr_button = QPushButton(tr("RTR"))
        self._rtr_button.setFixedSize(50, 36)
        self._rtr_button.setFont(QFont("Arial", 7, QFont.Weight.Bold))
        self._rtr_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )
        self._rtr_button.setToolTip(tr("Remote Transmission Request"))
        setCheckableWithIndicator(self._rtr_button)
        self._rtr_button.toggled.connect(self._on_rtr_toggled)

        # «Кол-во строк»: сколько позиций таблицы умещается на экране —
        # фон ячейки ID циклится по этому числу оттенков, чтобы каждая
        # видимая строка имела различимый цвет.
        self._row_color_spin = QSpinBox()
        self._row_color_spin.setRange(1, 64)
        self._row_color_spin.setValue(self._row_color_count)
        self._row_color_spin.setFont(font)
        self._row_color_spin.setFixedWidth(52)
        self._row_color_spin.setToolTip(
            tr("Число строк мониторинга на экране — фон ID циклится\n"
               "по этому количеству оттенков")
        )
        self._row_color_spin.valueChanged.connect(self._on_row_color_count_changed)

        self._cyclic_timer = QTimer(self)
        self._cyclic_timer.timeout.connect(self._send_cyclic_frame)

        self._send_dlc_spin.valueChanged.connect(self._on_send_dlc_changed)
        self._on_send_dlc_changed(self._send_dlc_spin.value())

        self._stats_label = QLabel(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._stats_label.setFont(font)

        # Полный DBC-разбор кадра показывается во всплывающей подсказке
        # ячейки (describe_frame в _build_row_items) — отдельной панели
        # под таблицей нет: её место отдано строкам приёма.

        # Мини-индикатор загрузки шины рядом со строкой статуса:
        # зелёный < 50%, оранжевый < 80%, красный выше — как «Память».
        self._load_bar = QProgressBar()
        self._load_bar.setRange(0, 100)
        self._load_bar.setValue(0)
        self._load_bar.setTextVisible(False)
        self._load_bar.setFixedSize(120, 8)
        self._load_bar.setToolTip(tr("Загрузка шины CAN"))

    def _layout_widgets(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        layout.setContentsMargins(4, 4, 4, 4)

        control_layout = QHBoxLayout()
        control_layout.setSpacing(4)
        control_layout.addWidget(self._start_button)
        control_layout.addWidget(self._stop_button)
        control_layout.addWidget(self._clear_button)
        control_layout.addWidget(QLabel(tr("Поиск:")))
        control_layout.addWidget(self._search_edit)
        control_layout.addWidget(self._hide_sel_button)
        control_layout.addStretch()
        layout.addLayout(control_layout)

        layout.addWidget(self._table, 1)

        send_layout = QVBoxLayout()
        send_layout.setSpacing(4)

        send_top = QHBoxLayout()
        send_top.setSpacing(4)
        send_top.addWidget(QLabel(tr("Бит")))
        send_top.addWidget(self._send_bit_combo)
        send_top.addWidget(QLabel(tr("ID")))
        send_top.addWidget(self._send_id_edit)
        send_top.addWidget(QLabel(tr("DLC")))
        send_top.addWidget(self._send_dlc_spin)
        send_top.addWidget(QLabel(tr("Data")))
        send_top.addWidget(self._send_data_widget)
        send_top.addWidget(self._send_copy_paste)
        send_top.addStretch()
        self._send_top_layout = send_top

        send_bottom = QHBoxLayout()
        send_bottom.setSpacing(4)
        send_bottom.addWidget(QLabel(tr("Период")))
        send_bottom.addWidget(self._send_period_spin)
        send_bottom.addWidget(self._send_button)
        send_bottom.addWidget(self._cyclic_button)
        send_bottom.addWidget(self._rtr_button)
        self._row_color_label = QLabel(tr("Кол-во строк"))
        self._row_color_label.setFont(self._font)
        send_bottom.addWidget(self._row_color_label)
        send_bottom.addWidget(self._row_color_spin)
        send_bottom.addStretch()

        self._sent_label = QLabel(tr("Отправлено: 0"))
        self._sent_label.setFont(self._font)

        send_layout.addLayout(send_top)
        send_layout.addLayout(send_bottom)
        send_layout.addWidget(self._sent_label)
        layout.addLayout(send_layout)

        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(8)
        stats_layout.addWidget(self._stats_label, 1)
        stats_layout.addWidget(self._load_bar)
        layout.addLayout(stats_layout)

    def _setup_timers(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_stats)
        self._timer.start(1000)

    def _on_send_dlc_changed(self, value: int) -> None:
        rtr = self._rtr_button.isChecked()
        for i, edit in enumerate(self._send_data_edits):
            if i >= value or rtr:
                edit.setText("")
                edit.setEnabled(False)
            else:
                edit.setEnabled(True)

    def _fill_send_from_packet(self, parsed: dict[str, Any]) -> None:
        """Заполняет панель отправки из распарсенного пакета."""
        can_id = parsed.get("id")
        if can_id is None:
            return
        self._send_bit_combo.setCurrentIndex(1 if can_id > 0x7FF else 0)
        self._send_id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        dlc = max(1, min(8, parsed.get("dlc", 8)))
        self._send_dlc_spin.setValue(dlc)
        data = parsed.get("data", [])
        for i, edit in enumerate(self._send_data_edits):
            # «X» (wildcard из триггеров) в обычных hex-полях не
            # выразима — позиция остаётся пустой, а не «00».
            edit.setText(
                f"{data[i]:02X}" if i < len(data) and data[i] is not None else ""
            )
        self._on_send_dlc_changed(dlc)

    def _on_cyclic_toggled(self, checked: bool) -> None:
        if checked:
            self._cyclic_button.setStyleSheet("background-color: #4CAF50; color: #FFFFFF;")
            self._send_manual()
        else:
            self._cyclic_button.setStyleSheet("")
            self._stop_cyclic_timer()

    def _on_rtr_toggled(self, checked: bool) -> None:
        if checked:
            self._rtr_button.setStyleSheet(
                "QPushButton { background-color: #FF9800; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #F57C00; }"
            )
            self._send_data_widget.setEnabled(False)
            opacity = QGraphicsOpacityEffect(self._send_data_widget)
            opacity.setOpacity(0.35)
            self._send_data_widget.setGraphicsEffect(opacity)
        else:
            self._rtr_button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )
            self._send_data_widget.setEnabled(True)
            self._send_data_widget.setGraphicsEffect(None)

    def _start(self) -> None:
        if not self._serial_manager.is_open():
            port = self._config.get("port", "")
            baudrate = self._config.get("baudrate", 115200)
            emulation = self._config.get("emulation", False)
            error_probability = self._config.get("error_probability", 0)
            if not port:
                QMessageBox.warning(self, tr("Внимание"), tr("Устройство не подключено"))
                return
            if not self._serial_manager.open_port(
                port, baudrate, emulation, auto_reconnect=True,
                error_probability=error_probability,
            ):
                QMessageBox.warning(self, tr("Внимание"), tr("Устройство не подключено"))
                return
        self._running = True
        self._update_monitor_buttons()
        self.monitoring_state_changed.emit(self._channel, True)
        logger.info("Мониторинг CAN%d запущен", self._channel)

    def _stop(self) -> None:
        self._running = False
        self._pending_rx.clear()
        self._rx_flush_timer.stop()
        self._cyclic_button.setChecked(False)
        self._stop_cyclic_timer()
        self._update_monitor_buttons()
        self.monitoring_state_changed.emit(self._channel, False)
        logger.info("Мониторинг CAN%d остановлен", self._channel)

    def _update_monitor_buttons(self) -> None:
        """Визуально отображает текущее состояние мониторинга."""
        if self._running:
            self._start_button.setText(tr("Запущено"))
            self._start_button.setEnabled(False)
            self._start_button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: #FFFFFF; border: none; border-radius: 4px; }"
            )
            self._stop_button.setText(tr("Остановить"))
            self._stop_button.setEnabled(True)
            self._stop_button.setStyleSheet("")
        else:
            self._start_button.setText(tr("Запустить"))
            self._start_button.setEnabled(True)
            self._start_button.setStyleSheet("")
            self._stop_button.setText(tr("Остановлено"))
            self._stop_button.setEnabled(False)
            self._stop_button.setStyleSheet(
                "QPushButton { background-color: #F44336; color: #FFFFFF; border: none; border-radius: 4px; }"
            )

    def _clear(self) -> None:
        self._pending_rx.clear()
        self._rx_flush_timer.stop()
        self._hidden_ids.clear()
        self._hide_sel_button.blockSignals(True)
        self._hide_sel_button.setChecked(False)
        self._hide_sel_button.setStyleSheet("")
        self._hide_sel_button.blockSignals(False)
        self._table.setRowCount(0)
        self._id_to_row.clear()
        self._id_stats.clear()
        self._id_data_variants.clear()
        self._id_tx_echo.clear()
        self._id_history.clear()
        for timer in self._highlight_timers.values():
            timer.stop()
        self._highlight_timers.clear()
        self._received_count = 0
        self._sent_count = 0
        self._packet_times.clear()
        self._last_packet_time = None
        self._prev_busoff = 0
        self._prev_lost = 0
        self._warn_level = 0
        self._stats_label.setStyleSheet("")
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._update_sent_label()

    def _row_id(self, row: int) -> int | None:
        """CAN ID строки таблицы или None."""
        item = self._table.item(row, 0)
        return hex_to_int(item.text()) if item is not None else None

    def _row_matches_search(self, row: int, query: str) -> bool:
        """Строка проходит поисковый запрос (по любой колонке)."""
        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None and query in item.text().lower():
                return True
        return False

    def _is_row_visible(self, row: int) -> bool:
        """Итоговая видимость строки: поиск + «скрыть выделенное»."""
        frame_id = self._row_id(row)
        if frame_id is not None and frame_id in self._hidden_ids:
            return False
        query = self._search_edit.text().strip().lower()
        return not query or self._row_matches_search(row, query)

    def _apply_row_visibility(self, row: int) -> None:
        """Применяет скрытие к одной строке — используется при вставке
        нового ID под активным поиском/«скрыть выделенное»."""
        self._table.setRowHidden(row, not self._is_row_visible(row))

    def _apply_search(self, text: str = "") -> None:
        for row in range(self._table.rowCount()):
            self._apply_row_visibility(row)

    def _on_hide_selected_toggled(self, checked: bool) -> None:
        """«Скрыть выделенное»: зажата — выбранные ID прячутся, отжата —
        возвращаются (если их не режет поиск/фильтр)."""
        if checked:
            for index in self._table.selectedIndexes():
                frame_id = self._row_id(index.row())
                if frame_id is not None:
                    self._hidden_ids.add(frame_id)
            self._hide_sel_button.setStyleSheet(
                "QPushButton { background-color: #C62828; color: #FFFFFF; border: none; }"
            )
        else:
            self._hidden_ids.clear()
            self._hide_sel_button.setStyleSheet("")
        self._apply_search()

    def _recolor_id_column(self, from_row: int = 0) -> None:
        """Перекрашивает фон ID по позиции строки — после вставки/
        удаления строк или смены «кол-во строк»."""
        for row in range(from_row, self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setBackground(_id_row_color(row, self._row_color_count))

    def _on_row_color_count_changed(self, value: int) -> None:
        self._row_color_count = max(1, value)
        self._config.set("monitor_row_colors", self._row_color_count)
        self._recolor_id_column(0)

    def _update_sent_label(self) -> None:
        self._sent_label.setText(tr("Отправлено: {0}").format(self._sent_count))

    def _send_manual(self) -> None:
        can_id = hex_to_int(self._send_id_edit.text())
        if can_id is None:
            return
        dlc = self._send_dlc_spin.value()
        data = self._data_from_send_edits(dlc)
        rtr = self._rtr_button.isChecked() if self._rtr_button is not None else False
        self._cyclic_frame = pack_can_frame(self._channel_byte, can_id, data, rtr=rtr, dlc=dlc)
        if self._send_cyclic_frame() and self._cyclic_button.isChecked():
            self._start_cyclic_timer()

    def _send_frame_once(self, can_id: int, data: bytes, dlc: int, rtr: bool) -> None:
        """Разовая отправка кадра из диалога истории ID."""
        dlc = max(0, min(dlc, len(data), 8))
        frame = pack_can_frame(self._channel_byte, can_id, data[:dlc], rtr=rtr, dlc=dlc)
        if self._serial_manager.send_data(frame):
            self._sent_count += 1
            self._update_sent_label()
        else:
            show_toast(self, tr("Отправка не удалась"), success=False)

    def _data_from_send_edits(self, dlc: int) -> bytes:
        values = [edit.text() for edit in self._send_data_edits[:dlc]]
        parsed = parse_data_bytes(values)
        return bytes(parsed[:dlc])

    def _start_cyclic_timer(self) -> None:
        interval_ms = max(10, self._send_period_spin.value())
        self._cyclic_timer.start(interval_ms)

    def _stop_cyclic_timer(self) -> None:
        if self._cyclic_timer.isActive():
            self._cyclic_timer.stop()

    def _send_cyclic_frame(self) -> bool:
        if self._cyclic_frame is None:
            return False
        result = self._serial_manager.send_data(self._cyclic_frame)
        if result:
            self._sent_count += 1
            self._update_sent_label()
        return result

    def _update_stats(self) -> None:
        now = time.time()
        while self._packet_times and now - self._packet_times[0][0] > 1.0:
            self._packet_times.popleft()
        speed = len(self._packet_times)
        bitrate = int(self._config.get(f"can{self._channel}_speed", 500000) or 500000)
        bits_per_s = sum(bits for _t, bits in self._packet_times)
        load_pct = min(999.0, bits_per_s * 100.0 / bitrate) if bitrate else 0.0
        text = tr("Принято: {0} | Скорость: {1} пак/с | Нагрузка: {2:.0f}%").format(
            self._received_count, speed, load_pct
        )
        try:
            if (
                self._serial_manager.is_open()
                and not self._config.get("emulation", False)
                # Идёт пачка команд настроек (запись/вычитка триггеров):
                # статистика, вклинившись между командами, на живой шине
                # висит до таймаута и держит _lock секундами — пропускаем
                # этот тик, в следующий всё дочитается.
                and not self._serial_manager.in_control_session
            ):
                # Обе команды — одной сессией: иначе каждая гоняет
                # QThread reader stop/start, что на двух каналах
                # давало ~4 пересоздания потока в секунду непрерывно.
                with self._serial_manager.control_session():
                    device = self._serial_manager.read_can_stats(self._channel)
                    usb = self._serial_manager.read_usb_stats()
                ready = tr("OK") if device.get("ready") else tr("INIT FAIL")
                text += tr(
                    " | Ready: {0} | RX: {1} TX: {2} Потеряно: {3} Errors: {4} Bus-off: {5} Recovery: {6}"
                ).format(
                    ready,
                    device["rx_count"],
                    device["tx_count"],
                    device["lost_count"],
                    device["error_count"],
                    device["busoff_count"],
                    device["recovery_count"],
                )
                text += tr(" USB dropped: {0}").format(usb["tx_dropped"])
                # TXfail: кадры, которые МК не смог поставить на шину
                # (все TX-ящики заняты — арбитраж/нет ACK/bus-off) —
                # именно так выглядит молчаливый пропуск ответа триггера.
                tx_fail = device.get("tx_fail_count", 0)
                if tx_fail:
                    text += tr(" | TXfail: {0}").format(tx_fail)
                # FIFOpoll: кадры, спасённые backstop-опросом — RX0-IRQ их
                # пропустило. Рост счётчика = проблема с доставкой IRQ.
                fifo_poll = device.get("fifo_poll_count", 0)
                if fifo_poll:
                    text += tr(" | FIFOpoll: {0}").format(fifo_poll)
                # Полевая телеметрия: по дельтам счётчиков между опросами
                # в логе видно, где теряется время — МК медленный
                # (last_cmd_ms велик, poll_count замирает) или ПК не
                # забирает данные (tx_busy_waits растёт).
                logger.debug(
                    "CAN%d stats: rx=%d tx=%d lost=%d err=%d busoff=%d baud=%d txfail=%d fifopoll=%d | "
                    "usb drop=%d busy=%d cmd=%d cmdms=%d loop=%d",
                    self._channel,
                    device["rx_count"], device["tx_count"],
                    device["lost_count"], device["error_count"],
                    device["busoff_count"], device.get("baud_kbps", -1),
                    tx_fail, fifo_poll,
                    usb["tx_dropped"], usb.get("tx_busy_waits", -1),
                    usb.get("cmd_count", -1), usb.get("last_cmd_ms", -1),
                    usb.get("poll_count", -1),
                )
                self._update_error_warnings(device)
        except Exception:  # noqa: BLE001
            pass
        if self._warn_level == 2:
            text += tr("  ⚠ BUS-OFF — проверьте шину/терминацию")
        elif self._warn_level == 1:
            text += tr("  ⚠ Потеряны кадры CAN")
        self._stats_label.setText(text)
        bar_value = int(min(100, round(load_pct)))
        self._load_bar.setValue(bar_value)
        color = "#4CAF50" if load_pct < 50 else ("#E65100" if load_pct < 80 else "#C62828")
        self._load_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 4px; background: #2b2b2b; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )

    def _update_error_warnings(self, device: dict[str, int]) -> None:
        """Подсвечивает строку статуса при bus-off или росте потерь кадров."""
        busoff = int(device.get("busoff_count", 0))
        lost = int(device.get("lost_count", 0))
        if busoff > self._prev_busoff:
            self._warn_level = 2
            logger.warning("CAN%d: bus-off (всего %d)", self._channel, busoff)
        elif lost > self._prev_lost and self._warn_level < 2:
            self._warn_level = 1
            logger.warning("CAN%d: потеряны кадры (всего %d)", self._channel, lost)
        self._prev_busoff = busoff
        self._prev_lost = lost
        if self._warn_level == 2:
            self._stats_label.setStyleSheet("color: #FFFFFF; background-color: #C62828;")
        elif self._warn_level == 1:
            self._stats_label.setStyleSheet("color: #FFFFFF; background-color: #E65100;")
        else:
            self._stats_label.setStyleSheet("")

    def _format_signals(self, can_id: int, data: bytes) -> str:
        db = self._dbc_manager.get_cantools_db()
        if db is None:
            return ""
        decoded = decode_frame(db, can_id, data)
        if decoded is None:
            return ""
        parts = []
        for name, info in list(decoded.items())[:3]:
            if isinstance(info, dict):
                parts.append(f"{name}={info['value']:.2f}{info.get('unit', '')}")
            else:
                parts.append(f"{name}={info}")
        return " | ".join(parts)

    def _format_period(self, can_id: int, now: float) -> str:
        stats = self._id_stats.get(can_id)
        if stats is None or stats.get("last_time") is None:
            return ""
        period_ms = int((now - stats["last_time"]) * 1000)
        return f"{period_ms} ms"

    def _build_row_items(
        self, frame_id: int, dlc: int, data: bytes, rtr: bool, timestamp: str, period: str, count: int
    ) -> list[str]:
        id_width = 8 if frame_id > 0x7FF else 3
        signals = "" if rtr else self._format_signals(frame_id, data)
        return [
            int_to_hex(frame_id, id_width),
            str(dlc),
            "rtr" if rtr else " ".join(format_data_bytes(data)),
            period,
            str(count),
            "" if rtr else _ascii_from_data(data),
            signals,
        ]

    def queue_frame(self, frame: dict[str, object]) -> None:
        """Кадр во входную очередь — сливается таймером в обновления
        таблицы (~40 мс). Приём с двух насыщенных CAN даёт тысячи кадров
        в секунду: per-frame setItem и вызывало «дикие тормоза» UI."""
        if not self._running:
            return
        self._pending_rx.append(frame)
        if not self._rx_flush_timer.isActive():
            self._rx_flush_timer.start()

    def _account_frame(self, frame: dict[str, object]) -> bool:
        """Пер-кадровый учёт: фильтр, счётчики, варианты, история ID,
        открытые диалоги истории. False — кадр отфильтрован."""
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        rtr = bool(frame.get("rtr", False))
        dlc = int(frame.get("dlc", len(data)))
        self._id_tx_echo[frame_id] = bool(frame.get("tx_echo", False))
        if self._filter_enabled and self._matches_filter(frame_id, data):
            return False
        self._received_count += 1
        now = time.time()
        # Оценка бит кадра для нагрузки шины: ~45 служебных + dlc*8
        # данных, ×1.15 на битстаффинг, +3 бита interframe.
        self._packet_times.append((now, int((45 + dlc * 8) * 1.15) + 3))
        self._last_packet_time = now
        self._id_data_variants.setdefault(frame_id, set()).add(data)
        self._id_history.setdefault(frame_id, deque(maxlen=2000)).append(
            (now, data, rtr, dlc)
        )
        for dialog in self._history_dialogs:
            if dialog.can_id == frame_id:
                dialog.add_sample(now, data, rtr, dlc)
        return True

    def _flush_rx(self) -> None:
        """Разбирает накопленные кадры: учёт/история — по каждому кадру,
        а в таблицу уходит только последнее состояние каждого ID (со
        счётчиком принятых) — промежуточные перезаписи ячеек внутри
        одного UI-тика не имеют смысла."""
        pending = self._pending_rx
        self._pending_rx = []
        if not pending:
            return
        merged: dict[int, list] = {}  # id -> [последний кадр, число]
        for frame in pending:
            frame_id = int(frame["id"])
            if not self._account_frame(frame):
                continue
            entry = merged.get(frame_id)
            if entry is None:
                merged[frame_id] = [frame, 1]
            else:
                entry[1] += 1
        for frame, count in merged.values():
            self._update_table_row(frame, count)

    def add_frame(self, frame: dict[str, object]) -> None:
        """Одиночный кадр (холодный путь: тесты, всплывшие в командных
        сессиях). Горячий путь — queue_frame → _flush_rx."""
        if not self._running:
            return
        if self._account_frame(frame):
            self._update_table_row(frame, 1)

    def _update_table_row(self, frame: dict[str, object], count: int = 1) -> None:
        """Обновляет строку таблицы последним состоянием кадра; count —
        сколько кадров этого ID слилось в одно обновление (батч-путь)."""
        if not self._running:
            return
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        rtr = bool(frame.get("rtr", False))
        dlc = int(frame.get("dlc", len(data)))
        # tx_echo: кадр отправлен самим МК (ответ триггера / другой
        # программы МК) — bxCAN себя не слышит, прошивка возвращает
        # собственные передачи в RX-поток с флагом. Такие строки
        # подсвечиваются цветом направления (_tx_echo_colors).
        tx_echo = bool(frame.get("tx_echo", False))
        # Автопрокрутка — только когда бегунок уже внизу: при потоке
        # кадров пользователь иначе не может прокрутить таблицу вверх —
        # каждый новый кадр сносил позицию на конец списка.
        scrollbar = self._table.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        now = time.time()
        stats = self._id_stats.setdefault(
            frame_id,
            {"count": 0, "last_time": None, "last_data": b"", "last_receive_time": None},
        )
        stats["count"] += count
        period = self._format_period(frame_id, now)
        prev_data = stats.get("last_data")
        prev_time = stats.get("last_time")  # момент прошлого кадра этого ID
        stats["last_time"] = now

        timestamp = time.strftime("%H:%M:%S") + f".{int((now % 1) * 1000):03d}"
        items = self._build_row_items(frame_id, dlc, data, rtr, timestamp, period, stats["count"])

        tooltip = ""
        if not rtr and self._dbc_manager.is_loaded():
            tooltip = self._dbc_manager.describe_frame(frame_id, data)

        if frame_id in self._id_to_row:
            row = self._id_to_row[frame_id]
            for col, text in enumerate(items):
                item = self._table.item(row, col)
                if item is None:
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if col == 0:
                        # Тёмный фон кодирует позицию строки — текст явно
                        # белый, иначе на светлой теме палитра давала
                        # чёрный.
                        item.setBackground(_id_row_color(row, self._row_color_count))
                        item.setForeground(QColor("#FFFFFF"))
                    self._table.setItem(row, col, item)
                else:
                    item.setText(text)
                if tooltip:
                    item.setToolTip(tooltip)
            self._paint_row_direction(row, tx_echo)
            # Подсветка изменившихся данных: красный шрифт конкретного
            # байта на 500 мс. «Интервал подсветки» — фильтр по давности
            # смены: 0 — подсвечивать все изменения; N — только если
            # прошлый кадр этого ID был БОЛЬШЕ чем N мс назад (чем больше
            # N, тем реже подсветка — ловим редко меняющиеся байты).
            if prev_data is not None and prev_data != data:
                gap_ms = (now - prev_time) * 1000.0 if prev_time else float("inf")
                if self._highlight_interval_ms == 0 or gap_ms > self._highlight_interval_ms:
                    changed = {
                        i
                        for i in range(max(len(prev_data), len(data)))
                        if (prev_data[i] if i < len(prev_data) else None)
                        != (data[i] if i < len(data) else None)
                    }
                    if changed:
                        self._highlight_data_cell(row, changed)
        else:
            if self._table.rowCount() >= MAX_TABLE_ROWS:
                last_row = self._table.rowCount() - 1
                id_item = self._table.item(last_row, 0)
                if id_item is not None:
                    fid = hex_to_int(id_item.text())
                    if fid is not None:
                        self._id_to_row.pop(fid, None)
                        self._id_stats.pop(fid, None)
                        self._id_tx_echo.pop(fid, None)
                self._table.removeRow(last_row)
                for fid, r in list(self._id_to_row.items()):
                    if r >= last_row:
                        self._id_to_row[fid] = r - 1
            row = self._find_insert_row(frame_id)
            self._table.insertRow(row)
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 0:
                    item.setBackground(_id_row_color(row, self._row_color_count))
                    item.setForeground(QColor("#FFFFFF"))
                if tooltip:
                    item.setToolTip(tooltip)
                self._table.setItem(row, col, item)
            for fid, r in list(self._id_to_row.items()):
                if r >= row:
                    self._id_to_row[fid] = r + 1
            self._id_to_row[frame_id] = row
            self._paint_row_direction(row, tx_echo)
            # Вставка посередине сдвигает позиции ниже — фон ID кодирует
            # позицию, перекрашиваем хвост таблицы.
            self._recolor_id_column(row + 1)
            self._apply_row_visibility(row)

        stats["last_receive_time"] = now
        stats["last_data"] = data

        if was_at_bottom:
            self._table.scrollToBottom()

    def _matches_filter(self, frame_id: int, data: bytes) -> bool:
        if frame_id in self._ignored_ids:
            return True

        allow_rules = [r for r in self._filter_rules if r.get("mode") == "show"]
        hide_rules = [r for r in self._filter_rules if r.get("mode") != "show"]

        if allow_rules and not self._rule_matches(allow_rules, frame_id, data):
            return True

        return bool(self._rule_matches(hide_rules, frame_id, data))

    def _rule_matches(self, rules: list[dict[str, Any]], frame_id: int, data: bytes) -> bool:
        for rule in rules:
            id_from = rule.get("id_from")
            id_to = rule.get("id_to")
            if id_from is not None and id_to is not None:
                if not (id_from <= frame_id <= id_to):
                    continue
            elif id_from is not None and frame_id != id_from:
                continue

            from_bytes = rule.get("data_from", b"")
            to_bytes = rule.get("data_to", b"")
            length = min(len(data), len(from_bytes), len(to_bytes))
            if length == 0 and (len(from_bytes) > 0 or len(to_bytes) > 0):
                continue
            match = True
            for i in range(length):
                if not (from_bytes[i] <= data[i] <= to_bytes[i]):
                    match = False
                    break
            if match:
                return True
        return False

    def _find_insert_row(self, frame_id: int) -> int:
        """Строки идут сверху вниз по возрастанию ID (отчёт мастера)."""
        for row in range(self._table.rowCount()):
            id_item = self._table.item(row, 0)
            if id_item is None:
                continue
            existing = hex_to_int(id_item.text())
            if existing is not None and existing > frame_id:
                return row
        return self._table.rowCount()

    def _paint_row_direction(self, row: int, is_tx: bool) -> None:
        """Заливка полей строки для кадров, отправленных самим МК
        (tx_echo: ответ триггера/другой программы МК): тёмная тема —
        оранжевый фон, светлая — чёрный. Столбец ID сохраняет свой
        цвет — он кодирует сам ID.
        Флаг пишется в ячейку DATA, чтобы _reset_data_background после
        вспышки подсветки вернул правильный цвет, а не дефолт."""
        bg, fg = _tx_echo_colors()
        for col in range(1, self._table.columnCount()):
            item = self._table.item(row, col)
            if item is None:
                continue
            if is_tx:
                item.setBackground(bg)
                item.setForeground(fg)
            else:
                # Обычная строка — фон как общий фон окна, в тёмной теме
                # шрифт явно белый (отчёт мастера: строки были почти
                # чёрными на тёмном фоне).
                item.setBackground(_row_base_bg())
                item.setForeground(_row_base_fg())
        data_item = self._table.item(row, 2)
        if data_item is not None:
            data_item.setData(Qt.ItemDataRole.UserRole, bool(is_tx))

    def _highlight_data_cell(self, row: int, changed_bytes: set[int]) -> None:
        if row in self._highlight_timers:
            self._highlight_timers[row].stop()
            del self._highlight_timers[row]
        data_item = self._table.item(row, 2)
        if data_item is None:
            return
        # Подсветка — жёлтый шрифт изменившихся байтов (делегат колонки
        # DATA), а не заливка фона; длительность фиксированные 500 мс.
        data_item.setData(_DATA_HL_ROLE, sorted(changed_bytes))
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda r=row: self._reset_data_background(r))
        timer.start(500)
        self._highlight_timers[row] = timer

    def _reset_data_background(self, row: int) -> None:
        data_item = self._table.item(row, 2)
        if data_item is not None:
            data_item.setData(_DATA_HL_ROLE, None)
        self._highlight_timers.pop(row, None)

    def _show_filter_dialog(self) -> None:
        """Переключено на глобальное управление фильтром в CanMonitorTab."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Двойной клик ПКМ по строке таблицы открывает историю ID."""
        from PySide6.QtCore import QEvent

        if (
            watched is self._table.viewport()
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.RightButton
        ):
            row = self._table.rowAt(event.position().toPoint().y())
            if row >= 0:
                self._show_id_history(row)
                return True
        # False, а не super().eventFilter(): проброс события назад в
        # watched->event() даёт взаимную рекурсию между фильтрами.
        return False

    def _show_context_menu(self, position) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        # Второй правый клик подряд по той же строке — это ТЗ-шный
        # «двойной ПКМ»: открываем историю ID вместо меню.
        now = time.monotonic()
        if row == self._last_rclick_row and now - self._last_rclick_time < 0.6:
            self._last_rclick_row = -1
            self._show_id_history(row)
            return
        self._last_rclick_time = now
        self._last_rclick_row = row
        menu = QMenu(self)
        menu.addAction(tr("Копировать ID"), lambda: self._copy_selected_id(row))
        menu.addAction(tr("Копировать данные"), lambda: self._copy_selected_data(row))
        menu.addAction(tr("Копировать всю строку"), lambda: self._copy_selected_row(row))
        menu.addAction(
            tr("Копировать пакет (для триггера)"), lambda: self._copy_row_as_packet(row)
        )
        menu.addAction(tr("Создать триггер"), lambda: self._create_trigger_from_row(row))
        menu.addAction(tr("Показать варианты данных"), lambda: self._show_data_variants(row))
        menu.addAction(tr("Битовая карта"), lambda: self._show_bitmap(row))
        menu.addAction(tr("История ID…"), lambda: self._show_id_history(row))
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _copy_selected_id(self, row: int) -> None:
        item = self._table.item(row, 0)
        if item is not None:
            QApplication.clipboard().setText(item.text())

    def _copy_selected_data(self, row: int) -> None:
        item = self._table.item(row, 2)
        if item is not None:
            QApplication.clipboard().setText(item.text())

    def _copy_selected_row(self, row: int) -> None:
        values = [
            self._table.item(row, col).text() if self._table.item(row, col) is not None else ""
            for col in range(self._table.columnCount())
        ]
        QApplication.clipboard().setText("  ".join(values))

    def _copy_row_as_packet(self, row: int) -> None:
        """Копирует строку в формате ID=.. DLC=.. DATA=.. для вставки в триггер."""
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        dlc_item = self._table.item(row, 1)
        try:
            dlc = int(dlc_item.text()) if dlc_item is not None else 8
        except ValueError:
            dlc = 8
        data_item = self._table.item(row, 2)
        data_text = data_item.text() if data_item is not None else ""
        if data_text.strip().lower() == "rtr":
            data_text = ""
        QApplication.clipboard().setText(f"ID=0x{can_id:X} DLC={dlc} DATA={data_text.strip()}")

    def _create_trigger_from_row(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        data_item = self._table.item(row, 2)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        data_values = data_item.text().split() if data_item is not None else []
        packet: dict[str, Any] = {
            "id": can_id,
            "data": bytes(parse_data_bytes(data_values)),
        }
        dlc_item = self._table.item(row, 1)
        try:
            packet["dlc"] = int(dlc_item.text()) if dlc_item is not None else len(packet["data"])
        except ValueError:
            packet["dlc"] = len(packet["data"])
        self.create_trigger_requested.emit(packet)

    def _show_data_variants(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        variants = self._id_data_variants.get(can_id, set())
        if not variants:
            show_toast(self, tr("Нет вариантов данных для этого ID"), success=False)
            return
        dialog = DataVariantsDialog(can_id, variants, self)
        dialog.exec()

    def _show_id_history(self, row: int) -> None:
        """Открывает диалог истории по ID из строки таблицы."""
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        samples = list(self._id_history.get(can_id, ()))
        send_cb = self._send_frame_once if self._serial_manager.is_open() else None
        dialog = IdHistoryDialog(can_id, self._channel, samples, self, send_callback=send_cb)
        self._history_dialogs.append(dialog)
        dialog.finished.connect(
            lambda *_a, d=dialog: self._history_dialogs.remove(d)
            if d in self._history_dialogs else None
        )
        dialog.show()

    def _show_bitmap(self, row: int) -> None:
        id_item = self._table.item(row, 0)
        data_item = self._table.item(row, 2)
        if id_item is None:
            return
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return
        data_values = data_item.text().split() if data_item is not None else []
        data = bytes(parse_data_bytes(data_values))
        dialog = BitmapDialog(can_id, data, self)
        dialog.exec()

    def set_dbc(self, dbc) -> None:
        """Уведомляет канал о смене DBC."""
        self._apply_search(self._search_edit.text())

    def set_filter(self, enabled: bool, rules: list[dict[str, Any]], ignored_ids: list[int], interval_ms: int) -> None:
        """Устанавливает правила фильтрации и интервал подсветки."""
        self._filter_enabled = enabled
        self._filter_rules = rules
        self._ignored_ids = set(ignored_ids)
        self._highlight_interval_ms = max(0, interval_ms)

    def get_known_ids(self) -> list[int]:
        """Возвращает список ID, которые уже были получены в канале."""
        return list(self._id_to_row.keys())

    def retranslate_ui(self) -> None:
        """Обновляет статические строки панели мониторинга канала."""
        self._clear_button.setText(tr("Очистить"))
        self._search_edit.setPlaceholderText(tr("Поиск по ID или данным…"))
        self._hide_sel_button.setText(tr("Скрыть выделенное"))
        self._hide_sel_button.setToolTip(
            tr("Скрыть строки выделенных ID, пока кнопка нажата")
        )
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("Счётчик"), tr("ASCII"), tr("Пояснение")]
        )
        self._send_button.setText(tr("Отправить"))
        self._cyclic_button.setToolTip(tr("Циклически"))
        self._stats_label.setText(tr("Принято: 0 | Скорость: 0 пак/с"))
        self._sent_label.setText(tr("Отправлено: 0"))
        self._row_color_label.setText(tr("Кол-во строк"))
        self._update_monitor_buttons()


class CanMonitorTab(QWidget):
    """Вкладка мониторинга CAN с двумя каналами."""

    create_trigger_requested = Signal(dict)
    # Прогресс прогрузки CAN-настроек в устройство (0-100 + текст этапа) —
    # окно настроек показывает процентный индикатор во время «Сохранить».
    progress_updated = Signal(int, str)

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._recording = False
        self._csv_file: TextIO | None = None
        self._csv_writer: csv.writer | None = None
        self._csv_path: Path | None = None
        self._dbc_manager = DBCManager()
        self._memory_indicator = MemoryIndicator(self)
        self._syncing_config = False
        self._create_widgets()
        self._layout_widgets()
        self._mark_save_signature_scope()

    def _mark_save_signature_scope(self) -> None:
        # Настройки устройства в мониторе — только эти виджеты: скорости,
        # терминаторы, параметры сна. Всё остальное (панель отправки,
        # поиск, фильтры, декодирование, диалоги) — рабочие инструменты,
        # их ввод не должен включать «Сохранить». Помечаем позитивно:
        # _widgets_signature исключает любой виджет внутри монитора,
        # у которого нет предка с save_setting — это покрывает и
        # динамически создаваемые виджеты (диалоги истории/фильтра),
        # которых на момент разметки ещё нет в дереве.
        for widget in (
            self._can1_speed_combo, self._can2_speed_combo,
            self._can1_terminator_check, self._can2_terminator_check,
            self._sleep_time_spin, self._sleep_mode_combo,
        ):
            widget.setProperty("save_setting", True)

    def _create_widgets(self) -> None:
        compact_font = QFont("Segoe UI", 9)

        self._filter_button = QPushButton(tr("Фильтр"))
        self._filter_button.setFixedSize(80, 28)
        self._filter_button.setFont(compact_font)
        self._filter_button.clicked.connect(self._show_filter_dialog)

        self._highlight_interval_spin = QSpinBox()
        self._highlight_interval_spin.setRange(0, 9999)
        self._highlight_interval_spin.setValue(500)
        self._highlight_interval_spin.setSuffix(tr(" мс"))
        self._highlight_interval_spin.setFont(compact_font)
        self._highlight_interval_spin.setToolTip(
            tr("Подсветка смены DATA: 0 — все изменения; N — только байты,\n"
               "изменившиеся позже чем через N мс после прошлого кадра этого ID.\n"
               "Чем больше N, тем реже подсветка. Длительность 500 мс.")
        )
        self._highlight_interval_spin.valueChanged.connect(self._on_highlight_interval_changed)

        self._can1_speed_label = QLabel(tr("Скорость CAN1"))
        self._can1_speed_label.setFont(compact_font)
        self._can1_speed_combo = QComboBox()
        self._can1_speed_combo.setFont(compact_font)
        self._can1_speed_combo.setEditable(True)
        self._can1_speed_combo.setFixedWidth(100)
        # Только бод-рейты, которые bxCAN реально умеет при APB1=36 МГц
        # (configure_bit_timing в прошивке): 33.3 и 800 кбит/с аппаратно
        # недостижимы на этом кварце — не показываем нерабочие варианты.
        for preset in ["10", "20", "50", "100", "125", "250", "500", "1000"]:
            self._can1_speed_combo.addItem(preset)
        self._can1_speed_combo.setMaxVisibleItems(12)
        self._can1_speed_combo.lineEdit().setValidator(QDoubleValidator(0.1, 10000.0, 1, self))
        self._can1_speed_combo.lineEdit().setPlaceholderText(tr("кбит/с"))

        self._can1_speed_button = QPushButton()
        self._can1_speed_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        self._fit_speed_button(self._can1_speed_button, self._can1_speed_combo)
        self._can1_speed_button.setToolTip(tr("Выбрать из списка"))
        self._can1_speed_button.clicked.connect(self._can1_speed_combo.showPopup)

        self._can1_terminator_check = QPushButton(tr("120 Ом"))
        self._can1_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can1_terminator_check.setFont(compact_font)
        self._can1_terminator_check.setCheckable(True)
        self._can1_terminator_check.setChecked(self._config.get("can1_terminator", False))
        self._can1_terminator_check.toggled.connect(self._on_can1_terminator_toggled)
        self._update_terminator_style(self._can1_terminator_check)

        self._can2_speed_label = QLabel(tr("Скорость CAN2"))
        self._can2_speed_label.setFont(compact_font)
        self._can2_speed_combo = QComboBox()
        self._can2_speed_combo.setFont(compact_font)
        self._can2_speed_combo.setEditable(True)
        self._can2_speed_combo.setFixedWidth(100)
        for preset in ["10", "20", "50", "100", "125", "250", "500", "1000"]:
            self._can2_speed_combo.addItem(preset)
        self._can2_speed_combo.setMaxVisibleItems(12)
        self._can2_speed_combo.lineEdit().setValidator(QDoubleValidator(0.1, 10000.0, 1, self))
        self._can2_speed_combo.lineEdit().setPlaceholderText(tr("кбит/с"))

        self._can2_speed_button = QPushButton()
        self._can2_speed_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        self._fit_speed_button(self._can2_speed_button, self._can2_speed_combo)
        self._can2_speed_button.setToolTip(tr("Выбрать из списка"))
        self._can2_speed_button.clicked.connect(self._can2_speed_combo.showPopup)

        self._can2_terminator_check = QPushButton(tr("120 Ом"))
        self._can2_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can2_terminator_check.setFont(compact_font)
        self._can2_terminator_check.setCheckable(True)
        self._can2_terminator_check.setChecked(self._config.get("can2_terminator", False))
        self._can2_terminator_check.toggled.connect(self._on_can2_terminator_toggled)
        self._update_terminator_style(self._can2_terminator_check)

        self._sleep_mode_label = QLabel(tr("Переход в режим сна"))
        self._sleep_mode_label.setFont(compact_font)
        self._sleep_time_spin = QSpinBox()
        self._sleep_time_spin.setRange(0, 9999)
        self._sleep_time_spin.setSuffix(tr(" с"))
        self._sleep_time_spin.setFont(compact_font)
        self._sleep_time_spin.setFixedWidth(80)
        self._sleep_time_spin.setValue(self._config.get("sleep_time", 0))
        self._sleep_time_spin.valueChanged.connect(self._on_sleep_time_changed)

        self._sleep_mode_combo = QComboBox()
        self._sleep_mode_combo.setFont(compact_font)
        self._sleep_mode_combo.setFixedWidth(140)
        self._sleep_mode_combo.addItems([
            tr("Не переходить в сон"),
            tr("Слип мод"),
            tr("Стоп мод"),
        ])
        self._sleep_mode_combo.setCurrentIndex(self._config.get("sleep_mode", 0))
        self._sleep_mode_combo.currentIndexChanged.connect(self._on_sleep_mode_changed)
        self._update_sleep_time_state(self._sleep_mode_combo.currentIndex())

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._monitor1 = CanChannelMonitor(1, self._serial_manager, self)
        self._monitor2 = CanChannelMonitor(2, self._serial_manager, self)
        self._monitor1.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor2.create_trigger_requested.connect(self.create_trigger_requested)
        self._monitor1.monitoring_state_changed.connect(self._on_monitor_state_changed)
        self._monitor2.monitoring_state_changed.connect(self._on_monitor_state_changed)
        self._splitter.addWidget(self._monitor1)
        self._splitter.addWidget(self._monitor2)
        self._splitter.setSizes([450, 450])
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)

        self._can1_speed_combo.setCurrentText(self._format_speed(self._config.get("can1_speed", 500000)))
        self._can1_speed_combo.currentIndexChanged.connect(self._on_can1_speed_changed)
        self._can1_speed_combo.lineEdit().editingFinished.connect(self._on_can1_speed_changed)

        self._can2_speed_combo.setCurrentText(self._format_speed(self._config.get("can2_speed", 500000)))
        self._can2_speed_combo.currentIndexChanged.connect(self._on_can2_speed_changed)
        self._can2_speed_combo.lineEdit().editingFinished.connect(self._on_can2_speed_changed)

    def _layout_widgets(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self._filter_button)
        buttons_layout.addWidget(QLabel(tr("Интервал подсветки")))
        buttons_layout.addWidget(self._highlight_interval_spin)
        buttons_layout.addWidget(self._can1_speed_label)
        buttons_layout.addWidget(self._can1_speed_combo)
        buttons_layout.addWidget(self._can1_speed_button)
        buttons_layout.addWidget(self._can1_terminator_check)
        buttons_layout.addWidget(self._can2_speed_label)
        buttons_layout.addWidget(self._can2_speed_combo)
        buttons_layout.addWidget(self._can2_speed_button)
        buttons_layout.addWidget(self._can2_terminator_check)
        buttons_layout.addSpacing(16)
        buttons_layout.addWidget(self._sleep_mode_label)
        buttons_layout.addWidget(self._sleep_time_spin)
        buttons_layout.addWidget(self._sleep_mode_combo)
        buttons_layout.addStretch()
        layout.addLayout(buttons_layout)
        layout.addWidget(self._splitter)
        layout.addWidget(self._memory_indicator)

    def _show_filter_dialog(self) -> None:
        """Открывает диалог фильтра и применяет настройки к обоим каналам."""
        first = self._monitor1._filter_rules
        enabled = self._monitor1._filter_enabled
        ignored = list(self._monitor1._ignored_ids)
        dialog = FilterDialog(first, enabled, ignored, self._get_known_ids, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_result()
        if result is None:
            return
        interval = self._highlight_interval_spin.value()
        self._monitor1.set_filter(result["enabled"], result["rules"], result["ignored_ids"], interval)
        self._monitor2.set_filter(result["enabled"], result["rules"], result["ignored_ids"], interval)
        # Красный — визуальный маркер «фильтрация активна»: правила с
        # содержимым или список игнорируемых ID реально режут поток.
        # Пустая автодобавленная строка правила (все поля пусты) не
        # считается — иначе кнопка горела бы при просто включённой галке.
        real_rules = [
            r for r in result["rules"]
            if r.get("id_from") is not None or r.get("id_to") is not None
            or r.get("data_from") or r.get("data_to")
        ]
        active = bool(result["enabled"]) and bool(
            real_rules or result["ignored_ids"]
        )
        self._filter_button.setStyleSheet(
            "QPushButton { background-color: #C62828; color: #FFFFFF; border: none; }"
            if active else ""
        )
        self._refresh_memory_indicator()

    def _refresh_memory_indicator(self) -> None:
        """«Память» показывает долю Flash-страницы триггеров — как во
        вкладке «Триггеры», чтобы в мониторинге не было ложного 0%."""
        from core.trigger_protocol import count_configured_triggers

        self._memory_indicator.show_trigger_usage(
            count_configured_triggers(self._config.get("triggers", []))
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_memory_indicator()

    def toggle_monitoring(self) -> None:
        """F5: старт/стоп мониторинга обоих каналов одним действием."""
        if self._monitor1._running or self._monitor2._running:
            self._monitor1._stop()
            self._monitor2._stop()
        else:
            self._monitor1._start()
            self._monitor2._start()

    def _get_known_ids(self) -> list[int]:
        return list(set(self._monitor1.get_known_ids() + self._monitor2.get_known_ids()))

    def _on_highlight_interval_changed(self, value: int) -> None:
        self._monitor1._highlight_interval_ms = value
        self._monitor2._highlight_interval_ms = value

    def _format_speed(self, speed_bps: int) -> str:
        """Форматирует скорость в кбит/с для отображения в комбобоксе."""
        if speed_bps == 33300:
            return "33.3"
        if speed_bps and speed_bps % 1000 == 0:
            return str(speed_bps // 1000)
        if speed_bps:
            return f"{speed_bps / 1000:.1f}"
        return "500"

    def _speed_combo_kbps(self, combo: QComboBox) -> int:
        """Текущее значение комбобокса скорости в кбит/с (дефолт 500)."""
        try:
            return int(round(float(combo.currentText().strip() or "500")))
        except ValueError:
            return 500

    def _on_can1_speed_changed(self) -> None:
        # Только выбор значения — в устройство скорость уходит по общей
        # кнопке «Сохранить» (apply_can_settings_to_device), как и все
        # остальные настройки окна.
        kbps = self._speed_combo_kbps(self._can1_speed_combo)
        self._config.set("can1_speed", kbps * 1000)

    def _on_can2_speed_changed(self) -> None:
        kbps = self._speed_combo_kbps(self._can2_speed_combo)
        self._config.set("can2_speed", kbps * 1000)

    def apply_can_settings_to_device(self) -> None:
        """Прогружает скорости и режимы обоих каналов в устройство.

        Вызывается общей кнопкой «Сохранить» — так же, как триггеры,
        CAN-настройки становятся частью записанной конфигурации. Без связи
        с устройством просто ничего не делает.

        После записи каналы перечитываются через статистику (baud_kbps):
        команда могла быть принята, но периферия применила другое
        значение — тогда «Сохранить» не должно отчитываться успехом."""
        if not self._serial_manager.is_open():
            return
        # Одна сессия на всю пачку: скорости и режимы обоих каналов
        # плюс readback-проверка обоих.
        steps_done = 0
        total_steps = 6  # speed + mode + readback на каждый из двух каналов
        self.progress_updated.emit(0, tr("Применение CAN-настроек"))
        mismatches: list[str] = []
        with self._serial_manager.control_session():
            for channel in (1, 2):
                kbps = self._speed_combo_kbps(
                    self._can1_speed_combo if channel == 1 else self._can2_speed_combo
                )
                if kbps in SerialManager.SUPPORTED_CAN_BAUD_KBPS:
                    try:
                        self._serial_manager.set_can_speed(channel, kbps)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Сохранение: скорость CAN%d=%d не применена: %s", channel, kbps, exc)
                steps_done += 1
                self.progress_updated.emit(
                    int(steps_done * 100 / total_steps),
                    tr("CAN{0}: скорость {1} кбит/с").format(channel, kbps),
                )
                term = bool(self._config.get(f"can{channel}_terminator", False))
                try:
                    self._serial_manager.set_can_mode(channel, 0, term)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Сохранение: режим CAN%d не применён: %s", channel, exc)
                steps_done += 1
                self.progress_updated.emit(
                    int(steps_done * 100 / total_steps),
                    tr("CAN{0}: режим и терминатор").format(channel),
                )
                # Readback: фактический бод-рейт периферии из статистики —
                # независимое подтверждение, а не эхо собственной команды.
                try:
                    got = int(
                        self._serial_manager.read_can_stats(channel).get(
                            "baud_kbps", 0
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    got = 0
                    logger.warning(
                        "Сохранение: readback CAN%d не удался: %s", channel, exc
                    )
                actual = str(got) if got else tr("нет ответа")
                if got != kbps:
                    mismatches.append(
                        tr("CAN{0}: записано {1} кбит/с, устройство сообщает {2}").format(
                            channel, kbps, actual
                        )
                    )
                steps_done += 1
                self.progress_updated.emit(
                    int(steps_done * 100 / total_steps),
                    tr("CAN{0}: проверка ({1} кбит/с)").format(channel, actual),
                )
        if mismatches:
            QMessageBox.warning(
                self,
                tr("Проверка CAN-настроек"),
                tr("Устройство применило не то, что записано:\n\n• {0}").format(
                    "\n• ".join(mismatches)
                ),
            )
            raise CanSettingsReadbackMismatch("; ".join(mismatches))

    def sync_from_config(self) -> None:
        """Переносит значения конфига в виджеты после загрузки файла.

        Файл меняет только Config — без этого комбобоксы показывали бы
        устаревшие значения, а снимок «Сохранить» не видел разницы.
        Программное заполнение в МК не пишет — запись идёт по «Сохранить».
        """
        self._syncing_config = True
        try:
            self._can1_speed_combo.setCurrentText(
                self._format_speed(self._config.get("can1_speed", 500000)))
            self._can2_speed_combo.setCurrentText(
                self._format_speed(self._config.get("can2_speed", 500000)))
            self._can1_terminator_check.setChecked(
                bool(self._config.get("can1_terminator", False)))
            self._can2_terminator_check.setChecked(
                bool(self._config.get("can2_terminator", False)))
            self._sleep_time_spin.setValue(int(self._config.get("sleep_time", 0) or 0))
            self._sleep_mode_combo.setCurrentIndex(int(self._config.get("sleep_mode", 0) or 0))
        finally:
            self._syncing_config = False

    @staticmethod
    def _fit_speed_button(button: QPushButton, combo: QComboBox) -> None:
        """Кнопка списка скоростей со значком «стрелка вниз»: чуть ниже
        поля ввода, ширина с запасом под иконку."""
        height = combo.sizeHint().height()
        if height <= 0:
            height = 26
        height = max(18, height - 6)
        button.setFixedSize(height + 8, height)

    @staticmethod
    def _update_terminator_style(button: QPushButton) -> None:
        """Цветовая индикация включённого терминатора 120 Ом — зелёная кнопка."""
        if button.isChecked():
            button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: #FFFFFF; border: none; "
                "border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #45A049; }"
            )
        else:
            button.setStyleSheet(
                "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; "
                "border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #4A4A6A; }"
            )

    def _on_can1_terminator_toggled(self, checked: bool) -> None:
        self._config.set("can1_terminator", checked)
        self._update_terminator_style(self._can1_terminator_check)
        # Программное setChecked при загрузке файла в МК не пишет —
        # запись идёт по общей кнопке «Сохранить».
        if not self._syncing_config:
            self._apply_can_mode(1)

    def _on_can2_terminator_toggled(self, checked: bool) -> None:
        self._config.set("can2_terminator", checked)
        self._update_terminator_style(self._can2_terminator_check)
        if not self._syncing_config:
            self._apply_can_mode(2)

    def _on_monitor_state_changed(self, channel: int, running: bool) -> None:
        """При запуске мониторинга применяет текущий режим и терминатор."""
        if running:
            self._apply_can_mode(channel)

    def _apply_can_mode(self, channel: int) -> None:
        """Отправляет в МК режим Normal и состояние терминатора канала.

        Бод-рейт здесь НЕ применяем: выбранная в комбобоксе скорость
        уходит в устройство только по общей кнопке «Сохранить»
        (apply_can_settings_to_device) — иначе перебор значений в
        списке дёргал бы шину на каждый клик."""
        if not self._serial_manager.is_open():
            return
        term = bool(self._config.get(f"can{channel}_terminator", False))
        try:
            self._serial_manager.set_can_mode(channel, 0, term)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось установить режим CAN%d: %s", channel, exc)

    def _on_sleep_time_changed(self, value: int) -> None:
        self._config.set("sleep_time", value)

    def _on_sleep_mode_changed(self, index: int) -> None:
        self._config.set("sleep_mode", index)
        self._update_sleep_time_state(index)

    def _update_sleep_time_state(self, index: int) -> None:
        """Активирует/деактивирует поле времени сна в зависимости от режима."""
        enabled = index != 0
        self._sleep_time_spin.setEnabled(enabled)
        effect = self._sleep_time_spin.graphicsEffect()
        if enabled:
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(1.0)
            self._sleep_time_spin.setStyleSheet("")
        else:
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(self._sleep_time_spin)
                self._sleep_time_spin.setGraphicsEffect(effect)
            effect.setOpacity(0.5)
            self._sleep_time_spin.setStyleSheet("QSpinBox { color: #888888; }")

    def process_frame(self, frame: dict[str, object]) -> None:
        channel = int(frame["channel"])
        if channel == 1:
            self._monitor1.queue_frame(frame)
        elif channel == 2:
            self._monitor2.queue_frame(frame)
        self._write_frame_to_csv(frame)

    def process_frames(self, frames: list) -> None:
        """Батч за одно чтение порта — горячий путь. CSV пишется по
        каждому кадру, в таблицы уходит через очереди слияния."""
        for frame in frames:
            self.process_frame(frame)

    def set_dbc(self, dbc) -> None:
        """Уведомляет вкладку о смене DBC."""
        for monitor in (self._monitor1, self._monitor2):
            monitor.set_dbc(dbc)

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки мониторинга."""
        self._filter_button.setText(tr("Фильтр"))
        self._can1_speed_label.setText(tr("Скорость CAN1"))
        self._can1_terminator_check.setText(tr("120 Ом"))
        self._can1_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._can2_speed_label.setText(tr("Скорость CAN2"))
        self._can2_terminator_check.setText(tr("120 Ом"))
        self._can2_terminator_check.setToolTip(tr("Включить терминатный резистор 120 Ом"))
        self._sleep_mode_label.setText(tr("Переход в режим сна"))
        self._sleep_time_spin.setSuffix(tr(" с"))
        index = self._sleep_mode_combo.currentIndex()
        self._sleep_mode_combo.clear()
        self._sleep_mode_combo.addItems([
            tr("Не переходить в сон"),
            tr("Слип мод"),
            tr("Стоп мод"),
        ])
        self._sleep_mode_combo.setCurrentIndex(index)
        self._monitor1.retranslate_ui()
        self._monitor2.retranslate_ui()

    def _start_recording(self, path: str) -> None:
        try:
            self._csv_path = Path(path)
            # SIM115 осознанно: файл живёт в self._csv_file до
            # _stop_recording — context manager закрыл бы его на выходе.
            self._csv_file = self._csv_path.open("w", newline="", encoding="utf-8")  # noqa: SIM115
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["timestamp", "channel", "dir", "id", "dlc", "data"])
            self._recording = True
            logger.info("Потоковая запись CAN начата: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка открытия CSV: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть файл: {0}").format(exc))

    def _stop_recording(self) -> None:
        self._recording = False
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка закрытия CSV: %s", exc)
            finally:
                self._csv_file = None
                self._csv_writer = None

    def _write_frame_to_csv(self, frame: dict[str, object]) -> None:
        if not self._recording or self._csv_writer is None:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        frame_id = int(frame["id"])
        data = bytes(frame["data"])
        # dir: RX = кадр принят с шины (внешний приём), TX = кадр отправлен
        # самим МК (tx_echo — ответ триггера/программы МК или ретрансляция
        # кадра ПК). В трейсе видно, что устройство передало само.
        direction = "TX" if frame.get("tx_echo") else "RX"
        self._csv_writer.writerow(
            [timestamp, frame["channel"], direction, int_to_hex(frame_id, 8), len(data), bytes_to_hex_string(data)]
        )
