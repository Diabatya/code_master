"""Встроенная библиотека CAN ID для приложения «Код Мастер»."""

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import get_library_root, int_to_hex
from ui.can_trigger_tab import CanTriggerTab
from ui.ui_utils import setup_button

logger = get_logger(__name__)

LIBRARY_ROOT = get_library_root()


def _get_index_path() -> Path:
    return LIBRARY_ROOT / "vehicles.json"


def _ensure_index() -> Dict[str, Any]:
    path = _get_index_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {
        "vehicles": [
            {"make": "Demo", "model": "Vehicle", "year": 2024, "dbc": "dbc/demo_vehicle.dbc", "description": "Demo database"},
        ],
        "user_ids": [],
    }


def _save_index(data: Dict[str, Any]) -> None:
    path = _get_index_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_dbc(path: Path) -> Optional[Any]:
    try:
        import cantools
        return cantools.db.load_file(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось загрузить DBC %s: %s", path, exc)
    return None


class _AddVehicleDialog(QDialog):
    """Диалог добавления нового автомобиля в библиотеку."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Добавить автомобиль"))
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        font = QFont("Segoe UI", 10)

        self._make = QLineEdit()
        self._make.setFont(font)
        self._make.setPlaceholderText(tr("Производитель"))

        self._model = QLineEdit()
        self._model.setFont(font)
        self._model.setPlaceholderText(tr("Модель"))

        self._year = QLineEdit()
        self._year.setFont(font)
        self._year.setPlaceholderText(tr("Год"))
        self._year.setText("2024")

        self._dbc_path = QLineEdit()
        self._dbc_path.setFont(font)
        self._dbc_path.setPlaceholderText(tr("Путь к DBC"))

        browse_button = QPushButton(tr("Обзор..."))
        browse_button.setFont(font)
        browse_button.clicked.connect(self._browse)

        dbc_layout = QHBoxLayout()
        dbc_layout.addWidget(self._dbc_path, 1)
        dbc_layout.addWidget(browse_button)

        layout.addWidget(QLabel(tr("Производитель")))
        layout.addWidget(self._make)
        layout.addWidget(QLabel(tr("Модель")))
        layout.addWidget(self._model)
        layout.addWidget(QLabel(tr("Год")))
        layout.addWidget(self._year)
        layout.addWidget(QLabel(tr("Файл DBC")))
        layout.addLayout(dbc_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Выберите DBC"), "", "DBC files (*.dbc)")
        if path:
            self._dbc_path.setText(path)

    def get_result(self) -> Optional[Dict[str, Any]]:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        make = self._make.text().strip()
        model = self._model.text().strip()
        year = self._year.text().strip()
        dbc = self._dbc_path.text().strip()
        if not make or not model or not year or not dbc:
            return None
        try:
            year_int = int(year)
        except ValueError:
            return None
        return {"make": make, "model": model, "year": year_int, "dbc": dbc, "description": ""}


class _AddUserIdDialog(QDialog):
    """Диалог добавления пользовательского CAN ID."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Добавить CAN ID"))
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        font = QFont("Segoe UI", 10)

        self._id = QLineEdit()
        self._id.setFont(font)
        self._id.setPlaceholderText("0x123")

        self._name = QLineEdit()
        self._name.setFont(font)
        self._name.setPlaceholderText(tr("Название"))

        self._description = QLineEdit()
        self._description.setFont(font)
        self._description.setPlaceholderText(tr("Описание"))

        layout.addWidget(QLabel(tr("CAN ID")))
        layout.addWidget(self._id)
        layout.addWidget(QLabel(tr("Название")))
        layout.addWidget(self._name)
        layout.addWidget(QLabel(tr("Описание")))
        layout.addWidget(self._description)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_result(self) -> Optional[Dict[str, Any]]:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return {
            "id": self._id.text().strip(),
            "name": self._name.text().strip(),
            "description": self._description.text().strip(),
        }


