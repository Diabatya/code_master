"""Встроенная автономная библиотека CAN ID."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_id_loader import CanIdLoader
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import get_library_root, hex_to_int, int_to_hex
from ui.can_trigger_tab import CanTriggerTab
from ui.ui_utils import setup_button

logger = get_logger(__name__)

LIBRARY_ROOT = get_library_root()
USER_DATA_PATH = LIBRARY_ROOT / "can_id" / "user_data.json"


def _save_user_data(records: List[Dict[str, Any]]) -> None:
    USER_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_DATA_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_user_data() -> List[Dict[str, Any]]:
    if not USER_DATA_PATH.exists():
        return []
    try:
        return json.loads(USER_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


class _AddMessageDialog(QDialog):
    """Диалог добавления/редактирования CAN ID в библиотеке."""

    def __init__(self, message: Optional[Dict[str, Any]] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Добавить CAN ID") if message is None else tr("Редактировать CAN ID"))
        self.setMinimumWidth(360)
        self._message = message or {}
        font = QFont("Segoe UI", 10)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        self._make = QLineEdit(self._message.get("make", ""))
        self._make.setFont(font)
        self._make.setPlaceholderText(tr("Марка"))

        self._model = QLineEdit(self._message.get("model", ""))
        self._model.setFont(font)
        self._model.setPlaceholderText(tr("Модель"))

        self._year = QLineEdit(str(self._message.get("year", "2024")))
        self._year.setFont(font)
        self._year.setPlaceholderText(tr("Год"))

        self._id = QLineEdit()
        if "id" in self._message:
            self._id.setText(int_to_hex(self._message["id"], 8))
        self._id.setFont(font)
        self._id.setPlaceholderText("0x123")

        self._name = QLineEdit(self._message.get("name", ""))
        self._name.setFont(font)
        self._name.setPlaceholderText(tr("Название"))

        self._description = QLineEdit(self._message.get("description", ""))
        self._description.setFont(font)
        self._description.setPlaceholderText(tr("Описание"))

        self._dlc = QLineEdit(str(self._message.get("dlc", 8)))
        self._dlc.setFont(font)
        self._dlc.setPlaceholderText("DLC")

        self._data = QLineEdit(self._message.get("data", ""))
        self._data.setFont(font)
        self._data.setPlaceholderText("00 00 00 00 00 00 00 00")

        layout.addWidget(QLabel(tr("Марка")))
        layout.addWidget(self._make)
        layout.addWidget(QLabel(tr("Модель")))
        layout.addWidget(self._model)
        layout.addWidget(QLabel(tr("Год")))
        layout.addWidget(self._year)
        layout.addWidget(QLabel(tr("CAN ID")))
        layout.addWidget(self._id)
        layout.addWidget(QLabel(tr("Название")))
        layout.addWidget(self._name)
        layout.addWidget(QLabel(tr("Описание")))
        layout.addWidget(self._description)
        layout.addWidget(QLabel(tr("DLC")))
        layout.addWidget(self._dlc)
        layout.addWidget(QLabel(tr("Данные (пример)")))
        layout.addWidget(self._data)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_result(self) -> Optional[Dict[str, Any]]:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        make = self._make.text().strip()
        model = self._model.text().strip()
        year = self._year.text().strip()
        can_id = hex_to_int(self._id.text().strip())
        if not make or not model or not year or can_id is None:
            return None
        try:
            dlc = int(self._dlc.text())
        except ValueError:
            dlc = 8
        return {
            "make": make,
            "model": model,
            "year": int(year),
            "id": can_id,
            "bit": 1 if can_id > 0x7FF else 0,
            "dlc": max(1, min(8, dlc)),
            "name": self._name.text().strip(),
            "description": self._description.text().strip(),
            "data": self._data.text().strip(),
            "signals": [],
        }


class LibraryBrowser(QWidget):
    """Виджет для просмотра автономной библиотеки CAN ID."""

    def __init__(
        self,
        trigger_tab: CanTriggerTab,
        flexible_logic_tab: Optional[QWidget] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._trigger_tab = trigger_tab
        self._loader = CanIdLoader()
        self._loader.load()
        if not self._loader.data:
            self._loader.demo_fallback()
        self._current_messages: List[Dict[str, Any]] = []
        self._create_widgets()
        self._build_layout()
        self._build_tree()

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Библиотека CAN ID"))
        self._use_button.setText(tr("Использовать в триггере"))
        self._add_button.setText(tr("Добавить ID"))
        self._refresh_button.setText(tr("Обновить"))
        self._table.setHorizontalHeaderLabels([tr("ID (HEX)"), tr("DLC"), tr("Описание"), tr("Данные (пример)")])

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)
        self._font = font

        self._title = QLabel(tr("Библиотека CAN ID"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setFont(font)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)

        self._table = QTableWidget()
        self._table.setFont(font)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([tr("ID (HEX)"), tr("DLC"), tr("Описание"), tr("Данные (пример)")])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 250)
        self._table.setColumnWidth(3, 120)

        self._use_button = QPushButton(tr("Использовать в триггере"))
        setup_button(self._use_button, height=28)
        self._use_button.clicked.connect(self._use_in_trigger)

        self._add_button = QPushButton(tr("Добавить ID"))
        setup_button(self._add_button, height=28)
        self._add_button.clicked.connect(self._add_message)

        self._refresh_button = QPushButton(tr("Обновить"))
        setup_button(self._refresh_button, height=28)
        self._refresh_button.clicked.connect(self._refresh)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        right = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(10)
        rlayout.addWidget(QLabel(tr("CAN ID:")))
        rlayout.addWidget(self._table)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._add_button)
        buttons_layout.addWidget(self._refresh_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._use_button)
        rlayout.addLayout(buttons_layout)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _build_tree(self) -> None:
        self._tree.clear()
        data = self._loader.data
        for make in sorted(data):
            make_item = QTreeWidgetItem(self._tree, [make])
            make_item.setExpanded(True)
            for model in sorted(data[make]):
                model_item = QTreeWidgetItem(make_item, [model])
                model_item.setExpanded(True)
                for year in sorted(data[make][model]):
                    year_item = QTreeWidgetItem(model_item, [str(year)])
                    year_item.setData(0, Qt.ItemDataRole.UserRole, (make, model, year))

    def _on_tree_selection_changed(self, current: QTreeWidgetItem, previous: QTreeWidgetItem) -> None:
        self._table.setRowCount(0)
        self._current_messages = []
        if current is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data or not isinstance(data, tuple):
            return
        make, model, year = data
        messages = self._loader.data.get(make, {}).get(model, {}).get(year, [])
        self._current_messages = messages
        self._table.setRowCount(len(messages))
        for row, msg in enumerate(messages):
            self._table.setItem(row, 0, QTableWidgetItem(int_to_hex(msg.get("id", 0), 8)))
            self._table.setItem(row, 1, QTableWidgetItem(str(msg.get("dlc", 8))))
            self._table.setItem(row, 2, QTableWidgetItem(msg.get("description") or msg.get("name", "")))
            self._table.setItem(row, 3, QTableWidgetItem(msg.get("data", "")))
            for col in range(4):
                self._table.item(row, col).setData(Qt.ItemDataRole.UserRole, msg)

    def _use_in_trigger(self) -> None:
        current = self._table.currentRow()
        if current < 0 or current >= len(self._current_messages):
            return
        msg = self._current_messages[current]
        can_id = msg.get("id", 0)
        dlc = msg.get("dlc", 8)
        self._trigger_tab.create_trigger_from_packet({"id": can_id, "data": [0] * dlc, "dlc": dlc})
        QMessageBox.information(self, tr("Готово"), tr("Триггер создан из ID {0}").format(int_to_hex(can_id, 8)))

    def _add_message(self) -> None:
        dialog = _AddMessageDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_result()
        if result is None:
            return
        records = _load_user_data()
        records.append(result)
        _save_user_data(records)
        self._refresh()

    def _refresh(self) -> None:
        self._loader.load()
        self._build_tree()
