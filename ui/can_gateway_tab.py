"""Вкладка «CAN-шлюз» — ретрансляция, игнорирование и подмена кадров.

Окно разделено на две половины: левая — CAN1, правая — CAN2
(отчёт мастера). Каждая программа шлюза — карточка со спецификацией
фрейма в каждой половине и тремя кнопками-стрелками между ними:

* ← — направление CAN2 → CAN1;
* → — направление CAN1 → CAN2;
* ↔ — обе стороны.

Неактивная стрелка — белый контур, активная — красная.

Программы двух видов:

* «Игнорирование» — фрейм, записанный в половине CAN1, с активной
  стрелкой → (или ↔) не проходит из CAN1 в CAN2; фрейм в половине
  CAN2 со стрелкой ← (или ↔) не проходит из CAN2 в CAN1. Все
  остальные пакеты проходят в обычном режиме;
* «Подмена» — приходящий кадр, подходящий под спецификацию исходной
  стороны, не пропускается; вместо него в противоположный канал
  отправляется записанный в другой половине пакет. При → приём —
  спецификация CAN1, подмена — пакет CAN2; при ← наоборот; при ↔
  работает в обе стороны.

Правила записи пакетов — как в триггерах: шестнадцатеричный ID и
побайтовая DATA, «X» — любой байт (wildcard), пустое поле не
участвует в сравнении.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
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
from models.utils import hex_to_int
from ui.hex_edit import create_data_field_widget
from ui.packet_clipboard import create_clipboard_buttons
from ui.ui_utils import setup_button
from ui.variables_tab import _HexIdEdit
from ui.memory_indicator import MemoryIndicator

logger = get_logger(__name__)

# Направления (кнопки-стрелки между половинами карточки).
_DIR_LEFT = 0   # ← : CAN2 -> CAN1
_DIR_RIGHT = 1  # → : CAN1 -> CAN2
_DIR_BOTH = 2   # ↔ : обе стороны

_MODE_IGNORE = "ignore"
_MODE_SUBSTITUTE = "substitute"

# Неактивная стрелка — белый контур, активная — красная.
_ARROW_STYLE = (
    "QPushButton {"
    " border: 2px solid #FFFFFF; color: #FFFFFF;"
    " background: transparent; border-radius: 6px;"
    " font-size: 18px; font-weight: bold; padding: 2px 10px;"
    "}"
    "QPushButton:checked {"
    " border-color: #F44336; color: #F44336;"
    " background: rgba(244, 67, 54, 60);"
    "}"
)


def _spec_match(spec: dict[str, Any], frame_id: int, data: bytes) -> bool:
    """Фрейм подходит под спецификацию: ID равен, все заполненные
    байты DATA равны («X» и пустое поле — wildcard)."""
    spec_id = hex_to_int(str(spec.get("id", "")))
    if spec_id is None or spec_id != frame_id:
        return False
    tokens = str(spec.get("data", "")).split()
    for i, token in enumerate(tokens[:8]):
        token = token.strip().upper()
        if not token or token == "X":
            continue
        value = hex_to_int(token)
        if value is None or i >= len(data) or data[i] != value:
            return False
    return True


def _spec_filled(spec: dict[str, Any]) -> bool:
    """В спецификации записан хотя бы ID — иначе она не работает."""
    return hex_to_int(str(spec.get("id", ""))) is not None


class _FrameSpec(QWidget):
    """Спецификация CAN-фрейма: ID + 8 байт DATA с wildcard «X»."""

    def __init__(self, font: QFont, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        id_row = QHBoxLayout()
        id_row.addWidget(QLabel("ID"))
        self.can_id = _HexIdEdit(font)
        id_row.addWidget(self.can_id)
        id_row.addStretch()
        layout.addLayout(id_row)

        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=34, allow_x=True,
        )
        layout.addWidget(data_widget)

        clip = create_clipboard_buttons(self, self.can_id, None, self.data)
        layout.addWidget(clip)

    def read(self) -> dict[str, Any]:
        return {
            "id": self.can_id.text().strip(),
            "data": " ".join(e.text().strip().upper() for e in self.data),
        }

    def write(self, spec: dict[str, Any]) -> None:
        self.can_id.setText(str(spec.get("id", "")))
        tokens = str(spec.get("data", "")).split()
        for i, edit in enumerate(self.data):
            edit.setText(tokens[i] if i < len(tokens) else "")


class _DirectionButtons(QWidget):
    """Три кнопки-стрелки направления: ← (CAN2→CAN1), → (CAN1→CAN2),
    ↔ (обе стороны). Неактивная — белый контур, активная — красная.
    Одновременно активна только одна стрелка."""

    def __init__(self, font: QFont, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(2, 0, 2, 0)
        self._buttons: dict[int, QPushButton] = {}
        for direction, symbol in (
            (_DIR_LEFT, "←"),
            (_DIR_RIGHT, "→"),
            (_DIR_BOTH, "↔"),
        ):
            button = QPushButton(symbol)
            button.setFont(font)
            button.setCheckable(True)
            button.setStyleSheet(_ARROW_STYLE)
            button.setToolTip({
                _DIR_LEFT: tr("CAN2 → CAN1"),
                _DIR_RIGHT: tr("CAN1 → CAN2"),
                _DIR_BOTH: tr("Обе стороны"),
            }[direction])
            button.clicked.connect(
                lambda checked, d=direction: self._select(d, checked)
            )
            self._buttons[direction] = button
            layout.addWidget(button)
        layout.addStretch()

    def _select(self, direction: int, checked: bool) -> None:
        for d, button in self._buttons.items():
            button.blockSignals(True)
            button.setChecked(checked and d == direction)
            button.blockSignals(False)

    def direction(self) -> int | None:
        for d, button in self._buttons.items():
            if button.isChecked():
                return d
        return None

    def set_direction(self, direction: int | None) -> None:
        for d, button in self._buttons.items():
            button.setChecked(d == direction)


class _GatewayProgram(QGroupBox):
    """Карточка программы шлюза: половина CAN1 | стрелки | половина
    CAN2. Для «Игнорирования» обе половины — блокируемые фреймы;
    для «Подмены» — пакет приёма на своей стороне и подмена в
    противоположный канал."""

    def __init__(
        self,
        tab: CanGatewayTab,
        mode: str,
        font: QFont,
        rule: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self.mode = mode
        self.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        # Толстая серая рамка вокруг программы (отчёт мастера).
        self.setStyleSheet(
            "QGroupBox { border: 2px solid #6E6E78; border-radius: 8px;"
            " margin-top: 10px; padding-top: 8px; }"
            "QGroupBox::title { subcontrol-origin: margin;"
            " subcontrol-position: top left; padding: 0 6px; }"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # Шапка: №/название программы, активность, удаление.
        header = QHBoxLayout()
        self._title_label = QLabel()
        self._title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        header.addWidget(self._title_label)
        header.addStretch()
        self._active = QCheckBox(tr("Активна"))
        self._active.setFont(font)
        self._active.toggled.connect(tab.mark_dirty)
        header.addWidget(self._active)
        self._remove = QPushButton("✕")
        self._remove.setFont(font)
        self._remove.setFixedSize(24, 24)
        self._remove.setToolTip(tr("Удалить программу"))
        self._remove.clicked.connect(lambda: tab.remove_program(self))
        header.addWidget(self._remove)
        layout.addLayout(header)

        # Две половины: CAN1 слева, стрелки по центру, CAN2 справа.
        body = QHBoxLayout()
        body.setSpacing(8)

        left_group = QGroupBox("CAN1")
        left_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        left_layout = QVBoxLayout(left_group)
        self.spec_left = _FrameSpec(font)
        left_layout.addWidget(self.spec_left)
        left_layout.addStretch()
        body.addWidget(left_group, 1)

        self.direction = _DirectionButtons(font)
        body.addWidget(self.direction, 0)

        right_group = QGroupBox("CAN2")
        right_group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        right_layout = QVBoxLayout(right_group)
        self.spec_right = _FrameSpec(font)
        right_layout.addWidget(self.spec_right)
        right_layout.addStretch()
        body.addWidget(right_group, 1)

        layout.addLayout(body)

        hint = QLabel(
            tr("«X» — любой байт, пустое поле не участвует. "
               "Стрелка задаёт направление действия программы.")
        )
        hint.setFont(font)
        hint.setStyleSheet("color: #9A9AA5;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.refresh_title()
        if rule is not None:
            self.write(rule)

    def refresh_title(self) -> None:
        name = (
            tr("Игнорирование")
            if self.mode == _MODE_IGNORE
            else tr("Подмена")
        )
        index = self._tab._programs.index(self) + 1 if self in self._tab._programs else 0
        self._title_label.setText(f"№ {index} · {name}")

    def read(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active": self._active.isChecked(),
            "direction": self.direction.direction(),
            "spec1": self.spec_left.read(),
            "spec2": self.spec_right.read(),
        }

    def write(self, rule: dict[str, Any]) -> None:
        self._active.setChecked(bool(rule.get("active", True)))
        direction = rule.get("direction")
        self.direction.set_direction(
            int(direction) if direction is not None else None
        )
        self.spec_left.write(rule.get("spec1") or {})
        self.spec_right.write(rule.get("spec2") or {})


class CanGatewayTab(QWidget):
    """Вкладка CAN-шлюза: программы «Игнорирование» и «Подмена»
    между CAN1 и CAN2 со стрелками направления."""

    def __init__(
        self,
        serial_manager: SerialManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._running = False
        self._programs: list[_GatewayProgram] = []
        self._internal_rules: list[dict[str, Any]] = []
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_config)
        self._memory_indicator = MemoryIndicator(self)
        self._create_widgets()
        self._build_layout()
        self._load_config()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки шлюза."""
        self._add_ignore_button.setText(tr("＋ Игнорирование"))
        self._add_substitute_button.setText(tr("＋ Подмена"))
        for program in self._programs:
            program.refresh_title()

    # ---- виджеты ------------------------------------------------------

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 9)

        self._add_ignore_button = QPushButton(tr("＋ Игнорирование"))
        setup_button(self._add_ignore_button, bold=True, height=34)
        self._add_ignore_button.clicked.connect(
            lambda: self.add_program(_MODE_IGNORE)
        )

        self._add_substitute_button = QPushButton(tr("＋ Подмена"))
        setup_button(self._add_substitute_button, bold=True, height=34)
        self._add_substitute_button.clicked.connect(
            lambda: self.add_program(_MODE_SUBSTITUTE)
        )

        self._programs_widget = QWidget()
        self._programs_layout = QVBoxLayout(self._programs_widget)
        self._programs_layout.setSpacing(12)
        self._programs_layout.setContentsMargins(0, 0, 0, 0)
        self._programs_layout.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._programs_widget)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

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

        self._font = font

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(tr("CAN-шлюз"))
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setProperty("title", True)
        layout.addWidget(title)

        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self._add_ignore_button)
        top.addWidget(self._add_substitute_button)
        top.addStretch()
        layout.addLayout(top)

        layout.addWidget(self._scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._stop_button)
        buttons.addStretch()
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._load_button)
        layout.addLayout(buttons)
        layout.addWidget(self._memory_indicator)

    # ---- программы -----------------------------------------------------

    def add_program(
        self, mode: str, rule: dict[str, Any] | None = None
    ) -> _GatewayProgram:
        """Добавляет карточку программы («Игнорирование»/«Подмена»)."""
        program = _GatewayProgram(self, mode, self._font, rule)
        self._programs.append(program)
        self._programs_layout.insertWidget(
            self._programs_layout.count() - 1, program
        )
        for p in self._programs:
            p.refresh_title()
        self.mark_dirty()
        return program

    def remove_program(self, program: _GatewayProgram) -> None:
        if program in self._programs:
            self._programs.remove(program)
        program.setParent(None)
        program.deleteLater()
        for p in self._programs:
            p.refresh_title()
        self.mark_dirty()

    def mark_dirty(self, *_args) -> None:
        self._save_timer.start()

    # ---- конфигурация ---------------------------------------------------

    def _collect_rules(self) -> list[dict[str, Any]]:
        return [program.read() for program in self._programs]

    def _migrate_legacy(
        self, rules: list[dict[str, Any]], ignore_ids: list[Any] | None
    ) -> list[dict[str, Any]]:
        """Старый формат (recv_id/replace_id/direction 0..1 и список
        gateway_ignore) — в карточки «Подмена»/«Игнорирование»."""
        migrated: list[dict[str, Any]] = []
        for rule in rules:
            if "mode" in rule:
                migrated.append(rule)
                continue
            # Пустые неактивные строки старого формата — просто
            # пустые места под правила; в новый UI не переносим.
            if (
                not rule.get("active")
                and not str(rule.get("recv_id", "")).strip()
                and not str(rule.get("replace_id", "")).strip()
            ):
                continue
            migrated.append({
                "mode": _MODE_SUBSTITUTE,
                "active": rule.get("active", False),
                # Старый direction: 0 = CAN1→CAN2 (→), 1 = CAN2→CAN1 (←).
                "direction": _DIR_RIGHT
                if int(rule.get("direction", 0)) == 0
                else _DIR_LEFT,
                "spec1": {
                    "id": rule.get("recv_id", ""),
                    "data": rule.get("recv_data", ""),
                },
                "spec2": {
                    "id": rule.get("replace_id", ""),
                    "data": rule.get("replace_data", ""),
                },
            })
        for raw in ignore_ids or []:
            if hex_to_int(str(raw)) is None:
                continue
            migrated.append({
                "mode": _MODE_IGNORE,
                "active": True,
                "direction": _DIR_BOTH,
                "spec1": {"id": str(raw), "data": ""},
                "spec2": {"id": str(raw), "data": ""},
            })
        return migrated

    def _load_config(
        self,
        rules: list[dict[str, Any]] | None = None,
        ignore_ids: list[Any] | None = None,
    ) -> None:
        if rules is None:
            rules = self._config.get("gateway_rules", [])
        if not isinstance(rules, list):
            rules = []
        if ignore_ids is None:
            ignore_ids = self._config.get("gateway_ignore", [])
        if not isinstance(ignore_ids, list):
            ignore_ids = []
        rules = self._migrate_legacy(rules, ignore_ids)
        while self._programs:
            self.remove_program(self._programs[-1])
        for rule in rules:
            mode = rule.get("mode", _MODE_IGNORE)
            if mode not in (_MODE_IGNORE, _MODE_SUBSTITUTE):
                continue
            self.add_program(mode, rule)
        self._save_timer.stop()

    def set_config(
        self,
        rules: list[dict[str, Any]],
        ignore_ids: list[Any] | None = None,
    ) -> None:
        """Устанавливает правила шлюза из внешней конфигурации."""
        self._load_config(rules, ignore_ids)

    def _save_config(self) -> None:
        rules = self._collect_rules()
        self._config.set("gateway_rules", rules)
        # Старый ключ больше не используется — всё в программах.
        self._config.set("gateway_ignore", [])
        self._memory_indicator.update_usage(
            self._memory_indicator.estimate_rules(rules)
        )

    # ---- исполнение ------------------------------------------------------

    def _build_internal_rules(self) -> list[dict[str, Any]]:
        rules = []
        for program in self._programs:
            rule = program.read()
            if not rule["active"] or rule["direction"] is None:
                continue
            if not (_spec_filled(rule["spec1"]) or _spec_filled(rule["spec2"])):
                continue
            rules.append(rule)
        return rules

    def _start(self) -> None:
        self._save_config()
        self._internal_rules = self._build_internal_rules()
        self._running = True
        logger.info(
            "CAN-шлюз запущен: %d программ", len(self._internal_rules)
        )
        QMessageBox.information(self, tr("CAN-шлюз"), tr("Шлюз запущен"))

    def _stop(self) -> None:
        self._running = False
        logger.info("CAN-шлюз остановлен")
        QMessageBox.information(self, tr("CAN-шлюз"), tr("Шлюз остановлен"))

    def process_frame(self, frame: dict[str, Any]) -> None:
        if not self._running:
            return
        if frame.get("tx_echo"):
            # Эхо собственной передачи МК — не внешний кадр; иначе
            # кадр мог бы зациклиться между каналами.
            return
        frame_id = int(frame["id"])
        frame_channel = int(frame["channel"])
        data = bytes(frame["data"])

        for rule in self._internal_rules:
            direction = rule["direction"]
            # Идёт ли кадр в «своём» направлении программы:
            # → (или ↔) для CAN1→CAN2, ← (или ↔) для CAN2→CAN1.
            if frame_channel == 1:
                direction_ok = direction in (_DIR_RIGHT, _DIR_BOTH)
                match_spec, out_spec = rule["spec1"], rule["spec2"]
            else:
                direction_ok = direction in (_DIR_LEFT, _DIR_BOTH)
                match_spec, out_spec = rule["spec2"], rule["spec1"]
            if not direction_ok or not _spec_filled(match_spec):
                continue
            if not _spec_match(match_spec, frame_id, data):
                continue

            if rule["mode"] == _MODE_IGNORE:
                # Игнорирование: подходящий пакет не пропускается,
                # все остальные идут в обычном режиме.
                logger.debug(
                    "Шлюз: игнорирование ID=0x%X в CAN%d",
                    frame_id, frame_channel,
                )
                return

            # Подмена: входящий пакет не пропускается; вместо него
            # в другой канал уходит записанный пакет подмены
            # («X»/пустые байты подмены берутся из исходного кадра).
            out_id = hex_to_int(str(out_spec.get("id", "")))
            if out_id is None:
                out_id = frame_id
            payload = bytearray(data[:8].ljust(8, b"\x00"))
            tokens = str(out_spec.get("data", "")).split()
            for i, token in enumerate(tokens[:8]):
                token = token.strip().upper()
                if not token or token == "X":
                    continue
                value = hex_to_int(token)
                if value is not None:
                    payload[i] = value & 0xFF
            target = 2 if frame_channel == 1 else 1
            self._serial_manager.send_data(
                pack_can_frame(target, out_id, bytes(payload))
            )
            logger.debug(
                "Шлюз: подмена ID=0x%X -> 0x%X в CAN%d",
                frame_id, out_id, target,
            )
            return

        # Ретрансляция без изменений — все остальные пакеты
        # проходят в обычном режиме.
        target_channel = 2 if frame_channel == 1 else 1
        self._serial_manager.send_data(
            pack_can_frame(target_channel, frame_id, data)
        )
        logger.debug(
            "Шлюз: ретрансляция ID=0x%X из CAN%d в CAN%d",
            frame_id, frame_channel, target_channel,
        )

    # ---- файлы правил ----------------------------------------------------

    def _save_rules(self) -> None:
        self._save_config()
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить правила"), "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(
                    self._config.get("gateway_rules", []),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Правила шлюза сохранены в %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения правил шлюза: %s", exc)
            QMessageBox.critical(
                self, tr("Ошибка"),
                tr("Не удалось сохранить правила: {0}").format(exc),
            )

    def _load_rules(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Загрузить правила"), "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            rules = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(rules, list):
                raise ValueError(tr("Файл должен содержать список правил"))
            self._config.set("gateway_rules", rules)
            self._config.set("gateway_ignore", [])
            self._load_config()
            logger.info("Правила шлюза загружены из %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки правил шлюза: %s", exc)
            QMessageBox.critical(
                self, tr("Ошибка"),
                tr("Не удалось загрузить правила: {0}").format(exc),
            )
