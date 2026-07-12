"""Страница «Гибкая логика» с правилами if-then для CAN-кадров."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes
from ui.can_monitor_tab import DbcSignalDialog
from ui.ui_utils import setup_button

logger = get_logger(__name__)


class RuleEditDialog(QDialog):
    """Диалог добавления/редактирования правила гибкой логики."""

    def __init__(self, rule: Optional[Dict[str, object]] = None, parent: Optional[QWidget] = None) -> None:
        """Создаёт диалог.

        Args:
            rule: Существующее правило или None.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._rule = rule or {}
        self.setWindowTitle(tr("Правило") if rule is None else tr("Редактирование правила"))
        self.setMinimumWidth(360)
        self._create_widgets()
        self._build_layout()
        self._load_data()

    def _create_widgets(self) -> None:
        """Создаёт элементы диалога."""
        font = QFont("Segoe UI", 10)

        self._active_check = QCheckBox(tr("Активно"))
        self._active_check.setFont(font)

        self._id_edit = QLineEdit()
        self._id_edit.setFont(font)
        self._id_edit.setPlaceholderText(tr("ID HEX"))
        self._id_edit.setMaxLength(8)

        self._mask_edit = QLineEdit()
        self._mask_edit.setFont(font)
        self._mask_edit.setPlaceholderText(tr("FF 00 FF ... (8 байт)"))
        self._mask_edit.setToolTip(tr("Маска: FF — проверять байт, 00 — игнорировать. Должна быть 8 байт."))

        self._condition_data_edit = QLineEdit()
        self._condition_data_edit.setFont(font)
        self._condition_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._condition_data_edit.setToolTip(tr("Эталонные данные для сравнения по маске (8 байт)."))

        self._from_dbc_button = QPushButton(tr("Из DBC"))
        setup_button(self._from_dbc_button, height=26)
        self._from_dbc_button.clicked.connect(self._on_from_dbc)

        self._resp_id_edit = QLineEdit()
        self._resp_id_edit.setFont(font)
        self._resp_id_edit.setPlaceholderText(tr("ID HEX"))
        self._resp_id_edit.setMaxLength(8)

        self._resp_data_edit = QLineEdit()
        self._resp_data_edit.setFont(font)
        self._resp_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._resp_data_edit.setToolTip(tr("Данные ответа. Байты заменяются по маске ответа, если она задана."))

        self._resp_mask_edit = QLineEdit()
        self._resp_mask_edit.setFont(font)
        self._resp_mask_edit.setPlaceholderText(tr("FF FF ... (8 байт)"))
        self._resp_mask_edit.setToolTip(tr("Маска ответа: FF — заменить на resp_data, 00 — оставить из входящего пакета."))

        self._resp_channel_edit = QLineEdit()
        self._resp_channel_edit.setFont(font)
        self._resp_channel_edit.setPlaceholderText(tr("1 или 2 (пусто = тот же канал)"))
        self._resp_channel_edit.setFixedWidth(160)

        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(0, 10000)
        self._delay_spin.setValue(0)
        self._delay_spin.setSuffix(tr(" мс"))
        self._delay_spin.setFont(font)

        self._save_button = QPushButton(tr("Сохранить"))
        setup_button(self._save_button, height=30)
        self._save_button.clicked.connect(self.accept)

        self._cancel_button = QPushButton(tr("Отмена"))
        setup_button(self._cancel_button, height=30)
        self._cancel_button.clicked.connect(self.reject)

    def _build_layout(self) -> None:
        """Собирает компоновку диалога."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(self._active_check)
        layout.addWidget(QLabel(tr("ID условия:")))
        id_layout = QHBoxLayout()
        id_layout.addWidget(self._id_edit)
        id_layout.addWidget(self._from_dbc_button)
        id_layout.addStretch()
        layout.addLayout(id_layout)
        layout.addWidget(QLabel(tr("Маска данных (8 байт):")))
        layout.addWidget(self._mask_edit)
        layout.addWidget(QLabel(tr("Эталонные данные условия (8 байт):")))
        layout.addWidget(self._condition_data_edit)
        layout.addWidget(QLabel(tr("ID ответа:")))
        layout.addWidget(self._resp_id_edit)
        layout.addWidget(QLabel(tr("Данные ответа (8 байт):")))
        layout.addWidget(self._resp_data_edit)
        layout.addWidget(QLabel(tr("Маска замены ответа (8 байт):")))
        layout.addWidget(self._resp_mask_edit)
        layout.addWidget(QLabel(tr("Канал ответа (пусто = тот же):")))
        layout.addWidget(self._resp_channel_edit)
        layout.addWidget(QLabel(tr("Задержка ответа:")))
        layout.addWidget(self._delay_spin)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._cancel_button)
        buttons_layout.addWidget(self._save_button)
        layout.addLayout(buttons_layout)

    def _load_data(self) -> None:
        """Заполняет поля данными правила."""
        self._active_check.setChecked(self._rule.get("active", True))
        self._id_edit.setText(self._rule.get("id", ""))
        self._mask_edit.setText(self._rule.get("mask", ""))
        self._condition_data_edit.setText(self._rule.get("condition_data", ""))
        self._resp_id_edit.setText(self._rule.get("resp_id", ""))
        self._resp_data_edit.setText(self._rule.get("resp_data", ""))
        self._resp_mask_edit.setText(self._rule.get("resp_mask", ""))
        self._resp_channel_edit.setText(self._rule.get("resp_channel", ""))
        self._delay_spin.setValue(int(self._rule.get("delay", 0)))

    def _on_from_dbc(self) -> None:
        """Заполняет ID и данные условия из выбранного DBC-сигнала."""
        dialog = DbcSignalDialog(self)
        if dialog.exec() != 1:  # QDialog.DialogCode.Accepted == 1
            return
        result = dialog.get_result()
        if result is None:
            return
        can_id, data = result
        self._id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        self._mask_edit.setText("FF " * 8)
        self._condition_data_edit.setText(" ".join(f"{b:02X}" for b in data))

    def get_rule(self) -> Optional[Dict[str, object]]:
        """Возвращает правило из полей диалога."""
        can_id = hex_to_int(self._id_edit.text())
        if can_id is None:
            QMessageBox.warning(self, tr("Ошибка"), tr("Неверный ID условия"))
            return None
        resp_id = hex_to_int(self._resp_id_edit.text())
        if resp_id is None:
            resp_id = can_id
        return {
            "active": self._active_check.isChecked(),
            "id": self._id_edit.text().strip(),
            "mask": self._mask_edit.text().strip(),
            "condition_data": self._condition_data_edit.text().strip(),
            "resp_id": int_to_hex(resp_id, 8),
            "resp_data": self._resp_data_edit.text().strip(),
            "resp_mask": self._resp_mask_edit.text().strip(),
            "resp_channel": self._resp_channel_edit.text().strip(),
            "delay": self._delay_spin.value(),
        }


class FlexibleLogicTab(QWidget):
    """Страница гибкой логики с таблицей правил/скриптов."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт страницу гибкой логики.

        Args:
            serial_manager: Менеджер COM-порта для отправки ответов.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._active = False
        self._rules: List[Dict[str, object]] = []
        self._rule_counters: List[int] = []
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки страницы гибкой логики (заглушка)."""
        pass

    def _create_widgets(self) -> None:
        """Создаёт элементы управления страницы."""
        self._title = QLabel(tr("Гибкая логика"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._subtitle = QLabel(tr("Правила if-then для CAN-кадров"))
        self._subtitle.setFont(QFont("Segoe UI", 11))

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([tr("№"), tr("Условие"), tr("Действие"), tr("Статус"), tr("Срабатываний")])
        self._table.setFont(QFont("Segoe UI", 10))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 220)
        self._table.setColumnWidth(2, 220)
        self._table.setColumnWidth(3, 70)
        self._table.setColumnWidth(4, 90)

        self._add_button = QPushButton(tr("Добавить"))
        setup_button(self._add_button, height=28)
        self._add_button.clicked.connect(self._on_add)
        self._edit_button = QPushButton(tr("Редактировать"))
        setup_button(self._edit_button, height=28)
        self._edit_button.clicked.connect(self._on_edit)
        self._delete_button = QPushButton(tr("Удалить"))
        setup_button(self._delete_button, height=28)
        self._delete_button.clicked.connect(self._on_delete)

        self._apply_button = QPushButton(tr("Применить правила"))
        setup_button(self._apply_button, bold=True, height=30)
        self._apply_button.clicked.connect(self._apply_rules)

        self._stop_button = QPushButton(tr("Остановить"))
        setup_button(self._stop_button, height=30)
        self._stop_button.clicked.connect(self._stop_rules)

        self._save_button = QPushButton(tr("Сохранить в файл"))
        setup_button(self._save_button, height=28)
        self._save_button.clicked.connect(self._save_rules_to_file)

        self._load_button = QPushButton(tr("Загрузить из файла"))
        setup_button(self._load_button, height=28)
        self._load_button.clicked.connect(self._load_rules_from_file)

    def _build_layout(self) -> None:
        """Собирает компоновку страницы."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)
        layout.addWidget(self._subtitle)
        layout.addWidget(self._table)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._add_button)
        buttons_layout.addWidget(self._edit_button)
        buttons_layout.addWidget(self._delete_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._apply_button)
        buttons_layout.addWidget(self._stop_button)
        layout.addLayout(buttons_layout)

        file_layout = QHBoxLayout()
        file_layout.setSpacing(8)
        file_layout.addStretch()
        file_layout.addWidget(self._save_button)
        file_layout.addWidget(self._load_button)
        layout.addLayout(file_layout)

    def _load_config(self) -> None:
        """Загружает правила из конфигурации."""
        rules = self._config.get("flexible_rules", [])
        if not isinstance(rules, list):
            rules = []
        self._rules = rules
        self._rule_counters = [0] * len(rules)
        self._refresh_table()

    def _save_config(self) -> None:
        """Сохраняет правила в конфигурацию."""
        self._config.set("flexible_rules", self._rules)

    def get_config(self) -> List[Dict[str, object]]:
        """Возвращает текущие правила для экспорта."""
        self._save_config()
        return self._config.get("flexible_rules", [])

    def set_config(self, rules: List[Dict[str, object]]) -> None:
        """Загружает правила из импортированного профиля."""
        self._config.set("flexible_rules", rules)
        self._load_config()

    def _refresh_table(self) -> None:
        """Обновляет таблицу правил."""
        self._table.setRowCount(len(self._rules))
        for i, rule in enumerate(self._rules):
            active = rule.get("active", False)
            status = tr("Активно") if active else tr("Неактивно")
            condition = f"ID: {rule.get('id', '-')} | {rule.get('mask', '')} | {rule.get('condition_data', '')}"
            action = f"ID: {rule.get('resp_id', '-')} | {rule.get('resp_data', '')}"
            counter = self._rule_counters[i] if i < len(self._rule_counters) else 0

            item = QTableWidgetItem(str(i + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 0, item)
            self._table.setItem(i, 1, QTableWidgetItem(condition))
            self._table.setItem(i, 2, QTableWidgetItem(action))
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 3, status_item)
            counter_item = QTableWidgetItem(str(counter))
            counter_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 4, counter_item)

    def _on_add(self) -> None:
        """Открывает диалог добавления правила."""
        dialog = RuleEditDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule = dialog.get_rule()
            if rule is not None:
                self._rules.append(rule)
                self._rule_counters.append(0)
                self._save_config()
                self._refresh_table()

    def _on_edit(self) -> None:
        """Открывает диалог редактирования выбранного правила."""
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rules):
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите правило для редактирования"))
            return
        dialog = RuleEditDialog(self._rules[row], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule = dialog.get_rule()
            if rule is not None:
                self._rules[row] = rule
                self._save_config()
                self._refresh_table()

    def _on_delete(self) -> None:
        """Удаляет выбранное правило."""
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rules):
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите правило для удаления"))
            return
        del self._rules[row]
        if row < len(self._rule_counters):
            del self._rule_counters[row]
        self._save_config()
        self._refresh_table()

    def _build_internal_rules(self) -> None:
        """Формирует внутренний список активных правил для обработки кадров."""
        self._internal_rules = []
        for i, rule in enumerate(self._rules):
            if not rule.get("active", False):
                continue
            can_id = hex_to_int(rule.get("id", ""))
            if can_id is None:
                continue
            mask = self._pad_8(parse_data_bytes(rule.get("mask", "").split()))
            condition_data = self._pad_8(parse_data_bytes(rule.get("condition_data", "").split()))
            resp_id = hex_to_int(rule.get("resp_id", ""))
            if resp_id is None:
                resp_id = can_id
            resp_data = self._pad_8(parse_data_bytes(rule.get("resp_data", "").split()))
            resp_mask = self._pad_8(parse_data_bytes(rule.get("resp_mask", "").split()))
            try:
                delay_ms = int(rule.get("delay", 0))
            except ValueError:
                delay_ms = 0
            try:
                resp_channel = int(rule.get("resp_channel", "")) if rule.get("resp_channel", "") else None
            except ValueError:
                resp_channel = None
            self._internal_rules.append({
                "index": i,
                "id": can_id,
                "mask": mask,
                "condition_data": condition_data,
                "resp_id": resp_id,
                "resp_data": resp_data,
                "resp_mask": resp_mask,
                "resp_channel": resp_channel,
                "delay": max(0, delay_ms),
            })

    @staticmethod
    def _pad_8(data: List[int]) -> List[int]:
        """Дополняет или обрезает список до 8 байт."""
        data = data[:8]
        return data + [0] * (8 - len(data))

    def _apply_rules(self) -> None:
        """Активирует обработку правил."""
        self._save_config()
        self._build_internal_rules()
        self._active = True
        logger.info("Гибкая логика применена: %d активных правил", len(self._internal_rules))
        QMessageBox.information(self, tr("Гибкая логика"), tr("Правила применены и активны"))

    def _stop_rules(self) -> None:
        """Останавливает обработку правил."""
        self._active = False
        logger.info("Обработка гибкой логики остановлена")
        QMessageBox.information(self, tr("Гибкая логика"), tr("Обработка правил остановлена"))

    def _send_rule_response(self, resp_frame: bytes) -> None:
        """Отправляет подготовленный ответный кадр."""
        self._serial_manager.send_data(resp_frame)

    def set_dbc(self, dbc_manager) -> None:
        """Обновляет логику при смене DBC (заглушка)."""
        pass

    def process_frame(self, frame: Dict[str, object]) -> None:
        """Проверяет входящий кадр на совпадение с активными правилами."""
        if not self._active:
            return
        if not getattr(self, "_internal_rules", []):
            return

        frame_id = int(frame["id"])
        frame_data = self._pad_8(list(bytes(frame["data"])))
        frame_channel = int(frame["channel"])

        for rule in self._internal_rules:
            if rule["id"] != frame_id:
                continue
            # Проверка данных по маске
            match = True
            for i in range(8):
                if rule["mask"][i] and (frame_data[i] & rule["mask"][i]) != (rule["condition_data"][i] & rule["mask"][i]):
                    match = False
                    break
            if not match:
                continue

            idx = rule["index"]
            self._rule_counters[idx] += 1
            counter_item = self._table.item(idx, 4)
            if counter_item is not None:
                counter_item.setText(str(self._rule_counters[idx]))

            # Формируем ответные данные: по resp_mask берём из resp_data, иначе из входящего кадра
            resp_data = [
                (rule["resp_data"][i] & rule["resp_mask"][i]) | (frame_data[i] & (0xFF ^ rule["resp_mask"][i]))
                for i in range(8)
            ]
            channel = rule["resp_channel"] if rule["resp_channel"] in (1, 2) else frame_channel
            resp_frame = pack_can_frame(channel, rule["resp_id"], bytes(resp_data))
            delay_ms = rule.get("delay", 0)
            if delay_ms > 0:
                QTimer.singleShot(delay_ms, lambda rf=resp_frame: self._send_rule_response(rf))
            else:
                self._send_rule_response(resp_frame)
            logger.info(
                "Сработало правило гибкой логики: ID=0x%s -> ответ ID=0x%s в канал %d (задержка %d мс)",
                int_to_hex(frame_id, 8),
                int_to_hex(rule["resp_id"], 8),
                channel,
                delay_ms,
            )

    def _save_rules_to_file(self) -> None:
        """Сохраняет правила в JSON-файл."""
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить правила"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            self._save_config()
            Path(path).write_text(json.dumps(self._rules, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Правила гибкой логики сохранены в %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения правил: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить правила: {0}").format(exc))

    def _load_rules_from_file(self) -> None:
        """Загружает правила из JSON-файла."""
        path, _ = QFileDialog.getOpenFileName(self, tr("Загрузить правила"), "", "JSON files (*.json)")
        if not path:
            return
        try:
            rules = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(rules, list):
                raise ValueError(tr("Файл должен содержать список правил"))
            self._rules = rules
            self._rule_counters = [0] * len(rules)
            self._save_config()
            self._refresh_table()
            logger.info("Правила гибкой логики загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки правил: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить правила: {0}").format(exc))
