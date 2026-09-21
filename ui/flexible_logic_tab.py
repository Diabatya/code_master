"""Страница «Гибкая логика» с правилами if-then для CAN-кадров."""

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
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

from core.can_protocol import pack_can_frame
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes
from ui.can_monitor_tab import DbcSignalDialog
from ui.ui_utils import setup_button

logger = get_logger(__name__)


class RuleRowWidget(QWidget):
    """Одна строка правила: три вертикальные колонки с CAN-параметрами."""

    def __init__(self, tab: "FlexibleLogicTab", rule: dict[str, object] | None = None) -> None:
        super().__init__(tab)
        self._tab = tab
        self._rule = rule or {}
        self._create_widgets()
        self._build_layout()
        self._load_rule()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        # Условие
        self._condition_group = QGroupBox(tr("Условие"))
        self._condition_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._id_edit = QLineEdit()
        self._id_edit.setFont(font)
        self._id_edit.setPlaceholderText(tr("ID HEX"))
        self._id_edit.setMaxLength(8)
        self._mask_edit = QLineEdit()
        self._mask_edit.setFont(font)
        self._mask_edit.setPlaceholderText(tr("FF 00 FF ... (8 байт)"))
        self._condition_data_edit = QLineEdit()
        self._condition_data_edit.setFont(font)
        self._condition_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._from_dbc_button = QPushButton(tr("Из DBC"))
        self._from_dbc_button.setFont(font)
        self._from_dbc_button.clicked.connect(self._on_from_dbc)

        # Действие
        self._action_group = QGroupBox(tr("Действие"))
        self._action_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._resp_id_edit = QLineEdit()
        self._resp_id_edit.setFont(font)
        self._resp_id_edit.setPlaceholderText(tr("ID HEX"))
        self._resp_id_edit.setMaxLength(8)
        self._resp_data_edit = QLineEdit()
        self._resp_data_edit.setFont(font)
        self._resp_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._resp_mask_edit = QLineEdit()
        self._resp_mask_edit.setFont(font)
        self._resp_mask_edit.setPlaceholderText(tr("FF FF ... (8 байт)"))

        # Параметры
        self._params_group = QGroupBox(tr("Параметры"))
        self._params_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._active_check = QCheckBox(tr("Активно"))
        self._active_check.setFont(font)
        self._resp_channel_edit = QLineEdit()
        self._resp_channel_edit.setFont(font)
        self._resp_channel_edit.setPlaceholderText(tr("1 или 2"))
        self._resp_channel_edit.setFixedWidth(80)
        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(0, 10000)
        self._delay_spin.setValue(0)
        self._delay_spin.setSuffix(tr(" мс"))
        self._delay_spin.setFont(font)
        self._delay_spin.setFixedWidth(90)
        self._counter_label = QLabel("0")
        self._counter_label.setFont(font)
        self._counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._remove_button = QPushButton(tr("Удалить"))
        self._remove_button.setFont(font)
        self._remove_button.clicked.connect(self._on_remove)

    def _build_layout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        cond_layout = QVBoxLayout(self._condition_group)
        cond_layout.setSpacing(4)
        cond_layout.setContentsMargins(8, 8, 8, 8)
        cond_layout.addWidget(QLabel(tr("ID")))
        cond_layout.addWidget(self._id_edit)
        cond_layout.addWidget(QLabel(tr("Маска")))
        cond_layout.addWidget(self._mask_edit)
        cond_layout.addWidget(QLabel(tr("Данные")))
        cond_layout.addWidget(self._condition_data_edit)
        cond_layout.addWidget(self._from_dbc_button)
        cond_layout.addStretch()

        action_layout = QVBoxLayout(self._action_group)
        action_layout.setSpacing(4)
        action_layout.setContentsMargins(8, 8, 8, 8)
        action_layout.addWidget(QLabel(tr("ID ответа")))
        action_layout.addWidget(self._resp_id_edit)
        action_layout.addWidget(QLabel(tr("Данные ответа")))
        action_layout.addWidget(self._resp_data_edit)
        action_layout.addWidget(QLabel(tr("Маска замены")))
        action_layout.addWidget(self._resp_mask_edit)
        action_layout.addStretch()

        params_layout = QVBoxLayout(self._params_group)
        params_layout.setSpacing(4)
        params_layout.setContentsMargins(8, 8, 8, 8)
        params_layout.addWidget(self._active_check)
        params_layout.addWidget(QLabel(tr("Канал")))
        params_layout.addWidget(self._resp_channel_edit)
        params_layout.addWidget(QLabel(tr("Задержка")))
        params_layout.addWidget(self._delay_spin)
        params_layout.addWidget(QLabel(tr("Срабатываний")))
        params_layout.addWidget(self._counter_label)
        params_layout.addWidget(self._remove_button)
        params_layout.addStretch()

        layout.addWidget(self._condition_group, 1)
        layout.addWidget(self._action_group, 1)
        layout.addWidget(self._params_group, 1)

    def _load_rule(self) -> None:
        self._active_check.setChecked(self._rule.get("active", True))
        self._id_edit.setText(self._rule.get("id", ""))
        self._mask_edit.setText(self._rule.get("mask", ""))
        self._condition_data_edit.setText(self._rule.get("condition_data", ""))
        self._resp_id_edit.setText(self._rule.get("resp_id", ""))
        self._resp_data_edit.setText(self._rule.get("resp_data", ""))
        self._resp_mask_edit.setText(self._rule.get("resp_mask", ""))
        self._resp_channel_edit.setText(self._rule.get("resp_channel", ""))
        try:
            self._delay_spin.setValue(int(self._rule.get("delay", 0)))
        except ValueError:
            self._delay_spin.setValue(0)

    def _on_from_dbc(self) -> None:
        """Заполняет условие из выбранного DBC-сигнала."""
        dialog = DbcSignalDialog(self)
        if dialog.exec() != 1:
            return
        result = dialog.get_result()
        if result is None:
            return
        can_id, data = result
        self._id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        self._mask_edit.setText("FF " * 8)
        self._condition_data_edit.setText(" ".join(f"{b:02X}" for b in data))

    def _on_remove(self) -> None:
        self._tab._remove_row(self)

    def get_rule(self) -> dict[str, object]:
        """Собирает правило из полей строки."""
        can_id = hex_to_int(self._id_edit.text())
        id_text = self._id_edit.text().strip() if can_id is None else self._id_edit.text().strip()
        resp_id = hex_to_int(self._resp_id_edit.text())
        return {
            "active": self._active_check.isChecked(),
            "id": id_text,
            "mask": self._mask_edit.text().strip(),
            "condition_data": self._condition_data_edit.text().strip(),
            "resp_id": int_to_hex(resp_id, 8) if resp_id is not None else self._resp_id_edit.text().strip(),
            "resp_data": self._resp_data_edit.text().strip(),
            "resp_mask": self._resp_mask_edit.text().strip(),
            "resp_channel": self._resp_channel_edit.text().strip(),
            "delay": self._delay_spin.value(),
        }

    def set_counter(self, value: int) -> None:
        self._counter_label.setText(str(value))

    def retranslate_ui(self) -> None:
        self._condition_group.setTitle(tr("Условие"))
        self._action_group.setTitle(tr("Действие"))
        self._params_group.setTitle(tr("Параметры"))
        self._id_edit.setPlaceholderText(tr("ID HEX"))
        self._mask_edit.setPlaceholderText(tr("FF 00 FF ... (8 байт)"))
        self._condition_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._from_dbc_button.setText(tr("Из DBC"))
        self._resp_id_edit.setPlaceholderText(tr("ID HEX"))
        self._resp_data_edit.setPlaceholderText(tr("D0 D1 ... (8 байт)"))
        self._resp_mask_edit.setPlaceholderText(tr("FF FF ... (8 байт)"))
        self._active_check.setText(tr("Активно"))
        self._resp_channel_edit.setPlaceholderText(tr("1 или 2"))
        self._delay_spin.setSuffix(tr(" мс"))
        self._remove_button.setText(tr("Удалить"))


