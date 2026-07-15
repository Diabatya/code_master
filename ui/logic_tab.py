"""Вкладка «Логика»: цепочки Событие → Условия → Действия."""

from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes
from ui.hex_edit import HexDataEdit, create_data_field_widget
from ui.id_edit import IdPasteEdit

logger = get_logger(__name__)

BIT_RATES = [tr("11 бит"), tr("29 бит")]


class _IdValidator:
    """Валидатор HEX ID с подсветкой."""

    def __init__(self, edit: QLineEdit, bit_combo: QComboBox) -> None:
        self._edit = edit
        self._bit_combo = bit_combo
        self._edit.textChanged.connect(self._validate)
        self._bit_combo.currentIndexChanged.connect(self._update_bitness)
        self._update_bitness()

    def _update_bitness(self) -> None:
        max_chars = 8 if self._bit_combo.currentIndex() == 1 else 3
        self._edit.setMaxLength(max_chars)
        self._edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(f"[0-9A-Fa-f]{{0,{max_chars}}}"))
        )
        self._validate()

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
        if value > max_value:
            self._edit.setStyleSheet("color: #F44336;")
        else:
            self._edit.setStyleSheet("color: #4CAF50;")


class _LogicRow(QWidget):
    """Одна строка события/условия/действия."""

    def __init__(self, font: QFont, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._font = font
        self._create_widgets()
        self._layout_widgets()

    def _create_widgets(self) -> None:
        self._type_combo = QComboBox()
        self._type_combo.setFont(self._font)
        self._type_combo.addItems([tr("Вход/выход"), tr("CAN-сообщение")])
        self._type_combo.setFixedWidth(130)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)

        self._name_edit = QLineEdit()
        self._name_edit.setFont(self._font)
        self._name_edit.setPlaceholderText(tr("Название канала"))
        self._name_edit.setFixedWidth(180)

        self._bit_combo = QComboBox()
        self._bit_combo.setFont(self._font)
        self._bit_combo.addItems(BIT_RATES)
        self._bit_combo.setFixedWidth(90)

        self._id_edit = IdPasteEdit()
        self._id_edit.setFont(self._font)
        self._id_edit.setFixedWidth(90)
        self._id_edit.setPlaceholderText("ID")
        self._id_validator = _IdValidator(self._id_edit, self._bit_combo)
        self._id_edit.set_fill_callback(self._fill_from_packet)

        self._dlc_spin = QSpinBox()
        self._dlc_spin.setRange(1, 8)
        self._dlc_spin.setValue(8)
        self._dlc_spin.setFont(self._font)
        self._dlc_spin.setFixedWidth(50)
        self._dlc_spin.valueChanged.connect(self._on_dlc_changed)

        self._data_edits, self._data_widget = create_data_field_widget(self._font, 8, edit_width=42)

        self._can_frame = QWidget()
        can_layout = QHBoxLayout(self._can_frame)
        can_layout.setSpacing(4)
        can_layout.setContentsMargins(0, 0, 0, 0)
        can_layout.addWidget(QLabel(tr("ID")))
        can_layout.addWidget(self._bit_combo)
        can_layout.addWidget(self._id_edit)
        can_layout.addWidget(QLabel(tr("DLC")))
        can_layout.addWidget(self._dlc_spin)
        can_layout.addWidget(QLabel(tr("Data")))
        can_layout.addWidget(self._data_widget)
        can_layout.addStretch()

        self._delete_button = QPushButton("×")
        self._delete_button.setFixedSize(26, 26)
        self._delete_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._delete_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )

        self._on_type_changed(0)

    def _layout_widgets(self) -> None:
        layout = QHBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._type_combo)
        layout.addWidget(self._name_edit)
        layout.addWidget(self._can_frame, 1)
        layout.addStretch()
        layout.addWidget(self._delete_button)

    def _on_type_changed(self, index: int) -> None:
        is_can = index == 1
        self._name_edit.setVisible(not is_can)
        self._can_frame.setVisible(is_can)

    def _on_dlc_changed(self, value: int) -> None:
        for i, edit in enumerate(self._data_edits):
            edit.setEnabled(i < value)
            if i >= value:
                edit.setText("")

    def _fill_from_packet(self, parsed: Dict[str, Any]) -> None:
        can_id = parsed.get("id")
        if can_id is None:
            return
        self._type_combo.setCurrentIndex(1)
        self._bit_combo.setCurrentIndex(1 if can_id > 0x7FF else 0)
        self._id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        dlc = max(1, min(8, parsed.get("dlc", 8)))
        self._dlc_spin.setValue(dlc)
        data = parsed.get("data", [])
        for i, edit in enumerate(self._data_edits):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")
        self._on_dlc_changed(dlc)

    def get_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {"type": "can" if self._type_combo.currentIndex() == 1 else "io"}
        if config["type"] == "io":
            config["name"] = self._name_edit.text()
        else:
            config["bit"] = self._bit_combo.currentIndex()
            config["id"] = self._id_edit.text()
            config["dlc"] = self._dlc_spin.value()
            config["data"] = " ".join(e.text() for e in self._data_edits)
        return config

    def set_config(self, config: Dict[str, Any]) -> None:
        if config.get("type") == "can":
            self._type_combo.setCurrentIndex(1)
            self._bit_combo.setCurrentIndex(config.get("bit", 0))
            self._id_edit.setText(config.get("id", ""))
            self._dlc_spin.setValue(config.get("dlc", 8))
            data = parse_data_bytes(config.get("data", "").split())
            for i, edit in enumerate(self._data_edits):
                edit.setText(f"{data[i]:02X}" if i < len(data) else "")
            self._on_dlc_changed(self._dlc_spin.value())
        else:
            self._type_combo.setCurrentIndex(0)
            self._name_edit.setText(config.get("name", ""))


