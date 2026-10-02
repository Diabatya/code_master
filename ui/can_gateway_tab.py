"""Вкладка «CAN-шлюз» — ретрансляция, игнорирование и подмена кадров.

Окно разделено на две половины: левая — CAN1, правая — CAN2
(отчёт мастера). Каждая программа шлюза — карточка со спецификацией
фрейма в каждой половине и тремя кнопками-стрелками между ними:

* ← — направление CAN2 → CAN1;
* → — направление CAN1 → CAN2;
* ↔ — обе стороны.

Неактивная стрелка — белый контур, активная — красная.

Программы двух видов:

* «Игнорирование» — фрейм, записанный в половине CAN1, с активной
  стрелкой → (или ↔) не проходит из CAN1 в CAN2; фрейм в половине
  CAN2 со стрелкой ← (или ↔) не проходит из CAN2 в CAN1. Все
  остальные пакеты проходят в обычном режиме;
* «Подмена» — приходящий кадр, подходящий под спецификацию исходной
  стороны, не пропускается; вместо него в противоположный канал
  отправляется записанный в другой половине пакет. При → приём —
  спецификация CAN1, подмена — пакет CAN2; при ← наоборот; при ↔
  работает в обе стороны.

Правила записи пакетов — как в триггерах: шестнадцатеричный ID и
побайтовая DATA, «X» — любой байт (wildcard), пустое поле не
участвует в сравнении.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int
from ui.hex_edit import create_data_field_widget
from ui.packet_clipboard import create_clipboard_buttons
from ui.ui_utils import setup_button
from ui.variables_tab import _HexIdEdit
from ui.memory_indicator import MemoryIndicator

logger = get_logger(__name__)

# Направления (кнопки-стрелки между половинами карточки).
_DIR_LEFT = 0   # ← : CAN2 -> CAN1
_DIR_RIGHT = 1  # → : CAN1 -> CAN2
_DIR_BOTH = 2   # ↔ : обе стороны

_MODE_IGNORE = "ignore"
_MODE_SUBSTITUTE = "substitute"

# Стрелки направления — голубые (отчёт мастера): неактивная —
# голубой контур, активная — голубая заливка.
_ARROW_STYLE = (
    "QPushButton {"
    " border: 2px solid #3A7BD5; color: #3A7BD5;"
    " background: transparent; border-radius: 6px;"
    " font-size: 18px; font-weight: bold; padding: 2px 10px;"
    "}"
    "QPushButton:checked {"
    " border-color: #3A7BD5; color: #FFFFFF;"
    " background: #3A7BD5;"
    "}"
    "QPushButton:hover:!checked { background: rgba(58,123,213,40); }"
)

# Голубой крестик закрытия — как на вкладке «Гибкая логика»
# (отчёт мастера).
_CLOSE_STYLE = (
    "QPushButton { color: #7C9EFF; border: none; font-weight: bold;"
    " font-size: 14px; padding: 0 2px; }"
    "QPushButton:hover { color: #FFFFFF; }"
)

# Голубая округлая рамка вокруг программы (дизайн как в ГЛ).
_CARD_STYLE = (
    "QGroupBox { border: 2px solid #3A7BD5; border-radius: 8px;"
    " margin-top: 10px; padding-top: 8px; }"
)


def _close_button(font: QFont, tooltip: str) -> QPushButton:
    """Голубой крестик «✕» — как в ГЛ (отчёт мастера)."""
    button = QPushButton("✕")
    button.setFont(font)
    button.setStyleSheet(_CLOSE_STYLE)
    button.setFixedSize(22, 22)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tooltip)
    return button


def _set_data_enabled(edits: list[QLineEdit], count: int) -> None:
    """DLC ограничивает поля DATA: за пределами DLC поля пустые
    и неактивные — как в триггерах (отчёт мастера)."""
    for i, edit in enumerate(edits):
        if i >= count:
            edit.setText("")
            edit.setEnabled(False)
        else:
            edit.setEnabled(True)


def _spec_match(
    spec: dict[str, Any],
    frame_id: int,
    data: bytes,
    byte_from: int = 1,
    byte_to: int = 8,
) -> bool:
    """Фрейм подходит под спецификацию: ID равен, все заполненные
    байты DATA в диапазоне «от»–«до» равны («X» и пустое поле —
    wildcard). byte_from/byte_to — позиции байтов с 1 (отчёт мастера:
    у «Игнорирования» тоже есть DATA от/до)."""
    spec_id = hex_to_int(str(spec.get("id", "")))
    if spec_id is None or spec_id != frame_id:
        return False
    tokens = str(spec.get("data", "")).split()
    for i, token in enumerate(tokens[:8]):
        token = token.strip().upper()
        if not token or token == "X":
            continue
        if not (byte_from <= i + 1 <= byte_to):
            continue  # байт вне диапазона от/до не участвует в сравнении
        value = hex_to_int(token)
        if value is None or i >= len(data) or data[i] != value:
            return False
    return True


def _spec_filled(spec: dict[str, Any]) -> bool:
    """В спецификации записан хотя бы ID — иначе она не работает."""
    return hex_to_int(str(spec.get("id", ""))) is not None


class _FrameSpec(QWidget):
    """Спецификация CAN-фрейма: ID + DLC + 8 байт DATA с wildcard
    «X». DLC ограничивает число доступных байтовых полей — как в
    триггерах (отчёт мастера: DLC везде, где ручной ввод DATA)."""

    def __init__(
        self, font: QFont, mark_dirty=None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        id_row = QHBoxLayout()
        id_row.addWidget(QLabel("ID"))
        self.can_id = _HexIdEdit(font)
        id_row.addWidget(self.can_id)
        id_row.addWidget(QLabel("DLC"))
        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(1, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(54)
        id_row.addWidget(self.dlc)
        id_row.addStretch()
        layout.addLayout(id_row)

        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=34, allow_x=True,
        )
        layout.addWidget(data_widget)

        clip = create_clipboard_buttons(self, self.can_id, None, self.data)
        layout.addWidget(clip)

        self.dlc.valueChanged.connect(
            lambda v: _set_data_enabled(self.data, v)
        )
        if mark_dirty is not None:
            self.can_id.textChanged.connect(mark_dirty)
            self.dlc.valueChanged.connect(mark_dirty)
            for edit in self.data:
                edit.textChanged.connect(mark_dirty)
        _set_data_enabled(self.data, self.dlc.value())

    def read(self) -> dict[str, Any]:
        return {
            "id": self.can_id.text().strip(),
            "dlc": self.dlc.value(),
            "data": " ".join(e.text().strip().upper() for e in self.data),
        }

    def write(self, spec: dict[str, Any]) -> None:
        self.can_id.setText(str(spec.get("id", "")))
        self.dlc.setValue(int(spec.get("dlc", 8) or 8))
        tokens = str(spec.get("data", "")).split()
        for i, edit in enumerate(self.data):
            edit.setText(tokens[i] if i < len(tokens) else "")
        _set_data_enabled(self.data, self.dlc.value())


class _CurveEditor(QWidget):
    """Интерактивный график подмены DATA: входной байт (ось X,
    0x00–0xFF) → выходной байт (ось Y). Клик по полю — новая точка,
    перетаскивание точки — правка, двойной клик по точке — удаление.
    Ломаная между точками — интерполяция: так оператор наклоняет
    прямую, сдвигает её вверх/вниз и строит кривую (отчёт мастера).
    Линии подписаны именами каналов («Приём CAN 1»/«Подмена CAN 2»),
    правки курсором синхронно отражаются в «Таблице привязки»
    (сигнал on_changed)."""

    _RADIUS = 5
    _MARGIN = 14

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[int, int]] = []
        self._drag_index: int | None = None
        self._rx_name = ""
        self._tx_name = ""
        # Колбэк после правки точек курсором — таблица привязки
        # перечитывает график (отчёт мастера).
        self.on_changed = None
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_line_names(self, rx_name: str, tx_name: str) -> None:
        """Подписи линий по каналам: «Приём CAN 1» (диагональ
        входящих данных) и «Подмена CAN 2» (кривая)."""
        self._rx_name = rx_name
        self._tx_name = tx_name
        self.update()

    def _emit_changed(self) -> None:
        if self.on_changed is not None:
            self.on_changed()

    def points(self) -> list[list[int]]:
        return [[x, y] for x, y in sorted(self._points)]

    def set_points(self, points: Any) -> None:
        self._points = []
        for p in points or []:
            try:
                x, y = int(p[0]), int(p[1])
            except (TypeError, ValueError, IndexError):
                continue
            self._points.append((min(max(x, 0), 255), min(max(y, 0), 255)))
        self.update()

    def _rect(self):
        return self.rect().adjusted(
            self._MARGIN, 8, -self._MARGIN, -self._MARGIN
        )

    def _to_screen(self, x: int, y: int) -> tuple[float, float]:
        r = self._rect()
        return (
            r.left() + x / 255.0 * r.width(),
            r.bottom() - y / 255.0 * r.height(),
        )

    def _from_screen(self, px: float, py: float) -> tuple[int, int]:
        r = self._rect()
        x = round((px - r.left()) / max(1, r.width()) * 255)
        y = round((r.bottom() - py) / max(1, r.height()) * 255)
        return min(max(x, 0), 255), min(max(y, 0), 255)

    def _point_at(self, px: float, py: float) -> int | None:
        for i, (x, y) in enumerate(self._points):
            sx, sy = self._to_screen(x, y)
            if (sx - px) ** 2 + (sy - py) ** 2 <= (self._RADIUS * 2) ** 2:
                return i
        return None

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._rect()
        painter.fillRect(r, QColor(38, 38, 48))
        painter.setPen(QPen(QColor(90, 90, 110), 1))
        painter.drawRect(r)
        # Сетка: по 4 линии на ось (0x00/0x40/0x80/0xC0/0xFF).
        grid_pen = QPen(QColor(70, 70, 86), 1)
        text_pen = QPen(QColor(170, 170, 185), 1)
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        for step in range(5):
            v = step * 64 if step < 4 else 255
            sx, sy = self._to_screen(v, v)
            painter.setPen(grid_pen)
            painter.drawLine(int(sx), r.top(), int(sx), r.bottom())
            painter.drawLine(r.left(), int(sy), r.right(), int(sy))
            painter.setPen(text_pen)
            painter.drawText(
                int(sx) - 20, r.bottom() + 2, 40, 12,
                Qt.AlignmentFlag.AlignHCenter, f"0x{v:02X}",
            )
            painter.drawText(
                0, int(sy) - 6, r.left() - 4, 12,
                Qt.AlignmentFlag.AlignRight, f"0x{v:02X}",
            )
        # Диагональ «без подмены» — линия приёма на своём канале:
        # «Приём CAN n» (отчёт мастера — имена по каналам).
        rx_color = QColor(124, 158, 255)
        painter.setPen(QPen(rx_color, 1, Qt.PenStyle.DashLine))
        x0, y0 = self._to_screen(0, 0)
        x1, y1 = self._to_screen(255, 255)
        painter.drawLine(int(x0), int(y0), int(x1), int(y1))
        # Кривая подмены — «Подмена CAN m».
        tx_color = QColor(255, 170, 80)
        painter.setPen(QPen(tx_color, 2))
        prev = None
        for x, y in sorted(self._points):
            sx, sy = self._to_screen(x, y)
            if prev is not None:
                painter.drawLine(
                    int(prev[0]), int(prev[1]), int(sx), int(sy)
                )
            prev = (sx, sy)
        painter.setBrush(tx_color)
        for x, y in self._points:
            sx, sy = self._to_screen(x, y)
            painter.drawEllipse(
                int(sx) - self._RADIUS, int(sy) - self._RADIUS,
                self._RADIUS * 2, self._RADIUS * 2,
            )
        # Легенда с именами каналов (отчёт мастера).
        legend_font = painter.font()
        legend_font.setPointSize(8)
        legend_font.setBold(True)
        painter.setFont(legend_font)
        ly = r.top() + 4
        if self._rx_name:
            painter.setPen(rx_color)
            painter.drawText(
                r.left() + 6, ly, r.width() - 12, 14,
                Qt.AlignmentFlag.AlignLeft,
                f"— {self._rx_name}",
            )
            ly += 14
        if self._tx_name:
            painter.setPen(tx_color)
            painter.drawText(
                r.left() + 6, ly, r.width() - 12, 14,
                Qt.AlignmentFlag.AlignLeft,
                f"— {self._tx_name}",
            )
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._point_at(pos.x(), pos.y())
            if hit is not None:
                self._drag_index = hit
            else:
                x, y = self._from_screen(pos.x(), pos.y())
                self._points.append((x, y))
                self._points.sort()
                self._drag_index = self._points.index((x, y))
                self.update()
                self._emit_changed()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_index is not None:
            x, y = self._from_screen(event.position().x(), event.position().y())
            self._points[self._drag_index] = (x, y)
            self._points.sort()
            self._drag_index = self._points.index((x, y))
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_index is not None:
            self._drag_index = None
            self.update()
            # Отражение правок курсора в «Таблице привязки» —
            # после отпускания (отчёт мастера).
            self._emit_changed()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        hit = self._point_at(pos.x(), pos.y())
        if hit is not None and len(self._points) > 0:
            del self._points[hit]
            self._drag_index = None
            self.update()
            self._emit_changed()
        super().mouseDoubleClickEvent(event)


def _curve_map(curve: Any, value: int) -> int:
    """Значение байта по кривой подмены (кусочно-линейная, края
    продолжаются горизонтально). Кривая хранится списком точек
    0..255 → 0..255."""
    points: list[tuple[int, int]] = []
    for p in (curve or {}).get("points") or []:
        try:
            x, y = int(p[0]), int(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        points.append((min(max(x, 0), 255), min(max(y, 0), 255)))
    if not points:
        return value
    points.sort()
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        if a[0] <= value <= b[0]:
            if b[0] == a[0]:
                return b[1]
            return round(a[1] + (b[1] - a[1]) * (value - a[0]) / (b[0] - a[0]))
    return value


class _BindTable(QTableWidget):
    """«Таблица привязки» подмены — те же правила, что в
    «Переменных»: колонка «Приём» и «Подмена» принимают только HEX
    (0x00–0xFF); строки — точки кривой графика. Пустые строки
    допустимы и пропускаются (отчёт мастера)."""

    _MAX_ROWS = 32

    def __init__(
        self, font: QFont, on_changed, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._on_changed = on_changed
        self._updating = False
        self.setFont(font)
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels([tr("Приём"), tr("Подмена")])
        self.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.verticalHeader().setVisible(False)
        self.setFixedHeight(120)
        self.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Проверка записи: только HEX-байт 0x00–0xFF, пустая —
        пропускается (те же правила, что в «Переменных»)."""
        if self._updating:
            return
        text = item.text().strip().upper()
        if text and (hex_to_int(text) is None or len(text) > 2):
            # Невалидная запись — откатываем (правило таблицы
            # привязки из «Переменных», отчёт мастера).
            self._updating = True
            item.setText("")
            self._updating = False
            return
        self._updating = True
        item.setText(text)
        self._updating = False
        if self._on_changed is not None:
            self._on_changed()

    def points(self) -> list[list[int]]:
        """Точки кривой из заполненных пар «Приём/Подмена»."""
        points: list[list[int]] = []
        for row in range(self.rowCount()):
            rx_item = self.item(row, 0)
            tx_item = self.item(row, 1)
            rx_text = rx_item.text().strip() if rx_item else ""
            tx_text = tx_item.text().strip() if tx_item else ""
            rx = hex_to_int(rx_text)
            tx = hex_to_int(tx_text)
            if rx is None or tx is None:
                continue
            points.append([rx, tx])
        return points

    def set_points(self, points: list[list[int]]) -> None:
        """Переписывает таблицу по точкам графика — отражение
        правок курсором (отчёт мастера)."""
        self._updating = True
        self.setRowCount(0)
        for x, y in points:
            row = self.rowCount()
            if row >= self._MAX_ROWS:
                break
            self.insertRow(row)
            self.setItem(row, 0, QTableWidgetItem(f"{x:02X}"))
            self.setItem(row, 1, QTableWidgetItem(f"{y:02X}"))
        self._updating = False

    def add_row(self) -> None:
        if self.rowCount() >= self._MAX_ROWS:
            return
        row = self.rowCount()
        self.insertRow(row)
        self.setItem(row, 0, QTableWidgetItem(""))
        self.setItem(row, 1, QTableWidgetItem(""))

    def remove_selected_row(self) -> None:
        row = self.currentRow()
        if row >= 0:
            self.removeRow(row)
            if self._on_changed is not None:
                self._on_changed()