class LibraryBrowser(QWidget):
    """Виджет для просмотра библиотеки CAN ID."""

    def __init__(
        self,
        trigger_tab: CanTriggerTab,
        flexible_logic_tab: Optional[QWidget] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = Config()
        self._trigger_tab = trigger_tab
        self._index: Dict[str, Any] = {}
        self._current_messages: List[Dict[str, Any]] = []
        self._create_widgets()
        self._build_layout()
        self._scan_library()

    def retranslate_ui(self) -> None:
        pass

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Библиотека CAN ID"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setFont(font)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)

        self._list = QListWidget()
        self._list.setFont(font)
        self._list.currentItemChanged.connect(self._on_message_selected)

        self._info_label = QLabel(tr("Выберите сообщение"))
        self._info_label.setFont(font)
        self._info_label.setWordWrap(True)
        self._info_label.setMinimumHeight(80)

        self._add_vehicle_button = QPushButton(tr("Добавить авто"))
        setup_button(self._add_vehicle_button, height=28)
        self._add_vehicle_button.clicked.connect(self._add_vehicle)

        self._add_id_button = QPushButton(tr("Добавить ID"))
        setup_button(self._add_id_button, height=28)
        self._add_id_button.clicked.connect(self._add_user_id)

        self._to_trigger_button = QPushButton(tr("В триггер"))
        setup_button(self._to_trigger_button, height=28)
        self._to_trigger_button.clicked.connect(self._to_trigger)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        right = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(10)
        rlayout.addWidget(QLabel(tr("Сообщения:")))
        rlayout.addWidget(self._list)
        rlayout.addWidget(self._info_label)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._add_vehicle_button)
        buttons_layout.addWidget(self._add_id_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._to_trigger_button)
        rlayout.addLayout(buttons_layout)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _scan_library(self) -> None:
        self._tree.clear()
        self._index = _ensure_index()
        _save_index(self._index)

        root = QTreeWidgetItem(self._tree, [tr("Библиотека")])
        root.setExpanded(True)

        makes: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
        for vehicle in self._index.get("vehicles", []):
            make = vehicle.get("make", "Unknown")
            model = vehicle.get("model", "Unknown")
            year = vehicle.get("year", 0)
            makes.setdefault(make, {}).setdefault(model, {})[year] = vehicle

        for make in sorted(makes):
            make_item = QTreeWidgetItem(root, [make])
            make_item.setExpanded(True)
            for model in sorted(makes[make]):
                model_item = QTreeWidgetItem(make_item, [model])
                model_item.setExpanded(True)
                for year in sorted(makes[make][model]):
                    vehicle = makes[make][model][year]
                    year_item = QTreeWidgetItem(model_item, [str(year)])
                    year_item.setData(0, Qt.ItemDataRole.UserRole, vehicle)

        user_root = QTreeWidgetItem(self._tree, [tr("Пользовательские")])
        user_root.setExpanded(True)
        for user_id in self._index.get("user_ids", []):
            item = QTreeWidgetItem(user_root, [f"{user_id.get('id', '')} {user_id.get('name', '')}".strip()])
            item.setData(0, Qt.ItemDataRole.UserRole, {"user_id": user_id})

    def _on_tree_selection_changed(self, current: QTreeWidgetItem, previous: QTreeWidgetItem) -> None:
        self._list.clear()
        self._current_messages = []
        self._info_label.setText(tr("Выберите сообщение"))
        if current is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if "user_id" in data:
            self._show_user_id(data["user_id"])
            return
        vehicle = data
        dbc = vehicle.get("dbc", "")
        dbc_path = LIBRARY_ROOT / dbc if dbc else None
        if dbc_path and dbc_path.exists():
            db = _load_dbc(dbc_path)
            if db:
                for msg in db.messages:
                    self._current_messages.append({
                        "id": msg.frame_id,
                        "name": msg.name,
                        "dlc": msg.length,
                        "signals": [s.name for s in msg.signals],
                        "comment": msg.comment or "",
                    })
            else:
                self._info_label.setText(tr("Не удалось загрузить DBC"))
        else:
            self._info_label.setText(tr("DBC файл не найден"))

        for msg in self._current_messages:
            item = QListWidgetItem(f"{int_to_hex(msg['id'], 8)} {msg['name']}")
            item.setData(Qt.ItemDataRole.UserRole, msg)
            self._list.addItem(item)

    def _show_user_id(self, user_id: Dict[str, Any]) -> None:
        msg = {
            "id": int(user_id.get("id", "0"), 0),
            "name": user_id.get("name", ""),
            "dlc": 8,
            "signals": [],
            "comment": user_id.get("description", ""),
        }
        self._current_messages.append(msg)
        item = QListWidgetItem(f"{int_to_hex(msg['id'], 8)} {msg['name']}")
        item.setData(Qt.ItemDataRole.UserRole, msg)
        self._list.addItem(item)

    def _on_message_selected(self, current: QListWidgetItem, previous: QListWidgetItem) -> None:
        if current is None:
            self._info_label.setText(tr("Выберите сообщение"))
            return
        msg = current.data(Qt.ItemDataRole.UserRole)
        if msg is None:
            return
        signals = ", ".join(msg.get("signals", [])) or "-"
        comment = msg.get("comment", "") or "-"
        self._info_label.setText(
            f"<b>{msg['name']}</b><br>"
            f"ID: {int_to_hex(msg['id'], 8)} | DLC: {msg['dlc']}<br>"
            f"{tr('Сигналы')}: {signals}<br>"
            f"{tr('Комментарий')}: {comment}"
        )

    def _to_trigger(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        msg = item.data(Qt.ItemDataRole.UserRole)
        if msg is None:
            return
        can_id = msg["id"]
        dlc = msg["dlc"]
        self._trigger_tab.create_trigger_from_packet({"id": can_id, "data": [0] * dlc, "dlc": dlc})
        QMessageBox.information(self, tr("Готово"), tr("Триггер создан из ID {0}").format(int_to_hex(can_id, 8)))

    def _add_vehicle(self) -> None:
        dialog = _AddVehicleDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_result()
        if result is None:
            return
        source = Path(result["dbc"])
        if not source.exists():
            QMessageBox.warning(self, tr("Внимание"), tr("DBC файл не найден"))
            return
        dbc_dir = LIBRARY_ROOT / "dbc"
        dbc_dir.mkdir(parents=True, exist_ok=True)
        dest = dbc_dir / source.name
        try:
            shutil.copy2(source, dest)
            result["dbc"] = f"dbc/{dest.name}"
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось скопировать DBC: {0}").format(exc))
            return
        self._index["vehicles"].append(result)
        _save_index(self._index)
        self._scan_library()

    def _add_user_id(self) -> None:
        dialog = _AddUserIdDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_result()
        if result is None:
            return
        self._index["user_ids"].append(result)
        _save_index(self._index)
        self._scan_library()
