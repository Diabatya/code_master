"""Страница «Гибкая логика» — программы if-then для CAN-кадров.

Каждая программа — карточка: слева вверху галочка включения и поле
имени, справа вверху «✕» удаления; тело — колонки «Условие» /
«Действие» / «Параметры», сворачивается кнопкой ▾/▸.

Сохранение — в общий конфиг приложения (ключ flexible_rules), без
отдельных файлов. Обработка автономна: включённая программа
срабатывает сама, без кнопки «Применить»; на неизменной DATA — строго
«Сработок на DATA» раз (по умолчанию 1), сколько бы одинаковых
пакетов ни пришло; при смене DATA счётчик сбрасывается.
"""

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
    """Одна программа: шапка (галочка, имя, ✕) + три колонки
    с CAN-параметрами. Сворачивается в строку-сводку."""

    def __init__(
        self,
        tab: "FlexibleLogicTab",
        rule: dict[str, object] | None = None,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self._rule = rule or {}
        self._create_widgets()
        self._build_layout()
        self._load_rule()

    @Slot()
    def _mark_dirty(self, *_args) -> None:
        self._tab.mark_dirty()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        # Шапка программы: вкл/выкл, имя, свёртка, удаление.
        self._active_check = QCheckBox()
        self._active_check.setFont(font)
        self._active_check.setToolTip(tr("Включить/выключить программу"))
        self._active_check.toggled.connect(self._mark_dirty)
        self._name_edit = QLineEdit()
        self._name_edit.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._name_edit.setPlaceholderText(tr("Программа"))
        self._name_edit.setClearButtonEnabled(True)
        self._name_edit.textChanged.connect(self._mark_dirty)
        self._collapse_button = QPushButton("▾")
        self._collapse_button.setFont(font)
        self._collapse_button.setFixedWidth(30)
        self._collapse_button.setToolTip(tr("Свернуть программу"))
        self._collapse_button.clicked.connect(self._toggle_collapsed)
        self._remove_button = QPushButton("✕")
        self._remove_button.setFont(font)
        self._remove_button.setFixedSize(26, 26)
        self._remove_button.setToolTip(tr("Удалить программу"))
        self._remove_button.clicked.connect(self._on_remove)
        self._summary_label = QLabel()
        self._summary_label.setFont(font)
        self._summary_label.setVisible(False)

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
        self._fire_limit_spin = QSpinBox()
        self._fire_limit_spin.setRange(1, 99)
        self._fire_limit_spin.setValue(1)
        self._fire_limit_spin.setFont(font)
        self._fire_limit_spin.setFixedWidth(70)
        self._fire_limit_spin.setToolTip(tr(
            "Сколько раз программа срабатывает, пока DATA условия "
            "не изменится. По умолчанию 1 — одинаковые пакеты "
            "не перезапускают программу."
        ))
        self._counter_label = QLabel("0")
        self._counter_label.setFont(font)
        self._counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Любое редактирование полей — пересборка правил и
        # отложенное сохранение в общий конфиг.
        for edit in (
            self._id_edit, self._mask_edit, self._condition_data_edit,
            self._resp_id_edit, self._resp_data_edit, self._resp_mask_edit,
            self._resp_channel_edit,
        ):
            edit.textChanged.connect(self._mark_dirty)
        for spin in (self._delay_spin, self._fire_limit_spin):
            spin.valueChanged.connect(self._mark_dirty)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 8)

        self._header = QFrame()
        self._header.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.04);"
            " border: 1px solid #454552; border-radius: 8px; }"
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(8)
        header_layout.addWidget(self._active_check)
        header_layout.addWidget(self._name_edit, 1)
        header_layout.addWidget(self._summary_label, 2)
        header_layout.addWidget(self._collapse_button)
        header_layout.addWidget(self._remove_button)
        layout.addWidget(self._header)

        self._body = QWidget()
        body_layout = QHBoxLayout(self._body)
        body_layout.setSpacing(8)
        body_layout.setContentsMargins(0, 0, 0, 0)

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
        self._channel_label = QLabel(tr("Канал"))
        params_layout.addWidget(self._channel_label)
        params_layout.addWidget(self._resp_channel_edit)
        self._delay_label = QLabel(tr("Задержка"))
        params_layout.addWidget(self._delay_label)
        params_layout.addWidget(self._delay_spin)
        self._fire_limit_label = QLabel(tr("Сработок на DATA"))
        params_layout.addWidget(self._fire_limit_label)
        params_layout.addWidget(self._fire_limit_spin)
        params_layout.addWidget(QLabel(tr("Срабатываний")))
        params_layout.addWidget(self._counter_label)
        params_layout.addStretch()

        body_layout.addWidget(self._condition_group, 1)
        body_layout.addWidget(self._action_group, 1)
        body_layout.addWidget(self._params_group, 1)
        layout.addWidget(self._body)

    def _load_rule(self) -> None:
        self._active_check.setChecked(self._rule.get("active", True))
        self._name_edit.setText(str(self._rule.get("title", "")))
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
        try:
            self._fire_limit_spin.setValue(
                int(self._rule.get("fire_limit", 1))
            )
        except ValueError:
            self._fire_limit_spin.setValue(1)
        if self._rule.get("collapsed"):
            self._toggle_collapsed()

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

    def _toggle_collapsed(self) -> None:
        """Сворачивает тело программы до шапки со сводкой и обратно."""
        # isVisible() ложно до первого показа окна — опираемся на
        # собственный флаг видимости виджета.
        collapsed = not self._body.isHidden()
        self._body.setVisible(not collapsed)
        self._summary_label.setVisible(collapsed)
        self._collapse_button.setText("▸" if collapsed else "▾")
        self._collapse_button.setToolTip(
            tr("Развернуть программу") if collapsed
            else tr("Свернуть программу")
        )
        if collapsed:
            self._summary_label.setText(
                tr("{0} → {1} (канал {2})").format(
                    self._id_edit.text() or "—",
                    self._resp_id_edit.text() or "—",
                    self._resp_channel_edit.text() or "—",
                )
            )
        self._mark_dirty()

    def get_rule(self) -> dict[str, object]:
        """Собирает программу из полей строки."""
        id_text = self._id_edit.text().strip()
        resp_id = hex_to_int(self._resp_id_edit.text())
        return {
            "title": self._name_edit.text().strip(),
            "active": self._active_check.isChecked(),
            "id": id_text,
            "mask": self._mask_edit.text().strip(),
            "condition_data": self._condition_data_edit.text().strip(),
            "resp_id": int_to_hex(resp_id, 8) if resp_id is not None
                else self._resp_id_edit.text().strip(),
            "resp_data": self._resp_data_edit.text().strip(),
            "resp_mask": self._resp_mask_edit.text().strip(),
            "resp_channel": self._resp_channel_edit.text().strip(),
            "delay": self._delay_spin.value(),
            "fire_limit": self._fire_limit_spin.value(),
            "collapsed": self._body.isHidden(),
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
        self._resp_channel_edit.setPlaceholderText(tr("1 или 2"))
        self._delay_spin.setSuffix(tr(" мс"))
        self._name_edit.setPlaceholderText(tr("Программа"))
        self._channel_label.setText(tr("Канал"))
        self._delay_label.setText(tr("Задержка"))
        self._fire_limit_label.setText(tr("Сработок на DATA"))
        self._collapse_button.setToolTip(
            tr("Развернуть программу") if self._body.isHidden()
            else tr("Свернуть программу")
        )


class FlexibleLogicTab(QWidget):
    """Вкладка гибкой логики: программы применяются сами —
    включённая галочка = программа активна."""

    def __init__(
        self,
        serial_manager: SerialManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._rules: list[dict[str, object]] = []
        self._rule_counters: list[int] = []
        self._row_widgets: list[RuleRowWidget] = []
        self._internal_rules: list[dict[str, object]] = []
        self._rules_dirty = True
        # Отложенное сохранение: Config.set пишет файл на каждый
        # вызов, а правки идут посимвольно — схлопываем в один сейв.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_config)
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def _create_widgets(self) -> None:
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setSpacing(10)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._rows_widget)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self._add_button = QPushButton(tr("＋ Добавить программу"))
        setup_button(self._add_button, bold=True, height=34)
        self._add_button.setMinimumWidth(240)
        self._add_button.clicked.connect(self._on_add)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self._add_button)
        top.addStretch()
        layout.addLayout(top)

        layout.addWidget(self._scroll, 1)

    def _load_config(self) -> None:
        """Загружает программы из общей конфигурации."""
        rules = self._config.get("flexible_rules", [])
        if not isinstance(rules, list):
            rules = []
        self._rules = rules
        self._rule_counters = [0] * len(rules)
        self._rebuild_rows()
        self._rules_dirty = True

    def mark_dirty(self) -> None:
        """Поля поменялись: пересобрать правила и отложенно сейвить."""
        self._rules_dirty = True
        self._save_timer.start()

    def _collect_rules(self) -> list[dict[str, object]]:
        """Собирает программы из всех строк."""
        return [row.get_rule() for row in self._row_widgets]

    def _save_config(self) -> None:
        """Сохраняет программы в общую конфигурацию."""
        self._rules = self._collect_rules()
        self._config.set("flexible_rules", self._rules)

    def get_config(self) -> list[dict[str, object]]:
        """Возвращает текущие программы для экспорта."""
        self._save_config()
        return self._config.get("flexible_rules", [])

    def set_config(self, rules: list[dict[str, object]]) -> None:
        """Загружает программы из импортированного профиля."""
        self._config.set("flexible_rules", rules)
        self._load_config()

    def _rebuild_rows(self) -> None:
        """Пересоздаёт виджеты строк из self._rules."""
        while self._row_widgets:
            self._remove_row(self._row_widgets[-1])
        for rule in self._rules:
            self._add_row_widget(rule)
        self._rule_counters = [0] * len(self._row_widgets)

    def _add_row_widget(
        self,
        rule: dict[str, object] | None = None,
    ) -> RuleRowWidget:
        """Добавляет виджет программы в конец списка."""
        row = RuleRowWidget(self, rule)
        self._row_widgets.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        return row

    def _remove_row(self, widget: RuleRowWidget) -> None:
        """Удаляет виджет программы."""
        if widget in self._row_widgets:
            self._row_widgets.remove(widget)
        # setParent(None) до отложенного deleteLater — снимок полей окна
        # настроек сразу перестаёт видеть удалённую строку.
        widget.setParent(None)
        widget.deleteLater()
        self._rule_counters = [0] * len(self._row_widgets)
        self.mark_dirty()

    def _on_add(self) -> None:
        """Добавляет новую пустую программу."""
        self._add_row_widget()
        self._rule_counters.append(0)
        self.mark_dirty()

    def _build_internal_rules(self) -> None:
        """Формирует внутренний список активных программ."""
        self._internal_rules = []
        self._rule_counters = [0] * len(self._row_widgets)
        for row_index, row in enumerate(self._row_widgets):
            rule = row.get_rule()
            if not rule.get("active", False):
                continue
            can_id = hex_to_int(str(rule.get("id", "")))
            if can_id is None:
                continue
            mask = self._pad_8(
                parse_data_bytes(str(rule.get("mask", "")).split())
            )
            condition_data = self._pad_8(
                parse_data_bytes(str(rule.get("condition_data", "")).split())
            )
            resp_id = hex_to_int(str(rule.get("resp_id", "")))
            if resp_id is None:
                resp_id = can_id
            resp_data = self._pad_8(
                parse_data_bytes(str(rule.get("resp_data", "")).split())
            )
            resp_mask = self._pad_8(
                parse_data_bytes(str(rule.get("resp_mask", "")).split())
            )
            try:
                delay_ms = int(rule.get("delay", 0))
            except ValueError:
                delay_ms = 0
            try:
                fire_limit = int(rule.get("fire_limit", 1))
            except ValueError:
                fire_limit = 1
            try:
                resp_channel = (
                    int(str(rule.get("resp_channel", "")))
                    if str(rule.get("resp_channel", "")) else None
                )
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
                "fire_limit": max(1, fire_limit),
                # Состояние «строго N раз на неизменной DATA».
                "last_data": None,
                "data_fires": 0,
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

    def process_frame(self, frame: dict[str, object]) -> None:
        """Проверяет входящий кадр на совпадение с активными программами.

        Работает всегда — включённость программы задаёт её галочка.
        На неизменной DATA программа срабатывает не более
        «Сработок на DATA» раз, сколько бы одинаковых пакетов ни шло."""
        if frame.get("tx_echo"):
            # Эхо собственной передачи МК — не внешний кадр; без этого
            # фильтра ответ программы, совпадающий с условием, мог
            # перезапускать ту же программу бесконечно.
            return
        if self._rules_dirty:
            self._build_internal_rules()
            self._rules_dirty = False
        if not self._internal_rules:
            return

        frame_id = int(frame["id"])
        frame_data = self._pad_8(list(bytes(frame["data"])))
        frame_channel = int(frame["channel"])

        for rule in self._internal_rules:
            if rule["id"] != frame_id:
                continue
            match = True
            for i in range(8):
                if (
                    rule["mask"][i]
                    and (frame_data[i] & rule["mask"][i])
                    != (rule["condition_data"][i] & rule["mask"][i])
                ):
                    match = False
                    break
            if not match:
                continue

            # «Строго N раз на неизменной DATA»: одинаковый поток не
            # перезапускает программу; новая DATA — счётчик заново.
            data_key = bytes(frame_data)
            if rule["last_data"] != data_key:
                rule["last_data"] = data_key
                rule["data_fires"] = 0
            if rule["data_fires"] >= rule["fire_limit"]:
                continue
            rule["data_fires"] += 1

            idx = rule["index"]
            self._rule_counters[idx] += 1
            self._row_widgets[idx].set_counter(self._rule_counters[idx])

            # Ответные данные: по resp_mask из resp_data, иначе — из
            # входящего кадра.
            resp_data = [
                (rule["resp_data"][i] & rule["resp_mask"][i])
                | (frame_data[i] & (0xFF ^ rule["resp_mask"][i]))
                for i in range(8)
            ]
            channel = (
                rule["resp_channel"]
                if rule["resp_channel"] in (1, 2) else frame_channel
            )
            resp_frame = pack_can_frame(
                channel, rule["resp_id"], bytes(resp_data)
            )
            delay_ms = rule["delay"]
            if delay_ms > 0:
                QTimer.singleShot(
                    delay_ms,
                    lambda rf=resp_frame: self._send_rule_response(rf),
                )
            else:
                self._send_rule_response(resp_frame)
            logger.info(
                "Сработала программа ГЛ: ID=0x%s -> ответ ID=0x%s "
                "в канал %d (задержка %d мс)",
                int_to_hex(frame_id, 8),
                int_to_hex(rule["resp_id"], 8),
                channel,
                delay_ms,
            )

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки."""
        self._add_button.setText(tr("＋ Добавить программу"))
        for row in self._row_widgets:
            row.retranslate_ui()
