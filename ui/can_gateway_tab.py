"""Вкладка «CAN-шлюз» для ретрансляции и подмены кадров между каналами."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes
from ui.ui_utils import setup_button
from ui.memory_indicator import MemoryIndicator

logger = get_logger(__name__)

RULE_COUNT = 10
IGNORE_COUNT = 10

DIRECTIONS = [tr("Из CAN1 в CAN2"), tr("Из CAN2 в CAN1")]


class CanGatewayTab(QWidget):
    """Вкладка CAN-шлюза: ретрансляция, игнорирование и подмена кадров."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._running = False
        self._ignore_edits: List[QLineEdit] = []
        self._rule_blocks: List[Dict[str, Any]] = []
        self._memory_indicator = MemoryIndicator(self)
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки шлюза (заглушка для полной локализации)."""
        pass

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        # Секция игнорирования
        self._ignore_group = QGroupBox(tr("Игнорировать"))
        self._ignore_group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        ignore_layout = QGridLayout(self._ignore_group)
        ignore_layout.setSpacing(6)
        for i in range(IGNORE_COUNT):
            edit = QLineEdit()
            edit.setFixedWidth(80)
            edit.setFont(font)
            edit.setMaxLength(8)
            edit.setPlaceholderText(tr("ID игнор."))
            edit.textChanged.connect(self._on_ignore_changed)
            self._ignore_edits.append(edit)
            row = i % 5
            col = i // 5
            ignore_layout.addWidget(edit, row, col)

        self._ignore_check = QCheckBox(tr("Включить игнорирование"))
        self._ignore_check.setFont(font)
        self._ignore_check.setEnabled(False)
        ignore_layout.addWidget(self._ignore_check, 5, 0, 1, 2)

        # Секция правил подмены
        self._rules_group = QGroupBox(tr("Правила подмены"))
        self._rules_group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        rules_layout = QVBoxLayout(self._rules_group)
        rules_layout.setSpacing(8)

        for i in range(RULE_COUNT):
            block = self._create_rule_block(i, font)
            self._rule_blocks.append(block)
            rules_layout.addWidget(block["frame"])

        # Кнопки управления
        self._start_button = QPushButton(tr("Запустить шлюз"))
        setup_button(self._start_button, bold=True, height=34)
        self._start_button.clicked.connect(self._start)

        self._stop_button = QPushButton(tr("Остановить"))
        setup_button(self._stop_button, height=34)
        self._stop_button.clicked.connect(self._stop)

        self._save_button = QPushButton(tr("Сохранить правила"))
        setup_button(self._save_button, height=28)
        self._save_button.clicked.connect(self._save_rules)

        self._load_button = QPushButton(tr("Загрузить правила"))
        setup_button(self._load_button, height=28)
        self._load_button.clicked.connect(self._load_rules)

    def _create_rule_block(self, index: int, font: QFont) -> Dict[str, Any]:
        frame = QGroupBox(tr("Правило {0}").format(index + 1))
        frame.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        frame.setStyleSheet("QGroupBox { border: 1px solid #444444; margin-top: 6px; padding-top: 6px; }")
        layout = QVBoxLayout(frame)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 6, 6, 6)

        active = QCheckBox(tr("Активен"))
        active.setFont(font)

        recv_id = QLineEdit()
        recv_id.setFixedWidth(90)
        recv_id.setFont(font)
        recv_id.setMaxLength(8)
        recv_id.setPlaceholderText(tr("ID"))

        recv_data: List[QLineEdit] = []
        for d in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(36)
            edit.setFont(font)
            edit.setMaxLength(2)
            edit.setPlaceholderText(f"D{d}")
            recv_data.append(edit)

        replace_id = QLineEdit()
        replace_id.setFixedWidth(90)
        replace_id.setFont(font)
        replace_id.setMaxLength(8)
        replace_id.setPlaceholderText(tr("ID"))

        replace_data: List[QLineEdit] = []
        for d in range(8):
            edit = QLineEdit()
            edit.setFixedWidth(36)
            edit.setFont(font)
            edit.setMaxLength(2)
            edit.setPlaceholderText(f"D{d}")
            replace_data.append(edit)

        direction = QComboBox()
        direction.setFont(font)
        direction.addItems(DIRECTIONS)
        direction.setFixedWidth(160)

        recv_layout = QHBoxLayout()
        recv_layout.setSpacing(4)
        recv_layout.addWidget(QLabel(tr("Прием:")))
        recv_layout.addWidget(recv_id)
        for edit in recv_data:
            recv_layout.addWidget(edit)
        recv_layout.addStretch()

        replace_layout = QHBoxLayout()
        replace_layout.setSpacing(4)
        replace_layout.addWidget(QLabel(tr("Подмена:")))
        replace_layout.addWidget(replace_id)
        for edit in replace_data:
            replace_layout.addWidget(edit)
        replace_layout.addStretch()

        options_layout = QHBoxLayout()
        options_layout.addWidget(active)
        options_layout.addWidget(QLabel(tr("Направление:")))
        options_layout.addWidget(direction)
        options_layout.addStretch()

        layout.addLayout(recv_layout)
        layout.addLayout(replace_layout)
        layout.addLayout(options_layout)

        return {
            "frame": frame,
            "active": active,
            "recv_id": recv_id,
            "recv_data": recv_data,
            "replace_id": replace_id,
            "replace_data": replace_data,
            "direction": direction,
        }

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(tr("CAN-шлюз"))
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setProperty("title", True)
        layout.addWidget(title)

        layout.addWidget(self._ignore_group)

        rules_scroll = QScrollArea()
        rules_scroll.setWidgetResizable(True)
        rules_scroll.setWidget(self._rules_group)
        rules_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        layout.addWidget(rules_scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._stop_button)
        buttons.addStretch()
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._load_button)
        layout.addLayout(buttons)
        layout.addWidget(self._memory_indicator)

    def _on_ignore_changed(self) -> None:
        has_value = any(edit.text().strip() for edit in self._ignore_edits)
        self._ignore_check.setEnabled(has_value)
        if not has_value:
            self._ignore_check.setChecked(False)

    def _load_config(self) -> None:
        rules = self._config.get("gateway_rules", [])
        if not isinstance(rules, list):
            rules = []
        for i in range(RULE_COUNT):
            rule = rules[i] if i < len(rules) else {}
            block = self._rule_blocks[i]
            block["active"].setChecked(rule.get("active", False))
            block["recv_id"].setText(rule.get("recv_id", ""))
            data = parse_data_bytes(rule.get("recv_data", "").split())
            for d, edit in enumerate(block["recv_data"]):
                edit.setText(f"{data[d]:02X}" if d < len(data) else "")
            block["replace_id"].setText(rule.get("replace_id", ""))
            rdata = parse_data_bytes(rule.get("replace_data", "").split())
            for d, edit in enumerate(block["replace_data"]):
                edit.setText(f"{rdata[d]:02X}" if d < len(rdata) else "")
            direction = int(rule.get("direction", 0))
            block["direction"].setCurrentIndex(direction if 0 <= direction < len(DIRECTIONS) else 0)

        ignore_ids = self._config.get("gateway_ignore", [])
        if not isinstance(ignore_ids, list):
            ignore_ids = []
        for i in range(IGNORE_COUNT):
            self._ignore_edits[i].setText(str(ignore_ids[i]) if i < len(ignore_ids) else "")
        self._on_ignore_changed()

    def _save_config(self) -> None:
        rules = []
        for block in self._rule_blocks:
            recv_data = " ".join(edit.text().strip() for edit in block["recv_data"] if edit.text().strip())
            replace_data = " ".join(edit.text().strip() for edit in block["replace_data"] if edit.text().strip())
            rules.append({
                "active": block["active"].isChecked(),
                "recv_id": block["recv_id"].text().strip(),
                "recv_data": recv_data,
                "replace_id": block["replace_id"].text().strip(),
                "replace_data": replace_data,
                "direction": block["direction"].currentIndex(),
            })
        self._config.set("gateway_rules", rules)
        self._config.set("gateway_ignore", [edit.text().strip() for edit in self._ignore_edits])
        self._memory_indicator.update_usage(self._memory_indicator.estimate_rules(rules))

    def _parse_id(self, text: str) -> Optional[int]:
        return hex_to_int(text.strip())

    def _parse_data(self, edits: List[QLineEdit]) -> List[Optional[int]]:
        """Возвращает список 8 значений; None для пустых полей."""
        result: List[Optional[int]] = []
        for edit in edits:
            text = edit.text().strip()
            if text:
                val = hex_to_int(text)
                result.append(val if val is not None else 0)
            else:
                result.append(None)
        return result

    def _build_internal_rules(self) -> List[Dict[str, Any]]:
        rules = []
        for i, block in enumerate(self._rule_blocks):
            if not block["active"].isChecked():
                continue
            recv_id = self._parse_id(block["recv_id"].text())
            if recv_id is None:
                continue
            recv_data = self._parse_data(block["recv_data"])
            replace_id = self._parse_id(block["replace_id"].text())
            if replace_id is None:
                replace_id = recv_id
            replace_data = self._parse_data(block["replace_data"])
            direction = block["direction"].currentIndex()  # 0 = 1->2, 1 = 2->1
            source_channel = 1 if direction == 0 else 2
            target_channel = 2 if direction == 0 else 1
            rules.append({
                "index": i,
                "recv_id": recv_id,
                "recv_data": recv_data,
                "replace_id": replace_id,
                "replace_data": replace_data,
                "source_channel": source_channel,
                "target_channel": target_channel,
            })
        return rules

    def _build_ignore_set(self) -> set[int]:
        ids: set[int] = set()
        if not self._ignore_check.isChecked():
            return ids
        for edit in self._ignore_edits:
            value = self._parse_id(edit.text())
            if value is not None:
                ids.add(value)
        return ids

    def _start(self) -> None:
        self._save_config()
        self._internal_rules = self._build_internal_rules()
        self._ignore_set = self._build_ignore_set()
        self._running = True
        logger.info("CAN-шлюз запущен: %d правил, игнорирование %s", len(self._internal_rules), "включено" if self._ignore_set else "выключено")
        QMessageBox.information(self, tr("CAN-шлюз"), tr("Шлюз запущен"))

    def _stop(self) -> None:
        self._running = False
        logger.info("CAN-шлюз остановлен")
        QMessageBox.information(self, tr("CAN-шлюз"), tr("Шлюз остановлен"))

    def process_frame(self, frame: Dict[str, Any]) -> None:
        if not self._running:
            return
        frame_id = int(frame["id"])
        frame_channel = int(frame["channel"])
        data = bytes(frame["data"])

        if frame_id in self._ignore_set:
            return

        for rule in self._internal_rules:
            if rule["source_channel"] != frame_channel:
                continue
            if rule["recv_id"] != frame_id:
                continue
            match = True
            for idx, expected in enumerate(rule["recv_data"]):
                if expected is None:
                    continue
                if idx >= len(data) or data[idx] != expected:
                    match = False
                    break
            if not match:
                continue

            # Подмена
            new_id = rule["replace_id"]
            new_data = bytearray(8)
            for idx, val in enumerate(rule["replace_data"]):
                if val is not None:
                    new_data[idx] = val & 0xFF
                elif idx < len(data):
                    new_data[idx] = data[idx]
            out_frame = pack_can_frame(rule["target_channel"], new_id, bytes(new_data))
            self._serial_manager.send_data(out_frame)
            logger.debug("Шлюз: подмена ID=0x%X -> 0x%X в канал %d", frame_id, new_id, rule["target_channel"])
            return

        # Ретрансляция без изменений
        target_channel = 2 if frame_channel == 1 else 1
        out_frame = pack_can_frame(target_channel, frame_id, data)
        self._serial_manager.send_data(out_frame)
        logger.debug("Шлюз: ретрансляция ID=0x%X из CAN%d в CAN%d", frame_id, frame_channel, target_channel)

    def _save_rules(self) -> None:
        self._save_config()
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить правила"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._config.get("gateway_rules", []), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Правила шлюза сохранены в %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения правил шлюза: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить правила: {0}").format(exc))

    def _load_rules(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Загрузить правила"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            rules = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(rules, list):
                raise ValueError(tr("Файл должен содержать список правил"))
            self._config.set("gateway_rules", rules)
            self._load_config()
            logger.info("Правила шлюза загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки правил шлюза: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить правила: {0}").format(exc))
