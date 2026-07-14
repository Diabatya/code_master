"""Диалог настройки фильтра мониторинга CAN."""

from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex, parse_data_bytes
from ui.hex_edit import HexDataEdit


class FilterDialog(QDialog):
    """Модальное окно с правилами фильтрации и списком принятых ID."""

    def __init__(
        self,
        rules: List[Dict[str, Any]],
        enabled: bool,
        ignored_ids: List[int],
        get_ids_callback: Callable[[], List[int]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Настройка фильтра"))
        self.resize(760, 420)
        self._font = QFont("Segoe UI", 9)
        self._get_ids_callback = get_ids_callback
        self._ignored_ids = set(ignored_ids)
        self._rules_widgets: List[Dict[str, Any]] = []

        self._enabled_check = QCheckBox(tr("Включить фильтр"))
        self._enabled_check.setFont(self._font)
        self._enabled_check.setChecked(enabled)

        self._add_button = QPushButton(tr("Добавить правило"))
        self._add_button.setFont(self._font)
        self._add_button.clicked.connect(self._add_rule)
        self._remove_button = QPushButton(tr("Удалить правило"))
        self._remove_button.setFont(self._font)
        self._remove_button.clicked.connect(self._remove_rule)

        self._rules_container = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setSpacing(4)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.addStretch()

        rules_scroll = QScrollArea()
        rules_scroll.setWidgetResizable(True)
        rules_scroll.setWidget(self._rules_container)
        rules_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        rules_tab = QWidget()
        rlayout = QVBoxLayout(rules_tab)
        rlayout.setContentsMargins(8, 8, 8, 8)
        rlayout.addLayout(self._rules_top_layout())
        rlayout.addWidget(rules_scroll)

        self._accepted_list = QListWidget()
        self._accepted_list.setFont(self._font)

        accepted_tab = QWidget()
        alayout = QVBoxLayout(accepted_tab)
        alayout.setContentsMargins(8, 8, 8, 8)
        alayout.addWidget(QLabel(tr("Принятые ID. Отметьте те, которые нужно игнорировать:")))
        alayout.addWidget(self._accepted_list)

        self._tabs = QTabWidget()
        self._tabs.setFont(self._font)
        self._tabs.addTab(rules_tab, tr("Правила"))
        self._tabs.addTab(accepted_tab, tr("Принятые"))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._enabled_check)
        layout.addWidget(self._tabs)
        layout.addWidget(buttons)

        for rule in rules:
            self._add_rule_widget(rule)
        if not self._rules_widgets:
            self._add_rule()

        self._refresh_accepted_ids()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_accepted_ids)
        self._refresh_timer.start(1000)

    def _rules_top_layout(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.addWidget(self._add_button)
        layout.addWidget(self._remove_button)
        layout.addStretch()
        layout.addWidget(QLabel(tr("Приоритет: Показывать → Не показывать → Игнорировать")))
        return layout

    def _add_rule(self) -> None:
        self._add_rule_widget({"mode": "show", "id_from": "", "id_to": "", "data_from": "", "data_to": ""})

    def _add_rule_widget(self, rule: Dict[str, Any]) -> None:
        widget = QWidget()
        row = QVBoxLayout(widget)
        row.setSpacing(4)
        row.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setSpacing(4)

        group = QButtonGroup(widget)
        show_radio = QRadioButton(tr("Показывать"))
        hide_radio = QRadioButton(tr("Не показывать"))
        show_radio.setFont(self._font)
        hide_radio.setFont(self._font)
        group.addButton(show_radio)
        group.addButton(hide_radio)
        if rule.get("mode") == "hide":
            hide_radio.setChecked(True)
        else:
            show_radio.setChecked(True)

        id_from = QLineEdit()
        id_from.setFixedWidth(90)
        id_from.setFont(self._font)
        id_from.setMaxLength(8)
        id_from.setPlaceholderText(tr("ID от"))
        id_from.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,8}"), id_from))
        id_from.setText(int_to_hex(rule.get("id_from", 0), 8) if rule.get("id_from") is not None else "")

        id_to = QLineEdit()
        id_to.setFixedWidth(90)
        id_to.setFont(self._font)
        id_to.setMaxLength(8)
        id_to.setPlaceholderText(tr("ID до"))
        id_to.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,8}"), id_to))
        id_to.setText(int_to_hex(rule.get("id_to", 0), 8) if rule.get("id_to") is not None else "")

        top.addWidget(show_radio)
        top.addWidget(hide_radio)
        top.addWidget(QLabel(tr("ID от")))
        top.addWidget(id_from)
        top.addWidget(QLabel(tr("до")))
        top.addWidget(id_to)
        top.addStretch()

        data_layout = QHBoxLayout()
        data_layout.setSpacing(4)
        data_layout.addWidget(QLabel(tr("Data от")))
        from_edits: List[HexDataEdit] = []
        to_edits: List[HexDataEdit] = []
        for i in range(8):
            fe = HexDataEdit(f"F{i}")
            fe.setFixedWidth(32)
            fe.setFont(self._font)
            from_edits.append(fe)
            te = HexDataEdit(f"T{i}")
            te.setFixedWidth(32)
            te.setFont(self._font)
            to_edits.append(te)
        for fe in from_edits:
            fe.set_siblings(from_edits)
        for te in to_edits:
            te.set_siblings(to_edits)

        from_values = parse_data_bytes(str(rule.get("data_from", "")).split())
        to_values = parse_data_bytes(str(rule.get("data_to", "")).split())
        for i, edit in enumerate(from_edits):
            edit.setText(f"{from_values[i]:02X}" if i < len(from_values) else "")
        for i, edit in enumerate(to_edits):
            edit.setText(f"{to_values[i]:02X}" if i < len(to_values) else "")

        for edit in from_edits:
            data_layout.addWidget(edit)
        data_layout.addWidget(QLabel(tr("до")))
        for edit in to_edits:
            data_layout.addWidget(edit)
        data_layout.addStretch()

        row.addLayout(top)
        row.addLayout(data_layout)

        self._rules_layout.insertWidget(self._rules_layout.count() - 1, widget)
        self._rules_widgets.append({
            "widget": widget,
            "show_radio": show_radio,
            "hide_radio": hide_radio,
            "id_from": id_from,
            "id_to": id_to,
            "data_from": from_edits,
            "data_to": to_edits,
        })

    def _remove_rule(self) -> None:
        if not self._rules_widgets:
            return
        rule = self._rules_widgets.pop()
        rule["widget"].deleteLater()

    def _refresh_accepted_ids(self) -> None:
        if not self._accepted_list.isVisible():
            return
        current_ids = set(self._get_ids_callback())
        existing: Dict[int, QListWidgetItem] = {}
        for i in range(self._accepted_list.count()):
            item = self._accepted_list.item(i)
            can_id = item.data(Qt.ItemDataRole.UserRole)
            if can_id in current_ids:
                existing[can_id] = item
            else:
                self._accepted_list.takeItem(i)
        for can_id in sorted(current_ids):
            if can_id in existing:
                continue
            item = QListWidgetItem(f"{int_to_hex(can_id, 8)}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if can_id in self._ignored_ids else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, can_id)
            self._accepted_list.addItem(item)

    def closeEvent(self, event) -> None:
        self._refresh_timer.stop()
        super().closeEvent(event)

    def get_result(self) -> Optional[Dict[str, Any]]:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        rules: List[Dict[str, Any]] = []
        for rule in self._rules_widgets:
            mode = "hide" if rule["hide_radio"].isChecked() else "show"
            id_from = hex_to_int(rule["id_from"].text())
            id_to = hex_to_int(rule["id_to"].text())
            data_from = bytes(parse_data_bytes([e.text() for e in rule["data_from"]]))
            data_to = bytes(parse_data_bytes([e.text() for e in rule["data_to"]]))
            rules.append({
                "mode": mode,
                "id_from": id_from,
                "id_to": id_to,
                "data_from": data_from,
                "data_to": data_to,
            })
        ignored_ids: List[int] = []
        for i in range(self._accepted_list.count()):
            item = self._accepted_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                can_id = item.data(Qt.ItemDataRole.UserRole)
                if can_id is not None:
                    ignored_ids.append(can_id)
        return {
            "enabled": self._enabled_check.isChecked(),
            "rules": rules,
            "ignored_ids": ignored_ids,
        }
