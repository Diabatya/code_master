"""Вкладка «Переменные».

Две колонки: «Чтение» (переменные, читаемые Гибкой логикой в условиях
«Если») и «Управление» (переменные для действий). Строк добавления
нет — оператор добавляет переменные кнопкой «＋ Добавить переменную»;
клик по строке открывает её настройку одного из двух видов:

* «Статическая переменная» — неограниченный набор фреймов с
  маской X по DATA (как «Приём» в триггерах); у каждого фрейма справа
  выбор значения «→ 1» / «→ 0» — приход фрейма пишет это значение
  в бит ОЗУ, привязанный к функции в ГЛ;
* «Динамическая переменная» — ID/DLC/диапазон Data, выбор байтов и
  редактируемый график перевода сырого значения в величину
  (обороты ДВС, наддув, температура и т.п.).

У каждой строки справа выбор носителя «ОЗУ / ПЗУ»; ПЗУ пока
некликабельно — появится после подключения EEPROM к МК.

Хранение: кнопки «Загрузить переменные» / «Сохранить переменные»
читают/пишут отдельный файл «Конфиг Инфо.json» (переменные + заметки
по ID из мониторинга). Файл чужого формата или иной версии не роняет
приложение — выводится предупреждение. Запись DATA переменных во
флэш МК (по аналогии с конфигурацией триггеров) — следующий этап,
после спецификации протокола.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.config import Config
from models.id_notes import IdNotes
from models.translations import _ as tr
from models.version import VERSION

_TYPE_STATIC = "static"
_TYPE_DYNAMIC = "dynamic"

# Метка и версия файла «Конфиг Инфо»: чужой формат или другая версия
# отвергаются сообщением, без падения приложения.
_CONFIG_INFO_KIND = "codemaster_config_info"
_CONFIG_INFO_VERSION = 1
_CONFIG_INFO_NAME = "Конфиг Инфо.json"


def _config_info_dir() -> str:
    """Отдельная папка для конфигураций переменных."""
    return str(Config().config_dir() / "variables")


class _GraphPreview(QWidget):
    """Мини-превью графика «сырое значение → величина»: ломаная по
    точкам таблицы правок. Только отображение — точки правятся
    в таблице рядом."""

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


class _FrameRow(QWidget):
    """Строка фрейма статической переменной:
    канал | ID | DLC | DATA (X — любой) | →1/→0 | ✕."""

    def __init__(
        self,
        font: QFont,
        on_remove,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.channel = QComboBox()
        self.channel.setFont(font)
        self.channel.addItems(["CAN1", "CAN2", tr("Любой")])
        row.addWidget(self.channel)

        self.can_id = QLineEdit()
        self.can_id.setFont(font)
        self.can_id.setFixedWidth(70)
        self.can_id.setPlaceholderText("7A0")
        row.addWidget(self.can_id)

        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(0, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(56)
        row.addWidget(self.dlc)

        self.data = QLineEdit()
        self.data.setFont(font)
        self.data.setPlaceholderText("XX XX XX XX XX XX XX XX")
        row.addWidget(self.data, 1)

        # Выбор записываемого значения — справа от фрейма (по ТЗ).
        self.value = QComboBox()
        self.value.setFont(font)
        self.value.addItem(tr("→ 1"), 1)
        self.value.addItem(tr("→ 0"), 0)
        row.addWidget(self.value)

        remove = QPushButton("✕")
        remove.setFont(font)
        remove.setFixedSize(26, 26)
        remove.setToolTip(tr("Удалить фрейм"))
        remove.clicked.connect(lambda: on_remove(self))
        row.addWidget(remove)

    def read(self) -> dict[str, Any]:
        return {
            "channel": self.channel.currentIndex(),
            "id": self.can_id.text(),
            "dlc": self.dlc.value(),
            "data": self.data.text(),
            "value": self.value.currentData(),
        }

    def write(self, data: dict[str, Any]) -> None:
        self.channel.setCurrentIndex(int(data.get("channel", 0)))
        self.can_id.setText(str(data.get("id", "")))
        self.dlc.setValue(int(data.get("dlc", 8)))
        self.data.setText(str(data.get("data", "")))
        idx = self.value.findData(int(data.get("value", 1)))
        self.value.setCurrentIndex(idx if idx >= 0 else 0)


class VariableDialog(QDialog):
    """Настройка одной переменной: выбор вида и параметров."""

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
        self._type_combo.addItem(tr("Статическая переменная"), _TYPE_STATIC)
        self._type_combo.addItem(tr("Динамическая переменная"), _TYPE_DYNAMIC)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        head.addWidget(self._type_combo)
        head.addSpacing(16)
        head.addWidget(QLabel(tr("Имя функции:")))
        self._name_edit = QLineEdit(config.get("name", ""))
        self._name_edit.setFont(font)
        self._name_edit.setFixedWidth(220)
        head.addWidget(self._name_edit)
        head.addStretch()
        layout.addLayout(head)

        self._stack = QStackedWidget()
        self._static_page = self._build_static_page(font)
        self._dynamic_page = self._build_dynamic_page(font)
        self._stack.addWidget(self._static_page)
        self._stack.addWidget(self._dynamic_page)
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

    # ---- страница «Статическая переменная» -------------------------

    def _build_static_page(self, font: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(6)

        hint = QLabel(tr(
            "Фреймов может быть сколько угодно: приход фрейма пишет "
            "«→ 1» или «→ 0» в бит ОЗУ функции. X в DATA — любой байт. "
            "Пример: фрейм «дверь открыта» → 1, «дверь закрыта» → 0. "
            "МК держит бит до прихода фрейма с противоположным "
            "значением; Гибкая логика опрашивает его в условии «Если»."
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        container = QWidget()
        self._frames_layout = QVBoxLayout(container)
        self._frames_layout.setSpacing(4)
        self._frames_layout.setContentsMargins(0, 0, 0, 0)
        self._frames_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setMinimumHeight(170)
        layout.addWidget(scroll, 1)

        add_row = QHBoxLayout()
        add_btn = QPushButton(tr("＋ Добавить фрейм"))
        add_btn.setFont(font)
        add_btn.clicked.connect(lambda: self._add_frame_row(None))
        add_row.addWidget(add_btn)
        add_row.addStretch()
        layout.addLayout(add_row)
        self._frame_rows: list[_FrameRow] = []
        return page

    def _add_frame_row(self, data: dict[str, Any] | None) -> None:
        font = QFont("Segoe UI", 9)
        row = _FrameRow(font, self._remove_frame_row)
        if data is not None:
            row.write(data)
        self._frame_rows.append(row)
        self._frames_layout.insertWidget(
            self._frames_layout.count() - 1, row
        )

    def _remove_frame_row(self, row: _FrameRow) -> None:
        if row in self._frame_rows:
            self._frame_rows.remove(row)
        self._frames_layout.removeWidget(row)
        row.deleteLater()

    # ---- страница «Динамическая переменная» --------------------------

    def _build_dynamic_page(self, font: QFont) -> QWidget:
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
        # Пример имени зависит от вида переменной (по ТЗ):
        # динамическая → «Обороты ДВС», статическая → «Дверь водителя».
        example = (
            tr("например, «Обороты ДВС»") if index == 1
            else tr("например, «Дверь водителя»")
        )
        self._name_edit.setPlaceholderText(example)

    def _apply_config(self, config: dict[str, Any]) -> None:
        var_type = config.get("type", _TYPE_STATIC)
        # Принимаем и старые обозначения видов (прототип ранних сборок).
        if var_type in ("flags", _TYPE_STATIC):
            var_type = _TYPE_STATIC
            idx = 0
        else:
            var_type = _TYPE_DYNAMIC
            idx = 1
        self._type_combo.setCurrentIndex(idx)
        if var_type == _TYPE_STATIC:
            frames = config.get("frames") or config.get("packets") or []
            for frame in frames:
                self._add_frame_row(frame)
            if not self._frame_rows:
                self._add_frame_row(None)
        else:
            self._graph_id.setText(str(config.get("id", "")))
            self._graph_dlc.setValue(int(config.get("dlc", 8)))
            self._graph_from.setText(str(config.get("from", "")))
            self._graph_to.setText(str(config.get("to", "")))
            for i, cb in enumerate(self._graph_bytes):
                cb.setChecked(i in (config.get("bytes") or [0, 1]))
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
        """Текущая конфигурация диалога."""
        base: dict[str, Any] = {"name": self._name_edit.text().strip()}
        if self._type_combo.currentData() == _TYPE_STATIC:
            base.update({
                "type": _TYPE_STATIC,
                "frames": [row.read() for row in self._frame_rows],
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
                "type": _TYPE_DYNAMIC,
                "id": self._graph_id.text().strip(),
                "dlc": self._graph_dlc.value(),
                "from": self._graph_from.text().strip(),
                "to": self._graph_to.text().strip(),
                "bytes": [
                    i for i, cb in enumerate(self._graph_bytes)
                    if cb.isChecked()
                ],
                "points": points,
            })
        return base


class _VariableRow(QFrame):
    """Строка переменной в колонке:

    [название] | [состояние] | [○ ОЗУ / ○ ПЗУ] | [✕]

    Клик по строке — редактор переменной. Состояние: у статической —
    «0»/«1», у динамической — число по графику (пока вводится с МК —
    отображается «0»). ПЗУ некликабельно до подключения EEPROM."""

    def __init__(
        self,
        column: _VarColumn,
        font: QFont,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(column)
        self._column = column
        self._font = font
        self.config: dict[str, Any] = config or {}
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("varRow", True)
        self.setStyleSheet(
            "QFrame[varRow='true'] {"
            "  border: 1px solid #454552; border-radius: 8px;"
            "  background: rgba(255,255,255,0.03); }"
            "QFrame[varRow='true']:hover { border-color: #7C9EFF; }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 6, 6)
        row.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        row.addWidget(self._name_label, 1)

        self._state_label = QLabel("0")
        self._state_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._state_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._state_label.setMinimumWidth(46)
        row.addWidget(self._state_label)

        # Носитель: ОЗУ сейчас, ПЗУ — после подключения EEPROM.
        self._ram_radio = QRadioButton(tr("ОЗУ"))
        self._rom_radio = QRadioButton(tr("ПЗУ"))
        for radio in (self._ram_radio, self._rom_radio):
            radio.setFont(font)
        self._ram_radio.setChecked(
            self.config.get("storage", "ram") == "ram"
        )
        self._rom_radio.setEnabled(False)
        self._rom_radio.setToolTip(
            tr("ПЗУ появится после подключения EEPROM к МК")
        )
        self._ram_radio.toggled.connect(self._on_storage_changed)
        row.addWidget(self._ram_radio)
        row.addWidget(self._rom_radio)

        remove = QPushButton("✕")
        remove.setFont(font)
        remove.setFixedSize(26, 26)
        remove.setToolTip(tr("Удалить переменную"))
        remove.clicked.connect(lambda: column.remove_row(self))
        row.addWidget(remove)

        self._refresh_labels()

    def _on_storage_changed(self, checked: bool) -> None:
        self.config["storage"] = "ram" if checked else "rom"
        self._column.persist()

    def _refresh_labels(self) -> None:
        name = self.config.get("name", "").strip()
        self._name_label.setText(name or tr("— (без имени)"))
        if self.config.get("type") == _TYPE_DYNAMIC:
            self._state_label.setText("0")
        else:
            self._state_label.setText("0")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Клик по свободному месту строки — редактор; по кнопкам/
        # радио событие не долетает сюда (у них свой приём).
        if event.button() == Qt.MouseButton.LeftButton:
            self._column.edit_row(self)
        super().mousePressEvent(event)


class _VarColumn(QWidget):
    """Колонка «Чтение»/«Управление»: список строк переменных +
    кнопка добавления."""

    def __init__(
        self,
        title: str,
        tab: VariablesTab,
        key: str,
        font: QFont,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self.key = key
        self._font = font
        self._rows: list[_VariableRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._group = QFrame()
        self._group.setStyleSheet(
            "QFrame { border: 1px solid #454552; border-radius: 10px; }"
        )
        box = QVBoxLayout(self._group)
        self._title = QLabel(title)
        self._title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        box.addWidget(self._title)

        container = QWidget()
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setSpacing(6)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        box.addWidget(scroll, 1)
        layout.addWidget(self._group, 1)

        self._add_btn = QPushButton(tr("＋ Добавить переменную"))
        self._add_btn.setFont(font)
        self._add_btn.clicked.connect(self._on_add)
        layout.addWidget(self._add_btn)

    def add_row(self, config: dict[str, Any] | None = None) -> _VariableRow:
        row = _VariableRow(self, self._font, config)
        if "storage" not in row.config:
            row.config["storage"] = "ram"
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        return row

    def remove_row(self, row: _VariableRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self.persist()

    def edit_row(self, row: _VariableRow) -> None:
        dialog = VariableDialog(self._tab, row.config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        row.config.update(dialog.config)
        row._refresh_labels()
        self.persist()

    def _on_add(self) -> None:
        row = self.add_row()
        self.edit_row(row)
        # Пустую (отменённую) строку не оставляем болтаться без имени.
        if not row.config:
            self.remove_row(row)
        else:
            self.persist()

    def configs(self) -> list[dict[str, Any]]:
        return [dict(row.config) for row in self._rows]

    def clear(self) -> None:
        for row in list(self._rows):
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        self.persist()

    def persist(self) -> None:
        self._tab.persist()


class VariablesTab(QWidget):
    """Вкладка «Переменные»: колонки «Чтение»/«Управление»,
    сохранение/загрузка «Конфиг Инфо»."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = QFont("Segoe UI", 9)
        self._config = Config()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title_col = QVBoxLayout()
        self._title = QLabel(tr("Переменные"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)
        title_col.addWidget(self._title)
        self._hint = QLabel(tr(
            "Переменные — именованные состояния и величины, которые "
            "Гибкая логика опрашивает в условиях «Если» и использует в "
            "действиях. Клик по строке — настройка переменной."
        ))
        self._hint.setFont(self._font)
        self._hint.setWordWrap(True)
        title_col.addWidget(self._hint)
        top.addLayout(title_col, 1)

        btn_col = QVBoxLayout()
        self._load_btn = QPushButton(tr("Загрузить переменные"))
        self._save_btn = QPushButton(tr("Сохранить переменные"))
        self._clear_btn = QPushButton(tr("Очистить"))
        for btn in (self._load_btn, self._save_btn, self._clear_btn):
            btn.setFont(self._font)
            btn.setFixedHeight(30)
            btn_col.addWidget(btn)
        self._load_btn.clicked.connect(self._load_file)
        self._save_btn.clicked.connect(self._save_file)
        self._clear_btn.clicked.connect(self._clear_all)
        top.addLayout(btn_col)
        layout.addLayout(top)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        self._read_col = _VarColumn(tr("Чтение"), self, "read", self._font)
        self._ctrl_col = _VarColumn(
            tr("Управление"), self, "control", self._font
        )
        columns.addWidget(self._read_col)
        columns.addWidget(self._ctrl_col)
        layout.addLayout(columns, 1)

        self._restore()

    # ---- хранение ------------------------------------------------------

    def export_config(self) -> dict[str, Any]:
        return {
            "read": self._read_col.configs(),
            "control": self._ctrl_col.configs(),
        }

    def persist(self) -> None:
        """Переменные — часть общего конфига (отдельная ветка ключа)."""
        self._config.set("variables", self.export_config())

    def _restore(self) -> None:
        saved = self._config.get("variables") or {}
        for cfg in saved.get("read") or []:
            self._read_col.add_row(cfg)
        for cfg in saved.get("control") or []:
            self._ctrl_col.add_row(cfg)

    def _clear_all(self) -> None:
        self._read_col.clear()
        self._ctrl_col.clear()

    # ---- файл «Конфиг Инфо» -------------------------------------------

    def _save_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить переменные"),
            f"{_config_info_dir()}/{_CONFIG_INFO_NAME}",
            tr("Конфиг Инфо (*.json)"),
        )
        if not path:
            return
        payload = {
            "kind": _CONFIG_INFO_KIND,
            "format": _CONFIG_INFO_VERSION,
            "app_version": VERSION,
            "variables": self.export_config(),
            "id_notes": IdNotes().export_all(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Ошибка"), tr("Не удалось сохранить файл: {0}")
                .format(exc)
            )

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить переменные"),
            _config_info_dir(),
            tr("Конфиг Инфо (*.json);;Все файлы (*)"),
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                payload: Any = json.load(f)
        except (OSError, json.JSONDecodeError):
            payload = None
        # Чужой формат / битый файл / несовместимая версия — только
        # предупреждение, приложение продолжает работать.
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != _CONFIG_INFO_KIND
            or payload.get("format") != _CONFIG_INFO_VERSION
        ):
            QMessageBox.warning(
                self,
                tr("Конфиг Инфо"),
                tr("Конфиг инфо не соответствует действующей "
                   "версии приложения"),
            )
            return
        variables = payload.get("variables") or {}
        if isinstance(variables, dict):
            self._read_col.clear()
            self._ctrl_col.clear()
            for cfg in variables.get("read") or []:
                if isinstance(cfg, dict):
                    self._read_col.add_row(cfg)
            for cfg in variables.get("control") or []:
                if isinstance(cfg, dict):
                    self._ctrl_col.add_row(cfg)
            self.persist()
        notes = payload.get("id_notes")
        if isinstance(notes, dict):
            IdNotes().import_all(notes)

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Переменные"))
        self._load_btn.setText(tr("Загрузить переменные"))
        self._save_btn.setText(tr("Сохранить переменные"))
        self._clear_btn.setText(tr("Очистить"))
        self._read_col._title.setText(tr("Чтение"))
        self._ctrl_col._title.setText(tr("Управление"))
        for col in (self._read_col, self._ctrl_col):
            col._add_btn.setText(tr("＋ Добавить переменную"))
            for row in col._rows:
                row._ram_radio.setText(tr("ОЗУ"))
                row._rom_radio.setText(tr("ПЗУ"))
                row._refresh_labels()
