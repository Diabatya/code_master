"""Вкладка «Переменные» — прототип.

Две колонки: «Чтение» (переменные, читаемые Гибкой логикой в условиях
«Если») и «Управление» (переменные для действий). Клик по строке
открывает настройку переменной одного из двух видов:

* «Фиксированные значения флагов» — набор пакетов (1..10), каждый
  устанавливает или сбрасывает свой бит ОЗУ функции; МК опрашивает бит
  при проверке условия «Если» в ГЛ (пример: пакет «дверь открыта» → 1,
  «дверь закрыта» → 0);
* «Графическая привязка к DATA» — ID/DLC/диапазон Data, выбор байтов и
  редактируемый график перевода сырого значения в физическую величину
  (обороты, наддув, температура и т.п.).

Сейчас это визуальный образ: конфигурации живут только в памяти
вкладки. Формат хранения и исполнение на МК будут заданы отдельно —
см. проект «Гибкая логика» (JSON: id/title/active/events/conditions/
actions).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.translations import _ as tr

# Пустых строк в каждой колонке — оператор кликает любую и настраивает.
_ROW_COUNT = 12
_TYPE_FLAGS = "flags"
_TYPE_GRAPH = "graph"


class _GraphPreview(QWidget):
    """Мини-превью графика «сырое значение → величина»: ломаная по
    точкам таблицы правок. Только отображение — точки правятся
    в таблице рядом (прототип)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[float, float]] = []
        self.setMinimumHeight(140)

    def set_points(self, points: list[tuple[float, float]]) -> None:
        self._points = sorted(points)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(rect, QColor(38, 38, 48))
        painter.setPen(QPen(QColor(90, 90, 110), 1))
        painter.drawRect(rect)
        if len(self._points) < 2:
            painter.setPen(QPen(QColor(160, 160, 170)))
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter,
                tr("Добавьте минимум 2 точки"),
            )
            painter.end()
            return
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 == x0:
            x1 = x0 + 1
        if y1 == y0:
            y1 = y0 + 1
        painter.setPen(QPen(QColor(108, 140, 255), 2))
        prev = None
        for x, y in self._points:
            px = rect.left() + (x - x0) / (x1 - x0) * rect.width()
            py = rect.bottom() - (y - y0) / (y1 - y0) * rect.height()
            if prev is not None:
                painter.drawLine(int(prev[0]), int(prev[1]), int(px), int(py))
            prev = (px, py)
        painter.setPen(QPen(QColor(255, 170, 80), 1))
        painter.setBrush(QColor(255, 170, 80))
        for x, y in self._points:
            px = rect.left() + (x - x0) / (x1 - x0) * rect.width()
            py = rect.bottom() - (y - y0) / (y1 - y0) * rect.height()
            painter.drawEllipse(int(px) - 3, int(py) - 3, 6, 6)
        painter.end()


