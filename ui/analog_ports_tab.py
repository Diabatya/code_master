"""Заглушка вкладки «Аналоговые порты» для устройств с аналоговыми входами."""

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.config import Config
from models.translations import _ as tr


class AnalogPortsTab(QWidget):
    """Вкладка настройки аналоговых портов (имя, цвет, пин)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self._create_widgets()
        self._build_layout()
        self._load_defaults()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._table = QTableWidget(0, 4)
        self._table.setFont(font)
        self._table.setHorizontalHeaderLabels(
            [tr("Имя"), tr("Цвет"), tr("Пин"), tr("Активен")]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.AllEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self._add_button = QPushButton(tr("Добавить"))
        self._add_button.setFont(font)
        self._add_button.clicked.connect(self._add_port)

        self._remove_button = QPushButton(tr("Удалить"))
        self._remove_button.setFont(font)
        self._remove_button.clicked.connect(self._remove_port)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        buttons = QHBoxLayout()
        buttons.addWidget(self._add_button)
        buttons.addWidget(self._remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addWidget(self._table, 1)

    def _load_defaults(self) -> None:
        ports = self._config.get("analog_ports", [])
        if not isinstance(ports, list):
            ports = []
        for port in ports:
            self._add_port(port)
        if self._table.rowCount() == 0:
            for _ in range(4):
                self._add_port()

    def _add_port(self, data: Optional[Dict[str, Any]] = None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        data = data or {}

        name_item = QTableWidgetItem(data.get("name", tr("Порт {0}").format(row + 1)))
        color_item = QTableWidgetItem(data.get("color", "#4CAF50"))
        color_item.setBackground(QColor(color_item.text()))
        pin_item = QTableWidgetItem(str(data.get("pin", row + 1)))
        active_item = QTableWidgetItem()
        active_item.setCheckState(Qt.CheckState.Checked if data.get("active", True) else Qt.CheckState.Unchecked)
        active_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        self._table.setItem(row, 0, name_item)
        self._table.setItem(row, 1, color_item)
        self._table.setItem(row, 2, pin_item)
        self._table.setItem(row, 3, active_item)

    def _remove_port(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)

    def collect_config(self) -> List[Dict[str, Any]]:
        ports = []
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            color_item = self._table.item(row, 1)
            pin_item = self._table.item(row, 2)
            active_item = self._table.item(row, 3)
            ports.append(
                {
                    "name": name_item.text() if name_item else "",
                    "color": color_item.text() if color_item else "#4CAF50",
                    "pin": int(pin_item.text()) if pin_item and pin_item.text().isdigit() else row,
                    "active": active_item.checkState() == Qt.CheckState.Checked if active_item else True,
                }
            )
        return ports