class LogicTab(QWidget):
    """Редактор цепочек Событие → Условия → Действия."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Логика"))
        self._events_group.setTitle(tr("События"))
        self._conditions_group.setTitle(tr("Условия"))
        self._actions_group.setTitle(tr("Действия"))
        self._factory_button.setText(tr("Заводские настройки"))
        self._save_device_button.setText(tr("Сохранить в устройство"))
        self._save_file_button.setText(tr("Сохранить в файл"))

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)
        self._font = font

        self._title = QLabel(tr("Логика"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._events_group = self._create_block_widget(tr("События"))
        self._conditions_group = self._create_block_widget(tr("Условия"))
        self._actions_group = self._create_block_widget(tr("Действия"))

        self._factory_button = QPushButton(tr("Заводские настройки"))
        self._factory_button.setFont(font)
        self._factory_button.setMinimumHeight(32)
        self._factory_button.clicked.connect(self._factory_reset)

        self._save_device_button = QPushButton(tr("Сохранить в устройство"))
        self._save_device_button.setFont(font)
        self._save_device_button.setMinimumHeight(32)
        self._save_device_button.clicked.connect(self._save_to_device)

        self._save_file_button = QPushButton(tr("Сохранить в файл"))
        self._save_file_button.setFont(font)
        self._save_file_button.setMinimumHeight(32)
        self._save_file_button.clicked.connect(self._save_to_file)

    def _create_block_widget(self, title: str) -> QGroupBox:
        group = QGroupBox(title)
        group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        group.setStyleSheet("QGroupBox { border: 1px solid #444444; border-radius: 6px; margin-top: 8px; padding-top: 8px; }")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._enable_check = QCheckBox(tr("Активен"))
        self._enable_check.setFont(self._font)
        self._enable_check.setChecked(True)
        self._enable_check.stateChanged.connect(lambda state, g=group: self._set_block_enabled(g, state))

        add_button = QPushButton(tr("Добавить"))
        add_button.setFont(self._font)
        add_button.setFixedHeight(26)
        header.addWidget(self._enable_check)
        header.addStretch()
        header.addWidget(add_button)
        layout.addLayout(header)

        rows_widget = QWidget()
        rows_layout = QVBoxLayout(rows_widget)
        rows_layout.setSpacing(4)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(rows_widget)

        # Keep references on the group for easy access
        group._rows_layout = rows_layout
        group._rows_widget = rows_widget
        group._enable_check = self._enable_check
        group._add_button = add_button

        add_button.clicked.connect(lambda: self._add_row(group))

        return group

    def _set_block_enabled(self, group: QGroupBox, state: int) -> None:
        enabled = state == Qt.CheckState.Checked.value
        group._rows_widget.setEnabled(enabled)
        group._add_button.setEnabled(enabled)
        if enabled:
            group._rows_widget.setStyleSheet("")
        else:
            group._rows_widget.setStyleSheet(
                "QWidget { color: #555555; } QLineEdit, QComboBox, QSpinBox { color: #555555; background-color: #2B2B3C; }"
            )

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(10)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(self._events_group)
        container_layout.addWidget(self._conditions_group)
        container_layout.addWidget(self._actions_group)
        container_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._factory_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._save_device_button)
        buttons_layout.addWidget(self._save_file_button)
        layout.addLayout(buttons_layout)

    def _add_row(self, group: QGroupBox) -> None:
        row = _LogicRow(self._font)
        row._delete_button.clicked.connect(lambda: self._remove_row(group, row))
        group._rows_layout.addWidget(row)
        group._rows_layout.addStretch()

    def _remove_row(self, group: QGroupBox, row: QWidget) -> None:
        group._rows_layout.removeWidget(row)
        row.deleteLater()
        self._compact_layout(group._rows_layout)

    def _compact_layout(self, layout: QVBoxLayout) -> None:
        for i in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(i)
            if item is not None and item.spacerItem() is not None:
                layout.removeItem(item)
                break
        layout.addStretch()

    def _collect_group(self, group: QGroupBox) -> Dict[str, Any]:
        return {
            "enabled": group._enable_check.isChecked(),
            "rows": [group._rows_layout.itemAt(i).widget().get_config() for i in range(group._rows_layout.count() - 1) if group._rows_layout.itemAt(i).widget() is not None],
        }

    def _apply_group(self, group: QGroupBox, data: Dict[str, Any]) -> None:
        group._enable_check.setChecked(data.get("enabled", True))
        for i in range(group._rows_layout.count() - 1, -1, -1):
            widget = group._rows_layout.itemAt(i).widget()
            if widget is not None:
                widget.deleteLater()
        self._compact_layout(group._rows_layout)
        for row_data in data.get("rows", []):
            row = _LogicRow(self._font)
            row._delete_button.clicked.connect(lambda checked=False, g=group, r=row: self._remove_row(g, r))
            row.set_config(row_data)
            group._rows_layout.addWidget(row)
        self._compact_layout(group._rows_layout)

    def _save_config(self) -> None:
        logic = {
            "events": self._collect_group(self._events_group),
            "conditions": self._collect_group(self._conditions_group),
            "actions": self._collect_group(self._actions_group),
        }
        self._config.set("logic", logic)

    def _load_config(self) -> None:
        logic = self._config.get("logic", {})
        self._apply_group(self._events_group, logic.get("events", {}))
        self._apply_group(self._conditions_group, logic.get("conditions", {}))
        self._apply_group(self._actions_group, logic.get("actions", {}))

    def _save_to_device(self) -> None:
        self._save_config()
        logger.info("Логика сохранена в Config")
        QMessageBox.information(self, tr("Готово"), tr("Логика сохранена в приложение"))

    def _save_to_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить логику"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            import json as _json
            data = {
                "events": self._collect_group(self._events_group),
                "conditions": self._collect_group(self._conditions_group),
                "actions": self._collect_group(self._actions_group),
            }
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, tr("Готово"), tr("Логика сохранена в файл"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить: {0}").format(exc))

    def _factory_reset(self) -> None:
        answer = QMessageBox.question(
            self,
            tr("Заводские настройки"),
            tr("Сбросить логику?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_group(self._events_group, {})
        self._apply_group(self._conditions_group, {})
        self._apply_group(self._actions_group, {})
        self._save_config()