class VariableDialog(QDialog):
    """Настройка одной переменной: выбор вида и параметров.

    Возвращает dict через .config — прототип, формат хранения будет
    задан позже вместе с исполнением на МК."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        font = QFont("Segoe UI", 9)
        self.setWindowTitle(tr("Настройка переменной"))
        self.setMinimumWidth(760)
        self.setFont(font)
        config = config or {}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel(tr("Вид переменной:")))
        self._type_combo = QComboBox()
        self._type_combo.setFont(font)
        self._type_combo.addItem(
            tr("Фиксированные значения флагов"), _TYPE_FLAGS
        )
        self._type_combo.addItem(
            tr("Графическая привязка к DATA"), _TYPE_GRAPH
        )
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        head.addWidget(self._type_combo)
        head.addSpacing(16)
        head.addWidget(QLabel(tr("Имя функции:")))
        self._name_edit = QLineEdit(config.get("name", ""))
        self._name_edit.setFont(font)
        self._name_edit.setPlaceholderText(
            tr("например, «Дверь водителя»")
        )
        self._name_edit.setFixedWidth(220)
        head.addWidget(self._name_edit)
        head.addStretch()
        layout.addLayout(head)

        self._stack = QStackedWidget()
        self._flags_page = self._build_flags_page(font)
        self._graph_page = self._build_graph_page(font)
        self._stack.addWidget(self._flags_page)
        self._stack.addWidget(self._graph_page)
        layout.addWidget(self._stack, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._apply_config(config)
        self._on_type_changed(self._type_combo.currentIndex())

    # ---- страница «Фиксированные значения флагов» --------------------

    def _build_flags_page(self, font: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Бит ОЗУ:")))
        self._bit_spin = QSpinBox()
        self._bit_spin.setFont(font)
        self._bit_spin.setRange(0, 63)
        self._bit_spin.setToolTip(
            tr("Номер бита в ОЗУ МК — Гибкая логика опрашивает его "
               "в условии «Если»")
        )
        top.addWidget(self._bit_spin)
        top.addSpacing(16)
        top.addWidget(QLabel(tr("Количество пакетов:")))
        self._packet_count = QSpinBox()
        self._packet_count.setFont(font)
        self._packet_count.setRange(1, 10)
        self._packet_count.valueChanged.connect(self._rebuild_packet_rows)
        top.addWidget(self._packet_count)
        top.addStretch()
        layout.addLayout(top)

        hint = QLabel(tr(
            "Каждый пакет приводит бит в состояние 1 (установить) или "
            "0 (сбросить): пример — пакет «дверь открыта» ставит бит, "
            "«дверь закрыта» сбрасывает. МК держит бит до прихода "
            "пакета с противоположным действием."
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        container = QWidget()
        self._packets_grid = QGridLayout(container)
        self._packets_grid.setSpacing(4)
        self._packets_grid.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setMinimumHeight(190)
        layout.addWidget(scroll, 1)
        self._packet_rows: list[dict[str, Any]] = []
        self._rebuild_packet_rows(1)
        return page

    def _rebuild_packet_rows(self, count: int) -> None:
        """Пересобирает строки пакетов под выбранное число (1..10)."""
        font = QFont("Segoe UI", 9)
        # Сохраняем текущие значения, чтобы смена числа не теряла ввод.
        saved = [self._read_packet_row(row) for row in self._packet_rows]
        while self._packets_grid.count():
            item = self._packets_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._packet_rows = []
        headers = [
            tr("Действие"), tr("Канал"), "ID", "DLC",
            tr("DATA (8 байт, X — любой)"),
        ]
        for col, text in enumerate(headers):
            label = QLabel(text)
            label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            self._packets_grid.addWidget(label, 0, col)
        for i in range(count):
            action = QComboBox()
            action.setFont(font)
            action.addItem(tr("Установить бит (→1)"), "set")
            action.addItem(tr("Сбросить бит (→0)"), "reset")
            channel = QComboBox()
            channel.setFont(font)
            channel.addItems(["CAN1", "CAN2", tr("Любой")])
            can_id = QLineEdit()
            can_id.setFont(font)
            can_id.setFixedWidth(70)
            can_id.setPlaceholderText("7A0")
            dlc = QSpinBox()
            dlc.setFont(font)
            dlc.setRange(0, 8)
            dlc.setValue(8)
            dlc.setFixedWidth(56)
            data_edit = QLineEdit()
            data_edit.setFont(font)
            data_edit.setPlaceholderText("XX XX XX XX XX XX XX XX")
            row_widgets = [action, channel, can_id, dlc, data_edit]
            for col, widget in enumerate(row_widgets):
                self._packets_grid.addWidget(widget, i + 1, col)
            self._packet_rows.append({
                "action": action,
                "channel": channel,
                "id": can_id,
                "dlc": dlc,
                "data": data_edit,
            })
        # Пружинная строка прижимает пакеты к верху — иначе сетка
        # размазывает строки по всей высоте скролла.
        self._packets_grid.setRowStretch(count + 1, 1)
        for i, row in enumerate(saved[:count]):
            self._write_packet_row(self._packet_rows[i], row)

    def _read_packet_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": row["action"].currentData(),
            "channel": row["channel"].currentIndex(),
            "id": row["id"].text(),
            "dlc": row["dlc"].value(),
            "data": row["data"].text(),
        }

    def _write_packet_row(self, row: dict[str, Any], data: dict[str, Any]) -> None:
        idx = row["action"].findData(data.get("action", "set"))
        row["action"].setCurrentIndex(idx if idx >= 0 else 0)
        row["channel"].setCurrentIndex(int(data.get("channel", 0)))
        row["id"].setText(str(data.get("id", "")))
        row["dlc"].setValue(int(data.get("dlc", 8)))
        row["data"].setText(str(data.get("data", "")))

    # ---- страница «Графическая привязка к DATA» ----------------------

    def _build_graph_page(self, font: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(6)

        line1 = QHBoxLayout()
        line1.addWidget(QLabel("ID:"))
        self._graph_id = QLineEdit()
        self._graph_id.setFont(font)
        self._graph_id.setFixedWidth(80)
        self._graph_id.setPlaceholderText("0C0")
        line1.addWidget(self._graph_id)
        line1.addWidget(QLabel("DLC:"))
        self._graph_dlc = QSpinBox()
        self._graph_dlc.setFont(font)
        self._graph_dlc.setRange(0, 8)
        self._graph_dlc.setValue(8)
        self._graph_dlc.setFixedWidth(56)
        line1.addWidget(self._graph_dlc)
        line1.addWidget(QLabel(tr("DATA от:")))
        self._graph_from = QLineEdit()
        self._graph_from.setFont(font)
        self._graph_from.setPlaceholderText("00 00 00 00 00 00 00 00")
        line1.addWidget(self._graph_from)
        line1.addWidget(QLabel(tr("до:")))
        self._graph_to = QLineEdit()
        self._graph_to.setFont(font)
        self._graph_to.setPlaceholderText("FF FF FF FF FF FF FF FF")
        line1.addWidget(self._graph_to)
        line1.addStretch()
        layout.addLayout(line1)

        line2 = QHBoxLayout()
        line2.addWidget(QLabel(tr("Байты для расчёта:")))
        self._graph_bytes: list[QCheckBox] = []
        for i in range(8):
            cb = QCheckBox(str(i))
            cb.setFont(font)
            cb.setChecked(i < 2)
            self._graph_bytes.append(cb)
            line2.addWidget(cb)
        line2.addSpacing(16)
        line2.addWidget(QLabel(tr("Величина:")))
        self._quantity_combo = QComboBox()
        self._quantity_combo.setFont(font)
        for item in (
            tr("Обороты (об/мин)"),
            tr("Наддув (бар)"),
            tr("Температура (°C)"),
            tr("Скорость (км/ч)"),
            tr("Другое…"),
        ):
            self._quantity_combo.addItem(item)
        line2.addWidget(self._quantity_combo)
        line2.addStretch()
        layout.addLayout(line2)

        hint = QLabel(tr(
            "График перевода: слева сырое значение выбранных байт, "
            "справа — величина. Точки правятся в таблице, линия между "
            "ними — интерполяция."
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self._points_table = QTableWidget(0, 2)
        self._points_table.setFont(font)
        self._points_table.setHorizontalHeaderLabels(
            [tr("Значение DATA"), tr("Величина")]
        )
        self._points_table.horizontalHeader().setStretchLastSection(True)
        self._points_table.setFixedWidth(300)
        self._points_table.itemChanged.connect(lambda _i: self._refresh_graph())
        left.addWidget(self._points_table, 1)
        buttons = QHBoxLayout()
        add_btn = QPushButton(tr("+ точка"))
        del_btn = QPushButton(tr("− точка"))
        for btn in (add_btn, del_btn):
            btn.setFont(font)
            btn.setFixedHeight(26)
        add_btn.clicked.connect(self._add_point_row)
        del_btn.clicked.connect(self._remove_point_row)
        buttons.addWidget(add_btn)
        buttons.addWidget(del_btn)
        buttons.addStretch()
        left.addLayout(buttons)
        body.addLayout(left)
        self._graph_preview = _GraphPreview()
        body.addWidget(self._graph_preview, 1)
        layout.addLayout(body, 1)
        return page

    def _add_point_row(self) -> None:
        row = self._points_table.rowCount()
        self._points_table.blockSignals(True)
        self._points_table.insertRow(row)
        self._points_table.setItem(row, 0, QTableWidgetItem(str(row * 100)))
        self._points_table.setItem(row, 1, QTableWidgetItem(str(row * 500)))
        self._points_table.blockSignals(False)
        self._refresh_graph()

    def _remove_point_row(self) -> None:
        row = self._points_table.currentRow()
        if row < 0:
            row = self._points_table.rowCount() - 1
        if row >= 0:
            self._points_table.blockSignals(True)
            self._points_table.removeRow(row)
            self._points_table.blockSignals(False)
            self._refresh_graph()

    def _refresh_graph(self) -> None:
        points: list[tuple[float, float]] = []
        for row in range(self._points_table.rowCount()):
            x_item = self._points_table.item(row, 0)
            y_item = self._points_table.item(row, 1)
            if x_item is None or y_item is None:
                continue
            try:
                x = float(x_item.text().replace(",", "."))
                y = float(y_item.text().replace(",", "."))
            except ValueError:
                continue
            points.append((x, y))
        self._graph_preview.set_points(points)

    # ---- общее --------------------------------------------------------

    def _on_type_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)

    def _apply_config(self, config: dict[str, Any]) -> None:
        var_type = config.get("type", _TYPE_FLAGS)
        idx = self._type_combo.findData(var_type)
        self._type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        if var_type == _TYPE_FLAGS:
            packets = config.get("packets") or []
            self._bit_spin.setValue(int(config.get("bit", 0)))
            self._packet_count.setValue(max(1, min(10, len(packets) or 1)))
            for row, data in zip(self._packet_rows, packets, strict=False):
                self._write_packet_row(row, data)
        else:
            self._graph_id.setText(str(config.get("id", "")))
            self._graph_dlc.setValue(int(config.get("dlc", 8)))
            self._graph_from.setText(str(config.get("from", "")))
            self._graph_to.setText(str(config.get("to", "")))
            for i, cb in enumerate(self._graph_bytes):
                cb.setChecked(i in (config.get("bytes") or [0, 1]))
            q_idx = int(config.get("quantity", 0))
            self._quantity_combo.setCurrentIndex(
                max(0, min(self._quantity_combo.count() - 1, q_idx))
            )
            self._points_table.setRowCount(0)
            for x, y in config.get("points") or []:
                row = self._points_table.rowCount()
                self._points_table.insertRow(row)
                self._points_table.setItem(row, 0, QTableWidgetItem(str(x)))
                self._points_table.setItem(row, 1, QTableWidgetItem(str(y)))
            if self._points_table.rowCount() == 0:
                for _ in range(4):
                    self._add_point_row()
            self._refresh_graph()

    @property
    def config(self) -> dict[str, Any]:
        """Текущая конфигурация диалога (прототип — in-memory)."""
        base: dict[str, Any] = {
            "name": self._name_edit.text().strip(),
        }
        if self._type_combo.currentData() == _TYPE_FLAGS:
            base.update({
                "type": _TYPE_FLAGS,
                "bit": self._bit_spin.value(),
                "packets": [
                    self._read_packet_row(row) for row in self._packet_rows
                ],
            })
        else:
            points: list[tuple[float, float]] = []
            for row in range(self._points_table.rowCount()):
                x_item = self._points_table.item(row, 0)
                y_item = self._points_table.item(row, 1)
                if x_item is None or y_item is None:
                    continue
                try:
                    points.append((
                        float(x_item.text().replace(",", ".")),
                        float(y_item.text().replace(",", ".")),
                    ))
                except ValueError:
                    continue
            base.update({
                "type": _TYPE_GRAPH,
                "id": self._graph_id.text().strip(),
                "dlc": self._graph_dlc.value(),
                "from": self._graph_from.text().strip(),
                "to": self._graph_to.text().strip(),
                "bytes": [
                    i for i, cb in enumerate(self._graph_bytes)
                    if cb.isChecked()
                ],
                "quantity": self._quantity_combo.currentIndex(),
                "points": points,
            })
        return base


class VariablesTab(QWidget):
    """Вкладка «Переменные»: колонки «Чтение»/«Управление», клик по
    строке — настройка переменной. Прототип: состояние только в памяти."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = QFont("Segoe UI", 9)
        self._read_vars: list[dict[str, Any] | None] = [None] * _ROW_COUNT
        self._ctrl_vars: list[dict[str, Any] | None] = [None] * _ROW_COUNT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel(tr("Переменные"))
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setProperty("title", True)
        layout.addWidget(title)

        hint = QLabel(tr(
            "Переменные — именованные состояния и величины, которые "
            "Гибкая логика опрашивает в условиях «Если» и использует в "
            "действиях. Клик по строке — настройка переменной."
        ))
        hint.setFont(self._font)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        self._read_list = self._build_column(
            columns, tr("Чтение"), self._read_vars
        )
        self._ctrl_list = self._build_column(
            columns, tr("Управление"), self._ctrl_vars
        )
        layout.addLayout(columns, 1)

    def _build_column(
        self,
        parent_layout: QHBoxLayout,
        title: str,
        store: list[dict[str, Any] | None],
    ) -> QListWidget:
        group = QGroupBox(title)
        group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        box = QVBoxLayout(group)
        lst = QListWidget()
        lst.setFont(self._font)
        for i in range(_ROW_COUNT):
            item = QListWidgetItem(self._row_text(i, None))
            lst.addItem(item)
        lst.itemClicked.connect(
            lambda item, l_=lst, s=store: self._edit_row(l_, s, item)
        )
        lst.itemDoubleClicked.connect(
            lambda item, l_=lst, s=store: self._edit_row(l_, s, item)
        )
        box.addWidget(lst)
        parent_layout.addWidget(group)
        return lst

    def _row_text(self, index: int, config: dict[str, Any] | None) -> str:
        if not config or not config.get("name"):
            return tr("{0:02d} — (пусто)").format(index + 1)
        kind = (
            tr("флаг") if config.get("type") == _TYPE_FLAGS
            else tr("график")
        )
        return tr("{0:02d} — {1} ({2})").format(
            index + 1, config["name"], kind
        )

    def _edit_row(
        self,
        lst: QListWidget,
        store: list[dict[str, Any] | None],
        item: QListWidgetItem,
    ) -> None:
        index = lst.row(item)
        dialog = VariableDialog(self, store[index])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        config = dialog.config
        store[index] = config
        item.setText(self._row_text(index, config))

    def retranslate_ui(self) -> None:
        for lst, store in (
            (self._read_list, self._read_vars),
            (self._ctrl_list, self._ctrl_vars),
        ):
            for i in range(lst.count()):
                lst.item(i).setText(self._row_text(i, store[i]))