class _SubCurveBlock(QWidget):
    """Блок подмены для одного направления: «ОТ»/«ДО» — байты DATA,
    к которым применяется кривая; «Таблица привязки» — точки графика
    (HEX-пары приём→подмена); график правится курсором и отражается
    в таблице (отчёт мастера). Линии подписаны каналами:
    «Приём CAN a» / «Подмена CAN b»."""

    def __init__(
        self,
        font: QFont,
        rx_channel: int,
        tx_channel: int,
        mark_dirty,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        self._rx_channel = rx_channel
        self._tx_channel = tx_channel

        head = QHBoxLayout()
        self._title = QLabel()
        self._title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        head.addWidget(self._title)
        head.addStretch()
        head.addWidget(QLabel(tr("DATA ОТ:")))
        self.byte_from = QSpinBox()
        self.byte_from.setFont(font)
        self.byte_from.setRange(1, 8)
        self.byte_from.setValue(1)
        self.byte_from.setFixedWidth(54)
        head.addWidget(self.byte_from)
        head.addWidget(QLabel(tr("ДО:")))
        self.byte_to = QSpinBox()
        self.byte_to.setFont(font)
        self.byte_to.setRange(1, 8)
        self.byte_to.setValue(8)
        self.byte_to.setFixedWidth(54)
        head.addWidget(self.byte_to)
        layout.addLayout(head)

        table_row = QHBoxLayout()
        self.table = _BindTable(font, self._on_table_changed)
        table_row.addWidget(self.table, 1)
        table_btns = QVBoxLayout()
        add_btn = QPushButton("＋")
        add_btn.setFont(font)
        add_btn.setFixedSize(24, 24)
        add_btn.setToolTip(tr("Добавить точку привязки"))
        add_btn.clicked.connect(self.table.add_row)
        remove_btn = QPushButton("−")
        remove_btn.setFont(font)
        remove_btn.setFixedSize(24, 24)
        remove_btn.setToolTip(tr("Удалить выбранную точку"))
        remove_btn.clicked.connect(self.table.remove_selected_row)
        table_btns.addWidget(add_btn)
        table_btns.addWidget(remove_btn)
        table_btns.addStretch()
        table_row.addLayout(table_btns)
        layout.addLayout(table_row)

        self.curve = _CurveEditor()
        self.curve.on_changed = self._on_curve_changed
        layout.addWidget(self.curve)
        self._refresh_names()

        self.byte_from.valueChanged.connect(self._validate_range)
        self.byte_to.valueChanged.connect(self._validate_range)
        self.byte_from.valueChanged.connect(mark_dirty)
        self.byte_to.valueChanged.connect(mark_dirty)

    def _validate_range(self, *_args) -> None:
        """ОТ не больше ДО — поля автоматически выравниваются."""
        if self.byte_from.value() > self.byte_to.value():
            self.byte_to.setValue(self.byte_from.value())

    def _refresh_names(self) -> None:
        """Имена линий по каналам: «Приём CAN a» и «Подмена CAN b»
        (при обратном направлении — наоборот; отчёт мастера)."""
        self._title.setText(
            tr("CAN{0} → CAN{1}").format(self._rx_channel, self._tx_channel)
        )
        self.curve.set_line_names(
            tr("Приём CAN {0}").format(self._rx_channel),
            tr("Подмена CAN {0}").format(self._tx_channel),
        )

    def _on_table_changed(self) -> None:
        """Правка таблицы → перерисовка кривой."""
        self.curve.set_points(self.table.points())

    def _on_curve_changed(self) -> None:
        """Правка кривой курсором → отражение в таблице привязки."""
        self.table.set_points(self.curve.points())

    def read(self) -> dict[str, Any]:
        return {
            "from": self.byte_from.value(),
            "to": self.byte_to.value(),
            "points": self.curve.points(),
        }

    def write(self, data: dict[str, Any]) -> None:
        self.byte_from.setValue(int(data.get("from", 1) or 1))
        self.byte_to.setValue(int(data.get("to", 8) or 8))
        points = data.get("points") or []
        self.table.set_points(points)
        self.curve.set_points(points)


class _DirectionButtons(QWidget):
    """Три кнопки-стрелки направления: ← (CAN2→CAN1), → (CAN1→CAN2),
    ↔ (обе стороны). Стрелки голубые (отчёт мастера); хинты
    поясняют, какую передачу блокирует программа. Одновременно
    активна только одна стрелка."""

    def __init__(self, font: QFont, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(2, 0, 2, 0)
        self._buttons: dict[int, QPushButton] = {}
        self.on_changed = None
        # Хинты блокировки направления — формулировки отчёта мастера.
        for direction, symbol, hint in (
            (_DIR_LEFT, "←",
             tr("Блокируем передачу данных из CAN 2 в CAN 1")),
            (_DIR_RIGHT, "→",
             tr("Блокируем передачу данных из CAN 1 в CAN 2")),
            (_DIR_BOTH, "↔",
             tr("Блокируем передачу данных CAN 1 в CAN 2 "
                "в обе стороны")),
        ):
            button = QPushButton(symbol)
            button.setFont(font)
            button.setCheckable(True)
            button.setStyleSheet(_ARROW_STYLE)
            button.setToolTip(hint)
            button.clicked.connect(
                lambda checked, d=direction: self._select(d, checked)
            )
            self._buttons[direction] = button
            layout.addWidget(button)
        layout.addStretch()

    def _select(self, direction: int, checked: bool) -> None:
        for d, button in self._buttons.items():
            button.blockSignals(True)
            button.setChecked(checked and d == direction)
            button.blockSignals(False)
        if self.on_changed is not None:
            self.on_changed()

    def direction(self) -> int | None:
        for d, button in self._buttons.items():
            if button.isChecked():
                return d
        return None

    def set_direction(self, direction: int | None) -> None:
        for d, button in self._buttons.items():
            button.setChecked(d == direction)


class _GatewayProgram(QGroupBox):
    """Карточка программы шлюза: половина CAN1 | стрелки | половина
    CAN2. Для «Игнорирования» обе половины — блокируемые фреймы;
    для «Подмены» — пакет приёма на своей стороне и подмена в
    противоположный канал. Дизайн — как на вкладке ГЛ: имя
    программы по центру крупно, голубой крестик закрытия
    (отчёт мастера)."""

    def __init__(
        self,
        tab: CanGatewayTab,
        mode: str,
        font: QFont,
        rule: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self.mode = mode
        self.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        # Голубая округлая рамка вокруг программы — как в ГЛ.
        self.setStyleSheet(_CARD_STYLE)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # Шапка: № + вид программы слева («ПОДМЕНА»/«Игнорирование»),
        # имя программы по центру крупно в поле ввода как в триггерах
        # (отчёт мастера), активность, голубой крестик.
        header = QHBoxLayout()
        self._number_label = QLabel()
        self._number_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._number_label.setStyleSheet("color: #9A9AA5;")
        header.addWidget(self._number_label)
        self._mode_label = QLabel(self._default_name().upper())
        self._mode_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._mode_label.setStyleSheet("color: #9A9AA5;")
        header.addWidget(self._mode_label)
        header.addStretch()
        self._name_edit = QLineEdit()
        self._name_edit.setFont(QFont("Segoe UI", 16))
        self._name_edit.setFixedSize(340, 40)
        self._name_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_edit.setPlaceholderText(self._default_name())
        self._name_edit.setMaxLength(40)
        self._name_edit.textChanged.connect(tab.mark_dirty)
        header.addWidget(self._name_edit)
        header.addStretch()
        self._active = QCheckBox(tr("Активна"))
        self._active.setFont(font)
        self._active.toggled.connect(tab.mark_dirty)
        header.addWidget(self._active)
        self._remove = _close_button(font, tr("Удалить программу"))
        self._remove.clicked.connect(lambda: tab.remove_program(self))
        header.addWidget(self._remove)
        layout.addLayout(header)

        # Две половины: CAN1 слева, стрелки по центру, CAN2 справа.
        body = QHBoxLayout()
        body.setSpacing(8)

        left_group = QGroupBox("CAN1")
        left_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        left_layout = QVBoxLayout(left_group)
        self.spec_left = _FrameSpec(font, tab.mark_dirty)
        left_layout.addWidget(self.spec_left)
        left_layout.addStretch()
        body.addWidget(left_group, 1)

        self.direction = _DirectionButtons(font)
        self.direction.on_changed = self._on_direction_changed
        body.addWidget(self.direction, 0)

        right_group = QGroupBox("CAN2")
        right_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        right_layout = QVBoxLayout(right_group)
        self.spec_right = _FrameSpec(font, tab.mark_dirty)
        right_layout.addWidget(self.spec_right)
        right_layout.addStretch()
        body.addWidget(right_group, 1)

        layout.addLayout(body)

        hint = QLabel(
            tr("«X» — любой байт, пустое поле не участвует. "
               "Стрелка задаёт направление действия программы.")
        )
        hint.setFont(font)
        hint.setStyleSheet("color: #9A9AA5;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # «Игнорирование»: DATA ОТ/ДО — диапазон байтов, участвующих в
        # сравнении (как в «Подмене» — отчёт мастера).
        self._ignore_range = QWidget()
        range_row = QHBoxLayout(self._ignore_range)
        range_row.setSpacing(6)
        range_row.setContentsMargins(0, 0, 0, 0)
        range_row.addWidget(QLabel(tr("DATA от:")))
        self._ignore_from = QSpinBox()
        self._ignore_from.setFont(font)
        self._ignore_from.setRange(1, 8)
        self._ignore_from.setValue(1)
        self._ignore_from.setFixedWidth(54)
        self._ignore_from.setToolTip(
            tr("Первый байт DATA, участвующий в сравнении (с 1)")
        )
        range_row.addWidget(self._ignore_from)
        range_row.addWidget(QLabel(tr("до:")))
        self._ignore_to = QSpinBox()
        self._ignore_to.setFont(font)
        self._ignore_to.setRange(1, 8)
        self._ignore_to.setValue(8)
        self._ignore_to.setFixedWidth(54)
        self._ignore_to.setToolTip(
            tr("Последний байт DATA, участвующий в сравнении")
        )
        range_row.addWidget(self._ignore_to)
        range_row.addStretch()
        self._ignore_from.valueChanged.connect(self._validate_ignore_range)
        self._ignore_to.valueChanged.connect(self._validate_ignore_range)
        self._ignore_from.valueChanged.connect(tab.mark_dirty)
        self._ignore_to.valueChanged.connect(tab.mark_dirty)
        self._ignore_range.setVisible(self.mode == _MODE_IGNORE)
        layout.addWidget(self._ignore_range)

        # Подмена DATA по графику — только для программ «Подмена»:
        # по блоку на направление («ОТ»/«ДО» — байты DATA, к которым
        # применяется кривая; «Таблица привязки» — HEX-пары;
        # график правится курсором и отражается в таблице).
        # Линии подписаны каналами: «Приём CAN a»/«Подмена CAN b».
        self._curve_check = QCheckBox(tr("Подмена DATA по графику"))
        self._curve_check.setFont(font)
        self._curve_check.setToolTip(tr(
            "Клик по графику — новая точка, перетаскивание — правка, "
            "двойной клик по точке — удаление. Байты «X»/пустые в "
            "диапазоне ОТ–ДО половины подмены проходят через кривую."
        ))
        layout.addWidget(self._curve_check)
        self._curve_widget = QWidget()
        curve_layout = QVBoxLayout(self._curve_widget)
        curve_layout.setSpacing(6)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        # CAN1 → CAN2: приём слева, подмена справа.
        self._curve_12 = _SubCurveBlock(font, 1, 2, tab.mark_dirty)
        # CAN2 → CAN1: приём справа, подмена слева.
        self._curve_21 = _SubCurveBlock(font, 2, 1, tab.mark_dirty)
        curve_layout.addWidget(self._curve_12)
        curve_layout.addWidget(self._curve_21)
        self._curve_widget.setVisible(False)
        layout.addWidget(self._curve_widget)
        self._curve_check.toggled.connect(self._curve_widget.setVisible)
        self._curve_check.toggled.connect(lambda _c: tab.mark_dirty())
        self._curve_check.toggled.connect(
            lambda _c: self._refresh_curve_visibility()
        )
        if self.mode != _MODE_SUBSTITUTE:
            self._curve_check.setVisible(False)
            self._curve_widget.setVisible(False)

        self.refresh_title()
        self._refresh_curve_visibility()
        if rule is not None:
            self.write(rule)

    def _default_name(self) -> str:
        return (
            tr("Игнорирование")
            if self.mode == _MODE_IGNORE
            else tr("Подмена")
        )

    def _validate_ignore_range(self, *_args) -> None:
        """ОТ не больше ДО — поля автоматически выравниваются."""
        if self._ignore_from.value() > self._ignore_to.value():
            self._ignore_to.setValue(self._ignore_from.value())

    def _on_direction_changed(self) -> None:
        """Стрелка направления: показываются блоки кривых только
        выбранных направлений (↔ — оба)."""
        self._refresh_curve_visibility()
        self._tab.mark_dirty()

    def _refresh_curve_visibility(self) -> None:
        """Видимость блоков подмены по выбранной стрелке.
        Пока направление не выбрано (None — новая программа),
        показываем оба блока: иначе «Подмена по графику»
        выглядела пустой (отчёт мастера)."""
        if self.mode != _MODE_SUBSTITUTE:
            return
        direction = self.direction.direction()
        self._curve_12.setVisible(
            direction is None or direction in (_DIR_RIGHT, _DIR_BOTH)
        )
        self._curve_21.setVisible(
            direction is None or direction in (_DIR_LEFT, _DIR_BOTH)
        )

    def refresh_title(self) -> None:
        index = (
            self._tab._programs.index(self) + 1
            if self in self._tab._programs else 0
        )
        self._number_label.setText(f"№ {index}")
        self._name_edit.setPlaceholderText(self._default_name())

    def read(self) -> dict[str, Any]:
        rule: dict[str, Any] = {
            "mode": self.mode,
            "active": self._active.isChecked(),
            "title": self._name_edit.text().strip(),
            "direction": self.direction.direction(),
            "spec1": self.spec_left.read(),
            "spec2": self.spec_right.read(),
        }
        if self.mode == _MODE_IGNORE:
            # DATA от/до игнорирования — диапазон байтов сравнения.
            rule["data_from"] = self._ignore_from.value()
            rule["data_to"] = self._ignore_to.value()
        if self.mode == _MODE_SUBSTITUTE and self._curve_check.isChecked():
            rule["curves"] = {
                "12": self._curve_12.read(),
                "21": self._curve_21.read(),
            }
        return rule

    def write(self, rule: dict[str, Any]) -> None:
        self._active.setChecked(bool(rule.get("active", True)))
        self._name_edit.setText(str(rule.get("title", "")))
        direction = rule.get("direction")
        self.direction.set_direction(
            int(direction) if direction is not None else None
        )
        self.spec_left.write(rule.get("spec1") or {})
        self.spec_right.write(rule.get("spec2") or {})
        if self.mode == _MODE_IGNORE:
            self._ignore_from.setValue(int(rule.get("data_from", 1) or 1))
            self._ignore_to.setValue(int(rule.get("data_to", 8) or 8))
        if self.mode == _MODE_SUBSTITUTE:
            curves = rule.get("curves") or {}
            legacy = rule.get("curve") or {}
            # Старый формат: одна кривая на программу — применялась
            # в обе стороны; переносим в оба направления.
            c12 = curves.get("12") or {
                "from": 1, "to": 8,
                "points": legacy.get("points") or [(0, 0), (255, 255)],
            }
            c21 = curves.get("21") or {
                "from": 1, "to": 8,
                "points": legacy.get("points") or [(0, 0), (255, 255)],
            }
            self._curve_12.write(c12)
            self._curve_21.write(c21)
            self._curve_check.setChecked(
                bool(legacy.get("enabled")) or bool(curves)
            )
            self._refresh_curve_visibility()


class CanGatewayTab(QWidget):
    """Вкладка CAN-шлюза: программы «Игнорирование» и «Подмена»
    между CAN1 и CAN2 со стрелками направления."""

    def __init__(
        self,
        serial_manager: SerialManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        # Кнопок запуска/остановки больше нет: программы действуют
        # всегда, пока открыт порт (отчёт мастера).
        self._running = True
        self._programs: list[_GatewayProgram] = []
        self._internal_rules: list[dict[str, Any]] = []
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_config)
        self._memory_indicator = MemoryIndicator(self)
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки шлюза."""
        self._add_ignore_button.setText(tr("＋ Игнорирование"))
        self._add_substitute_button.setText(tr("＋ Подмена"))
        for program in self._programs:
            program.refresh_title()

    # ---- виджеты ------------------------------------------------------

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        self._add_ignore_button = QPushButton(tr("＋ Игнорирование"))
        setup_button(self._add_ignore_button, bold=True, height=34)
        self._add_ignore_button.clicked.connect(
            lambda: self.add_program(_MODE_IGNORE)
        )

        self._add_substitute_button = QPushButton(tr("＋ Подмена"))
        setup_button(self._add_substitute_button, bold=True, height=34)
        self._add_substitute_button.clicked.connect(
            lambda: self.add_program(_MODE_SUBSTITUTE)
        )

        self._programs_widget = QWidget()
        self._programs_layout = QVBoxLayout(self._programs_widget)
        self._programs_layout.setSpacing(12)
        self._programs_layout.setContentsMargins(0, 0, 0, 0)
        self._programs_layout.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._programs_widget)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        # Кнопки «Запустить/Остановить/Сохранить/Загрузить правила»
        # убраны (отчёт мастера): программы действуют всегда,
        # сохранение — общей кнопкой «Сохранить» окна настроек.
        self._font = font

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(tr("CAN-шлюз"))
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setProperty("title", True)
        layout.addWidget(title)

        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self._add_ignore_button)
        top.addWidget(self._add_substitute_button)
        top.addStretch()
        layout.addLayout(top)

        layout.addWidget(self._scroll, 1)
        layout.addWidget(self._memory_indicator)

    # ---- программы -----------------------------------------------------

    def add_program(
        self, mode: str, rule: dict[str, Any] | None = None
    ) -> _GatewayProgram:
        """Добавляет карточку программы («Игнорирование»/«Подмена»)."""
        program = _GatewayProgram(self, mode, self._font, rule)
        self._programs.append(program)
        self._programs_layout.insertWidget(
            self._programs_layout.count() - 1, program
        )
        for p in self._programs:
            p.refresh_title()
        self.mark_dirty()
        return program

    def remove_program(self, program: _GatewayProgram) -> None:
        if program in self._programs:
            self._programs.remove(program)
        program.setParent(None)
        program.deleteLater()
        for p in self._programs:
            p.refresh_title()
        self.mark_dirty()

    def mark_dirty(self, *_args) -> None:
        self._save_timer.start()

    # ---- конфигурация ---------------------------------------------------

    def _collect_rules(self) -> list[dict[str, Any]]:
        return [program.read() for program in self._programs]

    def _migrate_legacy(
        self, rules: list[dict[str, Any]], ignore_ids: list[Any] | None
    ) -> list[dict[str, Any]]:
        """Старый формат (recv_id/replace_id/direction 0..1 и список
        gateway_ignore) — в карточки «Подмена»/«Игнорирование»."""
        migrated: list[dict[str, Any]] = []
        for rule in rules:
            if "mode" in rule:
                migrated.append(rule)
                continue
            # Пустые неактивные строки старого формата — просто
            # пустые места под правила; в новый UI не переносим.
            if (
                not rule.get("active")
                and not str(rule.get("recv_id", "")).strip()
                and not str(rule.get("replace_id", "")).strip()
            ):
                continue
            migrated.append({
                "mode": _MODE_SUBSTITUTE,
                "active": rule.get("active", False),
                # Старый direction: 0 = CAN1→CAN2 (→), 1 = CAN2→CAN1 (←).
                "direction": _DIR_RIGHT
                if int(rule.get("direction", 0)) == 0
                else _DIR_LEFT,
                "spec1": {
                    "id": rule.get("recv_id", ""),
                    "data": rule.get("recv_data", ""),
                },
                "spec2": {
                    "id": rule.get("replace_id", ""),
                    "data": rule.get("replace_data", ""),
                },
            })
        for raw in ignore_ids or []:
            if hex_to_int(str(raw)) is None:
                continue
            migrated.append({
                "mode": _MODE_IGNORE,
                "active": True,
                "direction": _DIR_BOTH,
                "spec1": {"id": str(raw), "data": ""},
                "spec2": {"id": str(raw), "data": ""},
            })
        return migrated

    def _load_config(
        self,
        rules: list[dict[str, Any]] | None = None,
        ignore_ids: list[Any] | None = None,
    ) -> None:
        if rules is None:
            rules = self._config.get("gateway_rules", [])
        if not isinstance(rules, list):
            rules = []
        if ignore_ids is None:
            ignore_ids = self._config.get("gateway_ignore", [])
        if not isinstance(ignore_ids, list):
            ignore_ids = []
        rules = self._migrate_legacy(rules, ignore_ids)
        while self._programs:
            self.remove_program(self._programs[-1])
        for rule in rules:
            mode = rule.get("mode", _MODE_IGNORE)
            if mode not in (_MODE_IGNORE, _MODE_SUBSTITUTE):
                continue
            self.add_program(mode, rule)
        self._save_timer.stop()
        self._internal_rules = self._build_internal_rules()

    def set_config(
        self,
        rules: list[dict[str, Any]],
        ignore_ids: list[Any] | None = None,
    ) -> None:
        """Устанавливает правила шлюза из внешней конфигурации."""
        self._load_config(rules, ignore_ids)

    def _save_config(self) -> None:
        rules = self._collect_rules()
        self._config.set("gateway_rules", rules)
        # Старый ключ больше не используется — всё в программах.
        self._config.set("gateway_ignore", [])
        # Изменения применяются сразу — кнопки запуска нет.
        self._internal_rules = self._build_internal_rules()
        self._memory_indicator.update_usage(
            self._memory_indicator.estimate_rules(rules)
        )
        # Оценка загрузки ОЗУ/CPU по числу активных правил шлюза
        # (отчёт мастера: две доп. строки процента у индикатора).
        self._memory_indicator.update_load(
            len(self._internal_rules),
            len(self._internal_rules) * 64,
        )

    # ---- исполнение ------------------------------------------------------

    def _build_internal_rules(self) -> list[dict[str, Any]]:
        rules = []
        for program in self._programs:
            rule = program.read()
            if not rule["active"] or rule["direction"] is None:
                continue
            if not (_spec_filled(rule["spec1"]) or _spec_filled(rule["spec2"])):
                continue
            rules.append(rule)
        return rules

    def process_frame(self, frame: dict[str, Any]) -> None:
        if not self._running:
            return
        if frame.get("tx_echo"):
            # Эхо собственной передачи МК — не внешний кадр; иначе
            # кадр мог бы зациклиться между каналами.
            return
        frame_id = int(frame["id"])
        frame_channel = int(frame["channel"])
        data = bytes(frame["data"])

        for rule in self._internal_rules:
            direction = rule["direction"]
            # Идёт ли кадр в «своём» направлении программы:
            # → (или ↔) для CAN1→CAN2, ← (или ↔) для CAN2→CAN1.
            if frame_channel == 1:
                direction_ok = direction in (_DIR_RIGHT, _DIR_BOTH)
                match_spec, out_spec = rule["spec1"], rule["spec2"]
            else:
                direction_ok = direction in (_DIR_LEFT, _DIR_BOTH)
                match_spec, out_spec = rule["spec2"], rule["spec1"]
            if not direction_ok or not _spec_filled(match_spec):
                continue
            if rule["mode"] == _MODE_IGNORE:
                # DATA от/до ограничивают байты сравнения (отчёт мастера).
                byte_from = int(rule.get("data_from", 1) or 1)
                byte_to = int(rule.get("data_to", 8) or 8)
            else:
                byte_from, byte_to = 1, 8
            if not _spec_match(match_spec, frame_id, data, byte_from, byte_to):
                continue

            if rule["mode"] == _MODE_IGNORE:
                # Игнорирование: подходящий пакет не пропускается,
                # все остальные идут в обычном режиме.
                logger.debug(
                    "Шлюз: игнорирование ID=0x%X в CAN%d",
                    frame_id, frame_channel,
                )
                return

            # Подмена: входящий пакет не пропускается; вместо него
            # в другой канал уходит записанный пакет подмены
            # («X»/пустые байты подмены берутся из исходного кадра).
            out_id = hex_to_int(str(out_spec.get("id", "")))
            if out_id is None:
                out_id = frame_id
            payload = bytearray(data[:8].ljust(8, b"\x00"))
            tokens = str(out_spec.get("data", "")).split()
            # Кривая по направлению: «12» — приём CAN1 → подмена
            # CAN2, «21» — наоборот; старая общая «curve»
            # применяется в выбранную сторону (миграция).
            curves = rule.get("curves") or {}
            curve = curves.get("12" if frame_channel == 1 else "21") or {}
            if not curve:
                legacy = rule.get("curve") or {}
                if legacy.get("enabled"):
                    curve = {
                        "from": 1, "to": 8,
                        "points": legacy.get("points") or [],
                    }
            byte_from = int(curve.get("from", 1) or 1)
            byte_to = int(curve.get("to", 8) or 8)
            curve_on = bool(curve.get("points"))
            for i in range(8):
                token = tokens[i].strip().upper() if i < len(tokens) else ""
                value = hex_to_int(token) if token and token != "X" else None
                if value is not None:
                    payload[i] = value & 0xFF
                elif (
                    curve_on and i < len(data)
                    and byte_from <= i + 1 <= byte_to
                ):
                    # Графическая подмена: байт из диапазона ОТ–ДО
                    # проходит через кривую от входящего значения.
                    payload[i] = _curve_map(curve, data[i]) & 0xFF
            dlc = int(out_spec.get("dlc", 8) or 8)
            target = 2 if frame_channel == 1 else 1
            self._serial_manager.send_data(
                pack_can_frame(target, out_id, bytes(payload[:dlc]))
            )
            logger.debug(
                "Шлюз: подмена ID=0x%X -> 0x%X в CAN%d",
                frame_id, out_id, target,
            )
            return

        # Ретрансляция без изменений — все остальные пакеты
        # проходят в обычном режиме.
        target_channel = 2 if frame_channel == 1 else 1
        self._serial_manager.send_data(
            pack_can_frame(target_channel, frame_id, data)
        )
        logger.debug(
            "Шлюз: ретрансляция ID=0x%X из CAN%d в CAN%d",
            frame_id, frame_channel, target_channel,
        )
