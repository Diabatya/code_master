"""Страница «Триггеры» с 10 расширенными блоками условий и ответов."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
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

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes

logger = get_logger(__name__)

TRIGGER_COUNT = 10
RESPONSE_COUNT = 5
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
        text = self._edit.text().strip()
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

    def _make_id_edit(self, font: QFont, bit_combo: QComboBox) -> QLineEdit:
        edit = QLineEdit()
        edit.setFixedWidth(80)
        edit.setFont(font)
        edit.setMaxLength(8)
        edit.setPlaceholderText("ID")
        _IdValidator(edit, bit_combo)
        return edit

    def _make_data_edits(self, font: QFont) -> List[QLineEdit]:
        edits: List[QLineEdit] = []
        for d in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(28)
            edit.setFont(font)
            edit.setMaxLength(2)
            edit.setPlaceholderText(f"D{d}")
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
        spin.setSuffix(" ms")
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

    def _create_response_block(self, font: QFont, index: int) -> Dict[str, Any]:
        group = QGroupBox(tr("Ответ {0}").format(index + 1))
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
        row1.addWidget(QLabel("Data"))
        data = self._make_data_edits(font)
        for edit in data:
            row1.addWidget(edit)
        row1.addWidget(QLabel(tr("Кол-во")))
        count = self._make_count_spin(font)
        row1.addWidget(count)
        row1.addWidget(QLabel(tr("Задержка")))
        delay = self._make_delay_spin(font)
        row1.addWidget(delay)
        row1.addStretch()

        dlc.valueChanged.connect(lambda value: self._set_data_enabled(data, value))
        self._set_data_enabled(data, dlc.value())

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.addWidget(QLabel(tr("Задержка мс")))
        delay2 = self._make_delay_spin(font)
        row2.addWidget(delay2)
        row2.addStretch()

        delay.valueChanged.connect(delay2.setValue)
        delay2.valueChanged.connect(delay.setValue)

        group_layout.addLayout(row1)
        group_layout.addLayout(row2)

        return {
            "group": group,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "data": data,
            "count": count,
            "delay": delay,
            "delay2": delay2,
        }

    def _create_cache_block(self, font: QFont) -> Dict[str, Any]:
        group = QGroupBox(tr("Кэш"))
        group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(4)
        group_layout.setContentsMargins(6, 6, 6, 6)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel(tr("Бит")))
        bit = self._make_bit_combo(font)
        row1.addWidget(bit)
        row1.addWidget(QLabel("ID"))
        can_id = self._make_id_edit(font, bit)
        row1.addWidget(can_id)
        row1.addWidget(QLabel("DLC"))
        dlc = self._make_dlc_spin(font)
        row1.addWidget(dlc)
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
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "from_data": from_data,
            "to_data": to_data,
        }

    def _set_data_enabled(self, edits: List[QLineEdit], count: int) -> None:
        for i, edit in enumerate(edits):
            edit.setEnabled(i < count)

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        self._apply_button = QPushButton(tr("Применить триггеры"))
        self._apply_button.setFixedSize(140, 32)
        self._apply_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._apply_button.clicked.connect(self._apply_triggers)

        self._save_button = QPushButton(tr("Сохранить триггеры"))
        self._save_button.setFixedSize(140, 32)
        self._save_button.setFont(QFont("Segoe UI", 10))
        self._save_button.clicked.connect(self._save_triggers)

        self._load_button = QPushButton(tr("Загрузить триггеры"))
        self._load_button.setFixedSize(150, 32)
        self._load_button.setFont(QFont("Segoe UI", 10))
        self._load_button.clicked.connect(self._load_triggers)

        for i in range(TRIGGER_COUNT):
            group = QGroupBox(tr("Триггер {0}").format(i + 1))
            group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

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
            responses = [self._create_response_block(font, idx) for idx in range(RESPONSE_COUNT)]
            cache = self._create_cache_block(font)

            block = {
                "group": group,
                "active": active,
                "cache_check": cache_check,
                "cache_active": cache_active,
                "recv": recv,
                "responses": responses,
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

            top = QHBoxLayout()
            top.addWidget(block["active"])
            top.addWidget(block["cache_check"])
            top.addWidget(block["cache_active"])
            top.addStretch()
            group_layout.addLayout(top)

            group_layout.addLayout(block["recv"]["layout"])

            for response in block["responses"]:
                group_layout.addWidget(response["group"])

            group_layout.addWidget(block["cache"]["group"])
            self._set_cache_enabled(block, False)

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
        for response in block["responses"]:
            response["group"].setEnabled(not enabled)
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
                "responses": self._collect_responses(block["responses"]),
                "cache_data": self._collect_cache(block["cache"]),
            })
        return triggers

    def _collect_responses(self, responses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for response in responses:
            can_id = self._parse_id(response["id"].text())
            if can_id is None:
                continue
            result.append({
                "channel": response["channel"].currentIndex(),
                "id": can_id,
                "data": self._parse_data(response["data"]),
                "count": response["count"].value(),
                "delay": response["delay"].value(),
            })
        return result

    def _collect_cache(self, cache: Dict[str, Any]) -> Dict[str, Any]:
        can_id = self._parse_id(cache["id"].text())
        return {
            "id": can_id,
            "channel": 0,
            "data_from": self._parse_data(cache["from_data"]),
            "data_to": self._parse_data(cache["to_data"]),
            "dlc": cache["dlc"].value(),
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
            for response in block["responses"]:
                responses.append({
                    "channel": response["channel"].currentIndex(),
                    "bit": response["bit"].currentIndex(),
                    "id": response["id"].text(),
                    "dlc": response["dlc"].value(),
                    "data": " ".join(e.text() for e in response["data"] if e.text()),
                    "count": response["count"].value(),
                    "delay": response["delay"].value(),
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
                "cache_bit": cache["bit"].currentIndex(),
                "cache_id": cache["id"].text(),
                "cache_dlc": cache["dlc"].value(),
                "cache_from_data": " ".join(e.text() for e in cache["from_data"] if e.text()),
                "cache_to_data": " ".join(e.text() for e in cache["to_data"] if e.text()),
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
            for r, response in enumerate(block["responses"]):
                responses = trigger.get("responses", [])
                data = responses[r] if r < len(responses) else {}
                self._set_response(response, data)
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

    def _set_response(self, response: Dict[str, Any], data: Dict[str, Any]) -> None:
        response["channel"].setCurrentIndex(int(data.get("channel", 0)))
        response["bit"].setCurrentIndex(int(data.get("bit", 0)))
        response["id"].setText(str(data.get("id", "")))
        response["dlc"].setValue(int(data.get("dlc", 8)))
        bytes_data = parse_data_bytes(str(data.get("data", "")).split())
        for d, edit in enumerate(response["data"]):
            edit.setText(f"{bytes_data[d]:02X}" if d < len(bytes_data) else "")
        self._set_data_enabled(response["data"], response["dlc"].value())
        response["count"].setValue(int(data.get("count", 1)))
        delay = int(data.get("delay", 0))
        response["delay"].setValue(delay)
        response["delay2"].setValue(delay)

    def _set_cache(self, cache: Dict[str, Any], data: Dict[str, Any]) -> None:
        cache["bit"].setCurrentIndex(int(data.get("cache_bit", 0)))
        cache["id"].setText(str(data.get("cache_id", "")))
        cache["dlc"].setValue(int(data.get("cache_dlc", 8)))
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

    def _data_from_edits(self, edits: List[QLineEdit]) -> bytes:
        parsed = self._parse_data(edits)
        data = bytearray(8)
        for idx, val in enumerate(parsed):
            if val is not None and idx < 8:
                data[idx] = val & 0xFF
        return bytes(data)

    def _send_frame(self, can_id: int, data: bytes, channel_index: int, count: int) -> None:
        if not self._serial_manager.is_open():
            return
        for _ in range(count):
            if channel_index == 0:
                frame = pack_can_frame(1, can_id, data)
                self._serial_manager.send_data(frame)
            elif channel_index == 1:
                frame = pack_can_frame(2, can_id, data)
                self._serial_manager.send_data(frame)
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
            if not self._match_condition(trigger, frame_id, frame_channel, data):
                continue
            if trigger["cache"]:
                self._send_cache_response(trigger, data)
            else:
                for response in trigger["responses"]:
                    row_data = bytearray(8)
                    for b, val in enumerate(response["data"]):
                        if val is not None and b < 8:
                            row_data[b] = val & 0xFF
                    self._send_frame(response["id"], bytes(row_data), response["channel"], response["count"])
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

    def _send_cache_response(self, trigger: Dict[str, Any], data: bytes) -> None:
        cache = trigger["cache_data"]
        if cache["id"] is None:
            return
        for i, (from_val, to_val) in enumerate(zip(cache["data_from"], cache["data_to"])):
            if i >= len(data):
                break
            if from_val is not None and data[i] < from_val:
                return
            if to_val is not None and data[i] > to_val:
                return
        self._send_frame(cache["id"], data, 0, 1)

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
