"""Страница «Триггеры» с 10 независимыми блоками условий и ответов."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
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


class CanTriggerTab(QWidget):
    """Страница управления триггерами CAN с 10 блоками."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._active = False
        self._internal_triggers: List[Dict[str, object]] = []
        self._trigger_counters = [0] * TRIGGER_COUNT
        self._blocks: List[Dict[str, QWidget]] = []

        self._create_widgets()
        self._build_layout()
        self._load_config()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)
        for i in range(TRIGGER_COUNT):
            group = QGroupBox(tr("Триггер {0}").format(i + 1))
            group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

            active = QCheckBox(tr("Активен"))
            active.setFont(font)

            counter = QLabel(tr("Срабатываний: 0"))
            counter.setFont(font)

            test_btn = QPushButton(tr("Тест"))
            test_btn.setFixedSize(60, 26)
            test_btn.setFont(font)
            test_btn.clicked.connect(lambda _=False, idx=i: self._on_test(idx))

            id_edit = QLineEdit()
            id_edit.setFixedWidth(80)
            id_edit.setFont(font)
            id_edit.setMaxLength(8)
            id_edit.setPlaceholderText(tr("ID"))

            data_edits = []
            data_layout = QHBoxLayout()
            data_layout.setSpacing(4)
            for d in range(8):
                edit = QLineEdit()
                edit.setFixedWidth(34)
                edit.setFont(font)
                edit.setMaxLength(2)
                edit.setPlaceholderText(f"D{d}")
                data_edits.append(edit)
                data_layout.addWidget(edit)

            resp_id_edit = QLineEdit()
            resp_id_edit.setFixedWidth(80)
            resp_id_edit.setFont(font)
            resp_id_edit.setMaxLength(8)
            resp_id_edit.setPlaceholderText(tr("ID отв."))

            resp_data_edits = []
            resp_data_layout = QHBoxLayout()
            resp_data_layout.setSpacing(4)
            for d in range(8):
                edit = QLineEdit()
                edit.setFixedWidth(34)
                edit.setFont(font)
                edit.setMaxLength(2)
                edit.setPlaceholderText(f"D{d}")
                resp_data_edits.append(edit)
                resp_data_layout.addWidget(edit)

            delay_spin = QSpinBox()
            delay_spin.setRange(0, 10000)
            delay_spin.setValue(0)
            delay_spin.setSuffix(tr(" мс"))
            delay_spin.setFont(font)
            delay_spin.setFixedWidth(90)

            top = QHBoxLayout()
            top.addWidget(active)
            top.addStretch()
            top.addWidget(counter)
            top.addWidget(test_btn)

            condition = QHBoxLayout()
            condition.setSpacing(6)
            condition.addWidget(QLabel(tr("ID:")))
            condition.addWidget(id_edit)
            condition.addWidget(QLabel(tr("Данные:")))
            condition.addLayout(data_layout)
            condition.addStretch()

            response = QHBoxLayout()
            response.setSpacing(6)
            response.addWidget(QLabel(tr("ID отв.:")))
            response.addWidget(resp_id_edit)
            response.addWidget(QLabel(tr("Данные отв.:")))
            response.addLayout(resp_data_layout)
            response.addStretch()

            delay_layout = QHBoxLayout()
            delay_layout.addWidget(QLabel(tr("Задержка:")))
            delay_layout.addWidget(delay_spin)
            delay_layout.addStretch()

            layout = QVBoxLayout(group)
            layout.addLayout(top)
            layout.addLayout(condition)
            layout.addLayout(response)
            layout.addLayout(delay_layout)

            self._blocks.append({
                "group": group,
                "active": active,
                "counter": counter,
                "id_edit": id_edit,
                "data_edits": data_edits,
                "resp_id_edit": resp_id_edit,
                "resp_data_edits": resp_data_edits,
                "delay_spin": delay_spin,
            })

        self._apply_button = QPushButton(tr("Применить триггеры"))
        self._apply_button.setFixedSize(140, 32)
        self._apply_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._apply_button.clicked.connect(self._apply_triggers)

        self._stop_button = QPushButton(tr("Остановить"))
        self._stop_button.setFixedSize(110, 32)
        self._stop_button.setFont(QFont("Segoe UI", 10))
        self._stop_button.clicked.connect(self._stop_triggers)

        self._save_button = QPushButton(tr("Сохранить в файл"))
        self._save_button.setFixedSize(130, 28)
        self._save_button.clicked.connect(self._save_triggers_to_file)

        self._load_button = QPushButton(tr("Загрузить из файла"))
        self._load_button.setFixedSize(140, 28)
        self._load_button.clicked.connect(self._load_triggers_from_file)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(tr("Триггеры CAN"))
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setProperty("title", True)
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(10)
        for block in self._blocks:
            container_layout.addWidget(block["group"])
        container_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._apply_button)
        buttons.addWidget(self._stop_button)
        buttons.addStretch()
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._load_button)
        layout.addLayout(buttons)

    def _load_config(self) -> None:
        triggers = self._config.get("triggers", [])
        if not isinstance(triggers, list):
            triggers = []
        for i in range(TRIGGER_COUNT):
            trigger = triggers[i] if i < len(triggers) else {}
            block = self._blocks[i]
            block["active"].setChecked(trigger.get("active", False))
            block["id_edit"].setText(trigger.get("id", ""))
            data = parse_data_bytes(trigger.get("data", "").split())
            for d, edit in enumerate(block["data_edits"]):
                edit.setText(f"{data[d]:02X}" if d < len(data) else "")
            block["resp_id_edit"].setText(trigger.get("resp_id", ""))
            resp_data = parse_data_bytes(trigger.get("resp_data", "").split())
            for d, edit in enumerate(block["resp_data_edits"]):
                edit.setText(f"{resp_data[d]:02X}" if d < len(resp_data) else "")
            block["delay_spin"].setValue(int(trigger.get("delay", 0)))
        self._trigger_counters = [0] * TRIGGER_COUNT
        self._update_counter_labels()

    def _save_config(self) -> None:
        triggers = []
        for i, block in enumerate(self._blocks):
            can_id = block["id_edit"].text().strip()
            data = " ".join(edit.text().strip() for edit in block["data_edits"] if edit.text().strip())
            resp_id = block["resp_id_edit"].text().strip()
            resp_data = " ".join(edit.text().strip() for edit in block["resp_data_edits"] if edit.text().strip())
            triggers.append({
                "active": block["active"].isChecked(),
                "id": can_id,
                "data": data,
                "resp_id": resp_id,
                "resp_data": resp_data,
                "delay": block["delay_spin"].value(),
            })
        self._config.set("triggers", triggers)

    def _update_counter_labels(self) -> None:
        for i, block in enumerate(self._blocks):
            block["counter"].setText(tr("Срабатываний: {0}").format(self._trigger_counters[i]))

    def _build_internal_triggers(self) -> None:
        self._internal_triggers = []
        for i, block in enumerate(self._blocks):
            if not block["active"].isChecked():
                continue
            can_id = hex_to_int(block["id_edit"].text())
            if can_id is None:
                continue
            data = parse_data_bytes([edit.text() for edit in block["data_edits"] if edit.text().strip()])
            resp_id = hex_to_int(block["resp_id_edit"].text())
            if resp_id is None:
                resp_id = can_id
            resp_data = parse_data_bytes([edit.text() for edit in block["resp_data_edits"] if edit.text().strip()])
            self._internal_triggers.append({
                "index": i,
                "id": can_id,
                "data": data,
                "resp_id": resp_id,
                "resp_data": resp_data,
                "delay": block["delay_spin"].value(),
            })

    def _apply_triggers(self) -> None:
        self._save_config()
        self._build_internal_triggers()
        self._active = True
        self._trigger_counters = [0] * TRIGGER_COUNT
        self._update_counter_labels()
        logger.info("Триггеры CAN применены: %d активных", len(self._internal_triggers))
        QMessageBox.information(self, tr("Триггеры"), tr("Триггеры применены и активны"))

    def _stop_triggers(self) -> None:
        self._active = False
        logger.info("Обработка триггеров CAN остановлена")
        QMessageBox.information(self, tr("Триггеры"), tr("Обработка триггеров остановлена"))

    def _on_test(self, index: int) -> None:
        block = self._blocks[index]
        can_id = hex_to_int(block["id_edit"].text())
        if can_id is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Неверный ID в триггере {0}").format(index + 1))
            return
        resp_id = hex_to_int(block["resp_id_edit"].text())
        if resp_id is None:
            resp_id = can_id
        resp_data = parse_data_bytes([edit.text() for edit in block["resp_data_edits"] if edit.text().strip()])
        frame = pack_can_frame(1, resp_id, bytes(resp_data))
        self._serial_manager.send_data(frame)
        self._trigger_counters[index] += 1
        self._update_counter_labels()

    def _send_trigger_response(self, resp_frame: bytes) -> None:
        self._serial_manager.send_data(resp_frame)

    def process_frame(self, frame: Dict[str, object]) -> None:
        if not self._active or not self._internal_triggers:
            return
        frame_id = int(frame["id"])
        frame_data = bytes(frame["data"])
        for trigger in self._internal_triggers:
            if trigger["id"] != frame_id:
                continue
            if trigger["data"]:
                if len(frame_data) < len(trigger["data"]):
                    continue
                if not all(frame_data[i] == trigger["data"][i] for i in range(len(trigger["data"]))):
                    continue
            idx = trigger["index"]
            self._trigger_counters[idx] += 1
            block = self._blocks[idx]
            block["counter"].setText(tr("Срабатываний: {0}").format(self._trigger_counters[idx]))
            resp_frame = pack_can_frame(
                int(frame["channel"]),
                trigger["resp_id"],
                bytes(trigger["resp_data"]),
            )
            delay_ms = trigger.get("delay", 0)
            if delay_ms > 0:
                QTimer.singleShot(delay_ms, lambda rf=resp_frame: self._send_trigger_response(rf))
            else:
                self._send_trigger_response(resp_frame)
            logger.info(
                "Сработал триггер %d: ID=0x%s -> ответ ID=0x%s (задержка %d мс)",
                idx + 1,
                int_to_hex(frame_id, 8),
                int_to_hex(trigger["resp_id"], 8),
                delay_ms,
            )

    def get_config(self) -> List[Dict[str, object]]:
        """Возвращает текущие настройки триггеров для экспорта."""
        self._save_config()
        return self._config.get("triggers", [])

    def set_config(self, triggers: List[Dict[str, object]]) -> None:
        """Загружает настройки триггеров из импортированного профиля."""
        self._config.set("triggers", triggers)
        self._load_config()

    def create_trigger_from_packet(self, packet: Dict[str, object]) -> None:
        """Создаёт триггер из пакета мониторинга."""
        for i, block in enumerate(self._blocks):
            if block["id_edit"].text().strip():
                continue
            block["active"].setChecked(True)
            block["id_edit"].setText(packet.get("id", ""))
            data = parse_data_bytes(packet.get("data", []))
            for d, edit in enumerate(block["data_edits"]):
                edit.setText(f"{data[d]:02X}" if d < len(data) else "")
            block["resp_id_edit"].setText(packet.get("id", ""))
            self._save_config()
            self._apply_triggers()
            return
        QMessageBox.warning(self, tr("Внимание"), tr("Все 10 триггеров заняты"))

    def _save_triggers_to_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить триггеры"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.get_config(), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Триггеры сохранены в %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения триггеров: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить триггеры: {0}").format(exc))

    def _load_triggers_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Загрузить триггеры"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            triggers = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(triggers, list):
                raise ValueError(tr("Файл должен содержать список триггеров"))
            self.set_config(triggers)
            logger.info("Триггеры загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки триггеров: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить триггеры: {0}").format(exc))
