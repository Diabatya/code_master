"""Вкладка «Переменные».

Две колонки: «Чтение» (переменные, читаемые Гибкой логикой в условиях
«Если») и «Управление» (переменные для действий). Строк добавления
нет — оператор добавляет переменные кнопкой «＋ Добавить переменную»;
клик по строке открывает её настройку одного из двух видов:

* «Статическая переменная» — неограниченный набор фреймов с
  маской X по DATA (как «Приём» в триггерах); у каждого фрейма справа
  выбор значения «→ 1» / «→ 0» — приход фрейма пишет это значение
  в бит ОЗУ, привязанный к функции в ГЛ. Выбор канала здесь
  отсутствует: канал фрейма задаётся в Гибкой логике (отчёт мастера);
* «Динамическая переменная» — ID/DLC/диапазон Data и редактируемый
  график перевода сырого значения в величину (обороты ДВС, наддув,
  температура и т.п.). Байты для расчёта — это заполненные поля DATA
  без «X» (отчёт мастера: галочки убраны).

Поля ввода DATA — побайтовые, как в триггерах: отдельное поле на
каждый байт, только HEX, «X» — байт не участвует в сравнении/расчёте,
пустое поле тоже игнорируется.

Носитель переменной «ОЗУ / ПЗУ» задаётся в диалоге настройки
(при записи), а не в строке таблицы (отчёт мастера). ПЗУ пока
некликабельно — появится после подключения EEPROM к МК.

Хранение: «Сохранить переменные» пишет отдельный файл
«Конфиг Инфо.kmc» (переменные + заметки по ID из мониторинга),
загрузка такого файла идёт общей кнопкой «Загрузить конфигурацию»
окна настроек (отчёт мастера). Файл чужого формата или иной версии
не роняет приложение — выводится предупреждение.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRegularExpression,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.aux_channels import (
    AUX_PINS,
    aux_pin_conflict,
    migrate_aux_variables,
    quantize_pwm,
)
from core.can_protocol import pack_can_frame
from core import steering_decoder
from core import variable_decode
from models.config import (
    Config,
    pack_config_file,
)
from models.id_notes import IdNotes
from models.translations import _ as tr
from models.utils import (
    CYRILLIC_LAYOUT_MAP,
    hex_to_int,
    translate_cyrillic_hex,
    translate_cyrillic_layout,
)
from models.version import VERSION
from ui.hex_edit import create_data_field_widget
from ui.id_edit import IdPasteEdit
from ui.ui_utils import retire

_TYPE_STATIC = "static"
_TYPE_DYNAMIC = "dynamic"      # «Численная переменная» (переименована)
_TYPE_DYNCACHE = "dyn_cache"   # новый вид «Динамическая переменная»
_TYPE_IMPULSE = "impulse"      # «Импульсная переменная» — вспышка 0.5 с
_TYPE_CACHE = "cache"          # «Кэш переменная» — буферы 1 (ОЗУ) и 2 (ОЗУ/ПЗУ)
# «Управление» больше не содержит видов переменных — только команды
# и папки/подпапки для их группировки (отчёт мастера).
_TYPE_COMMAND = "command"
_TYPE_FOLDER = "folder"


def _tokens_match(tokens: list[str], data: bytes) -> bool:
    """Проверка DATA по маске токенов: «»/«X» — байт не участвует."""
    for i, token in enumerate(tokens):
        if not token or token == "X":
            continue
        value = hex_to_int(token)
        if value is None:
            continue
        byte = data[i] if i < len(data) else 0
        if byte != value:
            return False
    return True

# Метка и версия файла переменных: чужой формат или другая версия
# отвергаются сообщением, без падения приложения. Имя файла —
# «Config Variable» (отчёт мастера); вид конфига и устройство хранятся
# ВНУТРИ файла, имя свободное.
_CONFIG_INFO_KIND = "codemaster_config_info"
_CONFIG_INFO_VERSION = 3  # 3 = новая разметка номеров доп. каналов (CM-HW-02)
_CONFIG_INFO_NAME = "Config Variable.kmc"


def _config_info_dir() -> str:
    """Отдельная папка для конфигураций переменных."""
    return str(Config().config_dir() / "variables")


class _HexIdEdit(IdPasteEdit):
    """Поле CAN ID с валидацией HEX как в триггерах: верхний регистр,
    до 8 знаков, зелёный текст — валидный ID, красный — мусор или
    значение вне выбранной разрядности (11/29 бит — отчёт мастера)."""

    _HEX_RE = re.compile(r"^[0-9A-F]{0,8}$")
    _MAX_11BIT = 0x7FF
    _MAX_29BIT = 0x1FFFFFFF

    def __init__(self, font: QFont, placeholder: str = "ID") -> None:
        super().__init__()
        self.setFont(font)
        # Шире: 29-битный ID (8 HEX-знаков) не влезал целиком
        # (отчёт мастера — повторно, 96 px всё ещё резало край).
        self.setFixedWidth(110)
        self.setMaxLength(8)
        self.setPlaceholderText(placeholder)
        # По умолчанию комбобоксы битности стоят на «11 бит».
        self._max_id = self._MAX_11BIT
        self.setValidator(
            QRegularExpressionValidator(
                QRegularExpression(r"[0-9A-Fa-f]{0,8}")
            )
        )
        self.textChanged.connect(self._restyle)

    def set_extended(self, extended: bool) -> None:
        """Выбранная разрядность ID: 29 бит → ≤0x1FFFFFFF,
        11 бит → ≤0x7FF. Введённое значение не режется — вылезающее
        за пределы красится красным и ловится проверкой диалога
        (отчёт мастера: при 11 бит принимался 29-битный ID)."""
        self._max_id = self._MAX_29BIT if extended else self._MAX_11BIT
        self._restyle(self.text())

    def is_within_width(self) -> bool:
        """Введённый ID лежит в выбранной разрядности."""
        value = hex_to_int(self.text())
        return value is not None and value <= self._max_id

    def _restyle(self, text: str) -> None:
        upper = text.upper()
        if text != upper:
            self.blockSignals(True)
            self.setText(upper)
            self.blockSignals(False)
            text = upper
        text = text.strip()
        if not text:
            self.setStyleSheet("")
            return
        value = hex_to_int(text)
        if (
            not self._HEX_RE.match(text)
            or value is None
            or value > self._max_id
        ):
            self.setStyleSheet("color: #F44336;")
        else:
            self.setStyleSheet("color: #4CAF50;")


class _BindingCellEdit(QLineEdit):
    """Редактор ячейки таблицы привязки: кириллица, набранная без
    переключения раскладки, подменяется символом той же клавиши
    латинской раскладки СРАЗУ при вводе и при вставке из буфера
    (отчёт мастера: в таблицах привязки подмена не работала — она
    срабатывала только после завершения редактирования ячейки).

    ``allowed`` — необязательный набор допустимых символов ПОСЛЕ
    подмены: «фыв» в HEX-ячейке даёт «ad» («ы»→«s» не hex —
    отбрасывается)."""

    def __init__(
        self,
        parent=None,
        allowed: str | None = None,
        max_len: int | None = None,
        group_hex: bool = False,
    ) -> None:
        super().__init__(parent)
        self._allowed = set(allowed) if allowed else None
        # Сплошной ввод: «0102AB» сам разворачивается в «01 02 AB» —
        # после второго символа байта курсор встаёт на первый символ
        # следующего, оператор не переводит курсор вручную
        # (отчёт мастера).
        self._group_hex = group_hex
        if group_hex:
            # textChanged, не textEdited: группировка нужна и при
            # вставке из буфера («0102AB» → «01 02 AB»).
            self.textChanged.connect(self._regroup_hex)
        if max_len is not None:
            # Длина ячейки: «Значение DATA» — один байт, максимум
            # два hex-символа (отчёт мастера).
            self.setMaxLength(max_len)

    def _regroup_hex(self, _text: str) -> None:
        """Группирует набранные подряд hex-символы по байтам."""
        text = self.text()
        old_pos = self.cursorPosition()
        digits_before = sum(1 for ch in text[:old_pos] if ch != " ")
        digits = "".join(ch for ch in text.upper() if ch != " ")
        grouped = " ".join(
            digits[i : i + 2] for i in range(0, len(digits), 2)
        )
        if grouped == text:
            return
        self.blockSignals(True)
        self.setText(grouped)
        # Позиция курсора: каждая завершённая пара перед ним добавляет
        # разделитель — «01| 02» → 3, «01 0|2» → 4.
        new_pos = digits_before + digits_before // 2
        self.setCursorPosition(min(new_pos, len(grouped)))
        self.blockSignals(False)

    def _insert_mapped(self, text: str) -> None:
        mapped = translate_cyrillic_layout(text)
        if self._allowed is not None:
            mapped = "".join(ch for ch in mapped if ch in self._allowed)
        if mapped:
            self.insert(mapped)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        text = event.text()
        # Служебные клавиши (Backspace/Delete/Tab/стрелки, Ctrl+C/V …)
        # не имеют печатного текста или дают управляющий символ — их
        # фильтр раскладки глушил: стирание в ячейке не работало
        # (отчёт мастера). Пропускаем в базовый QLineEdit как есть.
        if not text or not all(ch.isprintable() for ch in text):
            super().keyPressEvent(event)
            return
        if any(ch in CYRILLIC_LAYOUT_MAP for ch in text):
            self._insert_mapped(text)
            return
        if self._allowed is not None:
            filtered = "".join(ch for ch in text if ch in self._allowed)
            if filtered != text:
                if filtered:
                    self.insert(filtered)
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source.hasText():
            self._insert_mapped(source.text())
            return
        super().insertFromMimeData(source)


class _BindingCellDelegate(QStyledItemDelegate):
    """Делегат колонки таблицы привязки — выдаёт редактор с живой
    подменой раскладки (см. _BindingCellEdit)."""

    def __init__(
        self,
        allowed: str | None = None,
        parent=None,
        max_len: int | Callable[[], int] | None = None,
        group_hex: bool = False,
    ) -> None:
        super().__init__(parent)
        self._allowed = allowed
        self._max_len = max_len
        self._group_hex = group_hex

    def createEditor(self, parent, option, index):  # noqa: N802
        limit = (
            self._max_len() if callable(self._max_len) else self._max_len
        )
        return _BindingCellEdit(parent, self._allowed, limit, self._group_hex)


def _bind_id_width(bit_combo: QComboBox, *edits: _HexIdEdit) -> None:
    """Подвязывает комбобокс «11/29 бит» к полям ID: при смене
    разрядности предел обновляется сразу во всех полях (отчёт
    мастера: выбрано 11 бит, а 29-битный ID вводился)."""

    def _apply(_index: int = 0) -> None:
        data = bit_combo.currentData()
        # Комбобоксы со строковыми пунктами («11 Бит»/«29 Бит» в ГЛ)
        # не несут data — разрядность берём по индексу.
        extended = bool(data) if data is not None else (
            bit_combo.currentIndex() == 1
        )
        for edit in edits:
            edit.set_extended(extended)

    def _auto_extend(text: str) -> None:
        """Введённый ID длиннее 3 hex-знаков (больше 0x7FF) —
        разрядность сама переключается на 29 бит (отчёт мастера)."""
        digits = [ch for ch in text.strip() if ch in "0123456789abcdefABCDEF"]
        if len(digits) <= 3:
            return
        if bit_combo.currentIndex() != 1:
            bit_combo.setCurrentIndex(1)

    for edit in edits:
        edit.textChanged.connect(_auto_extend)

    bit_combo.currentIndexChanged.connect(_apply)
    _apply()


def _data_tokens(edits: list[QLineEdit]) -> list[str]:
    """Токены побайтового поля: введённое значение, «X» — wildcard,
    «» — пустое (не участвует)."""
    return [e.text().strip().upper() for e in edits]


def _data_to_text(edits: list[QLineEdit]) -> str:
    """Сериализация побайтового поля в строку 'AA BB X …' — в файле
    конфигурации читается как раньше."""
    tokens = _data_tokens(edits)
    while tokens and tokens[-1] == "":
        tokens.pop()
    return " ".join(tokens)


def _text_to_data(edits: list[QLineEdit], text: Any) -> None:
    """Заполняет побайтовые поля из строки 'AA BB X …' (принимает и
    список токенов из старых конфигов)."""
    tokens = (
        [str(t) for t in text]
        if isinstance(text, (list, tuple))
        else str(text or "").replace(",", " ").split()
    )
    for i, edit in enumerate(edits):
        edit.setText(tokens[i].upper() if i < len(tokens) else "")


def _data_bytes_used(edits: list[QLineEdit]) -> list[int]:
    """Индексы байтов, участвующих в расчёте: поле заполнено и не «X»
    (отчёт мастера: галочек нет — считаем по введённым байтам)."""
    return [
        i for i, t in enumerate(_data_tokens(edits))
        if t and t != "X"
    ]


def _edits_raw_value(edits: list[QLineEdit]) -> int | None:
    """Сырое значение поля DATA: заполненные байты склеиваются
    старшим вперёд (порядок байтов во фрейме). None — поле пустое."""
    tokens = _data_tokens(edits)
    value = 0
    used = False
    for t in tokens:
        if not t or t == "X":
            continue
        try:
            value = (value << 8) | int(t, 16)
            used = True
        except ValueError:
            continue
    return value if used else None


def _edits_bytes_sum(edits: list[QLineEdit]) -> int | None:
    """Сумма значений заполненных байтов DATA (без «X» и пустых).

    «Численная переменная» при нескольких выбранных байтах считает
    сырое значение как сумму байтов (отчёт мастера: «не наблюдаю
    сложение сумм байтов на графике, если их больше одного»)."""
    total = 0
    used = False
    for t in _data_tokens(edits):
        if not t or t == "X":
            continue
        try:
            total += int(t, 16)
            used = True
        except ValueError:
            continue
    return total if used else None


def _sblp_decode(window: bytes, endian: str) -> int | None:
    """Собирает число из выбранных байтов кадра по порядку
    старшинства (отчёт мастера).

    ``endian``:
    ``"be"`` — «Старший байт слева»: левый байт множится на 256
    (06 04 → 6·256+4 = 1540);
    ``"le"`` — «Старший байт справа»: правый байт множится на 256
    (09 03 → 9 + 3·256 = 777).
    ``None`` — выбрано меньше двух байтов."""
    if len(window) < 2:
        return None
    if endian == "le":
        window = window[::-1]
    return int.from_bytes(window, "big")


def _autofill_x_on_id(
    id_edit: QLineEdit, *edit_groups: list[QLineEdit]
) -> None:
    """После ввода ID поля DATA переменной чтения автоматически
    заполняются «X» — байт-подстановка «любое значение»
    (отчёт мастера). Срабатывает один раз на непустой ID; поля,
    уже заполненные оператором, не трогает."""
    filled = {"done": False}

    def _fill(text: str) -> None:
        if filled["done"] or not text.strip():
            return
        filled["done"] = True
        for edits in edit_groups:
            for edit in edits:
                if edit.isEnabled() and not edit.text().strip():
                    edit.setText("X")

    id_edit.textChanged.connect(_fill)


def _autofill_00_on_id(
    id_edit: QLineEdit, *edit_groups: list[QLineEdit]
) -> None:
    """После ввода ID поля DATA фрейма команды заполняются «00»
    (отчёт мастера — в управлении байты должны быть конкретными,
    а не «X»). Срабатывает один раз на непустой ID; уже заполненные
    поля не трогает."""
    filled = {"done": False}

    def _fill(text: str) -> None:
        if filled["done"] or not text.strip():
            return
        filled["done"] = True
        for edits in edit_groups:
            for edit in edits:
                if edit.isEnabled() and not edit.text().strip():
                    edit.setText("00")

    id_edit.textChanged.connect(_fill)


def _set_data_enabled(edits: list[QLineEdit], count: int) -> None:
    """DLC ограничивает поля DATA: за пределами DLC поля пустые
    и неактивные — как в триггерах (отчёт мастера)."""
    for i, edit in enumerate(edits):
        if i >= count:
            edit.setText("")
            edit.setEnabled(False)
        else:
            edit.setEnabled(True)


def _selectable(label: QLabel) -> QLabel:
    """Любая строка выделяется курсором и копируется (отчёт мастера)."""
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    return label


def _clip_icon_button(icon, tooltip: str) -> QToolButton:
    """Маленькая иконка-кнопка буфера обмена — та же стилистика, что
    у кнопок «копировать/вставить» в триггерах (отчёт мастера: текст
    «Копировать» заменён значком)."""
    button = QToolButton()
    button.setIcon(icon)
    button.setIconSize(QSize(18, 18))
    button.setFixedSize(24, 24)
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(
        "QToolButton { background-color: palette(button);"
        " color: palette(text); border: none; border-radius: 4px; }"
        "QToolButton:hover { background-color: palette(midlight); }"
        "QToolButton:pressed { background-color: palette(mid); }"
    )
    return button


def _clipboard_buttons(
    label: QLabel,
    font: QFont,
    target_edits: list | None = None,
    frame_fields: dict | None = None,
    copy_online: bool = False,
) -> QWidget:
    """Иконки «копировать» + «вставить» рядом с онлайн-строкой DATA
    (значки как в триггерах — отчёт мастера).

    ``copy_online=True`` — в буфер идёт вся онлайн-строка DATA
    целиком (отчёт мастера: «копируется только первый байт»);
    иначе — символы, введённые оператором в поля DATA.

    Вставка — разложить байты из буфера по полям DATA настройки
    переменной (target_edits). Полный пакет «ID=.. DLC=.. DATA=..»
    принимается целиком: при заданном ``frame_fields`` (id/bit/dlc)
    заполняются и поля фрейма, а не только DATA (отчёт мастера:
    «вставляй все данные вместе с ID и DLC»)."""
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)

    copy_button = _clip_icon_button(
        widget.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon),
        tr("Скопировать онлайн-DATA в буфер обмена"),
    )

    def _do_copy() -> None:
        # Текст может быть HTML (побайтовая подсветка X/значение) —
        # в буфер идёт сохранённая «плоская» строка.
        online = (
            str(label.property("live_plain") or "").strip()
            or label.text().strip()
        )
        if copy_online:
            # Вся онлайн-строка DATA разом — для вставки в таблицу
            # привязки/поля настройки (отчёт мастера).
            text = online
        elif target_edits:
            # Копируем СИМВОЛЫ, введённые оператором в поля DATA,
            # а не онлайн-кадр с шины (отчёт мастера). Когда у поля
            # есть свой ID/DLC — в буфер идёт ПОЛНЫЙ пакет
            # «ID=.. DLC=.. DATA=..», чтобы «вставить» заполняло
            # и идентификатор (отчёт мастера: «ID не вставляется»).
            data_text = " ".join(
                e.text().strip().upper()
                for e in target_edits if e.text().strip()
            )
            id_edit = (frame_fields or {}).get("id")
            id_text = (
                id_edit.text().strip().upper()
                if id_edit is not None and hasattr(id_edit, "text")
                else ""
            )
            if id_text:
                parts = [f"ID=0x{id_text}"]
                dlc_spin = (frame_fields or {}).get("dlc")
                if dlc_spin is not None and hasattr(dlc_spin, "value"):
                    parts.append(f"DLC={dlc_spin.value()}")
                rtr_check = (frame_fields or {}).get("rtr")
                if (
                    rtr_check is not None
                    and hasattr(rtr_check, "isChecked")
                    and rtr_check.isChecked()
                ):
                    parts.append("RTR=1")
                if data_text:
                    parts.append(f"DATA={data_text}")
                text = " ".join(parts)
            else:
                text = data_text
        else:
            text = online
        if text and text != "—":
            QApplication.clipboard().setText(text)

    copy_button.clicked.connect(_do_copy)
    row.addWidget(copy_button)

    if target_edits:
        paste_button = _clip_icon_button(
            widget.style().standardIcon(
                QStyle.StandardPixmap.SP_DialogOpenButton
            ),
            tr("Вставить DATA из буфера в поля настройки"),
        )

        def _do_paste() -> None:
            text = QApplication.clipboard().text().strip()
            if not text:
                return
            # Полный пакет «ID=.. DLC=.. DATA=..» — поля фрейма идут
            # в свои настройки, DATA — в байтовые поля (отчёт мастера:
            # при вставке всего фрейма подставлялись только байты).
            if "=" in text:
                if frame_fields:
                    id_match = re.search(
                        r"ID\s*=\s*(0[xX])?([0-9A-Fa-f]+)", text
                    )
                    if id_match is not None:
                        id_edit = frame_fields.get("id")
                        if id_edit is not None:
                            id_edit.setText(id_match.group(2).upper())
                    dlc_match = re.search(r"DLC\s*=\s*(\d+)", text)
                    if dlc_match is not None:
                        dlc_spin = frame_fields.get("dlc")
                        if dlc_spin is not None:
                            dlc_spin.setValue(
                                max(0, min(8, int(dlc_match.group(1))))
                            )
                    rtr_match = re.search(r"RTR\s*=\s*(\w+)", text)
                    rtr_check = frame_fields.get("rtr")
                    if rtr_match is not None and rtr_check is not None:
                        rtr_check.setChecked(
                            rtr_match.group(1).strip().lower()
                            in ("1", "true", "да", "rtr")
                        )
                match = re.search(
                    r"DATA\s*=\s*(.*)$", text, re.IGNORECASE | re.DOTALL
                )
                text = match.group(1) if match else ""
            text = translate_cyrillic_hex(text).strip()
            tokens = [t for t in re.split(r"[\s,;]+", text) if t]
            if len(tokens) == 1:
                # Слитная строка «0102AB» — режем по байтам.
                tokens = [
                    tokens[0][i : i + 2]
                    for i in range(0, len(tokens[0]), 2)
                ]
            for i, edit in enumerate(target_edits):
                token = tokens[i].upper() if i < len(tokens) else ""
                if token == "X" and not getattr(edit, "_allow_x", False):
                    token = ""
                if token and not all(
                    ch in "0123456789ABCDEF" for ch in token
                ) and token != "X":
                    token = ""
                edit.setText(token[:2])
                edit.textEdited.emit(token[:2])

        paste_button.clicked.connect(_do_paste)
        row.addWidget(paste_button)
    return widget


def _set_live_data_html(
    label: QLabel, data: bytes | None, edits: list | None = None
) -> None:
    """Онлайн-DATA кадра с побайтовой подсветкой маски оператора.

    Байт, у которого в поле настройки стоит «X»/пусто — голубой
    (не участвует в сравнении); байт с введённым значением —
    зелёный (отчёт мастера: «если записали что-то помимо X,
    соответствующий байт зажигается зелёным»).
    """
    if data is None:
        label.setText("—")
        label.setProperty("live_plain", "")
        return
    plain = " ".join(f"{b:02X}" for b in data)
    label.setProperty("live_plain", plain)
    if not edits:
        label.setText(plain)
        return
    # Байт «задан», если в ЛЮБОМ из переданных полей (от/до) у него
    # стоит значение, а не «X»/пусто.
    token_lists = [
        _data_tokens(group) for group in (
            edits if isinstance(edits[0], (list, tuple)) else [edits]
        ) if group
    ]
    parts = []
    for i, byte in enumerate(data):
        filled = any(
            i < len(tokens) and tokens[i] not in ("", "X")
            for tokens in token_lists
        )
        color = "#4CAF50" if filled else "#7C9EFF"
        parts.append(
            f"<span style='color:{color}'>{byte:02X}</span>"
        )
    label.setText(" ".join(parts))


def _map_points(points: list[tuple[float, float]], raw: float) -> float:
    """Значение по «таблице привязки»: ломаная линейная интерполяция.
    За крайними точками прямая ПРОДОЛЖАЕТСЯ по наклону краевого
    сегмента — график обязан доходить до DATA «от»/«до»
    (отчёт мастера)."""
    pts = sorted(points)
    if len(pts) < 2:
        return 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a[0] <= raw <= b[0] and b[0] != a[0]:
            return a[1] + (b[1] - a[1]) * (raw - a[0]) / (b[0] - a[0])
    # Экстраполяция краевых сегментов.
    a, b = (pts[0], pts[1]) if raw < pts[0][0] else (pts[-2], pts[-1])
    if b[0] == a[0]:
        return b[1]
    return a[1] + (b[1] - a[1]) * (raw - a[0]) / (b[0] - a[0])


def _parse_data_hex(text: str) -> float | None:
    """Сырое значение DATA из «таблицы привязки» — всегда HEX:
    оператор вводит байты как в поле DATA, «100» = 0x100
    (отчёт мастера: hex не пересчитывать в десятичную). Пробелы между
    байтами (вставка «01 02» из онлайн-строки DATA) игнорируются."""
    t = translate_cyrillic_hex(text).strip().lower().replace(" ", "")
    if t.startswith("0x"):
        t = t[2:]
    if t.endswith("h"):
        t = t[:-1]
    if not t or not all(ch in "0123456789abcdef" for ch in t):
        return None
    return float(int(t, 16))


def _data_text_bytes(text: str) -> bytes | None:
    """Текст ячейки DATA таблицы привязки → байты («09 03» →
    ``b"\\x09\\x03"``). ``None`` — текст не HEX или нечётное число
    hex-цифр."""
    t = translate_cyrillic_hex(text).strip().lower().replace(" ", "")
    if t.startswith("0x"):
        t = t[2:]
    if t.endswith("h"):
        t = t[:-1]
    if not t or len(t) % 2 or not all(
        ch in "0123456789abcdef" for ch in t
    ):
        return None
    return bytes(int(t[i : i + 2], 16) for i in range(0, len(t), 2))


def _parse_axis_value(text: str) -> float | None:
    """Значение точки графика: привычное десятичное число или HEX —
    «FF», «0xFF», «FFh». Чистые цифры читаются как десятичные
    («100» = сто, а не 0x100) — иначе молчаливая смена смысла старых
    конфигов. HEX с буквами нужен, т.к. сырое значение DATA — байты
    (отчёт мастера: «график по 2 точкам не строится» — HEX-значения
    отбрасывались float-парсером)."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    hex_text = text[:-1] if text.lower().endswith("h") else text
    explicit = hex_text.lower().startswith("0x")
    if explicit:
        hex_text = hex_text[2:]
    if (
        hex_text
        and all(ch in "0123456789abcdefABCDEF" for ch in hex_text)
        and (explicit or re.search(r"[a-fA-F]", hex_text))
    ):
        return float(int(hex_text, 16))
    return None