class FlexibleLogicTab(QWidget):
    """Вкладка гибкой логики с тремя колонками и неограниченными строками."""

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._active = False
        self._rules: list[dict[str, object]] = []
        self._rule_counters: list[int] = []
        self._row_widgets: list[RuleRowWidget] = []
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def _create_widgets(self) -> None:
        self._title = QLabel(tr("Гибкая логика"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._subtitle = QLabel(tr("Правила if-then для CAN-кадров"))
        self._subtitle.setFont(QFont("Segoe UI", 11))

        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setSpacing(8)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._rows_widget)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._add_button = QPushButton(tr("Добавить правило"))
        setup_button(self._add_button, height=28)
        self._add_button.clicked.connect(self._on_add)

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)
        layout.addWidget(self._subtitle)
        layout.addWidget(self._scroll, 1)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._add_button)
        buttons_layout.addWidget(self._apply_button)
        buttons_layout.addWidget(self._stop_button)
        buttons_layout.addStretch()
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
        self._rebuild_rows()

    def _collect_rules(self) -> list[dict[str, object]]:
        """Собирает правила из всех строк."""
        return [row.get_rule() for row in self._row_widgets]

    def _save_config(self) -> None:
        """Сохраняет правила в конфигурацию."""
        self._rules = self._collect_rules()
        self._config.set("flexible_rules", self._rules)

    def get_config(self) -> list[dict[str, object]]:
        """Возвращает текущие правила для экспорта."""
        self._save_config()
        return self._config.get("flexible_rules", [])

    def set_config(self, rules: list[dict[str, object]]) -> None:
        """Загружает правила из импортированного профиля."""
        self._config.set("flexible_rules", rules)
        self._load_config()

    def _rebuild_rows(self) -> None:
        """Пересоздаёт виджеты строк из self._rules."""
        while self._row_widgets:
            self._remove_row(self._row_widgets[-1])
        for rule in self._rules:
            self._add_row_widget(rule)
        self._rule_counters = [0] * len(self._row_widgets)

    def _add_row_widget(self, rule: dict[str, object] | None = None) -> RuleRowWidget:
        """Добавляет виджет строки в конец списка."""
        row = RuleRowWidget(self, rule)
        self._row_widgets.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        return row

    def _remove_row(self, widget: RuleRowWidget) -> None:
        """Удаляет виджет строки."""
        if widget in self._row_widgets:
            self._row_widgets.remove(widget)
        # setParent(None) до отложенного deleteLater — снимок полей окна
        # настроек сразу перестаёт видеть удалённую строку.
        widget.setParent(None)
        widget.deleteLater()
        self._rule_counters = [0] * len(self._row_widgets)

    def _on_add(self) -> None:
        """Добавляет новое пустое правило."""
        self._add_row_widget()
        self._rule_counters.append(0)

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

    def _build_internal_rules(self) -> None:
        """Формирует внутренний список активных правил для обработки кадров."""
        self._internal_rules = []
        self._rule_counters = [0] * len(self._row_widgets)
        for row_index, row in enumerate(self._row_widgets):
            rule = row.get_rule()
            if not rule.get("active", False):
                continue
            can_id = hex_to_int(rule.get("id", ""))
            if can_id is None:
                continue
            mask = self._pad_8(parse_data_bytes(str(rule.get("mask", "")).split()))
            condition_data = self._pad_8(parse_data_bytes(str(rule.get("condition_data", "")).split()))
            resp_id = hex_to_int(rule.get("resp_id", ""))
            if resp_id is None:
                resp_id = can_id
            resp_data = self._pad_8(parse_data_bytes(str(rule.get("resp_data", "")).split()))
            resp_mask = self._pad_8(parse_data_bytes(str(rule.get("resp_mask", "")).split()))
            try:
                delay_ms = int(rule.get("delay", 0))
            except ValueError:
                delay_ms = 0
            try:
                resp_channel = int(rule.get("resp_channel", "")) if rule.get("resp_channel", "") else None
            except ValueError:
                resp_channel = None
            self._internal_rules.append({
                "index": row_index,
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
    def _pad_8(data: list[int]) -> list[int]:
        """Дополняет или обрезает список до 8 байт."""
        data = data[:8]
        return data + [0] * (8 - len(data))

    def _send_rule_response(self, resp_frame: bytes) -> None:
        """Отправляет подготовленный ответный кадр."""
        self._serial_manager.send_data(resp_frame)

    def set_dbc(self, dbc_manager) -> None:
        """Обновляет логику при смене DBC (заглушка)."""
        pass

    def process_frame(self, frame: dict[str, object]) -> None:
        """Проверяет входящий кадр на совпадение с активными правилами."""
        if not self._active:
            return
        if frame.get("tx_echo"):
            # Эхо собственной передачи МК — не внешний кадр; без этого
            # фильтра ответ правила, совпадающий с условием, мог
            # перезапускать то же правило бесконечно.
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
                if (
                    rule["mask"][i]
                    and (frame_data[i] & rule["mask"][i]) != (rule["condition_data"][i] & rule["mask"][i])
                ):
                    match = False
                    break
            if not match:
                continue

            idx = rule["index"]
            self._rule_counters[idx] += 1
            self._row_widgets[idx].set_counter(self._rule_counters[idx])

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
            self._save_config()
            self._rebuild_rows()
            logger.info("Правила гибкой логики загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки правил: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить правила: {0}").format(exc))

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки."""
        self._title.setText(tr("Гибкая логика"))
        self._subtitle.setText(tr("Правила if-then для CAN-кадров"))
        self._add_button.setText(tr("Добавить правило"))
        self._apply_button.setText(tr("Применить правила"))
        self._stop_button.setText(tr("Остановить"))
        self._save_button.setText(tr("Сохранить в файл"))
        self._load_button.setText(tr("Загрузить из файла"))
        for row in self._row_widgets:
            row.retranslate_ui()
