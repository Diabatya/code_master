"""Страница «Триггеры» с 10 расширенными блоками условий и ответов."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes

logger = get_logger(__name__)

TRIGGER_COUNT = 10
MAX_RESPONSE_FRAMES = 5
CHANNELS = [tr("CAN1"), tr("CAN2"), tr("CAN1 и CAN2")]
BIT_RATES = [tr("11 бит"), tr("29 бит")]


class _IdValidator:
    """Вспомогательный валидатор HEX ID с проверкой максимума."""

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
        max_value = self._max_value()
        if value > max_value:
            self._edit.setStyleSheet("color: #F44336;")
        else:
            self._edit.setStyleSheet("color: #4CAF50;")

    def _max_value(self) -> int:
        return 0x1FFFFFFF if self._bit_combo.currentIndex() == 1 else 0x7FF


class CanTriggerTab(QWidget):
    """Страница управления триггерами CAN."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._active = False
        self._internal_triggers: List[Dict[str, Any]] = []
        self._blocks: List[Dict[str, Any]] = []

        self._create_widgets()
        self._build_layout()
        self._load_config()

    def _setup_button(self, button: QPushButton, bold: bool = False, height: int = 32) -> None:
        """Устанавливает политику размера кнопки по содержимому."""
        button.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)
        button.setMinimumHeight(height)
        if bold:
            button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        button.adjustSize()

    def _make_id_edit(self, font: QFont, bit_combo: QComboBox) -> QLineEdit:
        edit = QLineEdit()
        edit.setFixedWidth(90)
        edit.setFont(font)
        edit.setMaxLength(8)
        edit.setPlaceholderText("ID")
        _IdValidator(edit, bit_combo)
        return edit

    def _make_data_edits(self, font: QFont) -> List[QLineEdit]:
        edits: List[QLineEdit] = []
        for d in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(35)
            edit.setFont(font)
            edit.setMaxLength(2)
            edit.setPlaceholderText(f"D{d}")
            edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,2}")))
            edit.textChanged.connect(lambda text, e=edit: self._on_data_edit_changed(e, text))
            edits.append(edit)
        return edits

    def _make_channel_combo(self, font: QFont) -> QComboBox:
        combo = QComboBox()
        combo.setFont(font)
        combo.addItems(CHANNELS)
        combo.setFixedWidth(110)
        return combo

    def _make_bit_combo(self, font: QFont) -> QComboBox:
        combo = QComboBox()
        combo.setFont(font)
        combo.addItems(BIT_RATES)
        combo.setFixedWidth(90)
        return combo

    def _make_dlc_spin(self, font: QFont) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, 8)
        spin.setValue(8)
        spin.setFont(font)
        spin.setFixedWidth(50)
        return spin

    def _make_count_spin(self, font: QFont, max_value: int = 64) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, max_value)
        spin.setValue(1)
        spin.setFont(font)
        spin.setFixedWidth(70)
        return spin

    def _make_delay_spin(self, font: QFont) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 9999)
        spin.setValue(0)
        spin.setSuffix(tr(" мс"))
        spin.setFont(font)
        spin.setFixedWidth(90)
        return spin

    def _create_receive_row(self, font: QFont, label: str) -> Dict[str, Any]:
        layout = QHBoxLayout()
        layout.setSpacing(4)
        layout.addWidget(QLabel(label))
        channel = self._make_channel_combo(font)
        layout.addWidget(channel)
        bit = self._make_bit_combo(font)
        layout.addWidget(bit)
        can_id = self._make_id_edit(font, bit)
        layout.addWidget(can_id)
        layout.addWidget(QLabel("DLC"))
        dlc = self._make_dlc_spin(font)
        layout.addWidget(dlc)
        layout.addWidget(QLabel("Data"))
        data = self._make_data_edits(font)
        for edit in data:
            layout.addWidget(edit)
        layout.addStretch()

        dlc.valueChanged.connect(lambda value: self._set_data_enabled(data, value))
        self._set_data_enabled(data, dlc.value())

        return {
            "layout": layout,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "data": data,
        }

    def _create_response_block(self, font: QFont) -> Dict[str, Any]:
        """Создаёт блок динамического списка фреймов ответа."""
        group = QGroupBox(tr("Ответ"))
        group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(4)
        group_layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        header.addWidget(QLabel(tr("Фреймы ответа")))
        header.addStretch()
        add_button = QPushButton("+")
        add_button.setFixedSize(30, 30)
        add_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        add_button.setStyleSheet("QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }")
        add_button.setToolTip(tr("Добавить фрейм"))
        header.addWidget(add_button)
        group_layout.addLayout(header)

        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(4)
        group_layout.addLayout(rows_layout)

        block = {"group": group, "rows_layout": rows_layout, "add_button": add_button, "rows": []}
        add_button.clicked.connect(lambda: self._add_response_row(block, font))
        self._add_response_row(block, font)
        return block

    def _create_response_row(self, font: QFont, block: Dict[str, Any]) -> Dict[str, Any]:
        """Создаёт одну строку фрейма ответа с полями в одном ряду."""
        widget = QWidget()
        row_layout = QHBoxLayout(widget)
        row_layout.setSpacing(2)
        row_layout.setContentsMargins(0, 0, 0, 0)

        channel = self._make_channel_combo(font)
        bit = self._make_bit_combo(font)
        can_id = self._make_id_edit(font, bit)
        dlc = self._make_dlc_spin(font)
        data = self._make_data_edits(font)

        delay = self._make_delay_spin(font)
        delay.setFixedWidth(80)
        count = self._make_count_spin(font, 999)
        count.setFixedWidth(60)

        remove_button = QPushButton("\u2013")
        remove_button.setFixedSize(30, 30)
        remove_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        remove_button.setStyleSheet("QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; }")
        remove_button.setToolTip(tr("Удалить фрейм"))

        row_layout.addWidget(QLabel(tr("Канал")))
        row_layout.addWidget(channel)
        row_layout.addWidget(QLabel(tr("Бит")))
        row_layout.addWidget(bit)
        row_layout.addWidget(QLabel("ID"))
        row_layout.addWidget(can_id)
        row_layout.addWidget(QLabel("DLC"))
        row_layout.addWidget(dlc)
        for edit in data:
            row_layout.addWidget(edit)
        row_layout.addStretch()
        row_layout.addWidget(QLabel(tr("Задержка")))
        row_layout.addWidget(delay)
        row_layout.addWidget(QLabel(tr("Кол-во")))
        row_layout.addWidget(count)
        row_layout.addWidget(remove_button)

        dlc.valueChanged.connect(lambda value: self._set_data_enabled(data, value))
        self._set_data_enabled(data, dlc.value())

        next_delay = self._make_delay_spin(font)
        next_delay.setFixedWidth(80)
        pause_widget = self._create_pause_widget(font, next_delay)

        row = {
            "widget": widget,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "data": data,
            "delay": delay,
            "count": count,
            "next_delay": next_delay,
            "pause_widget": pause_widget,
            "remove_button": remove_button,
        }
        remove_button.clicked.connect(lambda: self._remove_response_row(block, row))
        return row

    def _create_pause_widget(self, font: QFont, spin: QSpinBox) -> QWidget:
        """Создаёт виджет паузы между фреймами с разделителем."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setStyleSheet("background-color: #4A4A6A;")
        line.setFixedHeight(1)
        line.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addStretch()
        row.addWidget(QLabel(tr("Пауза перед следующим:")))
        row.addWidget(spin)
        row.addStretch()
        layout.addLayout(row)

        return widget

    def _add_response_row(self, block: Dict[str, Any], font: QFont) -> None:
        """Добавляет строку фрейма в блок ответа (максимум 5)."""
        if len(block["rows"]) >= MAX_RESPONSE_FRAMES:
            return
        new_row = self._create_response_row(font, block)
        block["rows"].append(new_row)
        self._rebuild_response_rows(block)
        self._update_response_buttons(block)

    def _remove_response_row(self, block: Dict[str, Any], row: Dict[str, Any]) -> None:
        """Удаляет строку фрейма из блока ответа (минимум 1)."""
        if len(block["rows"]) <= 1:
            return
        block["rows"].remove(row)
        row["widget"].deleteLater()
        row["pause_widget"].deleteLater()
        self._rebuild_response_rows(block)
        self._update_response_buttons(block)

    def _rebuild_response_rows(self, block: Dict[str, Any]) -> None:
        """Перестраивает layout фреймов и видимость пауз."""
        for row in block["rows"]:
            block["rows_layout"].removeWidget(row["widget"])
            row["widget"].hide()
            block["rows_layout"].removeWidget(row["pause_widget"])
            row["pause_widget"].hide()
        for i, row in enumerate(block["rows"]):
            block["rows_layout"].addWidget(row["widget"])
            row["widget"].show()
            if i < len(block["rows"]) - 1:
                block["rows_layout"].addWidget(row["pause_widget"])
                row["pause_widget"].show()

    def _update_response_buttons(self, block: Dict[str, Any]) -> None:
        """Активирует/деактивирует кнопки +/- в зависимости от количества строк."""
        can_add = len(block["rows"]) < MAX_RESPONSE_FRAMES
        block["add_button"].setEnabled(can_add)
        for row in block["rows"]:
            row["remove_button"].setEnabled(len(block["rows"]) > 1)

    def _create_cache_block(self, font: QFont) -> Dict[str, Any]:
        group = QGroupBox(tr("Кэш"))
        group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(4)
        group_layout.setContentsMargins(6, 6, 6, 6)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel(tr("Канал")))
        channel = self._make_channel_combo(font)
        row1.addWidget(channel)
        row1.addWidget(QLabel(tr("Бит")))
        bit = self._make_bit_combo(font)
        row1.addWidget(bit)
        row1.addWidget(QLabel("ID"))
        can_id = self._make_id_edit(font, bit)
        row1.addWidget(can_id)
        row1.addWidget(QLabel("DLC"))
        dlc = self._make_dlc_spin(font)
        row1.addWidget(dlc)
        row1.addWidget(QLabel(tr("Задержка мс")))
        delay = self._make_delay_spin(font)
        delay.setSuffix("")
        row1.addWidget(delay)
        row1.addWidget(QLabel(tr("Кол-во отправок")))
        count = self._make_count_spin(font, 999)
        count.setSuffix("")
        row1.addWidget(count)
        row1.addStretch()

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.addWidget(QLabel(tr("От")))
        from_data = self._make_data_edits(font)
        for edit in from_data:
            row2.addWidget(edit)
        row2.addSpacing(8)
        row2.addWidget(QLabel(tr("До")))
        to_data = self._make_data_edits(font)
        for edit in to_data:
            row2.addWidget(edit)
        row2.addStretch()

        dlc.valueChanged.connect(lambda value: self._set_data_enabled(from_data, value))
        dlc.valueChanged.connect(lambda value: self._set_data_enabled(to_data, value))
        self._set_data_enabled(from_data, dlc.value())
        self._set_data_enabled(to_data, dlc.value())

        group_layout.addLayout(row1)
        group_layout.addLayout(row2)

        return {
            "group": group,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "from_data": from_data,
            "to_data": to_data,
            "delay": delay,
            "count": count,
        }

    def _set_data_enabled(self, edits: List[QLineEdit], count: int) -> None:
        for i, edit in enumerate(edits):
            edit.setEnabled(i < count)

    def _on_data_edit_changed(self, edit: QLineEdit, text: str) -> None:
        """Приводит введённые HEX-символы в полях Data к верхнему регистру."""
        upper = text.upper()
        if text != upper:
            edit.blockSignals(True)
            edit.setText(upper)
            edit.blockSignals(False)

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)
        self._font = font

        self._apply_button = QPushButton(tr("Применить триггеры"))
        self._setup_button(self._apply_button, bold=True)
        self._apply_button.clicked.connect(self._apply_triggers)

        self._save_button = QPushButton(tr("Сохранить триггеры"))
        self._setup_button(self._save_button)
        self._save_button.clicked.connect(self._save_triggers)

        self._load_button = QPushButton(tr("Загрузить триггеры"))
        self._setup_button(self._load_button)
        self._load_button.clicked.connect(self._load_triggers)

        for i in range(TRIGGER_COUNT):
            group = QGroupBox(tr("Триггер {0}").format(i + 1))
            group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            group.setCheckable(True)
            group.setChecked(True)

            active = QCheckBox(tr("Активен"))
            active.setFont(font)

            cache_check = QCheckBox(tr("Автоматическая запись DATA в Кэш"))
            cache_check.setFont(font)

            cache_active = QCheckBox(tr("Активно"))
            cache_active.setFont(font)
            cache_active.stateChanged.connect(lambda state, idx=i: self._on_cache_active_changed(idx, state))

            cache_check.stateChanged.connect(lambda state, box=cache_active: self._sync_cache_check(box, state))
            cache_active.stateChanged.connect(lambda state, box=cache_check: self._sync_cache_check(box, state))

            recv = self._create_receive_row(font, tr("Приём"))
            response = self._create_response_block(font)
            cache = self._create_cache_block(font)

            block = {
                "group": group,
                "active": active,
                "cache_check": cache_check,
                "cache_active": cache_active,
                "recv": recv,
                "response": response,
                "cache": cache,
            }
            self._blocks.append(block)

    def _sync_cache_check(self, box: QCheckBox, state: int) -> None:
        box.blockSignals(True)
        box.setChecked(state == Qt.CheckState.Checked.value)
        box.blockSignals(False)

    def _build_layout(self) -> None:
        font = QFont("Segoe UI", 9)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(10)
        container_layout.setContentsMargins(8, 8, 8, 8)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._apply_button)
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._load_button)
        buttons.addStretch()
        container_layout.addLayout(buttons)

        for block in self._blocks:
            group_layout = QVBoxLayout(block["group"])
            group_layout.setSpacing(5)
            group_layout.setContentsMargins(6, 6, 6, 6)

            content = QWidget()
            content_layout = QVBoxLayout(content)
            content_layout.setSpacing(5)
            content_layout.setContentsMargins(6, 6, 6, 6)

            top = QHBoxLayout()
            top.addWidget(block["active"])
            top.addWidget(block["cache_check"])
            top.addWidget(block["cache_active"])
            top.addStretch()
            content_layout.addLayout(top)

            content_layout.addLayout(block["recv"]["layout"])

            content_layout.addWidget(block["response"]["group"])

            content_layout.addWidget(block["cache"]["group"])
            self._set_cache_enabled(block, False)

            group_layout.addWidget(content)
            block["group"].toggled.connect(lambda checked, content=content: content.setVisible(checked))
            block["content"] = content

            container_layout.addWidget(block["group"])

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def _on_cache_active_changed(self, index: int, state: int) -> None:
        enabled = state == Qt.CheckState.Checked.value
        block = self._blocks[index]
        self._set_cache_enabled(block, enabled)

    def _set_cache_enabled(self, block: Dict[str, Any], enabled: bool) -> None:
        block["response"]["group"].setEnabled(not enabled)
        block["cache"]["group"].setEnabled(enabled)

    def _parse_id(self, text: str) -> Optional[int]:
        return hex_to_int(text.strip())

    def _parse_data(self, edits: List[QLineEdit]) -> List[Optional[int]]:
        result: List[Optional[int]] = []
        for edit in edits:
            text = edit.text().strip()
            if text:
                val = hex_to_int(text)
                result.append(val if val is not None else 0)
            else:
                result.append(None)
        return result

    def _build_internal_triggers(self) -> List[Dict[str, Any]]:
        triggers = []
        for i, block in enumerate(self._blocks):
            if not block["active"].isChecked():
                continue
            recv_id = self._parse_id(block["recv"]["id"].text())
            if recv_id is None:
                continue
            triggers.append({
                "index": i,
                "recv_id": recv_id,
                "recv_data": self._parse_data(block["recv"]["data"]),
                "recv_channel": block["recv"]["channel"].currentIndex(),
                "cache": block["cache_active"].isChecked(),
                "responses": self._collect_responses(block["response"]["rows"]),
                "cache_data": self._collect_cache(block["cache"]),
                "cached_frame": None,
            })
        return triggers

    def _collect_responses(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for row in rows:
            can_id = self._parse_id(row["id"].text())
            if can_id is None:
                continue
            result.append({
                "channel": row["channel"].currentIndex(),
                "id": can_id,
                "data": self._parse_data(row["data"]),
                "delay": row["delay"].value(),
                "count": row["count"].value(),
                "next_delay": row["next_delay"].value(),
            })
        return result

    def _collect_cache(self, cache: Dict[str, Any]) -> Dict[str, Any]:
        can_id = self._parse_id(cache["id"].text())
        return {
            "id": can_id,
            "channel": cache["channel"].currentIndex(),
            "data_from": self._parse_data(cache["from_data"]),
            "data_to": self._parse_data(cache["to_data"]),
            "dlc": cache["dlc"].value(),
            "delay": cache["delay"].value(),
            "count": cache["count"].value(),
        }

    def _apply_triggers(self) -> None:
        self._internal_triggers = self._build_internal_triggers()
        self._active = bool(self._internal_triggers)
        logger.info("Триггеры применены: %d активных", len(self._internal_triggers))
        QMessageBox.information(self, tr("Готово"), tr("Триггеры применены: {0}").format(len(self._internal_triggers)))

    def _load_config(self) -> None:
        triggers = self._config.get("triggers", [])
        self.set_config(triggers if isinstance(triggers, list) else [])

    def _save_config(self) -> None:
        self._config.set("triggers", self._collect_config())

    def _collect_config(self) -> List[Dict[str, Any]]:
        config = []
        for block in self._blocks:
            responses = []
            for row in block["response"]["rows"]:
                responses.append({
                    "channel": row["channel"].currentIndex(),
                    "bit": row["bit"].currentIndex(),
                    "id": row["id"].text(),
                    "dlc": row["dlc"].value(),
                    "data": " ".join(e.text() for e in row["data"] if e.text()),
                    "delay": row["delay"].value(),
                    "count": row["count"].value(),
                    "next_delay": row["next_delay"].value(),
                })
            cache = block["cache"]
            config.append({
                "active": block["active"].isChecked(),
                "cache": block["cache_active"].isChecked(),
                "recv_channel": block["recv"]["channel"].currentIndex(),
                "recv_bit": block["recv"]["bit"].currentIndex(),
                "recv_id": block["recv"]["id"].text(),
                "recv_dlc": block["recv"]["dlc"].value(),
                "recv_data": " ".join(e.text() for e in block["recv"]["data"] if e.text()),
                "responses": responses,
                "cache_channel": cache["channel"].currentIndex(),
                "cache_bit": cache["bit"].currentIndex(),
                "cache_id": cache["id"].text(),
                "cache_dlc": cache["dlc"].value(),
                "cache_from_data": " ".join(e.text() for e in cache["from_data"] if e.text()),
                "cache_to_data": " ".join(e.text() for e in cache["to_data"] if e.text()),
                "cache_delay": cache["delay"].value(),
                "cache_count": cache["count"].value(),
            })
        return config

    def set_config(self, triggers: List[Dict[str, Any]]) -> None:
        """Загружает конфигурацию триггеров из списка."""
        for i, block in enumerate(self._blocks):
            trigger = triggers[i] if i < len(triggers) else {}
            block["active"].setChecked(bool(trigger.get("active", False)))
            cache_active = bool(trigger.get("cache", False))
            block["cache_active"].setChecked(cache_active)
            block["cache_check"].setChecked(cache_active)
            self._on_cache_active_changed(i, Qt.CheckState.Checked.value if cache_active else Qt.CheckState.Unchecked.value)

            self._set_row(block["recv"], trigger, "recv")
            self._set_response_rows(block["response"], trigger.get("responses", []))
            self._set_cache(block["cache"], trigger)

    def _set_row(self, row: Dict[str, Any], data: Dict[str, Any], prefix: str) -> None:
        row["channel"].setCurrentIndex(int(data.get(f"{prefix}_channel", 0)))
        row["bit"].setCurrentIndex(int(data.get(f"{prefix}_bit", 0)))
        row["id"].setText(str(data.get(f"{prefix}_id", "")))
        row["dlc"].setValue(int(data.get(f"{prefix}_dlc", 8)))
        bytes_data = parse_data_bytes(str(data.get(f"{prefix}_data", "")).split())
        for d, edit in enumerate(row["data"]):
            edit.setText(f"{bytes_data[d]:02X}" if d < len(bytes_data) else "")
        self._set_data_enabled(row["data"], row["dlc"].value())

    def _set_response_rows(self, response_block: Dict[str, Any], responses: List[Dict[str, Any]]) -> None:
        """Заполняет динамический список фреймов ответа из конфигурации."""
        rows = response_block["rows"]
        for r, row in enumerate(rows):
            data = responses[r] if r < len(responses) else {}
            self._set_response(row, data)
        while len(rows) > len(responses) and len(rows) > 1:
            self._remove_response_row(response_block, rows[-1])
        for r in range(len(rows), len(responses)):
            self._add_response_row(response_block, self._font)
            self._set_response(response_block["rows"][-1], responses[r])

    def _set_response(self, response: Dict[str, Any], data: Dict[str, Any]) -> None:
        response["channel"].setCurrentIndex(int(data.get("channel", 0)))
        response["bit"].setCurrentIndex(int(data.get("bit", 0)))
        response["id"].setText(str(data.get("id", "")))
        response["dlc"].setValue(int(data.get("dlc", 8)))
        bytes_data = parse_data_bytes(str(data.get("data", "")).split())
        for d, edit in enumerate(response["data"]):
            edit.setText(f"{bytes_data[d]:02X}" if d < len(bytes_data) else "")
        self._set_data_enabled(response["data"], response["dlc"].value())
        response["delay"].setValue(int(data.get("delay", 0)))
        response["count"].setValue(int(data.get("count", 1)))
        response["next_delay"].setValue(int(data.get("next_delay", 0)))

    def _set_cache(self, cache: Dict[str, Any], data: Dict[str, Any]) -> None:
        cache["channel"].setCurrentIndex(int(data.get("cache_channel", 0)))
        cache["bit"].setCurrentIndex(int(data.get("cache_bit", 0)))
        cache["id"].setText(str(data.get("cache_id", "")))
        cache["dlc"].setValue(int(data.get("cache_dlc", 8)))
        cache["delay"].setValue(int(data.get("cache_delay", 0)))
        cache["count"].setValue(int(data.get("cache_count", 1)))
        from_bytes = parse_data_bytes(str(data.get("cache_from_data", "")).split())
        to_bytes = parse_data_bytes(str(data.get("cache_to_data", "")).split())
        for d, edit in enumerate(cache["from_data"]):
            edit.setText(f"{from_bytes[d]:02X}" if d < len(from_bytes) else "")
        for d, edit in enumerate(cache["to_data"]):
            edit.setText(f"{to_bytes[d]:02X}" if d < len(to_bytes) else "")
        self._set_data_enabled(cache["from_data"], cache["dlc"].value())
        self._set_data_enabled(cache["to_data"], cache["dlc"].value())

    def _save_triggers(self) -> None:
        self._save_config()
        path, _ = Path(""), None
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить триггеры"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._collect_config(), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Триггеры сохранены в %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения триггеров: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить триггеры: {0}").format(exc))

    def _load_triggers(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, tr("Загрузить триггеры"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            triggers = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(triggers, list):
                raise ValueError(tr("Файл должен содержать список триггеров"))
            self.set_config(triggers)
            self._save_config()
            logger.info("Триггеры загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки триггеров: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить триггеры: {0}").format(exc))

    def _data_from_response(self, response: Dict[str, Any]) -> bytes:
        """Формирует байты данных фрейма ответа с учётом DLC."""
        dlc = response["dlc"].value()
        parsed = self._parse_data(response["data"])
        data = bytearray(dlc)
        for i in range(dlc):
            if i < len(parsed) and parsed[i] is not None:
                data[i] = parsed[i] & 0xFF
        return bytes(data)

    def _send_frame(self, can_id: int, data: bytes, channel_index: int, count: int = 1) -> None:
        """Отправляет один или несколько CAN-кадров в указанный канал."""
        if not self._serial_manager.is_open():
            return
        for _ in range(max(1, count)):
            if channel_index == 0:
                self._serial_manager.send_data(pack_can_frame(1, can_id, data))
            elif channel_index == 1:
                self._serial_manager.send_data(pack_can_frame(2, can_id, data))
            else:
                self._serial_manager.send_data(pack_can_frame(1, can_id, data))
                self._serial_manager.send_data(pack_can_frame(2, can_id, data))

    def process_frame(self, frame: Dict[str, Any]) -> None:
        if not self._active:
            return
        frame_id = int(frame["id"])
        frame_channel = int(frame["channel"])
        data = bytes(frame["data"])

        for trigger in self._internal_triggers:
            self._update_cache(trigger, frame_id, frame_channel, data)
            if not self._match_condition(trigger, frame_id, frame_channel, data):
                continue
            if trigger["cache"]:
                self._send_cached_frame(trigger)
            else:
                self._send_responses(trigger)
            break

    def _match_condition(
        self,
        trigger: Dict[str, Any],
        frame_id: int,
        frame_channel: int,
        data: bytes,
    ) -> bool:
        if trigger["recv_id"] != frame_id:
            return False
        recv_channel = int(trigger["recv_channel"])
        if recv_channel != 2 and recv_channel + 1 != frame_channel:
            return False
        for idx, expected in enumerate(trigger["recv_data"]):
            if expected is None:
                continue
            if idx >= len(data) or data[idx] != expected:
                return False
        return True

    def _send_responses(self, trigger: Dict[str, Any]) -> None:
        """Последовательно отправляет фреймы ответа с задержками и паузами."""
        cumulative = 0
        for i, response in enumerate(trigger["responses"]):
            cumulative += response["delay"]
            data = self._data_from_response(response)
            count = response["count"]
            if cumulative == 0:
                self._send_frame(response["id"], data, response["channel"], count)
            else:
                QTimer.singleShot(
                    cumulative,
                    lambda cid=response["id"], d=data, ch=response["channel"], cnt=count: self._send_frame(cid, d, ch, cnt),
                )
            if i < len(trigger["responses"]) - 1:
                cumulative += response["next_delay"]

    def _send_cached_frame(self, trigger: Dict[str, Any]) -> None:
        """Отправляет последний сохранённый кадр из кэша с задержкой и повторами."""
        cached = trigger.get("cached_frame")
        if cached is None:
            return
        cache = trigger["cache_data"]
        channel = cache["channel"]
        delay = cache["delay"]
        count = cache["count"]
        if delay == 0:
            self._send_frame(cached["id"], cached["data"], channel, count)
        else:
            QTimer.singleShot(
                delay,
                lambda cid=cached["id"], d=cached["data"], ch=channel, cnt=count: self._send_frame(cid, d, ch, cnt),
            )

    def _update_cache(self, trigger: Dict[str, Any], frame_id: int, frame_channel: int, data: bytes) -> None:
        """Сохраняет кадр в кэш, если он попадает в заданный диапазон."""
        if not trigger["cache"]:
            return
        cache = trigger["cache_data"]
        if cache["id"] is None or cache["id"] != frame_id:
            return
        if not self._data_in_range(data, cache["data_from"], cache["data_to"], cache["dlc"]):
            return
        dlc = cache["dlc"]
        trigger["cached_frame"] = {
            "id": frame_id,
            "data": bytes(data[:dlc]) if len(data) >= dlc else bytes(data) + bytes(dlc - len(data)),
            "channel": frame_channel,
        }

    def _data_in_range(
        self,
        data: bytes,
        data_from: List[Optional[int]],
        data_to: List[Optional[int]],
        dlc: int,
    ) -> bool:
        """Проверяет, что data (big-endian) попадает в диапазон [От, До]."""
        from_bytes = bytearray(dlc)
        to_bytes = bytearray(dlc)
        for i in range(dlc):
            from_val = data_from[i]
            to_val = data_to[i]
            if from_val is None or to_val is None:
                return False
            from_bytes[i] = from_val & 0xFF
            to_bytes[i] = to_val & 0xFF
        from_int = int.from_bytes(from_bytes, "big")
        to_int = int.from_bytes(to_bytes, "big")
        value = int.from_bytes(bytes(data[:dlc]).ljust(dlc, b"\x00"), "big")
        return from_int <= value <= to_int

    def create_trigger_from_packet(self, packet: Dict[str, object]) -> None:
        """Создаёт первый триггер из пакета мониторинга."""
        if not self._blocks:
            return
        block = self._blocks[0]
        block["active"].setChecked(True)
        can_id = int(packet["id"])
        block["recv"]["id"].setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        block["recv"]["bit"].setCurrentIndex(1 if can_id > 0x7FF else 0)
        bytes_data = bytes(packet["data"])
        for d, edit in enumerate(block["recv"]["data"]):
            edit.setText(f"{bytes_data[d]:02X}" if d < len(bytes_data) else "")
        logger.info("Триггер создан из пакета ID=0x%X", can_id)