class _SignRecordTable(QWidget):
    """Одна таблица записи DATA декодера «Градусы» (CM-F06).

    Десять фиксированных строк-делителей 2..20 (колонка «Числу») с
    тремя состояниями «Кратно»/«Не кратно»/не участвует; ниже —
    растущая история записанных значений (ОЗУ, не сохраняется;
    F06 п.3 — история длиннее десяти не обрезается, п.5 — делители
    фиксированы и от истории не зависят). Колонки: DATA 16 |
    DATA 10 | Кратно | Числу | Не кратно."""

    COL_HEX, COL_DEC, COL_DIV, COL_NUM, COL_NOT = range(5)
    HISTORY_LIMIT = 512  # потолок записей на таблицу (F06 п.16)

    def __init__(
        self,
        sign: str,
        font: QFont,
        on_changed: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.sign = sign
        self._on_changed = on_changed
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        top = QHBoxLayout()
        self.record_btn = QPushButton(tr("Записать"))
        self.record_btn.setFont(font)
        self.record_btn.setFixedHeight(26)
        self._set_record_idle()
        self.record_btn.clicked.connect(self.toggle_record)
        top.addWidget(self.record_btn)
        self.status = QLabel("")
        self.status.setFont(font)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        title = (
            tr("− Отрицательные") if sign == "neg"
            else tr("+ Положительные")
        )
        lbl = QLabel(title)
        lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        _selectable(lbl)
        layout.addWidget(lbl)
        self.table = QTableWidget(0, 5)
        self.table.setFont(font)
        self.table.setHorizontalHeaderLabels([
            tr("DATA 16"), tr("DATA 10"), tr("Кратно"),
            tr("Числу"), tr("Не кратно"),
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_item)
        layout.addWidget(self.table, 1)
        for d in steering_decoder.DIVISORS:
            self._add_divisor_row(d)
        self.recording = False
        self._last_raw: int | None = None
        self._history = 0

    # ---- строки ----------------------------------------------------

    def _add_divisor_row(self, div: int) -> None:
        """Фиксированная строка делителя — «Числу» не редактируется."""
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._fill_row(r, "", div, fixed=True)

    def _add_history_row(self, hex_text: str) -> None:
        """Строка записанного значения: «Числу» свободно — оператор
        может назначить делитель вручную (F06 п.3)."""
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._fill_row(r, hex_text, None, fixed=False)

    def _fill_row(
        self, r: int, hex_text: str, div: int | None, fixed: bool
    ) -> None:
        hex_item = QTableWidgetItem(hex_text)
        hex_item.setToolTip(tr("Сырое значение поля DATA в HEX"))
        self.table.blockSignals(True)
        self.table.setItem(r, self.COL_HEX, hex_item)
        dec = QTableWidgetItem("")
        dec.setFlags(dec.flags() & ~Qt.ItemFlag.ItemIsEditable)
        dec.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(r, self.COL_DEC, dec)
        # «Кратно»/«Не кратно» — взаимоисключающие галочки требования
        # (третье состояние — обе сняты, строка вне таблицы).
        for col in (self.COL_DIV, self.COL_NOT):
            cell = QWidget()
            lay = QHBoxLayout(cell)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb = QCheckBox()
            cb.toggled.connect(
                lambda _on, rr=r, cc=col: self._on_req(rr, cc)
            )
            lay.addWidget(cb)
            self.table.setCellWidget(r, col, cell)
            placeholder = QTableWidgetItem("")
            placeholder.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(r, col, placeholder)
        num = QTableWidgetItem("" if div is None else str(div))
        num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if fixed:
            num.setFlags(num.flags() & ~Qt.ItemFlag.ItemIsEditable)
        else:
            num.setToolTip(tr("Делитель 2…20 — назначьте для правила"))
        self.table.setItem(r, self.COL_NUM, num)
        self.table.blockSignals(False)

    def _req_boxes(self, r: int) -> tuple[QCheckBox, QCheckBox] | None:
        w_div = self.table.cellWidget(r, self.COL_DIV)
        w_not = self.table.cellWidget(r, self.COL_NOT)
        if w_div is None or w_not is None:
            return None
        return (
            w_div.findChild(QCheckBox), w_not.findChild(QCheckBox)
        )

    def _on_req(self, r: int, col: int) -> None:
        """Взаимоисключение «Кратно»/«Не кратно» одной строки."""
        boxes = self._req_boxes(r)
        if boxes is None:
            return
        cb_div, cb_not = boxes
        if col == self.COL_DIV and cb_div.isChecked():
            cb_not.setChecked(False)
        elif col == self.COL_NOT and cb_not.isChecked():
            cb_div.setChecked(False)
        self._revalidate_row(r)
        self._on_changed()

    # ---- значения строк ---------------------------------------------

    def _row_hex(self, r: int) -> str:
        item = self.table.item(r, self.COL_HEX)
        return item.text().strip() if item is not None else ""

    def _row_raw(self, r: int) -> int | None:
        data = _data_text_bytes(self._row_hex(r))
        if data is None:
            return None
        return int.from_bytes(data, "big")

    def _row_div(self, r: int) -> int | None:
        item = self.table.item(r, self.COL_NUM)
        if item is None:
            return None
        try:
            d = int(item.text().strip())
        except ValueError:
            return None
        return d if d in steering_decoder.DIVISORS else None

    def _row_req(self, r: int) -> str:
        boxes = self._req_boxes(r)
        if boxes is None:
            return ""
        cb_div, cb_not = boxes
        if cb_div.isChecked():
            return "div"
        if cb_not.isChecked():
            return "not"
        return ""

    # ---- валидация (F06 п.8–9) ---------------------------------------

    def _mark(self, r: int, color: str | None, tip: str = "") -> None:
        item = self.table.item(r, self.COL_HEX)
        if item is None:
            return
        item.setForeground(
            QColor(color) if color else QColor("#E8E8EF")
        )
        item.setToolTip(tip)

    def _revalidate_row(self, r: int) -> None:
        """Пересчёт DATA 10 и пометок строки после ввода/галочки.

        Состояния различаются явно: невалидный/неполный HEX —
        красный (запись НЕ исчезает, F06 п.8); валидный код,
        нарушающий выбранное требование кратности, — жёлтый;
        согласованный — зелёный; без кода — нейтральный.

        setText/setForeground порождают itemChanged → рекурсия —
        вся переработка под блокировкой сигналов."""
        self.table.blockSignals(True)
        raw_hex = self._row_hex(r)
        dec_item = self.table.item(r, self.COL_DEC)
        data = _data_text_bytes(raw_hex)
        if not raw_hex:
            if dec_item is not None:
                dec_item.setText("")
            self._mark(r, None)
        elif data is None:
            if dec_item is not None:
                dec_item.setText("—")
            self._mark(r, "#F44336", tr("Неполный/невалидный HEX"))
        else:
            raw = int.from_bytes(data, "big")
            if dec_item is not None:
                dec_item.setText(str(raw))
            d = self._row_div(r)
            req = self._row_req(r)
            if req and d is not None and (
                (req == "div" and raw % d != 0)
                or (req == "not" and raw % d == 0)
            ):
                req_text = (
                    tr("кратно") if req == "div" else tr("не кратно")
                )
                self._mark(
                    r, "#FFB74D",
                    tr("Код не подходит «") + req_text + f" {d}" + tr("»"),
                )
            else:
                self._mark(r, "#4CAF50")
        self.table.blockSignals(False)

    def _on_item(self, item: QTableWidgetItem) -> None:
        """Валидация после завершения ввода (itemChanged срабатывает
        на commit редактора — аналог editingFinished, F06 п.8).
        Невалидный текст НЕ стирается — подменяется только
        кириллица на той же клавише."""
        if item.column() in (self.COL_HEX, self.COL_NUM):
            text = (
                translate_cyrillic_hex(item.text())
                if item.column() == self.COL_HEX
                else translate_cyrillic_layout(item.text())
            )
            if text != item.text():
                self.table.blockSignals(True)
                item.setText(text)
                self.table.blockSignals(False)
        self._revalidate_row(item.row())
        self._on_changed()

    # ---- запись (F06) -------------------------------------------------

    def _set_record_idle(self) -> None:
        self.record_btn.setText(tr("Записать"))
        self.record_btn.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white;"
            " font-weight: bold; }"
        )

    def _set_record_active(self) -> None:
        self.record_btn.setText(tr("Стоп"))
        self.record_btn.setStyleSheet(
            "QPushButton { background: #C62828; color: white;"
            " font-weight: bold; }"
        )

    def toggle_record(self) -> None:
        if self.recording:
            self.stop_record()
        else:
            # Старт ТОЛЬКО по явной команде (F06 п.14): история и
            # прошлые значения не трогаем — «Записать» продолжает их.
            self.recording = True
            self._last_raw = None
            self._set_record_active()

    def stop_record(self) -> None:
        """Стоп — подтверждённые делители и правила не меняет
        (F06 п.12): история остаётся, запись просто отключается."""
        self.recording = False
        self._set_record_idle()

    def capture(self, data: bytes) -> None:
        """Принять кадр: выделенное поле → новая строка истории.

        Повторяющееся подряд значение не плодит строки (F06 п.18);
        потолок HISTORY_LIMIT — явный стоп с подписью (F06 п.16)."""
        if not self.recording or data is None:
            return
        raw = int.from_bytes(data, "big")
        if raw == self._last_raw:
            return
        self._last_raw = raw
        if self._history >= self.HISTORY_LIMIT:
            self.stop_record()
            self.status.setText(
                tr("Стоп: лимит записей (%1)").replace(
                    "%1", str(self.HISTORY_LIMIT)
                )
            )
            return
        hex_text = " ".join(f"{b:02X}" for b in data)
        self._add_history_row(hex_text)
        self._history += 1
        self._revalidate_row(self.table.rowCount() - 1)
        self.status.setText(
            tr("Записано: %1").replace("%1", str(self._history))
        )
        self._on_changed()

    # ---- конфиг --------------------------------------------------------

    def errors(self) -> list[str]:
        """Невалидные записи таблицы (F06 п.9): неполный HEX,
        требования, которым введённый код не соответствует, и
        делитель вне фиксированных 2…20."""
        out: list[str] = []
        for r in range(self.table.rowCount()):
            text = self._row_hex(r)
            data = _data_text_bytes(text) if text else None
            if text and data is None:
                out.append(
                    tr("строка %1: невалидный HEX «%2»")
                    .replace("%1", str(r + 1)).replace("%2", text)
                )
                continue
            num_item = self.table.item(r, self.COL_NUM)
            num_text = (
                num_item.text().strip() if num_item is not None else ""
            )
            if num_text and self._row_div(r) is None:
                out.append(
                    tr("строка %1: делитель вне 2…20").replace(
                        "%1", str(r + 1)
                    )
                )
            if data is None:
                continue
            raw = int.from_bytes(data, "big")
            d = self._row_div(r)
            req = self._row_req(r)
            if req and d is not None and (
                (req == "div" and raw % d != 0)
                or (req == "not" and raw % d == 0)
            ):
                req_text = (
                    tr("кратно") if req == "div" else tr("не кратно")
                )
                out.append(
                    tr("строка %1: 0x%2 не подходит «")
                    .replace("%1", str(r + 1))
                    .replace("%2", f"{raw:X}")
                    + req_text + f" {d}" + tr("»")
                )
        return out

    def sign_rules(self) -> dict[int, str]:
        """Требования стороны для конфига: делитель → div/not —
        из строк с выбранной галочкой (фиксированных и записанных
        с назначенным «Числу»)."""
        out: dict[int, str] = {}
        for r in range(self.table.rowCount()):
            d = self._row_div(r)
            req = self._row_req(r)
            if d is not None and req:
                out[d] = req
        return out


class _DegreesDecoderDialog(QDialog):
    """Декодер режима «Градусы» (CM-F05/06): две таблицы кратности
    с записью DATA, «Инвертирование», «Температура», live-поле
    CAN ID/DATA. Код нейтрали, таймаут, калибровка и интерполяция
    удалены — нейтраль задаётся нулевой точкой таблицы привязки
    переменной, масштаб — её ветками (CM-F05 п.3–4)."""

    def __init__(
        self,
        cfg: dict[str, Any],
        font: QFont,
        parent: QWidget | None = None,
        live: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        conf = steering_decoder.normalize_config(cfg)
        self._conf = conf
        self._live = live or {}
        self.setWindowTitle(tr("Настройка «Градусы» — декодер"))
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # CM-F05 п.7: ширина ~×2 от редактора, содержимое в скролле,
        # OK/Отмена всегда видимы при любом масштабе Windows.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(6)

        hint = QLabel(tr(
            "Знак значения — две независимые таблицы условий: "
            "«Кратно»/«Не кратно» к делителю «Числу» (обе галочки "
            "сняты — строка не участвует). Условия внутри таблицы "
            "объединяются по И. Совпадение обеих таблиц — «конфликт», "
            "ни одной — «вне классов». Нейтраль и масштаб — из таблицы "
            "привязки переменной (нулевая точка = нейтраль). Кнопки "
            "«Записать» ловят DATA выбранного поля в строки истории."
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        _selectable(hint)
        layout.addWidget(hint)

        # CM-F05 п.8: live-поле — выбранный CAN ID и онлайн DATA по
        # настройке переменной (ID/канал/extended/байты/порядок байт).
        live_row = QHBoxLayout()
        self._live_label = QLabel("—")
        self._live_label.setFont(
            QFont("Consolas", 10, QFont.Weight.Bold)
        )
        self._live_label.setStyleSheet(
            "color: #4CAF50; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        _selectable(self._live_label)
        live_row.addWidget(self._live_label, 1)
        layout.addLayout(live_row)

        # Две раздельные таблицы записи (CM-F06): левая — коды
        # отрицательных значений, правая — положительных; по
        # независимой кнопке «Записать»/«Стоп» над каждой.
        tables_row = QHBoxLayout()
        signs = conf["signs"]
        self._tables: dict[str, _SignRecordTable] = {}
        for sign in ("neg", "pos"):
            tbl = _SignRecordTable(sign, font, self._on_rules_changed)
            self._restore_rules(tbl, signs.get(sign) or {})
            tables_row.addWidget(tbl, 1)
            self._tables[sign] = tbl
        layout.addLayout(tables_row, 1)

        # «Инвертирование» (CM-F05 п.5) — единственный флаг
        # направления; «Температура» (CM-F07) — семантическая метка
        # единицы, не конвертация.
        misc = QHBoxLayout()
        self._enabled = QCheckBox(tr("Включить декодер «Градусы»"))
        self._enabled.setFont(font)
        self._enabled.setChecked(conf["enabled"])
        misc.addWidget(self._enabled)
        self._inverted = QCheckBox(tr("Инвертирование"))
        self._inverted.setFont(font)
        self._inverted.setChecked(conf["inverted"])
        self._inverted.setToolTip(tr(
            "Меняет положительное/отрицательное направление: "
            "применяется один раз к уже декодированной величине"
        ))
        misc.addWidget(self._inverted)
        misc.addWidget(QLabel(tr("Температура:")))
        self._unit = QComboBox()
        self._unit.setFont(font)
        for label, key in (
            (tr("Не выбрано"), "none"),
            (tr("Цельсий"), "celsius"),
            (tr("Фаренгейт"), "fahrenheit"),
        ):
            self._unit.addItem(label, key)
        idx = self._unit.findData(conf["unit"])
        self._unit.setCurrentIndex(idx if idx >= 0 else 0)
        self._unit.setToolTip(tr(
            "Подпись величины (°/°C/°F) в контрольных строках, "
            "графике и условиях — без пересчёта числа"
        ))
        misc.addWidget(self._unit)
        misc.addStretch()
        layout.addLayout(misc)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Размер: ~×2 к ширине редактора переменной, но не более 90%
        # доступного экрана — кнопки видны при 100–200% масштабе
        # Windows (CM-F05 п.7).
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(
                min(1180, int(avail.width() * 0.9)),
                min(760, int(avail.height() * 0.9)),
            )
        else:
            self.resize(1180, 760)

        # Live + запись: тик — лёгкое обновление поля и слив
        # накопленных кадров в историю (пакетно, без подвисания UI,
        # F06 п.4).
        self._pending: list[bytes] = []
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._tick)
        self._live_timer.start()
        self._subscribe()

    # ---- live CAN (CM-F05 п.8) ----------------------------------------

    def _subscribe(self) -> None:
        """Подписка на поток кадров для записи — только через
        провайдера; отписка — при закрытии (F06 п.17)."""
        source = self._live.get("subscribe")
        if callable(source):
            source(self._on_frames)

    def _on_frames(self, frames: list[dict[str, Any]]) -> None:
        """Кадры шины → выбранное поле DATA → очередь записи."""
        get_id = self._live.get("fid")
        fid = get_id() if callable(get_id) else self._live.get("fid")
        if fid is None:
            return
        get_ext = self._live.get("extended")
        ext = get_ext() if callable(get_ext) else bool(
            self._live.get("extended")
        )
        for frame in frames:
            if int(frame.get("id", -1)) != fid:
                continue
            if bool(frame.get("extended", False)) != ext:
                continue
            data = bytes(frame.get("data", b""))
            window = self._field_window(data)
            if window is not None:
                self._pending.append(window)

    def _field_window(self, data: bytes) -> bytes | None:
        """Байты поля переменной из кадра; ``None`` — кадр не покрыл
        поле (в историю не пишем — F06 п.1 «выбранного поля»)."""
        get_used = self._live.get("bytes")
        used = get_used() if callable(get_used) else (
            self._live.get("bytes") or []
        )
        used = [int(p) for p in used]
        if not used or any(p < 0 or p >= len(data) for p in used):
            return None
        return bytes(data[p] for p in used)

    def _tick(self) -> None:
        """Live-строка + слив очереди записи (батч за тик)."""
        get_id = self._live.get("fid")
        fid = get_id() if callable(get_id) else self._live.get("fid")
        get_ext = self._live.get("extended")
        ext = get_ext() if callable(get_ext) else bool(
            self._live.get("extended")
        )
        get_frame = self._live.get("get_frame")
        data = (
            get_frame(fid, ext)
            if callable(get_frame) and fid is not None else None
        )
        id_text = "—" if fid is None else f"0x{fid:X}"
        data_text = (
            "—" if data is None
            else " ".join(f"{b:02X}" for b in data)
        )
        window = self._field_window(data) if data is not None else None
        field_text = "—"
        if window is not None:
            get_sblp = self._live.get("sblp")
            sblp = get_sblp() if callable(get_sblp) else (
                self._live.get("sblp") or ""
            )
            raw = (
                variable_decode.sblp_decode(window, sblp)
                if sblp
                else int.from_bytes(window, "big")
            )
            if raw is not None:
                field_text = f"0x{raw:X} ({raw})"
        self._live_label.setText(
            f"CAN ID {id_text}   |   DATA {data_text}   |   "
            + tr("поле") + f": {field_text}"
        )
        if self._pending:
            pending, self._pending = self._pending, []
            for window in pending:
                for tbl in self._tables.values():
                    tbl.capture(window)

    def _on_rules_changed(self) -> None:
        """Перевалидация всех строк — назначение делителя на одной
        строке меняет проверки остальных (F06 п.10)."""
        for tbl in self._tables.values():
            for r in range(tbl.table.rowCount()):
                tbl._revalidate_row(r)

    def _restore_rules(
        self, tbl: _SignRecordTable, rules: dict[int, str]
    ) -> None:
        """Конфиг → галочки «Кратно»/«Не кратно» фиксированных строк."""
        for r in range(tbl.table.rowCount()):
            d_item = tbl.table.item(r, tbl.COL_NUM)
            try:
                d = int(d_item.text()) if d_item is not None else None
            except (ValueError, TypeError):
                continue
            req = rules.get(d)
            boxes = tbl._req_boxes(r)
            if req and boxes is not None:
                cb_div, cb_not = boxes
                cb_div.setChecked(req == "div")
                cb_not.setChecked(req == "not")

    # ---- конфиг --------------------------------------------------------

    def _on_accept(self) -> None:
        """Некорректная настройка не подтверждается (F06 п.9):
        невалидный HEX / нарушение кратности / делитель вне 2…20 —
        диагностика и отказ."""
        errors: list[str] = []
        names = {"neg": tr("«−»"), "pos": tr("«+»")}
        for sign, tbl in self._tables.items():
            for err in tbl.errors():
                errors.append(f"{names[sign]} {err}")
        if errors:
            QMessageBox.warning(
                self, tr("Декодер «Градусы»"),
                tr("Некорректные записи (исправьте или очистите):\n")
                + "\n".join(errors[:10]),
            )
            return
        self.accept()

    def config(self) -> dict[str, Any]:
        """Нормализованная конфигурация декодера из полей диалога."""
        cfg = dict(self._conf)
        cfg["version"] = steering_decoder.CONFIG_VERSION
        cfg["enabled"] = self._enabled.isChecked()
        cfg["inverted"] = self._inverted.isChecked()
        cfg["unit"] = str(self._unit.currentData() or "none")
        cfg["signs"] = {
            sign: tbl.sign_rules() for sign, tbl in self._tables.items()
        }
        # Легаси-ключи не переписываем — нейтраль/калибровка живут в
        # таблице привязки переменной (CM-F05 п.3–4), интерполяция —
        # единственный контракт; направление v2 → inverted.
        for key in (
            "neutral", "calib", "interpolate", "left_is_negative",
            "divisors_pos", "divisors_neg", "points",
        ):
            cfg.pop(key, None)
        return cfg

    def stop_all(self) -> None:
        """Закрытие/отмена — запись остановлена и отписана
        (F06 п.17)."""
        for tbl in self._tables.values():
            tbl.stop_record()
        self._pending.clear()

    def accept(self) -> None:  # noqa: A003
        self.stop_all()
        super().accept()

    def reject(self) -> None:
        self.stop_all()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_all()
        super().closeEvent(event)
class _HoverTip(QLabel):
    """Плавно появляющаяся закруглённая табличка-подсказка над
    графиком (отчёт мастера: не «выскакивает резко»)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QLabel { background: rgba(30,32,44,235); color: #E8E8EF;"
            " border: 1px solid #3A7BD5; border-radius: 8px;"
            " padding: 4px 10px; }"
        )
        self.setFont(QFont("Segoe UI", 9))
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hide()

    def show_smooth(self) -> None:
        self._anim.stop()
        if not self.isVisible():
            self._effect.setOpacity(0.0)
            self.show()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def hide_smooth(self) -> None:
        self._anim.stop()
        if not self.isVisible():
            return
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._on_faded)
        self._anim.start()

    def _on_faded(self) -> None:
        with contextlib.suppress(RuntimeError, TypeError):
            self._anim.finished.disconnect(self._on_faded)
        if self._effect.opacity() <= 0.01:
            self.hide()


class _GraphPreview(QWidget):
    """График «сырое значение → величина»: прямая по точкам таблицы
    правок, ПРОДОЛЖЕННАЯ от значения DATA «от» до «до» (крайние точки
    линии — не точки таблицы, а границы диапазона — отчёт мастера).
    Ось X — сырое значение DATA в HEX, ось Y — величина; у обеих осей
    разметка. Наведение курсора на линию протягивает пунктиры к осям
    и плавно показывает закруглённую табличку «0xXXX = величина».
    Точки правятся в таблице рядом."""

    # Отступы под подписи осей: слева — Y, снизу — X.
    _MARGIN_LEFT = 52
    _MARGIN_BOTTOM = 30
    _MARGIN_TOP = 8
    _MARGIN_RIGHT = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[float, float]] = []
        # Ось X от полей DATA «от»/«до»: начало оси = значение «от»,
        # конец = значение «до» (отчёт мастера).
        self._axis_x: tuple[float, float] | None = None
        # «Градусы»: 4-зонный график, оси через (0,0); модель —
        # выведенная по точкам циклическая зависимость (зелёная
        # линия) или None — «перелом» (красная) (отчёт мастера).
        self._degree_mode = False
        self._degree_model = None
        # Точка курсора на линии — пунктирные проекции к осям.
        self._hover: tuple[float, float] | None = None
        self.setMinimumHeight(160)
        self.setMouseTracking(True)
        self._tip = _HoverTip(self)

    def set_points(self, points: list[tuple[float, float]]) -> None:
        self._points = sorted(points)
        self.update()

    def set_axis_x(self, x0: float | None, x1: float | None) -> None:
        """Границы оси X из полей DATA «от»/«до»; None — авто по точкам."""
        if x0 is None or x1 is None or x0 == x1:
            self._axis_x = None
        else:
            self._axis_x = (min(x0, x1), max(x0, x1))
        self.update()

    def set_degree_mode(self, enabled: bool) -> None:
        if self._degree_mode != bool(enabled):
            self._degree_mode = bool(enabled)
            self.update()

    def set_degree_model(self, model) -> None:
        self._degree_model = model
        self.update()

    def _plot_rect(self):
        return self.rect().adjusted(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            -self._MARGIN_RIGHT, -self._MARGIN_BOTTOM,
        )

    def _range(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        if self._axis_x is not None:
            x0, x1 = self._axis_x
        else:
            x0, x1 = (min(xs), max(xs)) if xs else (0.0, 1.0)
        # Точки могут выходить за ось — раздвигаем, чтобы ломаная
        # не обрезалась по краям.
        if xs:
            x0, x1 = min(x0, min(xs)), max(x1, max(xs))
        y0, y1 = (min(ys), max(ys)) if ys else (0.0, 1.0)
        # Обе оси всегда включают 0 — линия графика начинается
        # в точке пересечения осей (отчёт мастера).
        x0, y0 = min(x0, 0.0), min(y0, 0.0)
        if x1 == x0:
            x1 = x0 + 1
        if y1 == y0:
            y1 = y0 + 1
        return x0, x1, y0, y1

    def _to_screen(self, rect, x: float, y: float) -> tuple[float, float]:
        x0, x1, y0, y1 = self._range()
        px = rect.left() + (x - x0) / (x1 - x0) * rect.width()
        py = rect.bottom() - (y - y0) / (y1 - y0) * rect.height()
        return px, py

    def _from_screen(self, rect, px: float, py: float) -> tuple[float, float]:
        x0, x1, y0, y1 = self._range()
        x = x0 + (px - rect.left()) / max(1, rect.width()) * (x1 - x0)
        y = y0 + (rect.bottom() - py) / max(1, rect.height()) * (y1 - y0)
        return x, y

    @staticmethod
    def _fmt_x(value: float) -> str:
        return f"0x{int(round(value)):X}"

    @staticmethod
    def _fmt_y(value: float) -> str:
        return f"{value:g}"

    def _paint_degrees(self, painter: QPainter, rect) -> None:
        """«Градусы»: 4-зонный график — оси X и Y пересекаются
        в центре (0,0), ветки сторон таблицы привязки уходят от
        нулевой точки в своём направлении (CM-F05/F07). Зелёная
        линия — ветка с нулевой точкой и опорными; красная —
        контур неполной таблицы (нет нуля или стороны)."""
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        if len(self._points) < 3:
            painter.setPen(QPen(QColor(160, 160, 170)))
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter,
                tr("«Градусы»: минимум 3 точки "
                   "(нулевая, отрицательная, положительная)"),
            )
            return
        model = self._degree_model or {}
        sides = model.get("sides") or {}
        inverted = bool(model.get("inverted"))
        suffix = steering_decoder.unit_suffix(model.get("unit", "none"))
        zero = sides.get("zero")
        branches = {
            "neg": list(sides.get("neg") or []),
            "pos": list(sides.get("pos") or []),
        }
        if zero:
            branches["neg"].append((float(zero[0]), 0.0))
            branches["pos"].append((float(zero[0]), 0.0))
        # Ветка строится по всей ширине: точки + экстраполяция
        # крайних отрезков до границ графика — «график обязан
        # доходить до DATA от/до» (отчёт мастера).
        sign = -1.0 if inverted else 1.0
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        x_lim = max(1.0, max(abs(v) for v in xs))
        y_lim = max(1.0, max(abs(v) for v in ys))
        x_lim *= 1.15
        y_lim *= 1.15

        def to_screen(x: float, y: float) -> tuple[float, float]:
            px = rect.center().x() + x / x_lim * rect.width() / 2
            py = rect.center().y() - y / y_lim * rect.height() / 2
            return px, py

        cx, cy = int(rect.center().x()), int(rect.center().y())
        painter.setPen(QPen(QColor(150, 150, 165), 1))
        painter.drawLine(rect.left(), cy, rect.right(), cy)
        painter.drawLine(cx, rect.top(), cx, rect.bottom())
        painter.setPen(QPen(QColor(170, 170, 185), 1))
        for x in sorted({p[0] for p in self._points}):
            px, _ = to_screen(x, 0)
            painter.drawLine(int(px), cy, int(px), cy + 4)
            painter.drawText(
                int(px) - 26, cy + 5, 52, 14,
                Qt.AlignmentFlag.AlignHCenter, self._fmt_x(x),
            )
        for y in sorted({p[1] * sign for p in self._points if p[1] != 0}):
            _, py = to_screen(0, y)
            painter.drawText(
                0, int(py) - 7, self._MARGIN_LEFT - 6, 14,
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter,
                f"{y:g}{suffix}",
            )

        painter.setClipRect(rect)
        edge = x_lim * 1.05
        for side in ("neg", "pos"):
            branch = sorted(branches[side])
            complete = len(branch) >= 2 and zero is not None
            painter.setPen(QPen(
                QColor(76, 175, 80) if complete else QColor(244, 67, 54),
                2,
            ))
            if not branch:
                continue
            # Крайние точки ветки — продолжение её крайних отрезков
            # до границ графика (экстраполяция как у декодера).
            xs_line = sorted({-edge, *(x for x, _ in branch), edge})
            prev = None
            for x in xs_line:
                y = steering_decoder._piecewise(branch, x) * sign
                px, py = to_screen(x, y)
                if prev is not None:
                    painter.drawLine(
                        int(prev[0]), int(prev[1]), int(px), int(py)
                    )
                prev = (px, py)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(255, 170, 80), 1))
        painter.setBrush(QColor(255, 170, 80))
        for x, y in self._points:
            px, py = to_screen(x, y * sign)
            painter.drawEllipse(int(px) - 3, int(py) - 3, 6, 6)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()
        painter.fillRect(rect, QColor(38, 38, 48))
        painter.setPen(QPen(QColor(90, 90, 110), 1))
        painter.drawRect(rect)
        if self._degree_mode:
            self._paint_degrees(painter, rect)
            painter.end()
            return
        if len(self._points) < 2:
            painter.setPen(QPen(QColor(160, 160, 170)))
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter,
                tr("Добавьте минимум 2 точки"),
            )
            painter.end()
            return

        # Разметка осей: X — сырое значение в HEX (0 = «DATA от»,
        # конец = «DATA до»), Y — величина. От каждой точки —
        # пунктирные проекции на обе оси с фактическими значениями
        # в местах пересечения (отчёт мастера).
        x0, x1, y0, y1 = self._range()
        tick_pen = QPen(QColor(150, 150, 165), 1)
        text_pen = QPen(QColor(170, 170, 185), 1)
        grid_pen = QPen(QColor(70, 70, 86), 1, Qt.PenStyle.DashLine)
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        # Пунктирные проекции точек на оси X и Y.
        painter.setPen(grid_pen)
        for px_raw, py_raw in self._points:
            px, py = self._to_screen(rect, px_raw, py_raw)
            _, py_axis = self._to_screen(rect, px_raw, y0)
            px_axis, _ = self._to_screen(rect, x0, py_raw)
            painter.drawLine(int(px), int(py), int(px), int(py_axis))
            painter.drawLine(int(px_axis), int(py), int(px), int(py))
        # Тики на границах осей и под точками.
        painter.setPen(tick_pen)
        for _x in sorted({x0, x1, *(p[0] for p in self._points)}):
            px, _ = self._to_screen(rect, _x, y0)
            painter.drawLine(int(px), rect.bottom(), int(px), rect.bottom() + 4)
        for _y in sorted({y0, y1, *(p[1] for p in self._points)}):
            _, py = self._to_screen(rect, x0, _y)
            painter.drawLine(rect.left() - 4, int(py), rect.left(), int(py))
        # Значения на осях — под каждой точкой и на границах.
        painter.setPen(text_pen)
        for _x in sorted({x0, x1, *(p[0] for p in self._points)}):
            px, _ = self._to_screen(rect, _x, y0)
            painter.drawText(
                int(px) - 26, rect.bottom() + 6, 52, 16,
                Qt.AlignmentFlag.AlignHCenter, self._fmt_x(_x),
            )
        for _y in sorted({y0, y1, *(p[1] for p in self._points)}):
            _, py = self._to_screen(rect, x0, _y)
            painter.drawText(
                0, int(py) - 8, self._MARGIN_LEFT - 8, 16,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self._fmt_y(_y),
            )

        # Линия начинается в точке пересечения осей (0,0) и идёт до
        # значения DATA «до»: крайние точки — границы диапазона
        # «от»/«до» с экстраполяцией по краевым сегментам, точки
        # таблицы лежат на линии между ними (отчёт мастера).
        painter.setPen(QPen(QColor(108, 140, 255), 2))
        if self._axis_x is not None:
            lx0, lx1 = self._axis_x
        else:
            lx0 = min(p[0] for p in self._points)
            lx1 = max(p[0] for p in self._points)
        line_xs = [lx0, *(p[0] for p in self._points if lx0 < p[0] < lx1), lx1]
        prev = self._to_screen(rect, x0, y0)
        for x in line_xs:
            px, py = self._to_screen(rect, x, _map_points(self._points, x))
            painter.drawLine(int(prev[0]), int(prev[1]), int(px), int(py))
            prev = (px, py)
        painter.setPen(QPen(QColor(255, 170, 80), 1))
        painter.setBrush(QColor(255, 170, 80))
        for x, y in self._points:
            px, py = self._to_screen(rect, x, y)
            painter.drawEllipse(int(px) - 3, int(py) - 3, 6, 6)
        # Пунктирные проекции от точки курсора на линии к осям
        # (отчёт мастера).
        if self._hover is not None:
            hx, hy = self._hover
            px, py = self._to_screen(rect, hx, hy)
            _, py_axis = self._to_screen(rect, hx, y0)
            px_axis, _ = self._to_screen(rect, x0, hy)
            painter.setPen(QPen(QColor(124, 158, 255), 1, Qt.PenStyle.DashLine))
            painter.drawLine(int(px), int(py), int(px), int(py_axis))
            painter.drawLine(int(px_axis), int(py), int(px), int(py))
            painter.setBrush(QColor(124, 158, 255))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(int(px) - 4, int(py) - 4, 8, 8)
        painter.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Курсор на линии: пунктир к осям + плавная табличка
        «0xXXX = величина» (отчёт мастера)."""
        if len(self._points) < 2:
            self._hover = None
            self._tip.hide_smooth()
            self.update()
            return super().mouseMoveEvent(event)
        rect = self._plot_rect()
        x_raw, _y = self._from_screen(rect, event.position().x(), event.position().y())
        x0, x1, _y0, _y1 = self._range()
        x_clamped = min(max(x_raw, x0), x1)
        value = _map_points(self._points, x_clamped)
        self._hover = (x_clamped, value)
        # Табличка рядом с курсором: появление — плавное, углы
        # закруглены (отчёт мастера).
        self._tip.setText(
            tr("{0} → {1}").format(self._fmt_x(x_clamped), f"{value:g}")
        )
        self._tip.adjustSize()
        tip_x = int(event.position().x()) + 14
        tip_y = int(event.position().y()) - self._tip.height() - 10
        tip_x = max(0, min(tip_x, self.width() - self._tip.width()))
        tip_y = max(0, tip_y)
        self._tip.move(tip_x, tip_y)
        self._tip.show_smooth()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self._tip.hide_smooth()
        self.update()
        super().leaveEvent(event)


class _FrameRow(QWidget):
    """Строка фрейма статической переменной:
    ID | DLC | DATA по байтам (X — любой) | →1/→0 | ✕.

    Выбора канала нет — по отчёту мастера канал фрейма задаётся
    в Гибкой логике, а здесь фрейм описывает только «какой пакет»."""

    def __init__(
        self,
        font: QFont,
        on_remove,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.can_id = _HexIdEdit(font)
        row.addWidget(self.can_id)

        # 11/29 бит — выбор разрядности ID фрейма (отчёт мастера).
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItem(tr("11 бит"), False)
        self.bit.addItem(tr("29 бит"), True)
        # Шире — слово «бит» должно влезать целиком (отчёт мастера).
        self.bit.setFixedWidth(92)
        self.bit.setToolTip(tr("Разрядность CAN-идентификатора"))
        row.addWidget(self.bit)
        _bind_id_width(self.bit, self.can_id)

        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(0, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(56)
        row.addWidget(self.dlc)

        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        row.addWidget(data_widget)
        # DLC ограничивает количество доступных полей DATA
        # (отчёт мастера) — как в триггерах.
        self.dlc.valueChanged.connect(
            lambda v: _set_data_enabled(self.data, v)
        )
        _set_data_enabled(self.data, self.dlc.value())
        # После ввода ID поля DATA заполняются «X» — «любой байт»
        # (отчёт мастера).
        _autofill_x_on_id(self.can_id, self.data)

        # Выбор записываемого значения — справа от фрейма (по ТЗ).
        self.value = QComboBox()
        self.value.setFont(font)
        self.value.addItem(tr("→ 1"), 1)
        self.value.addItem(tr("→ 0"), 0)
        row.addWidget(self.value)

        # Онлайн-строка DATA кадра с этим ID — как в мониторинге
        # (отчёт мастера: «как только записали ID — сразу выводи»).
        self.live_label = QLabel("—")
        self.live_label.setFont(QFont("Consolas", 8))
        self.live_label.setStyleSheet("color: #7C9EFF;")
        # 8 байт «AA BB …» в Consolas 8 ≈ 130 px — 96 резало строку
        # (отчёт мастера: онлайн-DATA не помещалась).
        self.live_label.setMinimumWidth(140)
        self.live_label.setToolTip(
            tr("Онлайн-данные кадра с этим ID на шине")
        )
        _selectable(self.live_label)
        row.addWidget(self.live_label)
        row.addWidget(
            _clipboard_buttons(
                self.live_label,
                font,
                self.data,
                frame_fields={
                    "id": self.can_id,
                    "bit": self.bit,
                    "dlc": self.dlc,
                },
            )
        )

        # Крестик — стандартная иконка закрытия: символ «✕» в части
        # шрифтов не рендерится (отчёт мастера).
        remove = QPushButton()
        remove.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        remove.setFixedSize(26, 26)
        remove.setToolTip(tr("Удалить фрейм"))
        remove.clicked.connect(lambda: on_remove(self))
        row.addWidget(remove)

    def read(self) -> dict[str, Any]:
        return {
            "id": self.can_id.text(),
            "extended": bool(self.bit.currentData()),
            "dlc": self.dlc.value(),
            "data": _data_to_text(self.data),
            "value": self.value.currentData(),
        }

    def write(self, data: dict[str, Any]) -> None:
        self.can_id.setText(str(data.get("id", "")))
        self.bit.setCurrentIndex(1 if data.get("extended") else 0)
        self.dlc.setValue(int(data.get("dlc", 8)))
        _set_data_enabled(self.data, self.dlc.value())
        _text_to_data(self.data, data.get("data"))
        idx = self.value.findData(int(data.get("value", 1)))
        self.value.setCurrentIndex(idx if idx >= 0 else 0)

    def update_live(self, tab) -> None:
        """Онлайн-DATA кадра с текущим ID — опрашивается диалогом
        по таймеру (отчёт мастера)."""
        fid = hex_to_int(self.can_id.text())
        data = (
            None
            if fid is None or tab is None
            else tab.live_frame(fid, bool(self.bit.currentData()))
        )
        _set_live_data_html(self.live_label, data, self.data)


class _ValuePage(QWidget):
    """Страница байтовой переменной: ID + битность + DLC, побайтовые
    DATA «от»/«до», live-значение из CAN над графиком, «таблица
    привязки» и график перевода сырого значения в величину.

    Общий виджет двух видов переменных (отчёт мастера):
    * «Численная переменная» — значение считается из байтов
      пришедшего кадра;
    * «Динамическая переменная» — МК кэширует указанные байты каждого
      приходящего пакета: новый пакет с заданной DATA перезаписывает
      прошлые значения в кэше (хранение на стороне устройства).

    ``on_changed`` — колбэк диалога (обновление метки «N Байт»);
    ``get_row`` — доступ к строке переменной для live-значения."""

    def __init__(
        self,
        font: QFont,
        kind: str,
        get_row,
        on_changed=None,
        get_tab=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._get_row = get_row
        self._get_tab = get_tab
        self._on_changed = on_changed
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        line1 = QHBoxLayout()
        line1.addWidget(QLabel("ID:"))
        self.can_id = _HexIdEdit(font, "0C0")
        line1.addWidget(self.can_id)
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItem(tr("11 бит"), False)
        self.bit.addItem(tr("29 бит"), True)
        self.bit.setFixedWidth(92)
        self.bit.setToolTip(tr("Разрядность CAN-идентификатора"))
        line1.addWidget(self.bit)
        _bind_id_width(self.bit, self.can_id)
        line1.addWidget(QLabel("DLC:"))
        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(0, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(56)
        line1.addWidget(self.dlc)
        # «SBLP» — порядок старшинства байтов при сборке числа
        # (отчёт мастера): активна, когда в вычислении ≥2 байтов;
        # меню — «Старший байт слева» (левый ×256, BE) и «Старший
        # байт справа» (правый ×256, LE).
        self._sblp = ""
        self.sblp_btn = QPushButton("SBLP")
        self.sblp_btn.setFont(font)
        self.sblp_btn.setFixedHeight(28)
        self.sblp_btn.setToolTip(tr(
            "SBLP: порядок старшинства байтов; активна при "
            "выборе двух и более байтов в вычислении"
        ))
        self._sblp_menu = QMenu(self.sblp_btn)
        for label, mode in (
            (tr("Старший байт слева"), "be"),
            (tr("Старший байт справа"), "le"),
        ):
            action = self._sblp_menu.addAction(
                label, lambda _c=False, m=mode: self._set_sblp(m)
            )
            action.setCheckable(True)
            action.setData(mode)
        self.sblp_btn.setMenu(self._sblp_menu)
        if kind == "numeric":
            line1.addWidget(self.sblp_btn)
            # «Вывод желаемого числа»: по умолчанию точки и значения
            # ограничены пределами «ОТ»–«ДО»; «От 0 до бесконечности»
            # снимает верхнюю границу — число выводится без потолка
            # (отчёт мастера).
            self.out_range = QComboBox()
            self.out_range.setFont(font)
            self.out_range.addItem(tr("От 0 до бесконечности (∞)"), "inf")
            # CM-F05 п.1: единый режим «Градусы» — дубль «Угол руля
            # (кратность)» удалён, старые конфиги мигрируются.
            self.out_range.addItem(tr("Градусы"), "degrees")
            self.out_range.setToolTip(
                tr("Допустимый диапазон выводимого числа")
            )
            self.out_range.currentIndexChanged.connect(
                lambda _i: self._on_out_range_changed()
            )
            line1.addWidget(self.out_range)
            self.steering_cfg: dict[str, Any] = {}
            self.steering_btn = QPushButton(tr("Декодер…"))
            self.steering_btn.setFont(font)
            self.steering_btn.setFixedHeight(28)
            self.steering_btn.setToolTip(tr(
                "Делители кратности, инвертирование и единица "
                "величины; запись DATA в таблицы декодера"
            ))
            self.steering_btn.clicked.connect(self._edit_steering)
            line1.addWidget(self.steering_btn)
        line1.addStretch()
        layout.addLayout(line1)

        # Подписи «от»/«до» одной ширины — поля DATA стоят друг
        # напротив друга по вертикали (отчёт мастера).
        from_label = QLabel(tr("DATA от:"))
        to_label = QLabel(tr("DATA до:"))
        for lbl in (from_label, to_label):
            lbl.setFont(font)
            lbl.setFixedWidth(56)

        line2 = QHBoxLayout()
        line2.addWidget(from_label)
        self.data_from, from_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        line2.addWidget(from_widget)
        line2.addStretch()
        layout.addLayout(line2)

        line3 = QHBoxLayout()
        line3.addWidget(to_label)
        self.data_to, to_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        line3.addWidget(to_widget)
        line3.addStretch()
        layout.addLayout(line3)

        self.dlc.valueChanged.connect(self._on_dlc_changed)
        for edit in (*self.data_from, *self.data_to):
            edit.textChanged.connect(lambda _t: self._on_edits_changed())
        # После ввода ID поля DATA «от»/«до» заполняются «X»
        # (отчёт мастера).
        _autofill_x_on_id(self.can_id, self.data_from, self.data_to)

        hint_text = tr(
            "В расчёт идут только заполненные байты без «X»; при "
            "нескольких байтах сырое значение — их сумма. Таблица "
            "привязки: слева сырое значение этих байт (HEX — как "
            "записали, так и остаётся), справа — величина. Точки "
            "правятся в таблице и должны лежать на линии графика в "
            "пределах «ОТ»–«ДО»."
        ) if kind == "numeric" else tr(
            "МК анализирует приходящие пакеты: у кадра с этим ID "
            "указанные байты DATA перезаписывают прежние значения в "
            "кэше переменной — каждый новый пакет замещает старые "
            "байты. В перезапись идут только заполненные поля «от»/"
            "«до» без «X». Таблица привязки задаёт символьное имя "
            "значению кэша: одинаковые имена у разных DATA объединяют "
            "состояния — такие строки раскрашиваются одним цветом."
        )
        hint = QLabel(hint_text)
        hint.setFont(font)
        hint.setWordWrap(True)
        _selectable(hint)
        layout.addWidget(hint)

        live_row = QHBoxLayout()
        live_row.setSpacing(8)
        self.live_label = QLabel("0.00")
        self.live_label.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.live_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_label.setStyleSheet(
            "color: #7C9EFF; border: 1px solid #3A7BD5;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        _selectable(self.live_label)
        self.live_hex_label = QLabel("0x—")
        self.live_hex_label.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.live_hex_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_hex_label.setStyleSheet(
            "color: #9A9AA5; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        _selectable(self.live_hex_label)
        # Онлайн-строка DATA всего кадра с этим ID — как в
        # мониторинге (отчёт мастера).
        self.live_data_label = QLabel("—")
        self.live_data_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.live_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_data_label.setStyleSheet(
            "color: #4CAF50; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        self.live_data_label.setToolTip(
            tr("Онлайн-данные кадра с этим ID на шине")
        )
        _selectable(self.live_data_label)
        live_row.addStretch()
        live_row.addWidget(self.live_label)
        live_row.addWidget(self.live_hex_label)
        live_row.addWidget(self.live_data_label)
        live_row.addWidget(
            _clipboard_buttons(
                self.live_data_label,
                font,
                self.data_from,
                frame_fields={
                    "id": self.can_id,
                    "bit": self.bit,
                    "dlc": self.dlc,
                },
                copy_online=True,
            )
        )
        live_row.addStretch()
        layout.addLayout(live_row)
        self._live_timer = QTimer(self)
        # 100 мс — онлайн-DATA «тормозила» и не показывала реальные
        # кадры шины (отчёт мастера); плюс мгновенный опрос при
        # вводе ID ниже.
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._update_live_label)
        self._live_timer.start()
        # Мгновенный опрос при вводе ID/смене битности — не ждём тик
        # таймера (отчёт мастера: онлайн-DATA «тормозила»).
        self.can_id.textChanged.connect(self._update_live_label)
        self.bit.currentIndexChanged.connect(self._update_live_label)

        body = QHBoxLayout()
        left = QVBoxLayout()
        binding_title = QLabel(tr("Таблица привязки"))
        binding_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        _selectable(binding_title)
        left.addWidget(binding_title)
        self.points_table = QTableWidget(0, 2)
        self.points_table.setFont(font)
        # «Динамическая переменная»: вторая колонка — символьное имя
        # состояния (не число) — отчёт мастера.
        self.points_table.setHorizontalHeaderLabels(
            [tr("Значение DATA"), tr("Величина")]
            if kind == "numeric"
            else [tr("Значение DATA"), tr("Имя")]
        )
        self.points_table.horizontalHeader().setStretchLastSection(True)
        self.points_table.setFixedWidth(300)
        # Живая подмена кириллицы в ячейках: колонка «Значение DATA» —
        # только HEX-символы после подмены раскладки; у «Численной»
        # колонка «Величина» — цифры и разделители (отчёт мастера:
        # подмена в таблицах привязки не работала). Имена состояний
        # «Динамической» — свободный текст, не трогаем.
        # DATA-ячейка: значение может быть многобайтовым — предел по
        # DLC: «байт×2 + разделитель» на каждый байт, последнему
        # разделитель не нужен → 3·DLC−1 (отчёт мастера: «больше,
        # чем DLC, написать нельзя»).
        self.points_table.setItemDelegateForColumn(
            0,
            _BindingCellDelegate(
                "0123456789ABCDEFabcdef ",
                self,
                max_len=lambda: max(
                    2, int(self.dlc.value()) * 3 - 1
                ),
                group_hex=True,
            ),
        )
        if kind == "numeric":
            self.points_table.setItemDelegateForColumn(
                1, _BindingCellDelegate("0123456789.,+-eE", self)
            )
        self.points_table.itemChanged.connect(self._on_point_item_changed)
        left.addWidget(self.points_table, 1)
        buttons = QHBoxLayout()
        add_btn = QPushButton(tr("+ точка"))
        del_btn = QPushButton(tr("− точка"))
        for btn in (add_btn, del_btn):
            btn.setFont(font)
            btn.setFixedHeight(26)
        add_btn.clicked.connect(self.add_point_row)
        del_btn.clicked.connect(self.remove_point_row)
        buttons.addWidget(add_btn)
        buttons.addWidget(del_btn)
        buttons.addStretch()
        left.addLayout(buttons)
        # Предупреждение о точках вне диапазона «ОТ»/«ДО»
        # (только «Численная переменная» — отчёт мастера).
        self._range_warn = QLabel(tr(
            'Значение в не допустимых пределах настройки "ОТ и ДО"'
        ))
        self._range_warn.setFont(font)
        self._range_warn.setWordWrap(True)
        self._range_warn.setStyleSheet("color: #F44336;")
        self._range_warn.setVisible(False)
        left.addWidget(self._range_warn)
        body.addLayout(left)
        self.graph = _GraphPreview()
        body.addWidget(self.graph, 1)
        # График — только у «Численной переменной» (отчёт мастера).
        if kind != "numeric":
            self.graph.setVisible(False)
        layout.addLayout(body, 1)

        # Палитра групп символьных имён «Динамической переменной»:
        # строки с одинаковым именем красятся одним цветом.
        self._name_colors: dict[str, QColor] = {}
        _NAME_PALETTE = (
            "#7C9EFF", "#4CAF50", "#FFB74D", "#BA68C8",
            "#4DD0E1", "#F06292", "#AED581", "#FFD54F",
        )
        self._name_palette = _NAME_PALETTE
        # Начальное состояние SBLP: без двух байтов в вычислении
        # кнопка неактивна (отчёт мастера).
        self._update_sblp_enabled()
        self._update_steering_btn()

    # ---- внутреннее -----------------------------------------------------

    def _emit_changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    def _on_out_range_changed(self) -> None:
        self._update_steering_btn()
        # CM-F07 п.1: «Градусы» — минимум три строки привязки
        # (отрицательная опорная, нулевая, положительная опорная).
        if (
            self.out_range.currentData() == "degrees"
            and self.points_table.rowCount() < 3
        ):
            for _ in range(3 - self.points_table.rowCount()):
                self.add_point_row()
        self._on_edits_changed()

    def _edit_steering(self) -> None:
        """Диалог декодера «Градусы» (CM-F05/06): делители с записью
        DATA, инвертирование, единица; live-поле — ID/DATA/байты/
        порядок байт самой переменной."""
        tab = self._get_tab() if self._get_tab is not None else None
        live = {
            "fid": lambda: hex_to_int(self.can_id.text()),
            "extended": lambda: bool(self.bit.currentData()),
            "bytes": self.bytes_used,
            "sblp": lambda: self._sblp,
            "get_frame": (
                tab.live_frame if tab is not None else None
            ),
            "subscribe": (
                lambda cb: self._subscribe_frames(tab, cb)
                if tab is not None else None
            ),
        }
        dialog = _DegreesDecoderDialog(
            self.steering_cfg, self.font(), self, live=live
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.steering_cfg = dialog.config()
            self._refresh_graph()
            self._emit_changed()

    def _subscribe_frames(self, tab, callback) -> None:
        """Подписка диалога на поток кадров вкладки — запись DATA
        декодера (CM-F06); отписка при уничтожении виджета."""
        manager = getattr(tab, "_serial_manager", None)
        if manager is not None:
            manager.new_can_frames.connect(callback)

    def _update_steering_btn(self) -> None:
        """Кнопка «Декодер…» открывает настройку кратности режима
        «Градусы» — единственного режима (CM-F05 п.2). Нейтраль и
        масштаб — по таблице привязки, не по полям диалога."""
        if getattr(self, "steering_btn", None) is not None:
            self.steering_btn.setVisible(
                self.out_range.currentData() == "degrees"
            )

    def _set_sblp(self, mode: str) -> None:
        """Переключает режим SBLP: повторный клик по выбранному
        варианту снимает режим (отчёт мастера)."""
        self._sblp = "" if self._sblp == mode else mode
        for action in self._sblp_menu.actions():
            action.setChecked(action.data() == self._sblp)
        labels = {
            "be": tr("Старший байт слева"),
            "le": tr("Старший байт справа"),
        }
        self.sblp_btn.setText(
            "SBLP" if not self._sblp else f"SBLP: {labels[self._sblp]}"
        )
        self._emit_changed()

    def _update_sblp_enabled(self) -> None:
        """SBLP активна только когда в вычислении ≥2 байтов — записи
        с префиксом короче быть не может (отчёт мастера)."""
        enabled = (
            self._kind == "numeric"
            and len(_data_bytes_used(self.data_from)) >= 2
        )
        self.sblp_btn.setEnabled(enabled)
        if not enabled and self._sblp:
            self._set_sblp(self._sblp)

    def _on_edits_changed(self) -> None:
        self._update_sblp_enabled()
        self._refresh_graph()
        self._emit_changed()

    def _on_dlc_changed(self, value: int) -> None:
        _set_data_enabled(self.data_from, value)
        _set_data_enabled(self.data_to, value)
        self._update_sblp_enabled()
        self._refresh_graph()
        self._emit_changed()

    def add_point_row(self) -> None:
        row = self.points_table.rowCount()
        self.points_table.blockSignals(True)
        self.points_table.insertRow(row)
        self.points_table.setItem(
            row, 0, QTableWidgetItem(f"{row * 100:X}")
        )
        # У «Динамической» вторая колонка — символьное имя состояния.
        second = (
            str(row * 500) if self._kind == "numeric" else ""
        )
        self.points_table.setItem(row, 1, QTableWidgetItem(second))
        self.points_table.blockSignals(False)
        # Новая точка сразу видна — бегунок опускается вниз
        # (отчёт мастера).
        self.points_table.scrollToBottom()
        self._refresh_graph()
        self._emit_changed()

    def remove_point_row(self) -> None:
        row = self.points_table.currentRow()
        if row < 0:
            row = self.points_table.rowCount() - 1
        if row >= 0:
            self.points_table.blockSignals(True)
            self.points_table.removeRow(row)
            self.points_table.blockSignals(False)
            self._refresh_graph()
            self._emit_changed()

    def _point_raw(self, text: str) -> float | None:
        """Сырое значение X-ячейки таблицы привязки.

        Без SBLP — обычный HEX («0903» → 0x0903). В режиме SBLP байты
        собираются по выбранному порядку старшинства, как и приходящие
        байты кадра (отчёт мастера: «09 03» + «Старший байт справа»
        → 9 + 3·256 = 777, иначе интерполяция считалась бы по 0x0903).
        """
        if self._sblp:
            window = _data_text_bytes(text)
            if window is None:
                return None
            decoded = _sblp_decode(window, self._sblp)
            return float(decoded) if decoded is not None else None
        return _parse_data_hex(text)

    def _format_point_x(self, x: float) -> str:
        """Обратная запись X точки в ячейку: в режиме SBLP число
        раскладывается обратно в байты («777» + «Старший байт справа»
        → «09 03»), чтобы round-trip конфига не менял ввод оператора."""
        value = int(round(x))
        if self._sblp:
            count = max(2, (value.bit_length() + 7) // 8)
            endian = "big" if self._sblp == "be" else "little"
            raw_bytes = value.to_bytes(count, endian)
            return " ".join(f"{b:02X}" for b in raw_bytes)
        return f"{value:X}"

    def read_points(self) -> list[tuple[float, float]]:
        """Численные точки графика (только «Численная переменная»)."""
        points: list[tuple[float, float]] = []
        for row in range(self.points_table.rowCount()):
            x_item = self.points_table.item(row, 0)
            y_item = self.points_table.item(row, 1)
            if x_item is None or y_item is None:
                continue
            x = self._point_raw(x_item.text())
            y = _parse_axis_value(y_item.text())
            if x is None or y is None:
                continue
            points.append((x, y))
        return points

    def read_bindings(self) -> list[tuple[int, str]]:
        """Привязки «Динамической переменной»: (сырое значение DATA,
        символьное имя состояния). Одинаковые имена у разных значений
        объединяют состояния (отчёт мастера)."""
        bindings: list[tuple[int, str]] = []
        for row in range(self.points_table.rowCount()):
            x_item = self.points_table.item(row, 0)
            name_item = self.points_table.item(row, 1)
            if x_item is None:
                continue
            x = _parse_data_hex(x_item.text())
            name = (name_item.text().strip() if name_item is not None else "")
            if x is None or not name:
                continue
            bindings.append((int(x), name))
        return bindings

    _HEX_CELL_RE = re.compile(r"^(0[xX])?[0-9A-Fa-f]+[hH]?$")

    def _name_color(self, name: str) -> QColor:
        """Цвет группы символьного имени: одинаковые имена у разных
        строк таблицы привязки красятся одним цветом (отчёт мастера)."""
        key = name.strip()
        if key not in self._name_colors:
            idx = len(self._name_colors) % len(self._name_palette)
            self._name_colors[key] = QColor(self._name_palette[idx])
        return self._name_colors[key]

    def _on_point_item_changed(self, item: QTableWidgetItem) -> None:
        """Колонка DATA принимает только HEX; у «Численной» величина —
        число, у «Динамической» — символьное имя. Невалидная ячейка и
        HEX-значение вне диапазона «ОТ»/«ДО» подсвечиваются красным
        (отчёт мастера)."""
        text = item.text().strip()
        if item.column() == 0:
            # Кириллица на той же клавише раскладки, что латинские
            # HEX-цифры A-F — подменяем на латиницу вместо того, чтобы
            # просто красить красным; пробелы между байтами (вставка
            # «01 02») тоже не учитываем — записываем слитно (отчёт
            # мастера).
            translated = translate_cyrillic_hex(text).replace(" ", "")
            # DLC жёстко ограничивает число байтов: длиннее 2·DLC
            # hex-знаков сырое значение быть не может — лишние
            # символы обрезаются, поле «не продолжает запись» (отчёт
            # мастера).
            limit = int(self.dlc.value()) * 2
            if limit > 0 and len(translated) > limit:
                translated = translated[:limit]
            if translated != text:
                text = translated
                self.points_table.blockSignals(True)
                item.setText(text)
                self.points_table.blockSignals(False)
            ok = bool(self._HEX_CELL_RE.match(text))
            unbounded = (
                getattr(self, "out_range", None) is not None
                and self.out_range.currentData() in ("inf", "degrees")
            )
            if ok and self._kind == "numeric" and text and not unbounded:
                value = self._point_raw(text)
                lo = _edits_bytes_sum(self.data_from)
                hi = _edits_bytes_sum(self.data_to)
                if value is not None and (
                    (lo is not None and value < lo)
                    or (hi is not None and value > hi)
                ):
                    ok = False  # вне пределов «ОТ»/«ДО» — красный
        else:
            if self._kind == "numeric":
                # Численная величина: кириллица на той же клавише
                # подменяется латиницей («ю»→«.», «б»→«,», буквы —
                # hex-цифрами) — вместо красной ячейки получаем
                # валидное число (отчёт мастера).
                translated = translate_cyrillic_layout(text)
                if translated != text:
                    text = translated
                    self.points_table.blockSignals(True)
                    item.setText(text)
                    self.points_table.blockSignals(False)
            ok = (
                _parse_axis_value(text) is not None
                if self._kind == "numeric"
                else True  # символьное имя — любой текст
            )
        if self._kind != "numeric" and item.column() == 1 and ok and text:
            item.setForeground(self._name_color(text))
        else:
            item.setForeground(
                QColor("#E8E8EF") if ok or not text else QColor("#F44336")
            )
        # CM-F07 п.3: в «Градусах» код X-ячейки проверяется по
        # кратности СВОЕЙ стороны и противоположной — валидный
        # зелёный, конфликт/чужой класс красный, код без кратности
        # жёлтый. Изменение величины меняет сторону → перевалидация
        # всех строк.
        if (
            self._kind == "numeric"
            and getattr(self, "out_range", None) is not None
            and self.out_range.currentData() == "degrees"
        ):
            self._recolor_degree_cells()
        self._refresh_graph()
        self._emit_changed()

    def _recolor_degree_cells(self) -> None:
        """Проверка кодов точек привязки по кратности сторон (F07.3).

        Сторона точки — знак её величины; нулевая точка — нейтраль,
        код освобождён от проверки (F06 п.7). Класс берётся из
        текущих делителей декодера; без делителей любой валидный код
        — зелёный."""
        conf = steering_decoder.normalize_config(self.steering_cfg)
        signs = conf["signs"]
        no_rules = not signs["pos"] and not signs["neg"]
        # setForeground тоже порождает itemChanged — перекраска под
        # блокировкой сигналов, иначе рекурсия.
        self.points_table.blockSignals(True)
        for row in range(self.points_table.rowCount()):
            x_item = self.points_table.item(row, 0)
            y_item = self.points_table.item(row, 1)
            if x_item is None:
                continue
            text = x_item.text().strip()
            if not text or not self._HEX_CELL_RE.match(text):
                continue  # невалидный HEX уже красный выше
            raw = self._point_raw(text)
            y = _parse_axis_value(
                y_item.text() if y_item is not None else ""
            )
            if raw is None or y is None:
                continue
            if y == 0 or no_rules:
                # Нулевая точка — нейтраль: код совпадает с любой
                # стороной и всегда валиден (F06 п.7).
                x_item.setForeground(QColor("#4CAF50"))
                continue
            side = "pos" if y > 0 else "neg"
            cls = steering_decoder.classify(int(raw), conf)
            if cls == side or cls == "free":
                x_item.setForeground(QColor("#4CAF50"))
            elif cls == steering_decoder.STATUS_NO_CLASS:
                x_item.setForeground(QColor("#FFB74D"))
            else:  # conflict / чужой класс — явная ошибка
                x_item.setForeground(QColor("#F44336"))
        self.points_table.blockSignals(False)

    def _degree_graph_model(self) -> dict[str, Any]:
        """Модель графика «Градусы»: ветки сторон от нулевой точки
        таблицы привязки + инверсия и единица для отрисовки
        (CM-F05/F07)."""
        conf = steering_decoder.normalize_config(self.steering_cfg)
        return {
            "sides": steering_decoder.split_points(self.read_points()),
            "inverted": conf["inverted"],
            "unit": conf["unit"],
        }

    def bytes_used(self) -> list[int]:
        return _data_bytes_used(self.data_from)

    def _refresh_graph(self) -> None:
        if self._kind != "numeric":
            # Без графика: перекраска групп имён и гашение
            # предупреждения (оно только для численной).
            self._recolor_names()
            self._range_warn.setVisible(False)
            return
        # Ось X: начало = СУММА байт поля DATA «от», конец = «до»
        # (отчёт мастера: при нескольких байтах значения складываются).
        lo = _edits_bytes_sum(self.data_from)
        hi = _edits_bytes_sum(self.data_to)
        self.graph.set_axis_x(lo, hi)
        points = self.read_points()
        self.graph.set_points(points)
        # «Градусы»: 4-зонный график с осями через (0,0); линии —
        # ветки сторон таблицы привязки от нулевой точки (нулевая =
        # нейтраль, CM-F05 п.4), инверсия применяется к выводу один
        # раз (CM-F05 п.5); единица — в подписях оси Y (CM-F07).
        degrees = (
            getattr(self, "out_range", None) is not None
            and self.out_range.currentData() == "degrees"
        )
        self.graph.set_degree_mode(degrees)
        self.graph.set_degree_model(
            self._degree_graph_model() if degrees else None
        )
        # Точки привязки вне диапазона «ОТ»/«ДО» — красные символы и
        # строка предупреждения под таблицей. В режимах «От 0 до
        # бесконечности» и «Градусы» границ нет — предупреждений нет
        # (отчёт мастера).
        unbounded = (
            getattr(self, "out_range", None) is not None
            and self.out_range.currentData() in ("inf", "degrees")
        )
        out_of_range = False
        for row in range(self.points_table.rowCount()):
            item = self.points_table.item(row, 0)
            if item is None or not item.text().strip():
                continue
            value = self._point_raw(item.text())
            if value is None:
                continue
            if (
                not unbounded
                and (
                    (lo is not None and value < lo)
                    or (hi is not None and value > hi)
                )
            ):
                out_of_range = True
                item.setForeground(QColor("#F44336"))
        self._range_warn.setVisible(out_of_range)

    def _recolor_names(self) -> None:
        """Групповая раскраска имён «Динамической переменной»."""
        for row in range(self.points_table.rowCount()):
            item = self.points_table.item(row, 1)
            if item is None:
                continue
            name = item.text().strip()
            item.setForeground(
                self._name_color(name) if name else QColor("#E8E8EF")
            )

    def _binding_name(self, raw: int) -> str:
        """Имя состояния «Динамической переменной» по сырому значению."""
        for x, name in self.read_bindings():
            if x == raw:
                return name
        return ""

    def _update_live_label(self) -> None:
        row = self._get_row() if self._get_row is not None else None
        # Онлайн-DATA кадра с ID из поля — показываем всю строку
        # сразу после ввода ID (отчёт мастера).
        tab = self._get_tab() if self._get_tab is not None else None
        fid = hex_to_int(self.can_id.text())
        data = (
            None
            if fid is None or tab is None
            else tab.live_frame(fid, bool(self.bit.currentData()))
        )
        _set_live_data_html(
            self.live_data_label, data,
            [self.data_from, self.data_to],
        )
        raw = getattr(row, "live_raw", None) if row is not None else None
        stale = bool(getattr(row, "_stale_shown", False))
        # RAW просроченного кадра не выдаём за свежий — та же
        # пометка «устарело», что у числа (CM-N15).
        hex_text = "0x—" if raw is None else f"0x{raw:X}"
        if stale and raw is not None:
            hex_text += tr(" (устарело)")
        self.live_hex_label.setText(hex_text)
        if self._kind != "numeric":
            # «Динамическая»: рядом с сырым значением — имя из таблицы
            # привязки, если значение совпало (отчёт мастера).
            # CM-N15: невалидный кадр — статус, а не залипшее имя.
            if row is not None and not getattr(row, "live_valid", True):
                name = getattr(row, "_live_text", "—")
            else:
                name = (
                    self._binding_name(raw) if raw is not None else ""
                )
                if stale and name:
                    name += tr(" (устарело)")
            self.live_label.setText(name or "—")
            return
        # CM-N15: редактор повторяет контракт валидности строки —
        # невалидный/неполный кадр показывает статус, а не залипшее
        # «0.00»; декодер угла не требует обычных точек калибровки.
        if row is None:
            self.live_label.setText("—")
            return
        if not getattr(row, "live_valid", False):
            self.live_label.setText(getattr(row, "_live_text", "—"))
            return
        live = getattr(row, "live_value", None)
        if live is None:
            self.live_label.setText("—")
            return
        steering = variable_decode.is_steering(
            {
                "out_range": str(self.out_range.currentData() or ""),
                "steering": getattr(self, "steering_cfg", {}) or {},
            }
        )
        if steering:
            # Подпись декодера уже содержит градусы и RAW-HEX —
            # показываем как в строке.
            text = getattr(row, "_live_text", f"{live:.2f}")
        elif len(self.read_points()) < 2:
            # Без таблицы привязки число не определено — прочерк,
            # а не выглядящий валидным «0.00».
            self.live_label.setText("—")
            return
        else:
            text = f"{live:.2f}"
        if stale:
            text += tr(" (устарело)")
        self.live_label.setText(text)

    # ---- конфиг ---------------------------------------------------------

    def write(self, config: dict[str, Any]) -> None:
        self.can_id.setText(str(config.get("id", "")))
        self.bit.setCurrentIndex(1 if config.get("extended") else 0)
        self.dlc.setValue(int(config.get("dlc", 8)))
        _set_data_enabled(self.data_from, self.dlc.value())
        _set_data_enabled(self.data_to, self.dlc.value())
        _text_to_data(self.data_from, config.get("from"))
        _text_to_data(self.data_to, config.get("to"))
        if self._kind == "numeric":
            # Порядок байтов выставляется ДО заполнения таблицы
            # точек: формат X в ячейке зависит от режима SBLP
            # (round-trip «09 03» ↔ 777 — отчёт мастера).
            self._set_sblp(str(config.get("sblp", "") or ""))
            self._update_sblp_enabled()
            # «В пределах «ОТ»–«ДО»» и «Угол руля (кратность)» —
            # заменены единым режимом «Градусы» (отчёт мастера,
            # CM-F05 п.1) — старые конфиги «range»/«steering»
            # читаем как градусный.
            out_mode = str(config.get("out_range", "inf") or "inf")
            if out_mode in ("range", "steering"):
                out_mode = "degrees"
            idx = self.out_range.findData(out_mode)
            self.out_range.setCurrentIndex(idx if idx >= 0 else 0)
            steering = dict(config.get("steering") or {})
            # CM-F05 п.4/миграция: легаси-«нейтраль»/«калибровка»
            # декодера → точки таблицы привязки (нулевая точка и
            # ±-опорные) — правила/привязки не теряются.
            legacy_points = steering_decoder.migration_points(steering)
            for key in ("neutral", "calib"):
                steering.pop(key, None)
            self.steering_cfg = steering_decoder.normalize_config(
                steering
            )
            self._legacy_points = legacy_points
            self._update_steering_btn()
        self.points_table.setRowCount(0)
        merged_points = list(config.get("points") or [])
        if self._kind == "numeric" and getattr(
            self, "_legacy_points", None
        ):
            have = {
                (float(p[0]), float(p[1]))
                for p in merged_points
                if isinstance(p, (list, tuple)) and len(p) >= 2
            }
            for x, y in self._legacy_points:
                if (x, y) not in have:
                    merged_points.append([x, y])
        for point in merged_points:
            try:
                x, y = point
            except (TypeError, ValueError):
                continue
            row = self.points_table.rowCount()
            self.points_table.insertRow(row)
            x_text = (
                self._format_point_x(float(x))
                if isinstance(x, (int, float)) else str(x)
            )
            self.points_table.setItem(row, 0, QTableWidgetItem(x_text))
            self.points_table.setItem(row, 1, QTableWidgetItem(str(y)))
        if self.points_table.rowCount() == 0:
            for _ in range(4):
                self.add_point_row()
        if self._kind != "numeric":
            self._recolor_names()
        self._refresh_graph()

    def read(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "id": self.can_id.text().strip(),
            "extended": bool(self.bit.currentData()),
            "dlc": self.dlc.value(),
            "from": _data_to_text(self.data_from),
            "to": _data_to_text(self.data_to),
            "bytes": _data_bytes_used(self.data_from),
        }
        if self._kind == "numeric":
            cfg["sblp"] = self._sblp
            cfg["out_range"] = str(self.out_range.currentData() or "inf")
            if cfg["out_range"] == "degrees" or self.steering_cfg:
                # v3-нормализация: легаси-ключи (neutral/calib/
                # left_is_negative/interpolate) не сохраняются —
                # они уже развернуты в points/inverted при загрузке.
                cfg["steering"] = steering_decoder.normalize_config(
                    self.steering_cfg
                )
            cfg["points"] = self.read_points()
        else:
            # «Динамическая переменная»: пары (сырое значение, имя).
            cfg["points"] = [[x, name] for x, name in self.read_bindings()]
        return cfg


class _ImpulsePage(QWidget):
    """Страница «Импульсной переменной» (отчёт мастера): один фрейм —
    ID и поле DATA на 1..8 байт; приход кадра с совпадающими байтами
    поднимает переменную в «1» на 0.5 с. Онлайн-строка DATA кадра —
    как в мониторинге."""

    def __init__(
        self,
        font: QFont,
        get_tab,
        get_row=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._get_tab = get_tab
        self._get_row = get_row
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        hint = QLabel(tr(
            "Один фрейм: приход пакета с совпадающими байтами DATA "
            "поднимает переменную в «1» на 0.5 секунды. X в DATA — "
            "любой байт, пустое поле не участвует в сравнении."
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        _selectable(hint)
        layout.addWidget(hint)

        line1 = QHBoxLayout()
        line1.addWidget(QLabel("ID:"))
        self.can_id = _HexIdEdit(font, "0C0")
        line1.addWidget(self.can_id)
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItem(tr("11 бит"), False)
        self.bit.addItem(tr("29 бит"), True)
        self.bit.setFixedWidth(92)
        self.bit.setToolTip(tr("Разрядность CAN-идентификатора"))
        line1.addWidget(self.bit)
        _bind_id_width(self.bit, self.can_id)
        line1.addWidget(QLabel("DLC:"))
        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(1, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(56)
        line1.addWidget(self.dlc)
        line1.addStretch()
        layout.addLayout(line1)

        line2 = QHBoxLayout()
        data_label = QLabel(tr("DATA:"))
        data_label.setFont(font)
        data_label.setFixedWidth(56)
        line2.addWidget(data_label)
        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        line2.addWidget(data_widget)
        line2.addStretch()
        layout.addLayout(line2)
        self.dlc.valueChanged.connect(
            lambda v: _set_data_enabled(self.data, v)
        )
        _set_data_enabled(self.data, self.dlc.value())
        # После ввода ID поля DATA заполняются «X» (отчёт мастера).
        _autofill_x_on_id(self.can_id, self.data)

        # Онлайн-строка DATA кадра с этим ID (отчёт мастера).
        self.live_data_label = QLabel("—")
        self.live_data_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.live_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_data_label.setStyleSheet(
            "color: #4CAF50; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        self.live_data_label.setToolTip(
            tr("Онлайн-данные кадра с этим ID на шине")
        )
        _selectable(self.live_data_label)
        # Флаг прихода — «1» на 0.5 с при совпадении DATA, иначе «0»
        # (отчёт мастера: «в окне состояния не работает флаг прихода»;
        # зеркалит live_value строки переменной, которую выставляет
        # row.pulse() из VariablesTab._on_can_frames()).
        self.flag_label = QLabel("0")
        self.flag_label.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        self.flag_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.flag_label.setFixedWidth(36)
        self.flag_label.setStyleSheet(
            "color: #E8E8EF; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 6px;"
        )
        self.flag_label.setToolTip(
            tr("Флаг прихода: «1» на 0.5 с при совпадении DATA на шине")
        )
        live_row = QHBoxLayout()
        live_row.addStretch()
        live_row.addWidget(self.live_data_label)
        live_row.addWidget(
            _clipboard_buttons(
                self.live_data_label,
                font,
                self.data,
                frame_fields={
                    "id": self.can_id,
                    "bit": self.bit,
                    "dlc": self.dlc,
                },
                copy_online=True,
            )
        )
        live_row.addSpacing(10)
        live_row.addWidget(self.flag_label)
        live_row.addStretch()
        layout.addLayout(live_row)
        layout.addStretch(1)

        self._live_timer = QTimer(self)
        # 100 мс + мгновенный опрос по вводу ID/битности — онлайн-DATA
        # «тормозила» и не показывала реальные кадры (отчёт мастера).
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._update_live_label)
        self._live_timer.start()
        self.can_id.textChanged.connect(self._update_live_label)
        self.bit.currentIndexChanged.connect(self._update_live_label)

    def bytes_used(self) -> list[int]:
        return _data_bytes_used(self.data)

    def _update_live_label(self) -> None:
        tab = self._get_tab() if self._get_tab is not None else None
        fid = hex_to_int(self.can_id.text())
        data = (
            None
            if fid is None or tab is None
            else tab.live_frame(fid, bool(self.bit.currentData()))
        )
        _set_live_data_html(self.live_data_label, data, self.data)
        row = self._get_row() if self._get_row is not None else None
        live = getattr(row, "live_value", None) if row is not None else None
        # CM-N15: тот же контракт, что у строки — до первого кадра и
        # при невалидном кадре прочерк/статус, а не уверенный «0».
        if row is not None and not getattr(row, "live_valid", True):
            self.flag_label.setText(getattr(row, "_live_text", "—"))
        elif live is None:
            self.flag_label.setText("—")
        else:
            text = "1" if live else "0"
            if getattr(row, "_stale_shown", False):
                text += tr(" (устарело)")
            self.flag_label.setText(text)

    def read(self) -> dict[str, Any]:
        return {
            "id": self.can_id.text().strip(),
            "extended": bool(self.bit.currentData()),
            "dlc": self.dlc.value(),
            "data": _data_to_text(self.data),
            "bytes": _data_bytes_used(self.data),
        }

    def write(self, config: dict[str, Any]) -> None:
        self.can_id.setText(str(config.get("id", "")))
        self.bit.setCurrentIndex(1 if config.get("extended") else 0)
        self.dlc.setValue(int(config.get("dlc", 8)))
        _set_data_enabled(self.data, self.dlc.value())
        _text_to_data(self.data, config.get("data"))


class _CachePage(QWidget):
    """Страница «Кэш переменная» (отчёт мастера): один CAN ID,
    кадр захватывается целиком. Камень непрерывно ловит кадры с
    этим ID в скрытый буфер 1 (всегда ОЗУ — оператор его не видит
    и не настраивает); действие ГЛ «Записать КЭШ» переносит кадр
    в буфер 2 — носитель буфера 2 выбирается в шапке (ОЗУ/ПЗУ).
    Онлайн-строка показывает DATA последнего кадра, как только
    введён ID."""

    def __init__(
        self,
        font: QFont,
        get_tab,
        get_row=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._get_tab = get_tab
        self._get_row = get_row
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        hint = QLabel(tr(
            "Кэш переменная будет автоматически записывать по команде "
            "в Гибкой логике (Действия-Записать Кэш переменную)"
        ))
        hint.setFont(font)
        hint.setWordWrap(True)
        _selectable(hint)
        layout.addWidget(hint)

        line1 = QHBoxLayout()
        line1.addWidget(QLabel("ID:"))
        self.can_id = _HexIdEdit(font, "0C0")
        line1.addWidget(self.can_id)
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItem(tr("11 бит"), False)
        self.bit.addItem(tr("29 бит"), True)
        self.bit.setFixedWidth(92)
        self.bit.setToolTip(tr("Разрядность CAN-идентификатора"))
        line1.addWidget(self.bit)
        _bind_id_width(self.bit, self.can_id)
        line1.addStretch()
        layout.addLayout(line1)

        # DATA «от»/«до» — пределы анализа по байтам (отчёт мастера):
        # кадр с нужным ID берётся в буфер 1, только если каждый
        # заданный байт лежит в своих рамках «от»–«до». «X»/пустое
        # поле — байт в анализе не учитывается. В буфер 1 переписывается
        # весь кадр целиком — отдельной настройки байтов «от–до» нет.
        line2 = QHBoxLayout()
        from_label = QLabel(tr("DATA от:"))
        from_label.setFont(font)
        from_label.setFixedWidth(56)
        line2.addWidget(from_label)
        self.data_from, from_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        line2.addWidget(from_widget)
        line2.addStretch()
        layout.addLayout(line2)

        line3 = QHBoxLayout()
        to_label = QLabel(tr("DATA до:"))
        to_label.setFont(font)
        to_label.setFixedWidth(56)
        line3.addWidget(to_label)
        self.data_to, to_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        line3.addWidget(to_widget)
        line3.addStretch()
        layout.addLayout(line3)
        # После ввода ID поля DATA «от»/«до» заполняются «X» —
        # пока рамки не заданы, кадр берётся целиком (отчёт мастера).
        _autofill_x_on_id(self.can_id, self.data_from, self.data_to)

        # Онлайн-DATA последнего кадра с этим ID — выбранные байты
        # «от–до» (отчёт мастера: «при вводе id выводи онлайн поле data»).
        self.live_id_label = QLabel("—")
        self.live_id_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.live_id_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_id_label.setStyleSheet(
            "color: #7C9EFF; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 8px;"
        )
        self.live_id_label.setToolTip(
            tr("ID последнего кадра на шине")
        )
        _selectable(self.live_id_label)
        self.live_data_label = QLabel("—")
        self.live_data_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.live_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_data_label.setStyleSheet(
            "color: #4CAF50; border: 1px solid #45455A;"
            " border-radius: 6px; padding: 2px 10px;"
        )
        self.live_data_label.setToolTip(
            tr("Онлайн-данные выбранных байтов последнего кадра")
        )
        _selectable(self.live_data_label)
        live_row = QHBoxLayout()
        live_row.addStretch()
        live_row.addWidget(self.live_id_label)
        live_row.addWidget(self.live_data_label)
        live_row.addWidget(
            _clipboard_buttons(
                self.live_data_label,
                font,
                self.data_from,
                frame_fields={"id": self.can_id, "bit": self.bit},
                copy_online=True,
            )
        )
        live_row.addStretch()
        layout.addLayout(live_row)
        layout.addStretch(1)

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._update_live_label)
        self._live_timer.start()
        self.can_id.textChanged.connect(self._update_live_label)
        self.bit.currentIndexChanged.connect(self._update_live_label)

    def byte_slice(self, data: bytes) -> bytes:
        """Байты пакета для буфера 1 — кадр берётся целиком."""
        return bytes(data)

    def _update_live_label(self, *_args) -> None:
        tab = self._get_tab() if self._get_tab is not None else None
        fid = hex_to_int(self.can_id.text())
        data = (
            None
            if fid is None or tab is None
            else tab.live_frame(fid, bool(self.bit.currentData()))
        )
        if data is None:
            self.live_id_label.setText("—")
            self.live_data_label.setText("—")
            self.live_data_label.setProperty("live_plain", "")
            return
        self.live_id_label.setText(f"{fid:X}")
        _set_live_data_html(
            self.live_data_label, self.byte_slice(data),
            [self.data_from, self.data_to],
        )

    def read(self) -> dict[str, Any]:
        return {
            "id": self.can_id.text().strip(),
            "extended": bool(self.bit.currentData()),
            "data_from": _data_to_text(self.data_from),
            "data_to": _data_to_text(self.data_to),
        }

    def write(self, config: dict[str, Any]) -> None:
        # Легаси-схема «ID от–до»: диапазон кадров сжимается до одного
        # ID — берём нижнюю границу; байтовый диапазон «от–до» убран —
        # кадр захватывается целиком (отчёт мастера).
        self.can_id.setText(
            str(config.get("id", "") or config.get("id_from", ""))
        )
        self.bit.setCurrentIndex(1 if config.get("extended") else 0)
        _text_to_data(self.data_from, config.get("data_from"))
        _text_to_data(self.data_to, config.get("data_to"))
        self._update_live_label()


def _legacy_to_command(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Переводит переменную «Управления» старого формата (статическая/
    численная/динамическая/импульсная) в команду(ы) нового вида
    (отчёт мастера: видов переменных в «Управлении» больше нет).

    Статическая давала две операции «→1»/«→0» — распадается на
    команды «имя → 1» и «имя → 0»; байтовые — одна команда с её
    фреймом. Хранение (ОЗУ/ПЗУ) отбрасывается."""
    name = str(cfg.get("name", "")).strip() or tr("Команда")

    def _frame(fr: dict[str, Any]) -> dict[str, Any]:
        return {
            "channel": int(fr.get("channel", 0) or 0),
            "extended": bool(fr.get("extended", False)),
            "id": str(fr.get("id", "")),
            "dlc": int(fr.get("dlc", 8) or 8),
            "data": str(fr.get("data", "")),
            "rtr": bool(fr.get("rtr", False)),
            "delay_before_send": int(fr.get("delay_before_send", 0) or 0),
            "delay_between": int(fr.get("delay_between", 0) or 0),
            "count": max(1, int(fr.get("count", 1) or 1)),
            "next_delay": int(fr.get("next_delay", 0) or 0),
        }

    if cfg.get("type") == _TYPE_STATIC:
        commands = []
        for want in (1, 0):
            frames = [
                _frame(fr)
                for fr in cfg.get("frames") or []
                if int(fr.get("value", 1) or 0) == want
            ]
            if frames:
                commands.append({
                    "type": _TYPE_COMMAND,
                    "name": f"{name} → {want}",
                    "folder": str(cfg.get("folder", "")),
                    "frames": frames,
                    "cache": {},
                })
        return commands or [{
            "type": _TYPE_COMMAND, "name": name,
            "folder": str(cfg.get("folder", "")),
            "frames": [_frame(cfg)], "cache": {},
        }]
    return [{
        "type": _TYPE_COMMAND,
        "name": name,
        "folder": str(cfg.get("folder", "")),
        "frames": [_frame(cfg)],
        "cache": {},
    }]


class _CmdFrameRow(QWidget):
    """Строка фрейма команды «Управления» — как строка «Ответа»
    триггера (отчёт мастера): бит/ID/DLC/DATA (X — байт берётся
    из кэша команды), RTR, «Пауза между пакетами», «Кол-во»,
    «Пауза до следующего» и ✕.

    По отчёту мастера канал CAN и «автоматическая запись в кэш»
    здесь не задаются — их определяет Гибкая логика; «пауза перед
    отправкой» убрана совсем (поле в сериализации сохранено нулём
    ради совместимости файлов)."""

    def __init__(
        self,
        font: QFont,
        on_remove,
        on_changed,
        get_tab=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._get_tab = get_tab
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        # Онлайн-строка DATA кадра с этим ID — ВЫНЕСЕНА НАВЕРХ,
        # над полями ввода (отчёт мастера: «онлайн поле контроля
        # введённого ID вынеси выше»): при вводе ID сразу видно,
        # что реально летает на шине.
        live_row = QHBoxLayout()
        live_row.setContentsMargins(0, 0, 0, 0)
        live_row.setSpacing(4)
        live_title = QLabel(tr("Онлайн:"))
        live_title.setFont(font)
        live_row.addWidget(live_title)
        self.live_label = QLabel("—")
        self.live_label.setFont(QFont("Consolas", 8))
        self.live_label.setStyleSheet("color: #7C9EFF;")
        # 8 байт «AA BB …» в Consolas 8 ≈ 130 px — 96 резало строку
        # (отчёт мастера: онлайн-DATA не помещалась).
        self.live_label.setMinimumWidth(140)
        self.live_label.setToolTip(
            tr("Онлайн-данные кадра с этим ID на шине")
        )
        _selectable(self.live_label)
        live_row.addWidget(self.live_label)
        # Clipboard-кнопки создаются после self.data — виджет ниже.
        live_row.addStretch()
        box.addLayout(live_row)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        # Порядок колонок — сначала ID, затем разрядность 29/11
        # (отчёт мастера).
        row.addWidget(QLabel("ID"))
        self.can_id = _HexIdEdit(font)
        row.addWidget(self.can_id)

        row.addWidget(QLabel(tr("Бит")))
        self.bit = QComboBox()
        self.bit.setFont(font)
        self.bit.addItem(tr("11 бит"), False)
        self.bit.addItem(tr("29 бит"), True)
        self.bit.setFixedWidth(92)
        row.addWidget(self.bit)
        _bind_id_width(self.bit, self.can_id)

        row.addWidget(QLabel("DLC"))
        self.dlc = QSpinBox()
        self.dlc.setFont(font)
        self.dlc.setRange(0, 8)
        self.dlc.setValue(8)
        self.dlc.setFixedWidth(54)
        row.addWidget(self.dlc)

        self.data, data_widget = create_data_field_widget(
            font, 8, edit_width=44, allow_x=True
        )
        row.addWidget(data_widget)

        self.rtr = QCheckBox("RTR")
        self.rtr.setFont(font)
        self.rtr.setToolTip(tr("Remote Transmission Request"))
        row.addWidget(self.rtr)

        # Кнопки «копировать/вставить» — в верхнюю онлайн-строку.
        live_row.insertWidget(
            2,
            _clipboard_buttons(
                self.live_label,
                font,
                self.data,
                frame_fields={
                    "id": self.can_id,
                    "bit": self.bit,
                    "dlc": self.dlc,
                    "rtr": self.rtr,
                },
                copy_online=True,
            ),
        )

        def _ms_spin(value: int = 0) -> QSpinBox:
            spin = QSpinBox()
            spin.setFont(font)
            spin.setRange(0, 9999)
            spin.setSuffix(tr(" мс"))
            spin.setFixedWidth(86)
            spin.setValue(value)
            return spin

        row.addStretch()
        between_label = QLabel(tr("Пауза между пакетами"))
        between_label.setFont(font)
        row.addWidget(between_label)
        # Дефолт новой команды/зеркала: 3 пакета, пауза 5 мс
        # (отчёт мастера).
        self.delay_between = _ms_spin(5)
        row.addWidget(self.delay_between)
        count_label = QLabel(tr("Кол-во"))
        count_label.setFont(font)
        row.addWidget(count_label)
        self.count = QSpinBox()
        self.count.setFont(font)
        self.count.setRange(1, 255)
        self.count.setValue(3)
        self.count.setFixedWidth(58)
        row.addWidget(self.count)

        remove = QPushButton()
        remove.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_TitleBarCloseButton
            )
        )
        remove.setFixedSize(26, 26)
        remove.setToolTip(tr("Удалить фрейм"))
        remove.clicked.connect(lambda: on_remove(self))
        row.addWidget(remove)
        box.addLayout(row)

        # «Пауза до следующего фрейма» — по центру МЕЖДУ строками
        # фреймов (отчёт мастера). У последней строки скрывается через
        # set_next_pause_visible().
        self.next_delay = _ms_spin()
        self._next_row = QWidget()
        next_row = QHBoxLayout(self._next_row)
        next_row.setContentsMargins(0, 0, 0, 0)
        next_row.addStretch()
        next_label = QLabel(tr("Пауза до следующего фрейма"))
        next_label.setFont(font)
        next_row.addWidget(next_label)
        next_row.addWidget(self.next_delay)
        next_row.addStretch()
        box.addWidget(self._next_row)

        # После ввода ID поля DATA заполняются «00» — фрейм команды
        # шлёт конкретные байты, не подстановку (отчёт мастера).
        _autofill_00_on_id(self.can_id, self.data)

        self.dlc.valueChanged.connect(
            lambda v: _set_data_enabled(
                self.data, 0 if self.rtr.isChecked() else v
            )
        )
        self.rtr.toggled.connect(
            lambda checked: _set_data_enabled(
                self.data, 0 if checked else self.dlc.value()
            )
        )
        _set_data_enabled(self.data, self.dlc.value())

        for widget in (
            self.bit, self.dlc, self.count,
            self.delay_between, self.next_delay,
        ):
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(on_changed)
            else:
                widget.valueChanged.connect(on_changed)
        self.can_id.textChanged.connect(on_changed)
        self.rtr.toggled.connect(on_changed)
        for edit in self.data:
            edit.textChanged.connect(on_changed)
        # Мгновенный опрос онлайн-DATA при вводе ID/битности.
        self.can_id.textChanged.connect(self.update_live)
        self.bit.currentIndexChanged.connect(self.update_live)

    def set_next_pause_visible(self, visible: bool) -> None:
        """Строка «Пауза до следующего фрейма» видна только между
        строками — у последнего фрейма скрыта (отчёт мастера)."""
        self._next_row.setVisible(visible)

    def update_live(self) -> None:
        """Онлайн-DATA кадра с текущим ID — опрашивается таймером
        диалога и сразу по вводу ID (отчёт мастера)."""
        tab = self._get_tab() if callable(self._get_tab) else None
        fid = hex_to_int(self.can_id.text())
        data = (
            None
            if fid is None or tab is None
            else tab.live_frame(fid, bool(self.bit.currentData()))
        )
        _set_live_data_html(self.live_label, data, self.data)

    def read(self) -> dict[str, Any]:
        return {
            # Канал и пауза перед отправкой убраны из настройки
            # (отчёт мастера) — ключи остаются нулевыми для
            # совместимости формата файла/провода.
            "channel": 0,
            "extended": bool(self.bit.currentData()),
            "id": self.can_id.text().strip(),
            "dlc": self.dlc.value(),
            "data": _data_to_text(self.data),
            "rtr": self.rtr.isChecked(),
            "delay_before_send": 0,
            "delay_between": self.delay_between.value(),
            "count": self.count.value(),
            "next_delay": self.next_delay.value(),
        }

    def write(self, data: dict[str, Any]) -> None:
        self.bit.setCurrentIndex(1 if data.get("extended") else 0)
        self.can_id.setText(str(data.get("id", "")))
        self.dlc.setValue(int(data.get("dlc", 8) or 8))
        self.rtr.setChecked(bool(data.get("rtr", False)))
        _text_to_data(self.data, data.get("data"))
        _set_data_enabled(
            self.data, 0 if self.rtr.isChecked() else self.dlc.value()
        )
        # Отсутствующие ключи → дефолт команды 3 пакета / 5 мс
        # (отчёт мастера); явные значения оператора сохраняются.
        self.delay_between.setValue(int(data.get("delay_between", 5) or 0))
        self.count.setValue(max(1, int(data.get("count", 3) or 3)))
        self.next_delay.setValue(int(data.get("next_delay", 0) or 0))


class _CommandDialog(QDialog):
    """Настройка команды «Управления» (отчёт мастера): имя + список
    фреймов «как в триггерах в разделе Ответ» — пауза между
    пакетами/до следующего, количество, RTR. Канал CAN и
    «автоматическая запись DATA в кэш» из этого окна убраны: их
    определяет Гибкая логика. Носителей ОЗУ/ПЗУ у команд нет."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        font = QFont("Segoe UI", 9)
        self.setFont(font)
        self.setWindowTitle(tr("Настройка команды"))
        # Шире и выше прежнего: строка фрейма с байтовыми полями,
        # онлайн-DATA и паузами не влезала (отчёт мастера — «символы
        # и поля не влезают, таблицу выше и шире»).
        self.setMinimumSize(1440, 560)
        config = config or {}
        self.config = dict(config)
        # Вкладка «Переменные» — источник онлайн-кадров шины для
        # live-DATA строк фреймов (отчёт мастера).
        self._var_tab = parent

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel(tr("Имя команды:")))
        self._name_edit = QLineEdit(config.get("name", ""))
        self._name_edit.setFont(font)
        self._name_edit.setFixedWidth(260)
        head.addWidget(self._name_edit)
        head.addStretch()
        layout.addLayout(head)

        # «Фреймы команды» — как «Фреймы ответа» в триггерах.
        self._frames_group = QFrame()
        self._frames_group.setStyleSheet(
            "QFrame { border: 1px solid #45455A; border-radius: 6px; }"
        )
        frames_box = QVBoxLayout(self._frames_group)
        frames_box.setSpacing(4)
        frames_head = QHBoxLayout()
        frames_title = QLabel(tr("Фреймы команды"))
        frames_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        frames_head.addWidget(frames_title)
        frames_head.addStretch()
        # ASCII «+» белым: fullwidth «＋» на Windows не покрывается
        # Segoe UI и рендерился синим прямоугольником-заглушкой
        # (отчёт мастера: «кнопка просто синяя, замени на белый +»).
        add_frame = QPushButton("+")
        add_frame.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        add_frame.setStyleSheet(
            "QPushButton { color: #FFFFFF; padding-bottom: 2px; }"
        )
        add_frame.setFixedSize(28, 28)
        add_frame.setToolTip(tr("Добавить фрейм"))
        add_frame.clicked.connect(lambda: self._add_frame_row(font))
        frames_head.addWidget(add_frame)
        frames_box.addLayout(frames_head)
        self._frames_layout = QVBoxLayout()
        self._frames_layout.setSpacing(4)
        # Строки фреймов прижаты к верху — окно не «раздувает» их
        # по высоте (отчёт мастера: «окно высокое с растянутыми
        # полями записи»).
        self._frames_layout.addStretch(1)
        frames_box.addLayout(self._frames_layout)
        # Секция фреймов тянется и по вертикали — строк не пережимает
        # при добавлении (отчёт мастера).
        layout.addWidget(self._frames_group, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._frame_rows: list[_CmdFrameRow] = []
        frames = config.get("frames")
        if frames:
            for fr in frames:
                row = self._add_frame_row(font)
                row.write(fr)
        else:
            self._add_frame_row(font)

        # Онлайн-DATA по введённым ID — опрос 100 мс (отчёт мастера).
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._refresh_live)
        self._live_timer.start()

    def _add_frame_row(self, font: QFont) -> _CmdFrameRow:
        row = _CmdFrameRow(
            font,
            on_remove=self._remove_frame_row,
            on_changed=lambda: None,
            get_tab=lambda: self._var_tab,
        )
        self._frame_rows.append(row)
        self._frames_layout.insertWidget(
            self._frames_layout.count() - 1, row
        )
        self._update_pause_rows()
        row.update_live()
        return row

    def _update_pause_rows(self) -> None:
        """«Пауза до следующего фрейма» показывается по центру МЕЖДУ
        строками — у последней строки скрыта (отчёт мастера)."""
        for i, row in enumerate(self._frame_rows):
            row.set_next_pause_visible(i < len(self._frame_rows) - 1)

    def _refresh_live(self) -> None:
        """Опрос онлайн-DATA всех строк фреймов (таймер диалога)."""
        for row in self._frame_rows:
            row.update_live()

    def _remove_frame_row(self, row: _CmdFrameRow) -> None:
        if row in self._frame_rows:
            self._frame_rows.remove(row)
        self._frames_layout.removeWidget(row)
        retire(row)
        self._update_pause_rows()

    def current_config(self) -> dict[str, Any]:
        """Снимок полей без валидации — частичный ввод тоже
        сохраняется при закрытии окна (отчёт мастера)."""
        return {
            "type": _TYPE_COMMAND,
            "name": self._name_edit.text().strip(),
            "folder": self.config.get("folder", ""),
            "frames": [r.read() for r in self._frame_rows],
            # Блок автокэша убран из настройки (его роль перешла в
            # Гибкую логику) — ключ сохранён выключенным ради
            # совместимости формата файла.
            "cache": {"enabled": False},
        }

    def accept(self) -> None:  # noqa: D102
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(
                self, tr("Команда"), tr("Введите имя команды.")
            )
            return
        frames = [r.read() for r in self._frame_rows]
        if not frames or not any(
            hex_to_int(str(fr.get("id", ""))) is not None
            for fr in frames
        ):
            QMessageBox.warning(
                self,
                tr("Команда"),
                tr("Добавьте хотя бы один фрейм с заполненным ID."),
            )
            return
        # ID за пределами выбранной разрядности (11 бит → ≤7FF,
        # 29 бит → ≤1FFFFFFF) — не даём сохранить (отчёт мастера).
        for row in self._frame_rows:
            if row.can_id.text().strip() and not row.can_id.is_within_width():
                QMessageBox.warning(
                    self,
                    tr("Команда"),
                    tr(
                        "ID {0} не помещается в {1} бит — исправьте "
                        "значение или разрядность."
                    ).format(
                        row.can_id.text().strip(),
                        "29" if row.bit.currentData() else "11",
                    ),
                )
                return
        self.config = self.current_config()
        super().accept()


class VariableDialog(QDialog):
    """Настройка одной переменной: выбор вида, имени, носителя
    (ОЗУ/ПЗУ — при записи) и параметров.

    ``row`` — строка переменной в таблице: по ней диалог показывает
    live-значение из CAN над графиком привязки. ``for_control`` —
    переменная из колонки «Управление»: подсказка статической
    страницы другая (отчёт мастера)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config: dict[str, Any] | None = None,
        row: _VariableRow | None = None,
        for_control: bool = False,
    ) -> None:
        super().__init__(parent)
        font = QFont("Segoe UI", 9)
        self.setWindowTitle(tr("Настройка переменной"))
        # Шире по отчёту мастера: онлайн-DATA не помещалась между
        # значением бита и кнопками копирования в статической строке —
        # расширение идёт за счёт общего увеличения окна.
        self.setMinimumWidth(1100)
        self.setFont(font)
        config = config or {}
        self._row = row
        self._for_control = for_control
        # Вкладка «Переменные» — источник онлайн-кадров для
        # live-строк DATA в таблицах настройки (отчёт мастера).
        self._var_tab = parent if isinstance(parent, VariablesTab) else None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel(tr("Вид переменной:")))
        self._type_combo = QComboBox()
        self._type_combo.setFont(font)
        self._type_combo.addItem(tr("Статическая переменная"), _TYPE_STATIC)
        # «Динамическая» переименована в «Численную» (отчёт мастера);
        # «Динамическая переменная» — новый вид с кэшем байтов на МК.
        self._type_combo.addItem(tr("Численная переменная"), _TYPE_DYNAMIC)
        self._type_combo.addItem(tr("Динамическая переменная"), _TYPE_DYNCACHE)
        self._type_combo.addItem(tr("Импульсная переменная"), _TYPE_IMPULSE)
        # «Кэш переменная» — диапазон ID, два буфера (скрытый ОЗУ +
        # операторский ОЗУ/ПЗУ), события/условия/действия в ГЛ
        # (отчёт мастера).
        self._type_combo.addItem(tr("Кэш переменная"), _TYPE_CACHE)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        head.addWidget(self._type_combo)
        head.addSpacing(16)
        head.addWidget(QLabel(tr("Имя функции:")))
        self._name_edit = QLineEdit(config.get("name", ""))
        self._name_edit.setFont(font)
        self._name_edit.setFixedWidth(220)
        head.addWidget(self._name_edit)
        head.addSpacing(16)
        # Носитель задаётся здесь — при записи переменной; из строки
        # таблицы убран по отчёту мастера. У «Импульсной переменной»
        # хранение вообще не применимо (значение — вспышка на 0.5 с) —
        # вся группа «Хранить» скрывается целиком (отчёт мастера).
        self._storage_label = QLabel(tr("Хранить:"))
        head.addWidget(self._storage_label)
        # Галочка кэширования настройки — «в бит» у статических, «в байт»
        # у динамических (переменная считается на байтах — отчёт
        # мастера). Включённая — выбор ОЗУ/ПЗУ запоминается.
        self._cache_bit_check = QCheckBox(tr("в бит"))
        self._cache_bit_check.setFont(font)
        self._cache_bit_check.setChecked(bool(config.get("cache_bit", True)))
        self._cache_bit_check.setToolTip(
            tr("Кэшировать настройку хранения в бите переменной")
        )
        head.addWidget(self._cache_bit_check)
        self._ram_radio = QRadioButton(tr("ОЗУ"))
        self._rom_radio = QRadioButton(tr("ПЗУ"))
        self._none_radio = QRadioButton(tr("Не хранить"))
        for radio in (self._ram_radio, self._rom_radio, self._none_radio):
            radio.setFont(font)
        storage = config.get("storage", "ram")
        self._ram_radio.setChecked(storage != "rom" and storage != "none")
        self._rom_radio.setChecked(storage == "rom")
        self._none_radio.setChecked(storage == "none")
        # Без галочки «в бит»/«в байт» выбор носителя не кликабелен
        # (отчёт мастера).
        for radio in (self._ram_radio, self._rom_radio, self._none_radio):
            self._cache_bit_check.toggled.connect(radio.setEnabled)
            radio.setEnabled(self._cache_bit_check.isChecked())
        head.addWidget(self._ram_radio)
        head.addWidget(self._rom_radio)
        head.addWidget(self._none_radio)
        # Под байтовую переменную память выделяется в байтах: показываем,
        # сколько байт займёт значение по заполненным полям DATA —
        # счёт обновляется при каждой правке DATA (отчёт мастера:
        # «1 Байт» не менялся при выборе двух байт).
        self._storage_size_label = QLabel("")
        self._storage_size_label.setFont(font)
        self._storage_size_label.setStyleSheet("color: #9A9AA5;")
        _selectable(self._storage_size_label)
        head.addWidget(self._storage_size_label)
        # «Перенести в управление»: копия переменной появляется
        # командой в колонке «Управление» — у каждого пакета из её
        # таблицы привязки своё «Кол-во»/«Пауза» (отчёт мастера).
        self._to_control_check = QCheckBox(tr("Перенести в управление"))
        self._to_control_check.setFont(font)
        self._to_control_check.setChecked(bool(config.get("to_control")))
        self._to_control_check.setToolTip(
            tr("Создаёт в разделе «Управление» команду с фреймами "
               "этой переменной — для вызова из Гибкой логики")
        )
        self._to_control_check.setVisible(not self._for_control)
        head.addWidget(self._to_control_check)
        head.addStretch()
        layout.addLayout(head)

        self._stack = QStackedWidget()
        self._static_page = self._build_static_page(font)
        self._num_page = _ValuePage(
            font, "numeric",
            get_row=lambda: self._row,
            on_changed=self._refresh_storage_size,
            get_tab=lambda: self._var_tab,
        )
        self._dyn_page = _ValuePage(
            font, "dyn",
            get_row=lambda: self._row,
            on_changed=self._refresh_storage_size,
            get_tab=lambda: self._var_tab,
        )
        self._impulse_page = _ImpulsePage(
            font, get_tab=lambda: self._var_tab, get_row=lambda: self._row
        )
        self._cache_page = _CachePage(
            font, get_tab=lambda: self._var_tab, get_row=lambda: self._row
        )
        self._stack.addWidget(self._static_page)
        self._stack.addWidget(self._num_page)
        self._stack.addWidget(self._dyn_page)
        self._stack.addWidget(self._impulse_page)
        self._stack.addWidget(self._cache_page)
        layout.addWidget(self._stack, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._apply_config(config)
        self._on_type_changed(self._type_combo.currentIndex())

        # Онлайн-DATA во всех таблицах настройки переменной —
        # опрос вкладки по таймеру (отчёт мастера: «как только
        # записали ID — сразу выводи онлайн»).
        self._live_poll = QTimer(self)
        self._live_poll.setInterval(250)
        self._live_poll.timeout.connect(self._poll_live)
        self._live_poll.start()

    def _poll_live(self) -> None:
        """Раздаёт онлайн-DATA строкам фреймов статической страницы
        (численная/динамическая/импульсная опрашивают свои таймеры)."""
        for row in self._frame_rows:
            row.update_live(self._var_tab)

    # ---- страница «Статическая переменная» -------------------------

    def _build_static_page(self, font: QFont) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(6)

        # У колонки «Управление» другая подсказка (отчёт мастера).
        hint_text = (
            tr(
                "Разные фреймы могут отвечать за выполнение одной и той "
                "же функции. И они же будут записывать и переписывать "
                "1 бит. X в DATA — любой байт, пустое поле не участвует "
                "в сравнении."
            )
            if self._for_control
            else tr(
                "Фреймов может быть сколько угодно: приход любого из них "
                "пишет «→ 1» или «→ 0» в один и тот же бит ОЗУ функции — "
                "разные фреймы могут включать и выключать её. X в DATA — "
                "любой байт, пустое поле не участвует в сравнении. "
                "Пример: фрейм «дверь открыта» → 1, «дверь закрыта» → 0. "
                "Канал фрейма задаётся в Гибкой логике."
            )
        )
        hint = QLabel(hint_text)
        hint.setFont(font)
        hint.setWordWrap(True)
        _selectable(hint)
        layout.addWidget(hint)

        container = QWidget()
        self._frames_layout = QVBoxLayout(container)
        self._frames_layout.setSpacing(4)
        self._frames_layout.setContentsMargins(0, 0, 0, 0)
        self._frames_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setMinimumHeight(170)
        layout.addWidget(scroll, 1)

        add_row = QHBoxLayout()
        add_btn = QPushButton(tr("＋ Добавить фрейм"))
        add_btn.setFont(font)
        add_btn.clicked.connect(lambda: self._add_frame_row(None))
        add_row.addWidget(add_btn)
        add_row.addStretch()
        layout.addLayout(add_row)
        self._frame_rows: list[_FrameRow] = []
        return page

    def _add_frame_row(self, data: dict[str, Any] | None) -> None:
        font = QFont("Segoe UI", 9)
        row = _FrameRow(font, self._remove_frame_row)
        if data is not None:
            row.write(data)
        self._frame_rows.append(row)
        self._frames_layout.insertWidget(
            self._frames_layout.count() - 1, row
        )

    def _remove_frame_row(self, row: _FrameRow) -> None:
        if row in self._frame_rows:
            self._frame_rows.remove(row)
        self._frames_layout.removeWidget(row)
        retire(row)

    def _active_value_page(self) -> _ValuePage | None:
        """Страница байтовой переменной текущего вида или None."""
        if self._type_combo.currentData() == _TYPE_DYNAMIC:
            return self._num_page
        if self._type_combo.currentData() == _TYPE_DYNCACHE:
            return self._dyn_page
        return None

    def _refresh_storage_size(self, *_args) -> None:
        """Метка «N Байт» в шапке — фактическое число байт значения
        по заполненным полям DATA «от» (обновляется при каждой правке
        — отчёт мастера: «1 Байт» не менялся при выборе двух байт)."""
        if self._type_combo.currentData() == _TYPE_IMPULSE:
            count = max(1, len(self._impulse_page.bytes_used()))
            self._storage_size_label.setText(
                tr("{0} Байт").format(count)
            )
            return
        if self._type_combo.currentData() == _TYPE_CACHE:
            # У кэш-переменной метка буфера не выводится
            # (отчёт мастера: «убери надпись "Буфер 2: 8 Байт"»).
            self._storage_size_label.setText("")
            return
        page = self._active_value_page()
        if page is None:
            self._storage_size_label.setText("")
            return
        count = max(1, len(page.bytes_used()))
        self._storage_size_label.setText(tr("{0} Байт").format(count))

    # ---- общее --------------------------------------------------------

    def _on_type_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        # Пример имени зависит от вида переменной (отчёт мастера):
        # численная → «Обороты ДВС», динамическая → «Состояние АКПП»,
        # импульсная → «Нажатие кнопки», статическая → «Дверь водителя».
        example = (
            tr("например, «Обороты ДВС»") if index == 1
            else tr("например, «Состояние АКПП»") if index == 2
            else tr("например, «Нажатие кнопки»") if index == 3
            else tr("например, «Кэш пакетов»") if index == 4
            else tr("например, «Дверь водителя»")
        )
        self._name_edit.setPlaceholderText(example)
        # У новой переменной таблица точек пуста — при первом заходе
        # на байтовую страницу даём две стартовые точки, чтобы график
        # сразу строился (отчёт мастера).
        page = self._active_value_page()
        if page is not None and page.points_table.rowCount() == 0:
            page.add_point_row()
            page.add_point_row()
        # «в бит» — статические (бит состояния), «в байт» — байтовые
        # переменные (значение занимает байты) — отчёт мастера.
        self._cache_bit_check.setText(
            tr("в байт")
            if page is not None or index == 3
            else tr("в бит")
        )
        # «Импульсная переменная» ничего не хранит (значение — вспышка
        # на 0.5 с, а не состояние) — весь выбор носителя скрывается,
        # а не просто «Не хранить» (отчёт мастера).
        is_impulse = index == 3
        # «Кэш переменная»: выбор ОЗУ/ПЗУ остаётся — он задаёт носитель
        # БУФЕРА 2 (буфер 1 всегда ОЗУ и оператором не настраивается);
        # галочка «в бит/в байт» здесь не применима (отчёт мастера).
        is_cache = index == 4
        for widget in (
            self._storage_label,
            self._ram_radio, self._rom_radio,
        ):
            widget.setVisible(not is_impulse)
        self._cache_bit_check.setVisible(not is_impulse and not is_cache)
        if is_cache:
            # Радио-кнопки были заблокированы снятой галочкой —
            # для кэш-переменной носитель выбирается напрямую.
            self._ram_radio.setEnabled(True)
            self._rom_radio.setEnabled(True)
        else:
            enabled = self._cache_bit_check.isChecked()
            self._ram_radio.setEnabled(enabled)
            self._rom_radio.setEnabled(enabled)
        # «Не хранить» был виден только у импульсной — теперь у неё
        # скрыта вся группа носителя, этот вариант больше нигде не нужен.
        self._none_radio.setVisible(False)
        if self._none_radio.isChecked():
            self._ram_radio.setChecked(True)
        if page is not None:
            _set_data_enabled(page.data_from, page.dlc.value())
            _set_data_enabled(page.data_to, page.dlc.value())
            page._refresh_graph()
        self._refresh_storage_size()

    def _apply_config(self, config: dict[str, Any]) -> None:
        var_type = config.get("type", _TYPE_STATIC)
        # Принимаем и старые обозначения видов (прототип ранних сборок).
        if var_type in ("flags", _TYPE_STATIC):
            var_type = _TYPE_STATIC
            idx = 0
        elif var_type == _TYPE_DYNCACHE:
            idx = 2
        elif var_type == _TYPE_IMPULSE:
            idx = 3
        elif var_type == _TYPE_CACHE:
            idx = 4
        else:
            var_type = _TYPE_DYNAMIC
            idx = 1
        self._type_combo.setCurrentIndex(idx)
        if var_type == _TYPE_STATIC:
            frames = config.get("frames") or config.get("packets") or []
            for frame in frames:
                self._add_frame_row(frame)
            if not self._frame_rows:
                self._add_frame_row(None)
        elif var_type == _TYPE_IMPULSE:
            self._impulse_page.write(config)
        elif var_type == _TYPE_CACHE:
            self._cache_page.write(config)
        else:
            page = self._num_page if var_type == _TYPE_DYNAMIC else self._dyn_page
            page.write(config)

    def _on_accept(self) -> None:
        """Проверка DATA при записи байтовой переменной
        (отчёт мастера): валидный ID, хотя бы один байт в «от»,
        минимум две точки графика."""
        if self._type_combo.currentData() == _TYPE_IMPULSE:
            errors: list[str] = []
            if hex_to_int(self._impulse_page.can_id.text()) is None:
                errors.append(tr("ID — шестнадцатеричное число"))
            elif not self._impulse_page.can_id.is_within_width():
                errors.append(tr("ID не помещается в выбранную разрядность"))
            if not self._impulse_page.bytes_used():
                errors.append(tr("DATA — заполните хотя бы один байт"))
            if errors:
                QMessageBox.warning(
                    self,
                    tr("Проверка переменной"),
                    tr("Исправьте поля: {0}").format(", ".join(errors)),
                )
                return
            self.accept()
            return
        if self._type_combo.currentData() == _TYPE_CACHE:
            # «Кэш переменная»: один CAN ID + байты «от–до» внутри
            # пакета (отчёт мастера).
            errors = []
            if hex_to_int(self._cache_page.can_id.text()) is None:
                errors.append(tr("ID — шестнадцатеричное число"))
            elif not self._cache_page.can_id.is_within_width():
                errors.append(tr("ID не помещается в выбранную разрядность"))
            if errors:
                QMessageBox.warning(
                    self,
                    tr("Проверка переменной"),
                    tr("Исправьте поля: {0}").format(", ".join(errors)),
                )
                return
            self.accept()
            return
        if self._type_combo.currentData() == _TYPE_STATIC:
            # Разрядность ID по каждому фрейму (отчёт мастера:
            # при «11 бит» принимался 29-битный ID).
            for row in self._frame_rows:
                if (
                    row.can_id.text().strip()
                    and not row.can_id.is_within_width()
                ):
                    QMessageBox.warning(
                        self,
                        tr("Проверка переменной"),
                        tr(
                            "ID {0} не помещается в {1} бит — исправьте "
                            "значение или разрядность."
                        ).format(
                            row.can_id.text().strip(),
                            "29" if row.bit.currentData() else "11",
                        ),
                    )
                    return
        page = self._active_value_page()
        if page is not None:
            errors = []
            if hex_to_int(page.can_id.text()) is None:
                errors.append(tr("ID — шестнадцатеричное число"))
            elif not page.can_id.is_within_width():
                errors.append(tr("ID не помещается в выбранную разрядность"))
            if not page.bytes_used():
                errors.append(tr("DATA от — заполните хотя бы один байт"))
            if page._kind == "numeric":
                if len(page.read_points()) < 2:
                    errors.append(
                        tr("график — минимум 2 точки с корректными значениями")
                    )
            elif not page.read_bindings():
                errors.append(
                    tr("таблица привязки — хотя бы одна строка "
                       "«значение DATA + имя»")
                )
            if errors:
                QMessageBox.warning(
                    self,
                    tr("Проверка переменной"),
                    tr("Исправьте поля: {0}").format(", ".join(errors)),
                )
                return
        self.accept()

    @property
    def config(self) -> dict[str, Any]:
        """Текущая конфигурация диалога."""
        base: dict[str, Any] = {
            "name": self._name_edit.text().strip(),
            "storage": (
                "none" if self._none_radio.isChecked()
                else "rom" if self._rom_radio.isChecked()
                else "ram"
            ),
            "cache_bit": self._cache_bit_check.isChecked(),
            "to_control": self._to_control_check.isChecked(),
        }
        var_type = self._type_combo.currentData()
        if var_type == _TYPE_STATIC:
            base.update({
                "type": _TYPE_STATIC,
                "frames": [row.read() for row in self._frame_rows],
            })
        elif var_type == _TYPE_IMPULSE:
            base.update(
                {"type": _TYPE_IMPULSE},
                **self._impulse_page.read(),
            )
        elif var_type == _TYPE_CACHE:
            base.update(
                {"type": _TYPE_CACHE},
                **self._cache_page.read(),
            )
        else:
            base.update(
                {"type": var_type},
                **self._active_value_page().read(),
            )
        return base


# Функции доп. канала входа/выхода (отчёт мастера: в таблице
# «Входы и выходы» настраивается только конкретный канал — без
# информации о фреймах).
_AUX_FUNCTIONS = (
    ("analog_out", "Аналоговый выход"),
    ("pwm", "ШИМ"),
    ("discrete_in", "Дискретный вход"),
    ("discrete_out", "Дискретный выход"),
)

# Вид «Переменная» — именованный флаг из одного бита ОЗУ/ПЗУ
# (отчёт мастера): оператор выбирает носитель, бит резервируется,
# имя фигурирует в Гибкой логике (событие «Стала 1/0», условие,
# действия «включить/включить на/выключить»).
_TYPE_FLAG = "flag"


class _AuxChannelsDialog(QDialog):
    """Состояние семи каналов выхода с устройства (CMD_AUX_GET) и
    владение спорными пинами (CMD_AUX_MAP) — протокол 13.

    Строки: канал (новая нумерация), физический пин, режим AUX_MODE_*,
    «работает» (логическое состояние — ШИМ в нуле и 0% duty считаются
    активными), уровень на пине. Спорные пины PA14/PD2/PB3 отдаются
    каналам галочками владения; снятая галочка возвращает пин
    SWD/TERM-функции. Маска хранится в CFGH — переживает перезагрузку
    и обновление."""

    _SHARED = (  # (канал, бит владения, чем делится пин)
        (1, 0x01, "SWD (SWCLK)"),
        (6, 0x20, "CAN2 TERM"),
        (7, 0x40, "CAN1 TERM"),
    )
    _MODES = ("off", "on", "pulse", "pwm")

    def __init__(self, serial_manager, font: QFont,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sm = serial_manager
        self.setWindowTitle(tr("Каналы выхода устройства"))
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        state = serial_manager.read_aux_state() if serial_manager else None
        if state is None:
            hint = QLabel(tr(
                "Устройство не поддерживает расширенные каналы "
                "(протокол 13) или не отвечает — обновите прошивку."
            ))
            hint.setFont(font)
            hint.setWordWrap(True)
            _selectable(hint)
            layout.addWidget(hint)
            close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            close.rejected.connect(self.reject)
            close.clicked.connect(self.reject)
            layout.addWidget(close)
            return

        owned = state["owned"]
        active = state["active"]
        level = state["level"]
        modes = state["modes"]

        # Расширенный ответ протокола 13 (CM-N6): фактические фазы
        # сигнала в мс — показываем ПРИМЕНЁННЫЙ ШИМ, не запрошенный.
        on_ms = state.get("on_ms") or []
        off_ms = state.get("off_ms") or []

        table = QTableWidget(7, 6)
        table.setFont(font)
        table.setHorizontalHeaderLabels(
            [tr("Канал"), tr("Пин"), tr("Режим"), tr("Работает"),
             tr("Уровень"), tr("Фазы (мс)")]
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for ch in range(1, 8):
            r = ch - 1
            mode = modes[r] if r < len(modes) else 0
            phases = "—"
            if mode in (2, 3) and r < len(on_ms) and r < len(off_ms):
                phases = f"{on_ms[r]}+{off_ms[r]}"
            cells = (
                str(ch),
                AUX_PINS[ch],
                self._MODES[mode] if mode < len(self._MODES) else "?",
                "✓" if active & (1 << r) else "—",
                "1" if level & (1 << r) else "0",
                phases,
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        table.setFixedHeight(
            table.verticalHeader().defaultSectionSize() * 7
            + table.horizontalHeader().height() + 4
        )
        layout.addWidget(table)

        layout.addWidget(QLabel(tr("Владение спорными пинами:")))
        self._own_checks: dict[int, QCheckBox] = {}
        for ch, bit, conflict in self._SHARED:
            cb = QCheckBox(
                tr("{0} ({1}) — отдать каналу {2}: {1} отключён")
                .format(AUX_PINS[ch], conflict, ch)
            )
            cb.setFont(font)
            cb.setChecked(bool(owned & bit))
            cb.setToolTip(tr(
                "Снятая галочка возвращает пин функции {0}"
            ).format(conflict))
            layout.addWidget(cb)
            self._own_checks[bit] = cb
        warn = QLabel(tr(
            "PA14 — отладочный SWCLK: после отдачи каналу прошивка по "
            "SWD недоступна до перезагрузки с неотданным пином "
            "(маска хранится в CFGH и применяется при старте)."
        ))
        warn.setFont(font)
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #B03030;")
        _selectable(warn)
        layout.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
        )
        apply_btn = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply_btn.setText(tr("Применить владение"))
        apply_btn.clicked.connect(self._apply_ownership)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_ownership(self) -> None:
        mask = 0
        for bit, cb in self._own_checks.items():
            if cb.isChecked():
                mask |= bit
        actual = self._sm.set_aux_pins_mask(mask)
        if actual is None:
            QMessageBox.warning(
                self, tr("Владение пинами"),
                tr("Команда CMD_AUX_MAP не подтверждена устройством")
            )
            return
        for bit, cb in self._own_checks.items():
            cb.setChecked(bool(actual & bit))
        QMessageBox.information(
            self, tr("Владение пинами"),
            tr("Применено: маска 0x{0:02X}").format(actual)
        )


class _AuxDialog(QDialog):
    """Настройка строки третьей колонки («Дополнительные каналы
    входа, выхода и переменные» — отчёт мастера): имя + вид.

    Вид «Канал входа/выхода» — номер пина и функция канала.
    Вид «Переменная» — один фиксированный бит: выбор носителя
    «ОЗУ»/«ПЗУ», имя — для использования в Гибкой логике."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        font = QFont("Segoe UI", 9)
        self.setWindowTitle(tr("Настройка доп. канала / переменной"))
        self.setMinimumWidth(420)
        self.setFont(font)
        config = config or {}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel(tr("Название:")))
        self._name_edit = QLineEdit(config.get("name", ""))
        self._name_edit.setFont(font)
        self._name_edit.setPlaceholderText(
            tr("например, «Управление заслонками»")
        )
        self._name_edit.setMinimumWidth(220)
        head.addWidget(self._name_edit, 1)
        layout.addLayout(head)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel(tr("Вид:")))
        self._kind = QComboBox()
        self._kind.setFont(font)
        self._kind.addItem(tr("Доп канал входа/выхода"), "aux")
        self._kind.addItem(tr("Переменная"), _TYPE_FLAG)
        kind_row.addWidget(self._kind, 1)
        kind_row.addStretch()
        layout.addLayout(kind_row)

        self._stack = QStackedWidget()

        channel_page = QWidget()
        ch_layout = QVBoxLayout(channel_page)
        ch_layout.setContentsMargins(0, 0, 0, 0)
        ch_layout.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Пин (канал):")))
        self._channel = QSpinBox()
        self._channel.setFont(font)
        # Семь выходов в новой нумерации (CM-HW-02, aux_channels.py):
        # 1=PA14, 2=PA15, 3=PC10, 4=PC11, 5=PC12, 6=PD2, 7=PB3.
        self._channel.setRange(1, 7)
        self._channel.setValue(int(config.get("channel", 1) or 1))
        self._channel.setFixedWidth(64)
        row.addWidget(self._channel)
        # Реальное имя пина и ограничение (SWCLK/TERM) — CM-N7:
        # доступность показывается явно, а не «канал есть = пин
        # свободен».
        self._pin_label = QLabel()
        self._pin_label.setFont(font)
        row.addWidget(self._pin_label)
        row.addWidget(QLabel(tr("Функция канала:")))
        self._function = QComboBox()
        self._function.setFont(font)
        for code, title in _AUX_FUNCTIONS:
            self._function.addItem(tr(title), code)
        fidx = self._function.findData(config.get("function", "analog_out"))
        self._function.setCurrentIndex(fidx if fidx >= 0 else 0)
        row.addWidget(self._function, 1)
        ch_layout.addLayout(row)

        # Настройки выхода (CM-N7): константа Выкл/Вкл или ШИМ с
        # частотой и заполнением — действие ГЛ «из настроек выхода»
        # берёт режим отсюда.
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(tr("Режим выхода:")))
        self._out_mode = QComboBox()
        self._out_mode.setFont(font)
        self._out_mode.addItem(tr("Выкл"), "off")
        self._out_mode.addItem(tr("Вкл"), "on")
        self._out_mode.addItem(tr("ШИМ"), "pwm")
        midx = self._out_mode.findData(str(config.get("out_mode", "on")))
        self._out_mode.setCurrentIndex(midx if midx >= 0 else 1)
        mode_row.addWidget(self._out_mode)
        mode_row.addStretch()
        ch_layout.addLayout(mode_row)

        self._pwm_row = QWidget()
        pwm_row = QHBoxLayout(self._pwm_row)
        pwm_row.setContentsMargins(0, 0, 0, 0)
        pwm_row.addWidget(QLabel(tr("Частота:")))
        self._pwm_freq = QSpinBox()
        self._pwm_freq.setFont(font)
        # Та же сетка, что у МК (quantize_pwm, aux_out.c): шаг 1 мс,
        # потолок 500 Гц (CM-N6).
        self._pwm_freq.setRange(1, 500)
        self._pwm_freq.setValue(int(config.get("pwm_freq", 250) or 250))
        self._pwm_freq.setSuffix(tr(" Гц"))
        pwm_row.addWidget(self._pwm_freq)
        pwm_row.addWidget(QLabel(tr("Заполнение:")))
        self._pwm_duty = QSpinBox()
        self._pwm_duty.setFont(font)
        self._pwm_duty.setRange(0, 100)
        self._pwm_duty.setValue(int(config.get("pwm_duty", 50) or 0))
        self._pwm_duty.setSuffix(" %")
        pwm_row.addWidget(self._pwm_duty)
        self._applied = QLabel()
        self._applied.setFont(font)
        self._applied.setStyleSheet("color: #9A9AA5;")
        pwm_row.addWidget(self._applied)
        pwm_row.addStretch()
        ch_layout.addWidget(self._pwm_row)

        self._conflict = QLabel()
        self._conflict.setFont(font)
        self._conflict.setWordWrap(True)
        self._conflict.setStyleSheet("color: #B03030;")
        _selectable(self._conflict)
        ch_layout.addWidget(self._conflict)
        ch_layout.addStretch()
        self._stack.addWidget(channel_page)

        self._channel.valueChanged.connect(self._refresh_aux_out)
        self._out_mode.currentIndexChanged.connect(self._refresh_aux_out)
        self._pwm_freq.valueChanged.connect(self._refresh_aux_out)
        self._pwm_duty.valueChanged.connect(self._refresh_aux_out)
        self._refresh_aux_out()

        flag_page = QWidget()
        flag_layout = QVBoxLayout(flag_page)
        flag_layout.setContentsMargins(0, 0, 0, 0)
        flag_layout.setSpacing(6)
        storage_row = QHBoxLayout()
        storage_row.addWidget(QLabel(tr("Носитель:")))
        self._storage = QComboBox()
        self._storage.setFont(font)
        # «выбери 1 бит озу или пзу и зафиксируй его» — бит
        # резервируется за переменной (отчёт мастера).
        self._storage.addItem(tr("ОЗУ"), "ram")
        self._storage.addItem(tr("ПЗУ"), "flash")
        sidx = self._storage.findData(config.get("storage", "ram"))
        self._storage.setCurrentIndex(sidx if sidx >= 0 else 0)
        storage_row.addWidget(self._storage)
        storage_row.addStretch()
        flag_layout.addLayout(storage_row)
        flag_hint = QLabel(tr(
            "Именованная переменная из одного бита: Гибкая логика "
            "может включать её постоянно, включать на время и "
            "выключать; события «Стала 1»/«Стала 0» запускают "
            "программы по переходу."
        ))
        flag_hint.setFont(font)
        flag_hint.setWordWrap(True)
        _selectable(flag_hint)
        flag_layout.addWidget(flag_hint)
        flag_layout.addStretch()
        self._stack.addWidget(flag_page)

        kidx = self._kind.findData(
            _TYPE_FLAG if config.get("type") == _TYPE_FLAG else "aux"
        )
        self._kind.setCurrentIndex(kidx if kidx >= 0 else 0)
        self._stack.setCurrentIndex(self._kind.currentIndex())
        self._kind.currentIndexChanged.connect(self._stack.setCurrentIndex)
        layout.addWidget(self._stack)

        self._hint = QLabel(tr(
            "Количество пинов и их названия появятся после "
            "программирования каналов в МК. Здесь настраивается "
            "только роль конкретного канала."
        ))
        self._hint.setFont(font)
        self._hint.setWordWrap(True)
        _selectable(self._hint)
        layout.addWidget(self._hint)
        self._hint.setVisible(self._kind.currentIndex() == 0)
        self._kind.currentIndexChanged.connect(
            lambda i: self._hint.setVisible(i == 0)
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_aux_out(self) -> None:
        """Подписи пина/конфликта и фактические фазы ШИМ (CM-N7):
        показываем ПРИМЕНЁННЫЙ сигнал после квантования в сетку
        целых мс — не запрошенный."""
        ch = self._channel.value()
        pin = AUX_PINS.get(ch, "?")
        conflict = aux_pin_conflict(ch)
        self._pin_label.setText(pin)
        mode = self._out_mode.currentData()
        self._pwm_row.setVisible(mode == "pwm")
        if conflict:
            self._conflict.setText(
                tr("⚠ {0}: {1}").format(pin, tr(conflict))
            )
        else:
            self._conflict.setText("")
        self._conflict.setVisible(bool(conflict))
        if mode == "pwm":
            q = quantize_pwm(self._pwm_freq.value(), self._pwm_duty.value())
            if q is None:
                self._applied.setText(tr("недостижимо"))
            else:
                period, on, off = q
                self._applied.setText(
                    tr("≈ {0}+{1} мс ({2:.1f}%)").format(
                        on, off, on * 100.0 / period
                    )
                )
        else:
            self._applied.setText("")

    @property
    def config(self) -> dict[str, Any]:
        if self._kind.currentData() == _TYPE_FLAG:
            return {
                "type": _TYPE_FLAG,
                "name": self._name_edit.text().strip(),
                "storage": self._storage.currentData(),
            }
        return {
            "type": "aux",
            "name": self._name_edit.text().strip(),
            "channel": self._channel.value(),
            "function": self._function.currentData(),
            "out_mode": self._out_mode.currentData(),
            "pwm_freq": self._pwm_freq.value(),
            "pwm_duty": self._pwm_duty.value(),
        }


class _VariableRow(QFrame):
    """Строка переменной в колонке:

    [название] | [состояние] | [✕]

    Клик по строке — редактор переменной. Состояние: у статической —
    «0»/«1», у динамической — число по графику (пока вводится с МК —
    отображается «0»). Носитель (ОЗУ/ПЗУ) выбирается в диалоге
    настройки — в строке не показывается (отчёт мастера)."""

    def __init__(
        self,
        column: _VarColumn,
        font: QFont,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(column)
        self._column = column
        self._font = font
        self.config: dict[str, Any] = config or {}
        # Онлайн-значение из CAN — обновляется вкладкой при приходе
        # кадра с ID переменной; None — кадр ещё не приходил.
        self.live_value: float | None = None
        # Сырое HEX-значение DATA того же кадра — показываем рядом
        # с десятичным над графиком привязки (отчёт мастера).
        self.live_raw: int | None = None
        # Достоверность показанного числа (CM-N11): невалидный кадр
        # обнуляет live_value и ставит статус-текст; отметка времени
        # последнего валидного значения нужна таймауту stale_ms.
        self.live_valid = False
        self._last_valid_ts = 0.0
        self._stale_shown = False
        self._live_text = "—"
        # «Импульсная переменная»: таймер вспышки «1» на 0.5 с
        # при совпадении DATA на шине (отчёт мастера).
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setSingleShot(True)
        self._pulse_timer.setInterval(500)
        self._pulse_timer.timeout.connect(self._pulse_end)
        # Перетаскивание за саму строку ЛКМ без ручки (отчёт мастера):
        # press запоминает точку, движение за порогом — живой перенос,
        # отпускание без движения — открытие редактора.
        self._press_pos = None
        self._dragging = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("varRow", True)
        self.setStyleSheet(
            "QFrame[varRow='true'] {"
            "  border: 1px solid #454552; border-radius: 8px;"
            "  background: rgba(255,255,255,0.03); }"
            "QFrame[varRow='true']:hover { border-color: #7C9EFF; }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 6, 6)
        row.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        row.addWidget(self._name_label, 1)

        self._state_label = QLabel("0")
        self._state_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._state_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._state_label.setMinimumWidth(46)
        row.addWidget(self._state_label)

        # «Тест» — вместо «N кадров» у переменных управления
        # (отчёт мастера): отправка команды сразу в оба канала.
        self._test_btn = QPushButton(tr("Тест"))
        self._test_btn.setFont(font)
        self._test_btn.setFixedHeight(24)
        self._test_btn.setToolTip(
            tr("Команда будет отправлена в CAN1 и CAN2")
        )
        self._test_btn.clicked.connect(self._send_test)
        self._test_btn.setVisible(False)
        row.addWidget(self._test_btn)

        remove = QPushButton()
        remove.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        remove.setFont(font)
        remove.setFixedSize(26, 26)
        remove.setToolTip(tr("Удалить переменную"))
        remove.clicked.connect(lambda: column.remove_row(self))
        row.addWidget(remove)

        self._refresh_labels()

    def set_live_value(
        self,
        value: float | None,
        raw: int | None = None,
        text: str | None = None,
        *,
        valid: bool = True,
    ) -> None:
        """Онлайн-значение переменной из CAN — дублируется в строке
        рядом с названием (отчёт мастера). ``text`` — готовая подпись
        (имя привязки «Динамической переменной»); без неё — число.

        ``valid=False`` — явный невалидный контракт (CM-N11): числовое
        значение обнуляется (``live_value=None``), подпись — статус
        ошибки; залипшего прошлого числа не остаётся."""
        self.live_value = value if valid else None
        self.live_valid = valid
        self.live_raw = raw
        self._stale_shown = False
        if valid:
            self._last_valid_ts = time.monotonic()
        self._live_text = (
            text
            if text is not None
            else (f"{value:.2f}" if value is not None else "—")
        )
        self._state_label.setText(self._live_text)

    def mark_stale(self) -> None:
        """Пометить показанное значение устаревшим (CM-N11): число
        остаётся видимым, но с явной отметкой — условия ГЛ его уже
        не используют, и отображение это отражает."""
        if self._stale_shown:
            return
        self._stale_shown = True
        self._state_label.setText(
            tr("{0} (устарело)").format(self._live_text)
        )

    def pulse(self) -> None:
        """Вспышка «1» на 0.5 с — «Импульсная переменная» при
        совпадении указанных байтов DATA на шине (отчёт мастера)."""
        self.live_value = 1.0
        self._state_label.setText("1")
        self._pulse_timer.start()

    def _pulse_end(self) -> None:
        self.live_value = 0.0
        self._state_label.setText("0")

    def _send_test(self) -> None:
        """«Тест» команды управления: все её фреймы отправляются
        сразу в ОБА канала (CAN1 и CAN2) с заданными паузами
        (отчёт мастера)."""
        sm = getattr(self._column._tab, "_serial_manager", None)
        if sm is None or not sm.is_open():
            return

        def _send(channel: int, can_id: int, data: bytes,
                  rtr: bool, dlc: int, delay_ms: int) -> None:
            packed = pack_can_frame(
                channel, can_id, data, rtr=rtr, dlc=dlc
            )
            if delay_ms > 0:
                QTimer.singleShot(
                    delay_ms, lambda p=packed: sm.send_data(p)
                )
            else:
                sm.send_data(packed)

        def _schedule(frames: list[dict[str, Any]], t0: int) -> int:
            t = t0
            for fr in frames:
                can_id = hex_to_int(str(fr.get("id", "")))
                if can_id is None:
                    continue
                count = max(1, int(fr.get("count", 1) or 1))
                between = int(fr.get("delay_between", 0) or 0)
                rtr = bool(fr.get("rtr", False))
                dlc = max(0, min(8, int(fr.get("dlc", 8) or 8)))
                tokens = str(fr.get("data", "")).split()
                payload = bytearray(8)
                for i, tok in enumerate(tokens[:8]):
                    if tok and tok.upper() != "X":
                        value = hex_to_int(tok)
                        payload[i] = (value or 0) & 0xFF
                for n in range(count):
                    for channel in (1, 2):
                        _send(
                            channel, can_id,
                            b"" if rtr else bytes(payload[:dlc]),
                            rtr, dlc, t + n * between,
                        )
                t += (count - 1) * between + int(
                    fr.get("next_delay", 0) or 0
                )
            return t

        t = _schedule(self.config.get("frames") or [], 0)
        # Зеркало статической переменной «→ 1»: «Тест» держит бит 1 с,
        # затем шлёт фреймы «→ 0» (отчёт мастера).
        pulse = self.config.get("pulse_frames") or []
        if pulse:
            _schedule(pulse, t + 1000)

    def _refresh_labels(self) -> None:
        name = self.config.get("name", "").strip()
        self._name_label.setText(name or tr("— (без имени)"))
        cfg_type = self.config.get("type")
        if cfg_type == _TYPE_COMMAND:
            # Переменная управления — команда: вместо «N кадров»
            # кнопка «Тест» — отправка в CAN1 и CAN2 (отчёт мастера).
            self._state_label.setVisible(False)
            self._test_btn.setVisible(True)
            return
        self._test_btn.setVisible(False)
        self._state_label.setVisible(True)
        if cfg_type == _TYPE_FLAG:
            # «Переменная» — именованный бит ОЗУ/ПЗУ (отчёт мастера).
            storage = self.config.get("storage", "ram")
            self._state_label.setText(
                tr("бит · {storage}").format(
                    storage=tr("ОЗУ") if storage == "ram" else tr("ПЗУ")
                )
            )
            return
        if cfg_type == "aux":
            # Доп. канал: пин (реальное имя) + применённый режим
            # выхода из настроек (CM-N7).
            ch = int(self.config.get("channel", 1) or 1)
            pin = AUX_PINS.get(ch, f"#{ch}")
            out_mode = str(self.config.get("out_mode", "on") or "on")
            if out_mode == "pwm":
                q = quantize_pwm(
                    int(self.config.get("pwm_freq", 0) or 0),
                    int(self.config.get("pwm_duty", 0) or 0),
                )
                mode_text = (
                    tr("ШИМ ≈{0:.0f}%").format(q[1] * 100.0 / q[0])
                    if q else tr("ШИМ?")
                )
            elif out_mode == "off":
                mode_text = tr("Выкл")
            else:
                mode_text = tr("Вкл")
            self._state_label.setText(f"{pin} · {mode_text}")
            return
        if cfg_type == _TYPE_CACHE:
            # «Кэш переменная»: до записи — прочерки; после записи в
            # Гибкой логике («Записать Кэш переменную») — записанные
            # данные, либо имя из таблицы привязки динамической
            # переменной, если значение совпало (отчёт мастера).
            tab = getattr(self._column, "_tab", None)
            name = str(self.config.get("name", "")).strip()
            data = (
                tab._cache_values.get(name)
                if tab is not None and name else None
            )
            if data is None:
                self._state_label.setText("—")
                return
            bound = tab.cache_binding_name(data) if tab is not None else ""
            self._state_label.setText(
                bound or " ".join(f"{b:02X}" for b in data)
            )
            return
        self._state_label.setText(
            "0.00"
            if cfg_type in (_TYPE_DYNAMIC, _TYPE_DYNCACHE)
            else "0"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Клик/захват по свободному месту строки; по кнопке удаления
        # событие сюда не долетает (у неё свой приём).
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # Удержание ЛКМ + движение за порогом — перенос строки
        # курсором вверх/вниз по списку (отчёт мастера).
        if (
            self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            if not self._dragging and (
                event.position().toPoint() - self._press_pos
            ).manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                self._column._drag_row_to(
                    self, event.globalPosition().toPoint().y()
                )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_pos is not None
        ):
            if self._dragging:
                # Отпустили после переноса — фиксируем порядок.
                self._column.persist()
            else:
                # Чистый клик без движения — редактор переменной.
                self._column.edit_row(self)
        self._press_pos = None
        self._dragging = False
        super().mouseReleaseEvent(event)


class _VarColumn(QWidget):
    """Колонка «Чтение»/«Управление»: список строк переменных +
    кнопка добавления."""

    def __init__(
        self,
        title: str,
        tab: VariablesTab,
        key: str,
        font: QFont,
    ) -> None:
        super().__init__(tab)
        self._tab = tab
        self.key = key
        self._font = font
        self._rows: list[_VariableRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._group = QFrame()
        self._group.setStyleSheet(
            "QFrame { border: 1px solid #454552; border-radius: 10px; }"
        )
        box = QVBoxLayout(self._group)
        self._title = QLabel(title)
        self._title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        box.addWidget(self._title)

        # Кнопка добавления — НАД таблицей (отчёт мастера); рядом —
        # число видимых строк списка (отчёт мастера).
        self._add_btn = QPushButton(tr("＋ Добавить переменную"))
        self._add_btn.setFont(font)
        self._add_btn.clicked.connect(self._on_add)
        add_row_layout = QHBoxLayout()
        add_row_layout.addWidget(self._add_btn)
        add_row_layout.addStretch()
        rows_label = QLabel(tr("Строк:"))
        rows_label.setFont(font)
        add_row_layout.addWidget(rows_label)
        self._visible_rows = QSpinBox()
        self._visible_rows.setFont(font)
        self._visible_rows.setRange(3, 30)
        self._visible_rows.setValue(8)
        self._visible_rows.setFixedWidth(56)
        self._visible_rows.setToolTip(
            tr("Сколько строк переменных видно без прокрутки")
        )
        self._visible_rows.valueChanged.connect(self._apply_visible_rows)
        add_row_layout.addWidget(self._visible_rows)
        box.addLayout(add_row_layout)

        container = QWidget()
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setSpacing(6)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        self._scroll = scroll
        box.addWidget(scroll, 1)
        layout.addWidget(self._group, 1)
        self._apply_visible_rows(self._visible_rows.value())

    # Базовая высота строки и шрифт — из них считается ужатие.
    _ROW_BASE_H = 38

    def _apply_visible_rows(self, count: int) -> None:
        """«Строк:» N — видимая плотность списка (отчёт мастера):
        N строк не влезает — строки и символы ужимаются; строк мало —
        размер списка и шапка над ним НЕ меняются."""
        self._desired_rows = count
        # Viewport при первом вызове ещё не размечен — считаем после.
        QTimer.singleShot(0, self._apply_row_density)

    def _apply_row_density(self) -> None:
        from shiboken6 import isValid
        if not isValid(self._scroll):
            # Отложенный singleShot пришёл после разрушения —
            # C++-объекты трогать нельзя (ревью CM-R4).
            return
        count = int(getattr(self, "_desired_rows", 8) or 8)
        viewport = self._scroll.viewport().height()
        if viewport <= 0:
            return
        need = count * self._ROW_BASE_H
        # row_h = 0 — всё влезает, естественная высота строк.
        row_h = 0 if need <= viewport else max(20, viewport // count)
        for row in self._rows:
            if not hasattr(row, "_base_pts"):
                row._base_pts = {
                    w: w.font().pointSize()
                    for w in row.findChildren(QWidget)
                    if w.font().pointSize() > 0
                }
            if row_h:
                row.setFixedHeight(row_h)
                scale = row_h / self._ROW_BASE_H
                for w, base_pt in row._base_pts.items():
                    try:
                        f = QFont(w.font())
                        f.setPointSize(max(5, int(base_pt * scale)))
                        w.setFont(f)
                    except RuntimeError:
                        continue
            else:
                row.setMinimumHeight(0)
                row.setMaximumHeight(16777215)
                for w, base_pt in row._base_pts.items():
                    try:
                        f = QFont(w.font())
                        f.setPointSize(base_pt)
                        w.setFont(f)
                    except RuntimeError:
                        continue

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_row_density()

    def move_row(self, row: _VariableRow, index: int) -> None:
        """Перемещает строку на позицию ``index`` в списке
        (перетаскивание за ручку «⋮⋮» — отчёт мастера)."""
        if row not in self._rows:
            return
        index = max(0, min(index, len(self._rows) - 1))
        self._rows.remove(row)
        self._rows.insert(index, row)
        self._rows_layout.removeWidget(row)
        self._rows_layout.insertWidget(index, row)

    def _drag_row_to(self, row: _VariableRow, global_y: int) -> None:
        """Целевая позиция строки по Y курсора: выше центра строки —
        перед ней (отчёт мастера)."""
        if row not in self._rows:
            return
        target = len(self._rows) - 1
        for i, other in enumerate(self._rows):
            center = other.mapToGlobal(other.rect().center()).y()
            if global_y < center:
                target = i
                break
        if target != self._rows.index(row):
            self.move_row(row, target)

    def add_row(self, config: dict[str, Any] | None = None) -> _VariableRow:
        cfg = dict(config or {})
        if self.key == "control" and cfg and cfg.get("type") != _TYPE_COMMAND:
            # Обратная совместимость: старые виды переменных
            # «Управления» и записи папок переводятся в команды
            # (папки — плоские записи, в колонке не отображаются).
            if cfg.get("type") == _TYPE_FOLDER:
                return None  # type: ignore[return-value]
            commands = _legacy_to_command(cfg)
            if not commands:
                return None  # type: ignore[return-value]
            cfg = commands[0]
            for extra in commands[1:]:
                self.add_row(extra)
        row = _VariableRow(self, self._font, cfg)
        if "storage" not in row.config:
            row.config["storage"] = "ram"
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        return row

    def remove_row(self, row: _VariableRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        retire(row)
        self.persist()
        if self.key == "read":
            # «Перенести в управление»: удалённая переменная тянет
            # за собой свою команду-зеркало (отчёт мастера).
            self._tab._sync_control_mirrors()

    def edit_row(self, row: _VariableRow) -> None:
        # Доп. каналы — компактный диалог без фреймов: только имя,
        # пин и функция канала; «Управление» — редактор команды
        # (список фреймов с паузами/количеством/кэшем); остальные —
        # полный диалог переменной (отчёт мастера).
        if self.key == "aux" or row.config.get("type") in (
            "aux", _TYPE_FLAG
        ):
            dialog = _AuxDialog(self._tab, row.config)
        elif self.key == "control" or row.config.get("type") == _TYPE_COMMAND:
            dialog = _CommandDialog(self._tab, row.config)
        else:
            dialog = VariableDialog(
                self._tab, row.config, row=row,
                for_control=(self.key == "control"),
            )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            # Частичный ввод не теряется при закрытии окна —
            # сохраняем всё, что оператор успел ввести, даже без
            # обязательных полей (отчёт мастера).
            partial = getattr(dialog, "current_config", None)
            if not callable(partial):
                partial = getattr(dialog, "config", None)
            snapshot = (
                partial() if callable(partial)
                else (partial if isinstance(partial, dict) else None)
            )
            if snapshot:
                row.config.update(snapshot)
                row._refresh_labels()
                self.persist()
                if self.key == "read":
                    self._tab._sync_control_mirrors()
            return
        row.config.update(dialog.config)
        row._refresh_labels()
        if self.key == "read":
            self.sort_rows()
            self._tab._sync_control_mirrors()
        self.persist()

    def _on_add(self) -> None:
        row = self.add_row()
        self.edit_row(row)
        # Пустую (отменённую) строку не оставляем болтаться без имени.
        if not row.config:
            self.remove_row(row)
        else:
            if self.key == "read":
                self.sort_rows()
                self._tab._sync_control_mirrors()
            self.persist()

    # Порядок разделов во вкладке «Чтение»: статическая → численная →
    # динамическая → импульсная, внутри раздела — по алфавиту
    # (отчёт мастера).
    _TYPE_ORDER = {
        _TYPE_STATIC: 0, _TYPE_DYNAMIC: 1,
        _TYPE_DYNCACHE: 2, _TYPE_IMPULSE: 3, _TYPE_CACHE: 4,
    }

    def sort_rows(self) -> None:
        """Переупорядочивает строки колонки «Чтение» по разделам
        (виду переменной), внутри раздела — по алфавиту."""
        self._rows.sort(
            key=lambda r: (
                self._TYPE_ORDER.get(r.config.get("type"), 99),
                r.config.get("name", "").strip().lower(),
            )
        )
        for row in self._rows:
            self._rows_layout.removeWidget(row)
        for row in self._rows:
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    def configs(self) -> list[dict[str, Any]]:
        return [dict(row.config) for row in self._rows]

    def clear(self) -> None:
        for row in list(self._rows):
            self._rows_layout.removeWidget(row)
            retire(row)
        self._rows.clear()
        self.persist()

    def persist(self) -> None:
        self._tab.persist()


class VariablesTab(QWidget):
    """Вкладка «Переменные»: колонки «Чтение»/«Управление»,
    сохранение/загрузка «Конфиг Инфо»."""

    # Файл переменных загружен — окно настроек включает «Сохранить»,
    # иначе загруженные переменные выглядели так, будто ничего не
    # изменилось (отчёт мастера: «кнопка Сохранить не активна»).
    loaded = Signal()

    def __init__(self, parent: QWidget | None = None, serial_manager=None) -> None:
        super().__init__(parent)
        self._font = QFont("Segoe UI", 9)
        self._config = Config()
        self._serial_manager = serial_manager
        # Последний кадр по каждому ID на шине — онлайн-строки DATA
        # в таблицах настройки переменных (отчёт мастера). Третий
        # элемент — глобальный номер кадра для «последний в
        # диапазоне» (Кэш переменная).
        self._last_frames: dict[int, tuple[bool, bytes, int]] = {}
        self._frame_seq = 0
        # Записанные значения «Кэш переменных» (буфер 2) — имя →
        # байты. Гибкая логика присылает set_cache_live() при записи/
        # стирании; строка показывает данные или прочерки.
        self._cache_values: dict[str, bytes | None] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title_col = QVBoxLayout()
        self._title = QLabel(tr("Переменные"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)
        title_col.addWidget(self._title)
        top.addLayout(title_col, 1)
        layout.addLayout(top)

        # Кнопки файловых операций — горизонтально сверху над
        # таблицами (отчёт мастера).
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        # «Сохранить переменные» живёт внизу окна настроек рядом с
        # общей «Сохранить» (отчёт мастера) — создаём здесь, а
        # размещает settings_window через save_variables_button.
        self._save_btn = QPushButton(tr("Сохранить переменные"))
        self._clear_btn = QPushButton(tr("Очистить"))
        self._aux_state_btn = QPushButton(tr("Каналы выхода…"))
        self._aux_state_btn.setToolTip(tr(
            "Состояние семи выходов с устройства и владение "
            "спорными пинами PA14/PD2/PB3 (протокол 13)"
        ))
        for btn in (self._save_btn, self._clear_btn, self._aux_state_btn):
            btn.setFont(self._font)
            btn.setFixedHeight(30)
        btn_row.addWidget(self._aux_state_btn)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch()
        self._save_btn.clicked.connect(self._save_file)
        self._clear_btn.clicked.connect(self._clear_all)
        self._aux_state_btn.clicked.connect(self._show_aux_channels)
        layout.addLayout(btn_row)

        self._hint = QLabel(tr(
            "Переменные — именованные состояния и величины, которые "
            "Гибкая логика опрашивает в условиях «Если» и использует в "
            "действиях. Клик по строке — настройка переменной."
        ))
        self._hint.setFont(self._font)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        self._read_col = _VarColumn(tr("Чтение"), self, "read", self._font)
        # «Управление» — обычная колонка строк, как «Чтение»/«Доп
        # каналы»: кнопка «Добавить переменную» внизу, без кнопок
        # «Команда»/«Папка»/«✕» (отчёт мастера).
        self._ctrl_col = _VarColumn(
            tr("Управление"), self, "control", self._font
        )
        # Три равные колонки: Чтение / Управление / Дополнительные
        # каналы входа, выхода и переменные (отчёт мастера).
        columns.addWidget(self._read_col, 1)
        columns.addWidget(self._ctrl_col, 1)
        self._aux_col = _VarColumn(
            tr("Дополнительные каналы входа, выхода и переменные"),
            self, "aux", self._font,
        )
        columns.addWidget(self._aux_col, 1)
        layout.addLayout(columns, 1)

        # Онлайн-значения переменных из CAN (отчёт мастера: раньше
        # поле «значение» не обновлялось).
        if self._serial_manager is not None:
            self._serial_manager.new_can_frames.connect(self._on_can_frames)

        # Таймаут свежести показанных значений (CM-N11): переменные с
        # настроенным stale_ms помечаются «(устарело)» — число не
        # выдаётся за актуальное, когда кадры давно не приходили.
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(200)
        self._stale_timer.timeout.connect(self._mark_stale_rows)
        self._stale_timer.start()

        self._restore()

    # ---- онлайн-значения из CAN ----------------------------------------

    @staticmethod
    def _var_status_text(status: str) -> str:
        """Человекочитаемая подпись машинного статуса декодера
        (CM-N11) — вместо сырого кода вроде «no-class»."""
        return {
            "no-data": tr("нет данных"),
            "no-field": tr("поле не задано"),
            "incomplete": tr("неполный кадр"),
            "conflict": tr("конфликт классов"),
            "no-class": tr("вне классов"),
            "no-calibration": tr("нет калибровки"),
            "no-points": tr("нет точек стороны"),
            "disabled": tr("выкл"),
        }.get(status, status)

    def _mark_stale_rows(self) -> None:
        """Пометка «устарело» для значений с настроенным stale_ms —
        прошло больше времени без валидного кадра (CM-N11)."""
        now = time.monotonic()
        for col in (self._read_col, self._ctrl_col, self._aux_col):
            for row in list(getattr(col, "_rows", [])):
                if not row.live_valid or row._stale_shown:
                    continue
                stale = variable_decode.stale_ms(row.config)
                if stale and now - row._last_valid_ts > stale / 1000.0:
                    row.mark_stale()

    def _show_aux_channels(self) -> None:
        """Диалог состояния выходов: CMD_AUX_GET (режимы/уровни) +
        CMD_AUX_MAP (владение спорными пинами) — CM-HW-02."""
        _AuxChannelsDialog(
            self._serial_manager, self._font, self
        ).exec()

    def _on_can_frames(self, frames: list[dict[str, Any]]) -> None:
        """Пересчитывает значения байтовых переменных по приходящим
        кадрам: ID (+ битность) совпал → сырое значение из заполненных
        байтов «DATA от» → величина по «таблице привязки». Результат
        виден в строке переменной и над графиком в её настройке.

        «Численная» переменная считает значение из байтов пришедшего
        кадра. «Динамическая» переменная (МК-кэш) перезаписывает
        кэшированные байты каждым новым пакетом: в перезапись идут
        только указанные оператором позиции DATA, остальные байты кэша
        сохраняют прежнее значение до следующего пакета (отчёт
        мастера)."""
        # Дедупликация пачки до последнего кадра на (ID, разрядность):
        # при тысячах кадров/с перебор «строки × вся пачка» давал
        # квадратичную нагрузку и подтормаживал онлайн-DATA в диалогах
        # (отчёт мастера — «data после написания ID тормозит»).
        # seq — порядковый номер кадра в пачке: у статической
        # переменной с несколькими фреймами («1» и «0» на разных ID)
        # побеждает ПОЗДНИЙ кадр, а не последний фрейм в списке.
        latest: dict[tuple[int, bool], tuple[int, bytes]] = {}
        for seq, frame in enumerate(frames):
            fid = int(frame.get("id", -1))
            if fid < 0:
                continue
            data = bytes(frame.get("data", b""))
            ext = bool(frame.get("extended", False))
            # Глобальный номер кадра хранится в записи — онлайн-
            # строкам переменных нужен ПОСЛЕДНИЙ кадр; seq пачки
            # между вызовами не монотонен.
            self._frame_seq += 1
            self._last_frames[fid] = (ext, data, self._frame_seq)
            latest[(fid, ext)] = (seq, data)
        if not latest:
            return
        for col in (self._read_col, self._ctrl_col, self._aux_col):
            # Колонка «Управление» — дерево команд, строк-переменных
            # у неё нет (отчёт мастера).
            for row in list(getattr(col, "_rows", [])):
                cfg = row.config
                var_type = cfg.get("type")
                if var_type == _TYPE_STATIC:
                    # Статическая переменная — бит: фрейм со значением
                    # «1» поднимает флаг, «0» — сбрасывает до прихода
                    # противоположного (отчёт мастера: «всегда 0»).
                    best_seq = -1
                    best_value: int | None = None
                    for fdef in cfg.get("frames") or []:
                        fid = hex_to_int(str(fdef.get("id", "")))
                        if fid is None:
                            continue
                        hit = latest.get(
                            (fid, bool(fdef.get("extended", False)))
                        )
                        if hit is None:
                            continue
                        seq, data = hit
                        tokens = str(fdef.get("data", "")).split()
                        if not _tokens_match(tokens, data):
                            continue
                        if seq > best_seq:
                            best_seq = seq
                            best_value = int(fdef.get("value", 1) or 0)
                    if best_value is not None and (
                        row.live_value != best_value
                    ):
                        # Бит — показываем «1»/«0», а не «1.00»
                        # (отчёт мастера).
                        row.set_live_value(
                            float(best_value), text=str(best_value)
                        )
                    continue
                if var_type == _TYPE_IMPULSE:
                    # «Импульсная переменная»: совпадение указанных
                    # байтов DATA — вспышка «1» на 0.5 с (отчёт мастера).
                    fid = hex_to_int(str(cfg.get("id", "")))
                    if fid is None:
                        continue
                    hit = latest.get(
                        (fid, bool(cfg.get("extended", False)))
                    )
                    if hit is not None and _tokens_match(
                        str(cfg.get("data", "")).split(), hit[1]
                    ):
                        row.pulse()
                    continue
                if var_type == _TYPE_CACHE:
                    # «Кэш переменная»: строка показывает записанное в
                    # буфер 2 значение (set_cache_live из ГЛ), а не
                    # последний кадр шины (отчёт мастера).
                    continue
                if var_type not in (_TYPE_DYNAMIC, _TYPE_DYNCACHE):
                    continue
                fid = hex_to_int(str(cfg.get("id", "")))
                points = cfg.get("points") or []
                used = cfg.get("bytes") or []
                steering = variable_decode.is_steering(cfg)
                # «Градусы»: таблица привязки — источник нейтрали и
                # масштаба (CM-F05); без точек и без декодера строку
                # пропускаем — считать нечего.
                if fid is None or not used or (not points and not steering):
                    continue
                hit = latest.get(
                    (fid, bool(cfg.get("extended", False)))
                )
                if hit is None:
                    continue
                data = hit[1]
                raw = 0
                seen = False
                if var_type == _TYPE_DYNCACHE:
                    # Перезапись кэша только указанными байтами —
                    # чужие позиции кэша не трогаем. Сырое значение
                    # — конкатенация кэшированных байтов: совпадение
                    # с таблицей привязки должно быть точным.
                    cache = getattr(row, "_dyn_cache", {})
                    for i in used:
                        if i < len(data):
                            cache[i] = data[i]
                    row._dyn_cache = cache
                    for i in used:
                        if i in cache:
                            raw = (raw << 8) | cache[i]
                            seen = True
                    if seen:
                        name = ""
                        for point in points:
                            try:
                                if int(point[0]) == raw:
                                    name = str(point[1])
                                    break
                            except (TypeError, ValueError, IndexError):
                                continue
                        row.set_live_value(
                            float(raw), raw,
                            text=name or f"0x{raw:X}",
                        )
                else:
                    # «Численная переменная»: RAW и полнота поля —
                    # единый контракт с рантаймом ГЛ (CM-N3):
                    # SBLP-склейка или сумма выбранных байтов;
                    # неполное поле значения не даёт.
                    res = variable_decode.decode_var_value(cfg, data)
                    if not res["complete"]:
                        # CM-N11: неполный кадр — явный статус, а не
                        # залипшее прошлое число.
                        row.set_live_value(
                            None, valid=False,
                            text=self._var_status_text(res["status"]),
                        )
                        continue
                    raw = res["raw"]
                    if steering:
                        # «Градусы» (единый режим, CM-F05): класс по
                        # делителям, нейтраль/масштаб — из таблицы
                        # привязки; статус показываем текстом при
                        # невалидном значении — живое число при этом
                        # НЕВАЛИДНО (live_value=None, CM-N11).
                        if res["valid"]:
                            suffix = steering_decoder.unit_suffix(
                                res.get("unit", "none")
                            )
                            row.set_live_value(
                                res["value"], raw,
                                text=(
                                    f'{res["value"]:.1f}{suffix} '
                                    f'({res["raw_hex"]})'
                                ),
                            )
                        else:
                            row.set_live_value(
                                None, raw, valid=False,
                                text=self._var_status_text(
                                    res["status"]
                                ),
                            )
                    else:
                        row.set_live_value(
                            _map_points(points, raw), raw
                        )

    def set_flag_live(self, name: str, value: int) -> None:
        """Онлайн-состояние бита «Переменная» из Гибкой логики —
        строка третьей колонки показывает «1»/«0» (отчёт мастера)."""
        name = str(name).strip()
        for row in self._aux_col._rows:
            cfg = row.config
            if cfg.get("type") == _TYPE_FLAG and (
                cfg.get("name", "").strip() == name
            ):
                row.set_live_value(float(value), text=str(int(value)))

    def set_cache_live(self, name: str, data: bytes | None) -> None:
        """Записанное значение «Кэш переменной» из Гибкой логики
        (действие «Записать Кэш переменную» перенесло буфер 1 → 2;
        «Стереть КЭШ» — None). Строка переменной показывает сырые
        байты либо имя из таблицы привязки «Динамической переменной»,
        если значение совпало (отчёт мастера); до первой записи и
        после стирания — прочерки."""
        name = str(name).strip()
        if not name:
            return
        self._cache_values[name] = bytes(data) if data is not None else None
        for col in (self._read_col, self._ctrl_col, self._aux_col):
            for row in list(getattr(col, "_rows", [])):
                cfg = row.config
                if cfg.get("type") == _TYPE_CACHE and (
                    cfg.get("name", "").strip() == name
                ):
                    row._refresh_labels()

    def cache_binding_name(self, data: bytes) -> str:
        """Имя из таблицы привязки «Динамической переменной», если
        данные буфера по её позициям байт «DATA» совпали со значением
        точки привязки (отчёт мастера). Пусто — если ни одна
        привязка не подошла."""
        for cfg in self._read_col.configs():
            if cfg.get("type") != _TYPE_DYNCACHE:
                continue
            points = cfg.get("points") or []
            used = cfg.get("bytes") or []
            if not points or not used:
                continue
            raw = 0
            for i in used:
                if i >= len(data):
                    break
                # Порядок байт — как у «Динамической переменной»:
                # старший первым (big-endian конкатенация).
                raw = (raw << 8) | data[i]
            else:
                for point in points:
                    try:
                        if int(point[0]) == raw:
                            return str(point[1])
                    except (TypeError, ValueError, IndexError):
                        continue
        return ""

    # ---- «Перенести в управление» -------------------------------------

    @staticmethod
    def _mirror_frames(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Фреймы команды-зеркала по конфигурации переменной чтения.

        «Динамическая переменная» даёт по пакету на каждую точку
        таблицы привязки (DATA = сырое значение точки), остальные
        виды — один пакет из её настройки фрейма (отчёт мастера)."""
        var_type = cfg.get("type")

        def _pad_tokens(text: str, dlc: int) -> str:
            """Дополняет DATA нулями до DLC — в зеркало переносится
            полный фрейм, а не усечённая запись (отчёт мастера)."""
            tokens = str(text).split()
            while len(tokens) < dlc:
                tokens.append("00")
            return " ".join(tokens[:dlc])

        def _base() -> dict[str, Any]:
            return {
                "channel": 0,
                "extended": bool(cfg.get("extended", False)),
                "id": str(cfg.get("id", "")),
                "dlc": int(cfg.get("dlc", 8) or 8),
                "rtr": False,
                "delay_before_send": 0,
                # Дефолт отправки команды: 3 пакета с паузой 5 мс
                # (отчёт мастера).
                "delay_between": 5,
                "count": 3,
                "next_delay": 0,
            }

        def _from_tokens(value: int | None = None) -> str:
            """DATA зеркала: всё поле «от» целиком (включая «X»-
            подстановки и хвост до DLC); позиции вычисления при
            заданном значении точки заменяются его байтами —
            по выбранному порядку старшинства SBLP (отчёт мастера)."""
            dlc = int(cfg.get("dlc", 8) or 8)
            used = [i for i in (cfg.get("bytes") or []) if i < dlc]
            tokens = str(cfg.get("from", "")).split()
            while len(tokens) < dlc:
                tokens.append("00")
            if value is not None and used:
                endian = "little" if cfg.get("sblp") == "le" else "big"
                raw = int(value).to_bytes(max(1, len(used)), endian)
                for pos, idx in enumerate(used):
                    if pos < len(raw):
                        tokens[idx] = f"{raw[pos]:02X}"
            return " ".join(tokens[:dlc])

        if var_type == _TYPE_STATIC:
            frames = []
            for fr in cfg.get("frames") or []:
                base = _base()
                fr_dlc = int(fr.get("dlc", 8) or 8)
                base.update({
                    "extended": bool(fr.get("extended", False)),
                    "id": str(fr.get("id", "")),
                    "dlc": fr_dlc,
                    "data": _pad_tokens(str(fr.get("data", "")), fr_dlc),
                    "rtr": bool(fr.get("rtr", False)),
                })
                frames.append(base)
            return frames or [dict(_base(), data=_from_tokens())]

        if var_type in (_TYPE_DYNCACHE, _TYPE_DYNAMIC):
            frames = []
            for point in cfg.get("points") or []:
                try:
                    value = int(point[0])
                except (TypeError, ValueError, IndexError):
                    continue
                frames.append(dict(_base(), data=_from_tokens(value)))
            if frames:
                return frames
            return [dict(_base(), data=_from_tokens())]

        data = _pad_tokens(
            str(
                cfg.get("data") or cfg.get("from")
                or cfg.get("data_from") or ""
            ),
            int(cfg.get("dlc", 8) or 8),
        )
        return [dict(_base(), data=data)]

    @staticmethod
    def _mirror_commands(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Команды-зеркала переменной чтения — по ОДНОЙ на каждую
        точку/фрейм (отчёт мастера: «переменные отдельными функциями,
        каждая должна отправляться самостоятельно», а не общей
        командой со всеми пакетами).

        * «Статическая переменная» распадается на «имя → 1» и
          «имя → 0» — как в «Управлении»; «→ 1» получает
          ``pulse_frames`` — «Тест» подаёт импульс 1 с → фреймы «0».
        * «Динамическая переменная» — команда на каждую точку
          таблицы привязки («имя · привязка»).
        * Остальные — команда на каждый настроенный фрейм."""
        name = str(cfg.get("name", "")).strip() or tr("Команда")
        folder = str(cfg.get("folder", ""))
        var_type = cfg.get("type")

        def _cmd(cname: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "type": _TYPE_COMMAND,
                "name": cname,
                "folder": folder,
                "frames": frames,
                "cache": {},
            }

        if var_type == _TYPE_STATIC:
            commands = _legacy_to_command(cfg)
            on_cmd = next(
                (c for c in commands if str(c["name"]).endswith("→ 1")),
                None,
            )
            off_cmd = next(
                (c for c in commands if str(c["name"]).endswith("→ 0")),
                None,
            )
            if on_cmd is not None and off_cmd is not None:
                on_cmd["pulse_frames"] = off_cmd["frames"]
            for c in commands:
                c["cache"] = {}
            return commands

        frames = VariablesTab._mirror_frames(cfg)
        if var_type in (_TYPE_DYNCACHE, _TYPE_DYNAMIC):
            points = cfg.get("points") or []
            commands = []
            for i, fr in enumerate(frames):
                try:
                    suffix = str(points[i][1]).strip() or f"точка {i + 1}"
                except (TypeError, IndexError):
                    suffix = f"точка {i + 1}"
                commands.append(_cmd(f"{name} · {suffix}", [fr]))
            return commands or [_cmd(name, frames)]

        if len(frames) <= 1:
            return [_cmd(name, frames)]
        return [
            _cmd(f"{name} · пакет {i + 1}", [fr])
            for i, fr in enumerate(frames)
        ]

    def _sync_control_mirrors(self) -> None:
        """Держит колонку «Управление» в соответствии с галочкой
        «Перенести в управление» у переменных чтения: по ОТДЕЛЬНОЙ
        команде-зеркалу на каждую точку/фрейм (отчёт мастера)."""
        wanted: dict[str, list[dict[str, Any]]] = {}
        for row in self._read_col._rows:
            cfg = row.config
            if not cfg.get("to_control"):
                continue
            name = str(cfg.get("name", "")).strip()
            if not name:
                continue
            wanted[name] = self._mirror_commands(cfg)

        # Зеркала без источника/снятой галочки — убираем.
        for row in list(self._ctrl_col._rows):
            src = str(row.config.get("mirror_of", "")).strip()
            if src and src not in wanted:
                self._ctrl_col.remove_row(row)

        for name, commands in wanted.items():
            # Существующие зеркала источника: переносим «Кол-во»/паузы
            # во фреймы по ключу (id, data) — у каждого пакета свои
            # настройки отправки (отчёт мастера).
            old = [
                row
                for row in self._ctrl_col._rows
                if str(row.config.get("mirror_of", "")).strip() == name
            ]
            tuned: dict[tuple[str, str], dict[str, Any]] = {}
            for row in old:
                for fr in row.config.get("frames") or []:
                    tuned[(str(fr.get("id", "")), str(fr.get("data", "")))] = fr
            used: set[int] = set()
            for i, cmd in enumerate(commands):
                for fr in cmd["frames"]:
                    keep = tuned.get(
                        (str(fr.get("id", "")), str(fr.get("data", "")))
                    )
                    if keep:
                        for key in ("count", "delay_between", "next_delay",
                                    "delay_before_send", "channel", "rtr"):
                            fr[key] = keep.get(key, fr.get(key))
                row = next(
                    (
                        r
                        for r in old
                        if id(r) not in used
                        and int(r.config.get("mirror_idx", 0) or 0) == i
                    ),
                    None,
                )
                if row is None:
                    row = next(
                        (r for r in old if id(r) not in used), None
                    )
                if row is None:
                    self._ctrl_col.add_row(
                        dict(
                            cmd,
                            mirror_of=name,
                            mirror_idx=i,
                        )
                    )
                    continue
                used.add(id(row))
                row.config.update(cmd)
                row.config["mirror_of"] = name
                row.config["mirror_idx"] = i
                row._refresh_labels()
            # Лишние зеркала (точки удалены из таблицы привязки).
            for row in old:
                if id(row) not in used and row in self._ctrl_col._rows:
                    self._ctrl_col.remove_row(row)
        self._ctrl_col.persist()

    def live_frame(
        self, frame_id: int, extended: bool = False
    ) -> bytes | None:
        """Последняя DATA кадра с этим ID на шине — для онлайн-строк
        в таблицах настройки переменных (отчёт мастера: «как только
        записали ID — сразу выводи онлайн»).

        Если кадр с таким ID есть, но другой разрядности — DATA всё
        равно показываем: пустая онлайн-строка при валидном ID
        выглядела как «нет данных на шине» (отчёт мастера)."""
        entry = self._last_frames.get(frame_id)
        if entry is None:
            return None
        return entry[1]

    # ---- списки переменных для Гибкой логики -------------------------

    def variable_names(
        self,
        column: str,
        var_type: str | tuple[str, ...] | None = None,
    ) -> list[str]:
        """Имена переменных колонки («read»/«control»/«aux»);
        var_type — фильтр по виду (строка или кортеж видов),
        None — все."""
        col = {
            "read": self._read_col,
            "control": self._ctrl_col,
            "aux": self._aux_col,
        }.get(column, self._read_col)
        # «Управление» — только команды (отчёт мастера); «Чтение» —
        # все виды переменных либо фильтр по виду.
        if var_type is None and column == "control":
            var_type = _TYPE_COMMAND
        names: list[str] = []
        for cfg in col.configs():
            if cfg.get("type") == _TYPE_FOLDER:
                continue
            if var_type is not None and cfg.get("type") not in (
                (var_type,) if isinstance(var_type, str) else var_type
            ):
                continue
            names.append(cfg.get("name", "").strip() or "—")
        return names

    def variable_binding_names(self, column: str, name: str) -> list[str]:
        """Символьные имена из таблицы привязки «Динамической
        переменной» name — для выпадающего списка состояний в событиях
        и условиях ГЛ (отчёт мастера)."""
        col = {
            "read": self._read_col,
            "control": self._ctrl_col,
            "aux": self._aux_col,
        }.get(column, self._read_col)
        for cfg in col.configs():
            if cfg.get("type") != _TYPE_DYNCACHE:
                continue
            if (cfg.get("name", "").strip() or "—") != name:
                continue
            seen: list[str] = []
            for point in cfg.get("points") or []:
                try:
                    label = str(point[1]).strip()
                except (TypeError, IndexError):
                    continue
                if label and label not in seen:
                    seen.append(label)
            return seen
        return []

    def aux_channel_configs(self) -> list[dict[str, Any]]:
        """Конфиги доп. каналов (вид «Доп канал входа/выхода») третьей
        колонки — для каскадного меню ГЛ: имя → пин/функция."""
        return [
            dict(c) for c in self._aux_col.configs()
            if c.get("type") == "aux"
            and str(c.get("name", "")).strip()
        ]

    # ---- хранение ------------------------------------------------------

    def export_config(self) -> dict[str, Any]:
        return {
            "read": self._read_col.configs(),
            "control": self._ctrl_col.configs(),
            "aux": self._aux_col.configs(),
        }

    def persist(self) -> None:
        """Без авто-кэша: переменные живут в сессии и сохраняются
        только кнопкой «Сохранить переменные» — иначе после перепрошивки
        МК из config.json воскресали старые переменные (отчёт мастера).
        В состав общего файла конфигурации («Config Program») они
        попадают через import_variables/export_config при записи."""

    def import_config(self, data: dict[str, Any]) -> None:
        """Прогружает переменные из словаря конфигурации (файл)."""
        # Записанные значения «Кэш переменных» относятся к набору
        # переменных — при сбросе/загрузке другого файла статусы
        # возвращаются к прочеркам (отчёт мастера).
        self._cache_values.clear()
        self._read_col.clear()
        self._ctrl_col.clear()
        self._aux_col.clear()
        for cfg in data.get("read") or []:
            if isinstance(cfg, dict):
                self._read_col.add_row(cfg)
        for cfg in data.get("control") or []:
            if isinstance(cfg, dict):
                self._ctrl_col.add_row(cfg)
        for cfg in data.get("aux") or []:
            if isinstance(cfg, dict):
                self._aux_col.add_row(cfg)
        # Раздел «Чтение» упорядочен по виду переменной и алфавиту
        # (отчёт мастера).
        self._read_col.sort_rows()
        # Зеркала «Перенести в управление»: существующие обновляются
        # из источников, недостающие — создаются (отчёт мастера).
        self._sync_control_mirrors()

    def _restore(self) -> None:
        """Столбцы стартуют пустыми — кэша переменных в config.json
        больше нет (отчёт мастера)."""

    def _clear_all(self) -> None:
        self._read_col.clear()
        self._ctrl_col.clear()
        self._aux_col.clear()

    # ---- файл «Config Variable» ----------------------------------------

    # Ключи, по которым JSON распознаётся как общий конфиг программы
    def _device_fields(self) -> dict[str, Any]:
        """Идентичность устройства внутри файла: по ней загрузка
        сверяет, подходит ли конфиг подключённому МК."""
        return {
            "device_name": self._config.get("device_name", ""),
            "device_type_name": self._config.get("device_type_name", ""),
            "device_serial": self._config.get("device_serial", "")
            or self._config.get("serial_number", ""),
            "device_type": self._config.get("device_type", 0),
        }

    def _device_matches(self, payload: dict[str, Any]) -> bool:
        """Сверка файла с подключённым устройством — только по типу
        оборудования, серийные номера игнорируются (отчёт мастера).
        Без подключения — всегда подходит (офлайн-редактирование);
        у файла без полей устройства (старый формат) — тоже."""
        file_type = payload.get("device_type")
        file_name = payload.get("device_name") or payload.get(
            "device_type_name"
        )
        if file_type in (None, "") and not file_name:
            return True
        if not self._serial_manager or not self._serial_manager.is_open():
            return True
        dev_type = self._config.get("device_type")
        if file_type not in (None, "") and dev_type is not None:
            try:
                return int(file_type) == int(dev_type)
            except (TypeError, ValueError):
                pass
        dev_name = self._config.get("device_type_name", "") or self._config.get(
            "device_name", ""
        )
        return not dev_name or not file_name or file_name == dev_name

    @property
    def save_variables_button(self) -> QPushButton:
        """Кнопка «Сохранить переменные» — размещается окном
        настроек в нижней панели рядом с общей «Сохранить»
        (отчёт мастера)."""
        return self._save_btn

    def _file_prefix(self) -> str:
        """Префикс имени файла — тип оборудования подключённого
        устройства (отчёт мастера: «в предложенных именах перед
        названием подставляй Тип оборудования»)."""
        name = (
            self._config.get("device_type_name", "")
            or self._config.get("device_name", "")
        ).strip()
        if name:
            name = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in name
            )
            return name + " "
        return ""

    def _save_file(self) -> None:
        last_dir = self._config.get("last_variables_dir", "")
        start_dir = last_dir or _config_info_dir()
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить переменные"),
            f"{start_dir}/{self._file_prefix()}{_CONFIG_INFO_NAME}",
            "Config Variable (*.kmc)",
        )
        if not path:
            return
        self._config.set("last_variables_dir", str(Path(path).parent))
        payload = {
            "kind": _CONFIG_INFO_KIND,
            "format": _CONFIG_INFO_VERSION,
            "app_version": VERSION,
            **self._device_fields(),
            "variables": self.export_config(),
            "id_notes": IdNotes().export_all(),
        }
        # Файл переменных — тот же бинарный контейнер KMCFG, что и
        # общий конфиг программы: идентичность в заголовке + CRC32
        # (отчёт мастера: «файлы обеих конфигураций должны быть в
        # формате BIN»).
        try:
            raw = pack_config_file(
                payload,
                str(payload.get("device_name")
                    or payload.get("device_type_name") or ""),
                str(payload.get("device_serial") or ""),
            )
            Path(path).write_bytes(raw)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self, tr("Ошибка"), tr("Не удалось сохранить файл: {0}")
                .format(exc)
            )

    def load_payload(self, payload: Any, warn_format: bool = False) -> bool:
        """Прогружает payload файла «Config Variable» (общий код
        проверок kind/format/устройства). Вызывается и из загрузчика
        общего конфига, когда оператор подал этот файл в «Загрузить
        конфигурацию» — иначе payload переменных ушёл бы в Config
        программы и потерялся."""
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != _CONFIG_INFO_KIND
            or payload.get("format") not in (1, 2, _CONFIG_INFO_VERSION)
        ):
            if warn_format:
                QMessageBox.warning(
                    self,
                    tr("Config Variable"),
                    tr("Файл не является конфигурацией переменных или "
                       "несовместим с версией приложения"),
                )
            return False
        # Верификация устройства: конфиг чужого МК не прогружаем.
        if not self._device_matches(payload):
            QMessageBox.warning(
                self,
                tr("Config Variable"),
                tr("Файл записан для другого устройства — "
                   "загрузка отменена"),
            )
            return False
        variables = payload.get("variables") or {}
        # Миграция разметки доп. каналов (CM-HW-02): файлы format<3
        # хранят легаси-номера 1..4 — переводим, физический пин
        # сохраняется.
        try:
            fmt = int(payload.get("format", 1) or 1)
        except (TypeError, ValueError):
            fmt = 1
        if isinstance(variables, dict) and fmt < 3:
            variables = dict(variables)
            variables["aux"] = migrate_aux_variables(
                variables.get("aux"), 1
            )
        if isinstance(variables, dict):
            self.import_config(variables)
        notes = payload.get("id_notes")
        if isinstance(notes, dict):
            IdNotes().import_all(notes)
        self.loaded.emit()
        return True

    def retranslate_ui(self) -> None:
        self._title.setText(tr("Переменные"))
        self._save_btn.setText(tr("Сохранить переменные"))
        self._clear_btn.setText(tr("Очистить"))
        self._read_col._title.setText(tr("Чтение"))
        self._ctrl_col._title.setText(tr("Управление"))
        self._aux_col._title.setText(
            tr("Дополнительные каналы входа, выхода и переменные")
        )
        for col in (self._read_col, self._ctrl_col, self._aux_col):
            col._add_btn.setText(tr("＋ Добавить переменную"))
            for row in col._rows:
                row._refresh_labels()
