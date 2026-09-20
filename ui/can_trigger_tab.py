"""Страница «Триггеры» — блоки условий и ответов, слоты во Flash МК."""

from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QEvent, QPoint, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import (
    CMD_TRIGGER_COMMIT,
    CMD_TRIGGER_READ,
    CMD_TRIGGER_STAGE,
    TRIGGER_CLEAR_ALL_KEY,
    pack_can_frame,
)
from core.serial_manager import SerialManager
from core.trigger_protocol import (
    GROUP_SEQ_FRAGMENT,
    TRIGGER_MAX_SLOTS,
    count_configured_triggers,
    expand_schedule,
    pack_trigger,
    unpack_trigger,
)
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex
from ui.hex_edit import create_data_field_widget
from ui.id_edit import IdPasteEdit
from ui.memory_indicator import MemoryIndicator
from ui.packet_clipboard import create_clipboard_buttons

logger = get_logger(__name__)


class TriggerValidationAborted(Exception):
    """Сохранение прервано проверкой триггеров: либо найдены ошибки,
    либо оператор отменил запись после предупреждений. В устройство
    ничего не записано; кнопка «Сохранить» остаётся активной."""

# Ёмкость списка триггеров: пул Flash 0x0803E000–0x0803FFFF (8 КБ,
# запись 82 Б), предел по RAM прошивки — TRIGGER_MAX_SLOTS.
# Старшие прошивки (TRIGGER_COUNT=10/37/49) просто отклоняют большие
# индексы — sync/write обрабатывают это как конец списка.
TRIGGER_COUNT = TRIGGER_MAX_SLOTS
MAX_RESPONSE_FRAMES = 5
CHANNELS = [tr("CAN1"), tr("CAN2"), tr("CAN1 и CAN2")]
BIT_RATES = [tr("11 бит"), tr("29 бит")]


def _commit_payload(total: int, protocol_version: int) -> bytes:
    """Payload для CMD_TRIGGER_COMMIT.

    Полное стирание списка (total=0): прошивка протокола v3 требует
    байт-ключ — голый «CB 01 00» она отвергает как возможный фантом
    рассинхрона CDC-потока. Старая прошивка ключ не знает и отвергла бы
    саму форму «CB 02 00 A5» — ей шлём легаси-формат."""
    if total == 0 and protocol_version >= 3:
        return bytes((0, TRIGGER_CLEAR_ALL_KEY))
    return bytes((total,))


class _IdValidator:
    """Вспомогательный валидатор HEX ID с проверкой максимума."""

    def __init__(self, edit: QLineEdit, bit_combo: QComboBox) -> None:
        self._edit = edit
        self._bit_combo = bit_combo
        self._edit.textChanged.connect(self._validate)
        self._bit_combo.currentIndexChanged.connect(self._update_bitness)
        self._update_bitness()

    def _update_bitness(self) -> None:
        """Меняет максимальную длину HEX ID в зависимости от битности."""
        # 11-битный ID = 0x7FF (3 HEX), 29-битный = 0x1FFFFFFF (8 HEX)
        max_chars = 8 if self._bit_combo.currentIndex() == 1 else 3
        self._edit.setMaxLength(max_chars)
        self._edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(f"[0-9A-Fa-f]{{0,{max_chars}}}"))
        )
        self._validate()

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

    # Срабатывает при любом пользовательском изменении полей триггеров —
    # окно настроек использует его, чтобы включить кнопку «Сохранить».
    settings_changed = Signal()
    # Прогресс обмена с устройством (0-100 внутри текущей фазы + текст
    # этапа) — окно настроек показывает процентный индикатор во время
    # вычитки и прогрузки триггеров в МК.
    progress_updated = Signal(int, str)

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._blocks: List[Dict[str, Any]] = []
        self._applying_device_state = False
        # device_managed[i] == True: триггер i записан во Flash устройства и
        # исполняется самим МК — приложение не должно дублировать ответ
        # (иначе на шине были бы двойные фреймы). Список параллелен
        # _blocks и растёт/сжимается вместе с ним. Сбрасывается при любом
        # редактировании блока и при отключении порта.
        self._device_managed: List[bool] = []
        # pc_suspended[i] == True: блок пришёл из «Загрузить конфигурацию»
        # и ещё не подтверждён «Сохранить» — приложение НЕ исполняет его,
        # иначе загрузка файла сразу запускала ответы на шине, как будто
        # конфиг прогрузили в МК. Снимается успешной write_to_device или
        # вычиткой устройства.
        self._pc_suspended: List[bool] = []
        # Персистентный кэш PC-исполнения: (индекс блока, строка кэша) →
        # последний подошедший кадр. Живёт между кадрами (как s_cache в
        # прошивке); сбрасывается при сохранении конфигурации — фильтры
        # могли измениться и старые данные уже не отвечают им.
        self._pc_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._memory_indicator = MemoryIndicator(self)

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

    def _make_id_edit(self, font: QFont, bit_combo: QComboBox) -> IdPasteEdit:
        edit = IdPasteEdit()
        edit.setFixedWidth(90)
        edit.setFont(font)
        edit.setPlaceholderText("ID")
        edit._id_validator = _IdValidator(edit, bit_combo)
        return edit

    def _make_data_edits(
        self, font: QFont, allow_x: bool = False
    ) -> Tuple[List[QLineEdit], QWidget]:
        return create_data_field_widget(font, 8, edit_width=42, allow_x=allow_x)

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

    def _make_count_spin(self, font: QFont, max_value: int = 100) -> QSpinBox:
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
        layout.addWidget(QLabel(tr("DLC")))
        dlc = self._make_dlc_spin(font)
        layout.addWidget(dlc)
        layout.addWidget(QLabel(tr("Data")))
        # Приёмная маска поддерживает «X» — байт не участвует в
        # сравнении (rx_data_mask=0 на этой позиции).
        data, data_widget = self._make_data_edits(font, allow_x=True)
        layout.addWidget(data_widget)

        rtr = QPushButton(tr("RTR"))
        # Те же размеры/стиль, что у кнопки RTR в строке «Ответ»
        # (64x20, Segoe UI 8) — в 38x24 надпись обрезалась.
        rtr.setFixedSize(64, 20)
        rtr.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        rtr.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; padding: 0px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
            "QPushButton:checked { background-color: #FF9800; color: #FFFFFF; }"
        )
        rtr.setCheckable(True)
        rtr.setToolTip(tr("Срабатывать только на RTR-запрос (Remote Transmission Request)"))
        layout.addWidget(rtr)

        copy_paste = create_clipboard_buttons(self, can_id, dlc, data, bit)
        layout.addWidget(copy_paste)

        layout.addStretch()

        def _on_dlc_or_rtr(*_args: object) -> None:
            # В RTR-режиме приёма кадр не несёт данных — поля Data
            # блокируются (матч идёт по каналу/битности/ID/DLC).
            self._set_data_enabled(data, 0 if rtr.isChecked() else dlc.value())

        dlc.valueChanged.connect(_on_dlc_or_rtr)
        rtr.toggled.connect(_on_dlc_or_rtr)
        self._set_data_enabled(data, dlc.value())

        row = {
            "layout": layout,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "data": data,
            "data_widget": data_widget,
            "rtr": rtr,
            "copy_paste": copy_paste,
        }
        can_id.set_fill_callback(lambda parsed, r=row: self._fill_row_from_packet(r, parsed))
        return row

    def _create_response_block(self, font: QFont) -> Dict[str, Any]:
        """Создаёт блок динамического списка фреймов ответа."""
        group = QGroupBox(tr("Ответ"))
        group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(4)
        group_layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        header_label = QLabel(tr("Фреймы ответа"))
        header.addWidget(header_label)
        header.addStretch()
        add_button = QPushButton("+")
        add_button.setFixedSize(32, 32)
        add_button.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        add_button.setStyleSheet(
            "QPushButton { background-color: #4A4A6A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #5A5A7A; }"
        )
        add_button.setToolTip(tr("Добавить фрейм"))
        header.addWidget(add_button)
        group_layout.addLayout(header)

        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(4)
        group_layout.addLayout(rows_layout)

        block = {"group": group, "header_label": header_label, "rows_layout": rows_layout, "add_button": add_button, "rows": []}
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
        data, data_widget = self._make_data_edits(font)

        # RTR — на каждый фрейм ответа свой, над колонкой «Бит». При
        # включении поле Data этой строки блокируется и бледнеет, активны
        # только ID и DLC.
        rtr = QPushButton(tr("RTR"))
        rtr.setFixedSize(64, 20)
        rtr.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        rtr.setCheckable(True)
        rtr.setToolTip(tr("Remote Transmission Request"))
        rtr.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; padding: 0px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
            "QPushButton:checked { background-color: #FF9800; color: #FFFFFF; }"
        )
        rtr.toggled.connect(
            lambda checked, d=data, w=data_widget, s=dlc: self._on_row_rtr_toggled(checked, d, w, s)
        )

        delay_before_send = self._make_delay_spin(font)
        delay_before_send.setFixedWidth(80)
        delay_between = self._make_delay_spin(font)
        delay_between.setFixedWidth(80)
        count = self._make_count_spin(font, 999)
        count.setFixedWidth(60)

        remove_button = QPushButton("\u2013")
        remove_button.setFixedSize(32, 32)
        remove_button.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        remove_button.setStyleSheet(
            "QPushButton { background-color: #4A4A6A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #5A5A7A; }"
        )
        remove_button.setToolTip(tr("Удалить фрейм"))

        delay_before_label = QLabel(tr("Пауза перед отправкой"))
        delay_between_label = QLabel(tr("Пауза между пакетами"))

        # Все элементы строки прижимаем к нижнему краю — иначе колонка
        # «Бит» с RTR сверху делала бы ряд выше, и остальные поля
        # центрировались бы над строкой ввода.
        bottom = Qt.AlignmentFlag.AlignBottom
        row_layout.addWidget(QLabel(tr("Канал")), alignment=bottom)
        row_layout.addWidget(channel, alignment=bottom)
        # Колонка «Бит» с кнопкой RTR над ней (RTR относится к этой
        # строке ответа, а не ко всему триггеру).
        bit_container = QWidget()
        bit_column = QVBoxLayout(bit_container)
        bit_column.setSpacing(1)
        bit_column.setContentsMargins(0, 0, 0, 0)
        bit_column.addWidget(rtr, alignment=Qt.AlignmentFlag.AlignHCenter)
        bit_row = QHBoxLayout()
        bit_row.setSpacing(2)
        bit_row.setContentsMargins(0, 0, 0, 0)
        bit_row.addWidget(QLabel(tr("Бит")))
        bit_row.addWidget(bit)
        bit_column.addLayout(bit_row)
        row_layout.addWidget(bit_container, alignment=bottom)
        row_layout.addWidget(QLabel(tr("ID")), alignment=bottom)
        row_layout.addWidget(can_id, alignment=bottom)
        row_layout.addWidget(QLabel(tr("DLC")), alignment=bottom)
        row_layout.addWidget(dlc, alignment=bottom)
        row_layout.addWidget(data_widget, alignment=bottom)

        copy_paste = create_clipboard_buttons(self, can_id, dlc, data, bit)
        row_layout.addWidget(copy_paste, alignment=bottom)

        row_layout.addStretch()
        row_layout.addWidget(delay_before_label, alignment=bottom)
        row_layout.addWidget(delay_before_send, alignment=bottom)
        row_layout.addWidget(delay_between_label, alignment=bottom)
        row_layout.addWidget(delay_between, alignment=bottom)
        row_layout.addWidget(QLabel(tr("Кол-во")), alignment=bottom)
        row_layout.addWidget(count, alignment=bottom)
        row_layout.addWidget(remove_button, alignment=bottom)

        dlc.valueChanged.connect(
            lambda value: self._set_data_enabled(data, 0 if rtr.isChecked() else value)
        )
        self._set_data_enabled(data, dlc.value())

        next_delay = self._make_delay_spin(font)
        next_delay.setFixedWidth(80)
        pause_widget = self._create_pause_widget(font, next_delay)

        row = {
            "widget": widget,
            "layout": row_layout,
            "channel": channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "data": data,
            "data_widget": data_widget,
            "copy_paste": copy_paste,
            "delay_before_send": delay_before_send,
            "delay_between": delay_between,
            "delay_before_label": delay_before_label,
            "delay_between_label": delay_between_label,
            "count": count,
            "rtr": rtr,
            "next_delay": next_delay,
            "pause_widget": pause_widget,
            "remove_button": remove_button,
        }
        remove_button.clicked.connect(lambda: self._remove_response_row(block, row))
        can_id.set_fill_callback(lambda parsed, r=row: self._fill_row_from_packet(r, parsed))
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
        # Строки, добавленные после _watch_block_signals, подписываем здесь.
        index = next(
            (i for i, b in enumerate(self._blocks) if b["response"] is block),
            None,
        )
        if index is not None:
            self._watch_widget_tree(new_row["widget"], index, [new_row["copy_paste"]])
        self._rebuild_response_rows(block)
        self._update_response_buttons(block)

    def _remove_response_row(self, block: Dict[str, Any], row: Dict[str, Any]) -> None:
        """Удаляет строку фрейма из блока ответа (минимум 1)."""
        if len(block["rows"]) <= 1:
            return
        block["rows"].remove(row)
        # setParent(None) — строка покидает дерево виджетов сразу, а не
        # после обработки deleteLater: иначе снимок полей окна настроек
        # ещё содержал бы удалённую строку и не видел изменения.
        row["widget"].setParent(None)
        row["pause_widget"].setParent(None)
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
            first = i == 0
            row["delay_before_label"].setVisible(first)
            row["delay_before_send"].setVisible(first)
            if i < len(block["rows"]) - 1:
                block["rows_layout"].addWidget(row["pause_widget"])
                row["pause_widget"].show()

    def _update_response_buttons(self, block: Dict[str, Any]) -> None:
        """Активирует/деактивирует кнопки +/- в зависимости от количества строк."""
        can_add = len(block["rows"]) < MAX_RESPONSE_FRAMES
        block["add_button"].setEnabled(can_add)
        for row in block["rows"]:
            row["remove_button"].setEnabled(len(block["rows"]) > 1)

    def _create_cache_block(self, font: QFont, index: int) -> Dict[str, Any]:
        """Блок кэша — динамический список строк, как «Фреймы ответа»:
        у каждой строки свой источник (канал/ID/DLC/диапазон От-До) и
        своя отправка (канал, паузы, количество). Один входящий кадр
        может пополнять несколько строк кэша."""
        group = QGroupBox(tr("Кэш"))
        group.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(4)
        group_layout.setContentsMargins(6, 6, 6, 6)

        cache_check = QCheckBox(tr("Автоматическая запись DATA в Кэш"))
        cache_check.setFont(font)
        cache_check.stateChanged.connect(lambda state, idx=index: self._on_cache_active_changed(idx, state))
        group_layout.addWidget(cache_check)

        fields_widget = QWidget()
        fields_layout = QVBoxLayout(fields_widget)
        fields_layout.setSpacing(4)
        fields_layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header_label = QLabel(tr("Строки кэша"))
        header.addWidget(header_label)
        header.addStretch()
        add_button = QPushButton("+")
        add_button.setFixedSize(32, 32)
        add_button.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        add_button.setStyleSheet(
            "QPushButton { background-color: #4A4A6A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #5A5A7A; }"
        )
        add_button.setToolTip(tr("Добавить строку кэша"))
        header.addWidget(add_button)
        fields_layout.addLayout(header)

        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(4)
        fields_layout.addLayout(rows_layout)
        group_layout.addWidget(fields_widget)

        cache = {
            "group": group,
            "cache_check": cache_check,
            "fields_widget": fields_widget,
            "header_label": header_label,
            "rows_layout": rows_layout,
            "add_button": add_button,
            "rows": [],
        }
        add_button.clicked.connect(lambda: self._add_cache_row(cache, font))
        self._add_cache_row(cache, font)
        return cache

    def _create_cache_row(self, font: QFont, cache: Dict[str, Any]) -> Dict[str, Any]:
        """Одна строка кэша: источник (канал/ID/DLC/диапазон От-До) и
        отправка закэшированного кадра (канал/паузы/количество).
        «X» в полях От/До — байт игнорируется при сравнении, а в ответе
        на его месте уходит 0x00."""
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setSpacing(2)
        column.setContentsMargins(0, 0, 0, 0)

        line1 = QHBoxLayout()
        line1.setSpacing(4)
        src_label = QLabel(tr("Откуда читаем"))
        line1.addWidget(src_label)
        channel = self._make_channel_combo(font)
        line1.addWidget(channel)
        line1.addWidget(QLabel(tr("Бит")))
        bit = self._make_bit_combo(font)
        line1.addWidget(bit)
        line1.addWidget(QLabel(tr("ID")))
        can_id = self._make_id_edit(font, bit)
        line1.addWidget(can_id)
        line1.addWidget(QLabel(tr("DLC")))
        dlc = self._make_dlc_spin(font)
        line1.addWidget(dlc)
        line1.addWidget(QLabel(tr("От")))
        from_data, from_data_widget = self._make_data_edits(font, allow_x=True)
        line1.addWidget(from_data_widget)
        from_copy_paste = create_clipboard_buttons(self, can_id, dlc, from_data, bit)
        line1.addWidget(from_copy_paste)
        line1.addWidget(QLabel(tr("До")))
        to_data, to_data_widget = self._make_data_edits(font, allow_x=True)
        line1.addWidget(to_data_widget)
        to_copy_paste = create_clipboard_buttons(self, can_id, dlc, to_data, bit)
        line1.addWidget(to_copy_paste)
        line1.addStretch()

        line2 = QHBoxLayout()
        line2.setSpacing(4)
        dst_label = QLabel(tr("Куда отправляем"))
        line2.addWidget(dst_label)
        tx_channel = self._make_channel_combo(font)
        line2.addWidget(tx_channel)
        delay_before_label = QLabel(tr("Пауза перед отправкой"))
        delay_between_label = QLabel(tr("Пауза между пакетами"))
        line2.addWidget(delay_before_label)
        delay_before_send = self._make_delay_spin(font)
        delay_before_send.setSuffix("")
        delay_before_send.setFixedWidth(80)
        line2.addWidget(delay_before_send)
        line2.addWidget(delay_between_label)
        delay_between = self._make_delay_spin(font)
        delay_between.setSuffix("")
        delay_between.setFixedWidth(80)
        line2.addWidget(delay_between)
        count_label = QLabel(tr("Кол-во отправок"))
        line2.addWidget(count_label)
        count = self._make_count_spin(font, 999)
        count.setSuffix("")
        count.setFixedWidth(60)
        line2.addWidget(count)
        line2.addStretch()

        remove_button = QPushButton("\u2013")
        remove_button.setFixedSize(32, 32)
        remove_button.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        remove_button.setStyleSheet(
            "QPushButton { background-color: #4A4A6A; color: #FFFFFF; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #5A5A7A; }"
        )
        remove_button.setToolTip(tr("Удалить строку"))
        line2.addWidget(remove_button)

        column.addLayout(line1)
        column.addLayout(line2)

        dlc.valueChanged.connect(lambda value: self._set_data_enabled(from_data, value))
        dlc.valueChanged.connect(lambda value: self._set_data_enabled(to_data, value))
        self._set_data_enabled(from_data, dlc.value())
        self._set_data_enabled(to_data, dlc.value())

        next_delay = self._make_delay_spin(font)
        next_delay.setFixedWidth(80)
        pause_widget = self._create_pause_widget(font, next_delay)

        row = {
            "widget": widget,
            "pause_widget": pause_widget,
            "next_delay": next_delay,
            "src_label": src_label,
            "dst_label": dst_label,
            "delay_before_label": delay_before_label,
            "delay_between_label": delay_between_label,
            "count_label": count_label,
            "channel": channel,
            "tx_channel": tx_channel,
            "bit": bit,
            "id": can_id,
            "dlc": dlc,
            "from_data": from_data,
            "from_data_widget": from_data_widget,
            "from_copy_paste": from_copy_paste,
            "to_data": to_data,
            "to_data_widget": to_data_widget,
            "to_copy_paste": to_copy_paste,
            "delay_before_send": delay_before_send,
            "delay_between": delay_between,
            "count": count,
            "remove_button": remove_button,
        }
        remove_button.clicked.connect(lambda: self._remove_cache_row(cache, row))
        can_id.set_fill_callback(
            lambda parsed, r=row: self._fill_cache_row_from_packet(r, parsed)
        )
        return row

    def _add_cache_row(self, cache: Dict[str, Any], font: QFont) -> None:
        """Добавляет строку кэша (максимум как у фреймов ответа)."""
        if len(cache["rows"]) >= MAX_RESPONSE_FRAMES:
            return
        new_row = self._create_cache_row(font, cache)
        cache["rows"].append(new_row)
        # Строки, добавленные после _watch_block_signals, подписываем здесь.
        index = next(
            (i for i, b in enumerate(self._blocks) if b["cache"] is cache),
            None,
        )
        if index is not None:
            self._watch_widget_tree(
                new_row["widget"],
                index,
                [new_row["from_copy_paste"], new_row["to_copy_paste"]],
            )
        self._rebuild_cache_rows(cache)
        self._update_cache_buttons(cache)

    def _remove_cache_row(self, cache: Dict[str, Any], row: Dict[str, Any]) -> None:
        """Удаляет строку кэша (минимум 1)."""
        if len(cache["rows"]) <= 1:
            return
        cache["rows"].remove(row)
        row["widget"].setParent(None)
        row["pause_widget"].setParent(None)
        row["widget"].deleteLater()
        row["pause_widget"].deleteLater()
        self._rebuild_cache_rows(cache)
        self._update_cache_buttons(cache)

    def _rebuild_cache_rows(self, cache: Dict[str, Any]) -> None:
        """Перестраивает layout строк кэша и видимость пауз — как у
        фреймов ответа: «Пауза перед отправкой» только в первой строке,
        между строками — «Пауза перед следующим»."""
        for row in cache["rows"]:
            cache["rows_layout"].removeWidget(row["widget"])
            row["widget"].hide()
            cache["rows_layout"].removeWidget(row["pause_widget"])
            row["pause_widget"].hide()
        for i, row in enumerate(cache["rows"]):
            cache["rows_layout"].addWidget(row["widget"])
            row["widget"].show()
            first = i == 0
            row["delay_before_label"].setVisible(first)
            row["delay_before_send"].setVisible(first)
            if i < len(cache["rows"]) - 1:
                cache["rows_layout"].addWidget(row["pause_widget"])
                row["pause_widget"].show()

    def _update_cache_buttons(self, cache: Dict[str, Any]) -> None:
        """Активирует/деактивирует кнопки +/- строк кэша."""
        can_add = len(cache["rows"]) < MAX_RESPONSE_FRAMES
        cache["add_button"].setEnabled(can_add)
        for row in cache["rows"]:
            row["remove_button"].setEnabled(len(cache["rows"]) > 1)

    def _set_data_enabled(self, edits: List[QLineEdit], count: int) -> None:
        for i, edit in enumerate(edits):
            if i >= count:
                edit.setText("")
                edit.setEnabled(False)
            else:
                edit.setEnabled(True)

    def _on_row_rtr_toggled(
        self, checked: bool, edits: List[QLineEdit], widget: QWidget, dlc: QSpinBox
    ) -> None:
        """RTR в строке ответа триггера: поле Data этой строки блокируется и
        бледнеет, редактируемыми остаются только ID и DLC."""
        self._set_data_enabled(edits, 0 if checked else dlc.value())
        widget.setEnabled(not checked)
        self._set_widget_opacity(widget, 0.35 if checked else 1.0)

    def _fill_row_from_packet(self, row: Dict[str, Any], parsed: Dict[str, Any]) -> None:
        """Заполняет строку (ID, DLC, Data) из распарсенного пакета."""
        can_id = parsed.get("id")
        if can_id is None:
            return
        bit_index = 1 if can_id > 0x7FF else 0
        row["bit"].setCurrentIndex(bit_index)
        row["id"].setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        dlc = max(1, min(8, parsed.get("dlc", 8)))
        row["dlc"].setValue(dlc)
        data = parsed.get("data", [])
        for i, edit in enumerate(row["data"]):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")
        rtr = row["rtr"].isChecked() if "rtr" in row else False
        self._set_data_enabled(row["data"], 0 if rtr else dlc)

    def _fill_cache_row_from_packet(self, row: Dict[str, Any], parsed: Dict[str, Any]) -> None:
        """Заполняет строку кэша (ID, DLC, От/До) из распарсенного пакета."""
        can_id = parsed.get("id")
        if can_id is None:
            return
        bit_index = 1 if can_id > 0x7FF else 0
        row["bit"].setCurrentIndex(bit_index)
        row["id"].setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        dlc = max(1, min(8, parsed.get("dlc", 8)))
        row["dlc"].setValue(dlc)
        data = parsed.get("data", [])
        for i, edit in enumerate(row["from_data"]):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")
        for i, edit in enumerate(row["to_data"]):
            edit.setText(f"{data[i]:02X}" if i < len(data) else "")
        self._set_data_enabled(row["from_data"], dlc)
        self._set_data_enabled(row["to_data"], dlc)

    def _create_widgets(self) -> None:
        self._font = QFont("Segoe UI", 9)
        # Блоки триггеров создаются лениво: кнопкой «Добавить триггер»,
        # загрузкой конфига или синхронизацией с устройством. Заводское
        # состояние — один пустой блок.

    def _create_trigger_block(self, index: int) -> Dict[str, Any]:
        """Создаёт виджеты одного блока триггера (позиция = index)."""
        font = self._font
        group = QGroupBox(tr("Триггер {0}").format(index + 1))
        group.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        group.setCheckable(True)
        group.setChecked(False)

        status = QLabel(tr("Статус: не читался"))
        status.setFont(font)
        status.setStyleSheet("color: #9E9E9E;")

        recv = self._create_receive_row(font, tr("Приём"))
        response = self._create_response_block(font)
        cache = self._create_cache_block(font, index)

        # Крестик удаления — в шапке блока (верхний правый угол).
        # Символ «✕» в части шрифтов не рендерится, поэтому берём
        # стандартную иконку закрытия окна — она есть в любой теме.
        delete_button = QPushButton()
        delete_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        delete_button.setFixedSize(24, 24)
        delete_button.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #5A2A2A; }"
        )
        delete_button.setToolTip(tr("Удалить триггер"))
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)

        return {
            "group": group,
            "status": status,
            "recv": recv,
            "response": response,
            "cache": cache,
            "delete_button": delete_button,
        }

    def _layout_trigger_block(self, block: Dict[str, Any], index: int) -> None:
        """Собирает layout блока и добавляет его в контейнер."""
        group_layout = QVBoxLayout(block["group"])
        group_layout.setSpacing(5)
        group_layout.setContentsMargins(6, 6, 6, 6)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(5)
        content_layout.setContentsMargins(6, 6, 6, 6)

        content_layout.addLayout(block["recv"]["layout"])
        content_layout.addWidget(block["response"]["group"])
        content_layout.addWidget(block["cache"]["group"])
        self._set_cache_enabled(block, False)
        block["cache"]["cache_check"].setEnabled(True)

        # Шапка блока: статус слева.
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(block["status"])
        header_row.addStretch()
        group_layout.addLayout(header_row)
        group_layout.addWidget(content)
        block["group"].toggled.connect(lambda checked, c=content: c.setVisible(checked))
        block["group"].toggled.connect(lambda checked, b=block: self._on_trigger_toggled_by_block(b, checked))
        block["content"] = content

        # Крестик удаления — НЕ ребёнок checkable QGroupBox: иначе Qt
        # глушит его вместе с содержимым при снятом чеке (в т.ч. на show).
        # Кладём кнопку в ту же ячейку сетки поверх группы — правый верхний
        # угол блока, активна всегда.
        delete_button = block["delete_button"]
        delete_button.clicked.connect(lambda _c=False, b=block: self._remove_trigger_block(b))

        wrapper = QWidget()
        grid = QGridLayout(wrapper)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(block["group"], 0, 0)
        # Крестик — по горизонтали ровно над «+» добавления ответа,
        # по вертикали — напротив поля Data строки «Приём»: обе
        # позиции известны только после компоновки, поэтому отступы
        # подстраиваются в _align_delete_button при Resize.
        # AlignTop|AlignRight — иначе holder растянется на весь
        # блок и перехватит все клики по полям триггера.
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 4, 22, 0)
        holder_layout.setSpacing(0)
        holder_layout.addStretch()
        holder_layout.addWidget(delete_button)
        grid.addWidget(
            holder, 0, 0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )
        delete_button.raise_()
        block["wrapper"] = wrapper
        block["delete_holder_layout"] = holder_layout
        wrapper.installEventFilter(self)
        self._blocks_layout.addWidget(wrapper)

    def eventFilter(self, watched: QWidget, event: QEvent) -> bool:  # noqa: N802
        """Подстраивает крестик удаления под поле Data строки «Приём»."""
        if event.type() == QEvent.Type.Resize:
            for block in self._blocks:
                if block.get("wrapper") is watched:
                    self._align_delete_button(block)
                    break
        # False, а не super().eventFilter(): проброс события в
        # watched->event() порождает взаимную рекурсию с фильтром
        # settings_window (~300 вложенных вызовов на событие) —
        # отсюда мерцание кнопки «Сохранить».
        return False

    def _align_delete_button(self, block: Dict[str, Any]) -> None:
        """Ставит крестик по высоте напротив поля Data в «Приём», а по
        горизонтали — ровно над «+» добавления фрейма ответа."""
        data_widget = block["recv"]["data_widget"]
        add_button = block["response"]["add_button"]
        wrapper = block.get("wrapper")
        layout = block.get("delete_holder_layout")
        if wrapper is None or layout is None:
            return
        try:
            center_y = (
                data_widget.mapTo(wrapper, QPoint(0, 0)).y()
                + data_widget.height() // 2
            )
            add_center_x = (
                add_button.mapTo(wrapper, QPoint(0, 0)).x()
                + add_button.width() // 2
            )
        except RuntimeError:
            return  # виджет уже уничтожен
        # Центр крестика — над центром «+»: отступ справа = расстоянию
        # от центра «+» до правого края минус половина крестика (12 px).
        # «+» всегда у правого края — отложенная карта при скрытом блоке
        # даёт x≈0, такие значения отбрасываем и держим прежний отступ.
        right = layout.contentsMargins().right()
        if add_center_x > wrapper.width() // 2:
            right = max(0, wrapper.width() - add_center_x - 12)
        layout.setContentsMargins(0, max(0, center_y - 12), right, 0)

    def _remove_block_at(self, index: int) -> None:
        """Внутреннее удаление блока по позиции (крестик/синхронизация)."""
        block = self._blocks.pop(index)
        self._device_managed.pop(index)
        self._pc_suspended.pop(index)
        host = block.get("wrapper") or block["group"]
        self._blocks_layout.removeWidget(host)
        # Отсоединяем от дерева сразу — до отложенного deleteLater,
        # чтобы снимок полей окна настроек не видел удалённый блок.
        host.setParent(None)
        host.deleteLater()

    def _remove_trigger_block(self, block: Dict[str, Any]) -> None:
        """Удаляет блок триггера из UI и из будущей записи в устройство."""
        if self._applying_device_state:
            return
        try:
            index = self._blocks.index(block)
        except ValueError:
            return
        self._remove_block_at(index)
        # Перенумеровываем заголовки оставшихся блоков.
        for i, b in enumerate(self._blocks):
            b["group"].setTitle(tr("Триггер {0}").format(i + 1))
        self._update_add_trigger_button()
        self._mark_dirty()
        self._save_config()

    def _add_trigger_block(self) -> Optional[int]:
        """Добавляет блок триггера. Лимит — ёмкость пула Flash
        (TRIGGER_COUNT записей по 82 Б + заголовок в 8 КБ над config)."""
        if len(self._blocks) >= TRIGGER_COUNT:
            return None
        index = len(self._blocks)
        block = self._create_trigger_block(index)
        self._blocks.append(block)
        self._device_managed.append(False)
        # Созданный в UI блок исполняется приложением сразу (предпросмотр
        # до «Сохранить») — подвешенным его не делаем.
        self._pc_suspended.append(False)
        if hasattr(self, "_blocks_layout"):
            self._layout_trigger_block(block, index)
            self._watch_block_signals(block)
            self._update_add_trigger_button()
        return index

    def _update_add_trigger_button(self) -> None:
        full = len(self._blocks) >= TRIGGER_COUNT
        self._add_trigger_button.setEnabled(not full)
        self._add_trigger_button.setToolTip(
            tr("Страница триггеров заполнена ({0}/{1})").format(len(self._blocks), TRIGGER_COUNT)
            if full
            else tr("Добавить триггер")
        )

    def _on_add_trigger_clicked(self) -> None:
        if self._add_trigger_block() is not None:
            self._mark_dirty(len(self._blocks) - 1)

    def _sim_records(self) -> List[Dict[str, Any]]:
        """Записи в формате firmware для симулятора — то же, что ушло бы
        в МК по «Сохранить» (непустые блоки, enabled как в UI)."""
        records = []
        for index in range(len(self._blocks)):
            values = self._device_trigger_values(index)
            if not self._is_empty_trigger(values):
                records.append(values)
        return records

    def _open_simulation(self) -> None:
        from ui.trigger_sim_dialog import TriggerSimDialog

        dialog = TriggerSimDialog(self._sim_records, self)
        dialog.exec()

    @staticmethod
    def _templates() -> Dict[str, Dict[str, Any]]:
        """Готовые сценарии триггеров — заполненный блок в один клик,
        дальше оператор правит ID/данные под себя."""
        return {
            tr("Эхо-ответ"): {
                "active": True,
                "recv_channel": 0,
                "recv_bit": 0,
                "recv_id": "100",
                "recv_dlc": 8,
                "recv_rtr": 0,
                "recv_data": "",
                "responses": [
                    {
                        "channel": 0, "bit": 0, "id": "101", "dlc": 8,
                        "data": "00 00 00 00 00 00 00 00", "rtr": 0,
                        "delay_before_send": 0, "delay_between": 0,
                        "count": 1, "next_delay": 0,
                    }
                ],
            },
            tr("Маршрутизация CAN1 → CAN2"): {
                "active": True,
                "recv_channel": 0,
                "recv_bit": 0,
                "recv_id": "123",
                "recv_dlc": 8,
                "recv_rtr": 0,
                "recv_data": "",
                "responses": [
                    {
                        "channel": 1, "bit": 0, "id": "123", "dlc": 8,
                        "data": "", "rtr": 0,
                        "delay_before_send": 0, "delay_between": 0,
                        "count": 1, "next_delay": 0,
                    }
                ],
            },
            tr("Ответ на RTR-запрос"): {
                "active": True,
                "recv_channel": 0,
                "recv_bit": 0,
                "recv_id": "200",
                "recv_dlc": 0,
                "recv_rtr": 1,
                "recv_data": "",
                "responses": [
                    {
                        "channel": 0, "bit": 0, "id": "200", "dlc": 8,
                        "data": "00 00 00 00 00 00 00 00", "rtr": 0,
                        "delay_before_send": 0, "delay_between": 0,
                        "count": 1, "next_delay": 0,
                    }
                ],
            },
            tr("Ответ пачкой ×3 с паузой"): {
                "active": True,
                "recv_channel": 0,
                "recv_bit": 0,
                "recv_id": "210",
                "recv_dlc": 8,
                "recv_rtr": 0,
                "recv_data": "",
                "responses": [
                    {
                        "channel": 0, "bit": 0, "id": "211", "dlc": 8,
                        "data": "", "rtr": 0,
                        "delay_before_send": 50, "delay_between": 20,
                        "count": 3, "next_delay": 0,
                    }
                ],
            },
            tr("Кэш-репитер CAN1 → CAN2"): {
                "active": True,
                "cache": True,
                "recv_channel": 0,
                "recv_bit": 0,
                "recv_id": "300",
                "recv_dlc": 0,
                "recv_rtr": 0,
                "recv_data": "",
                "responses": [],
                "cache_channel": 0,
                "cache_bit": 0,
                "cache_id": "300",
                "cache_dlc": 8,
                "cache_tx_channel": 1,
                "cache_from_data": "",
                "cache_to_data": "",
                "cache_delay_before_send": 0,
                "cache_delay_between": 0,
                "cache_count": 1,
            },
        }

    def _add_template_trigger(self, preset: Dict[str, Any]) -> None:
        """Добавляет блок триггера из шаблона (тем же путём, что загрузка
        конфига: текущие блоки + пресет → set_config)."""
        from copy import deepcopy

        if len(self._blocks) >= TRIGGER_COUNT:
            QMessageBox.warning(
                self, tr("Шаблон"), tr("Страница триггеров заполнена")
            )
            return
        self.set_config(self._collect_config() + [deepcopy(preset)])
        self._mark_dirty(len(self._blocks) - 1)

    def _build_layout(self) -> None:
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(10)
        container_layout.setContentsMargins(8, 8, 8, 8)

        self._blocks_layout = QVBoxLayout()
        self._blocks_layout.setSpacing(10)
        container_layout.addLayout(self._blocks_layout)

        self._add_trigger_button = QPushButton(tr("Добавить триггер"))
        self._add_trigger_button.setFont(QFont("Segoe UI", 9))
        self._add_trigger_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
            "QPushButton:disabled { color: #777777; }"
        )
        self._add_trigger_button.clicked.connect(self._on_add_trigger_clicked)

        self._sim_button = QPushButton(tr("Симуляция…"))
        self._sim_button.setFont(QFont("Segoe UI", 9))
        self._sim_button.setStyleSheet(
            "QPushButton { background-color: #2A4A3A; color: #FFFFFF; border: none; border-radius: 4px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #3A6A4A; }"
        )
        self._sim_button.setToolTip(
            tr("Прогнать кадры из лога через триггеры без железа")
        )
        self._sim_button.clicked.connect(self._open_simulation)

        self._template_button = QPushButton(tr("Шаблон ▾"))
        self._template_button.setFont(QFont("Segoe UI", 9))
        self._template_button.setStyleSheet(
            "QPushButton { background-color: #3A3A5A; color: #FFFFFF; border: none; border-radius: 4px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #4A4A6A; }"
        )
        template_menu = QMenu(self._template_button)
        for title, preset in self._templates().items():
            template_menu.addAction(
                title, lambda _c=False, p=preset: self._add_template_trigger(p)
            )
        self._template_button.setMenu(template_menu)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        buttons_row.addWidget(self._add_trigger_button)
        buttons_row.addWidget(self._template_button)
        buttons_row.addWidget(self._sim_button)
        buttons_row.addStretch()
        container_layout.addLayout(buttons_row)
        container_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        layout.addWidget(self._memory_indicator)

        # Пустое устройство — ноль блоков: блоки появляются кнопкой
        # «Добавить триггер», загрузкой конфига или вычиткой из МК.
        self._update_add_trigger_button()

    def _watch_block_signals(self, block: Dict[str, Any]) -> None:
        """Подписывает пользовательские изменения всех полей блока на
        _mark_dirty: снимает флаг device_managed и сообщает окну настроек,
        что появились несохранённые изменения. Индекс блока вычисляется
        в момент срабатывания — удаление не ломает подписки."""

        def mark(*_args: object) -> None:
            try:
                self._mark_dirty(self._blocks.index(block))
            except ValueError:
                self._mark_dirty()

        block["group"].toggled.connect(mark)
        skip = [
            block["recv"]["copy_paste"],
            *(row["from_copy_paste"] for row in block["cache"]["rows"]),
            *(row["to_copy_paste"] for row in block["cache"]["rows"]),
            *(row["copy_paste"] for row in block["response"]["rows"]),
        ]
        self._watch_widget_tree(block["group"], block, skip)

    @staticmethod
    def _inside_any(widget: QWidget, containers: List[QWidget]) -> bool:
        parent = widget
        while parent is not None:
            if parent in containers:
                return True
            parent = parent.parentWidget()
        return False

    def _watch_widget_tree(
        self, root: QWidget, block: Dict[str, Any], skip: Optional[List[QWidget]] = None
    ) -> None:
        """Подписывает на _mark_dirty все поля ввода внутри виджета root,
        кроме виджетов внутри контейнеров skip (кнопки копипасты)."""

        def mark(*_args: object) -> None:
            try:
                self._mark_dirty(self._blocks.index(block))
            except ValueError:
                self._mark_dirty()

        skip = skip or []
        for widget in root.findChildren(QComboBox):
            if not self._inside_any(widget, skip):
                widget.activated.connect(mark)
        for widget in root.findChildren(QLineEdit):
            if not self._inside_any(widget, skip):
                widget.textEdited.connect(mark)
        for widget in root.findChildren(QSpinBox):
            if self._inside_any(widget, skip):
                continue
            # valueChanged срабатывает и при программном setValue — считаем
            # изменение пользовательским только когда спин в фокусе.
            widget.valueChanged.connect(
                lambda *_a, w=widget, m=mark: m() if w.hasFocus() else None
            )
        for widget in root.findChildren(QCheckBox):
            if not self._inside_any(widget, skip):
                widget.clicked.connect(mark)
        for widget in root.findChildren(QPushButton):
            if not self._inside_any(widget, skip):
                widget.clicked.connect(mark)

    def _mark_dirty(self, index: Optional[int] = None) -> None:
        """Помечает конфигурацию изменённой пользователем."""
        if self._applying_device_state:
            return
        # Фильтры/ответы могли измениться — накопленный кэш PC-исполнения
        # им уже не отвечает (аналог memset(s_cache_valid) при коммите).
        self._pc_cache.clear()
        if index is not None and 0 <= index < len(self._device_managed):
            self._device_managed[index] = False
        self.settings_changed.emit()

    def _device_trigger_values(
        self, index: int, cache_row: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        block = self._blocks[index]
        recv = block["recv"]
        # rx_channel/tx_channel хранятся 0-based как индекс комбобокса:
        # 0=CAN1, 1=CAN2, 2=«CAN1 и CAN2» — firmware понимает все три.
        recv_id = self._parse_id(recv["id"].text()) or 0
        rx_extended = recv["bit"].currentIndex()
        rx_mask = 0x1FFFFFFF if rx_extended else 0x7FF
        rx_values = self._parse_data(recv["data"])
        rx_data = bytes((value or 0) & 0xFF for value in rx_values)
        rx_data_mask = bytes(0xFF if value is not None else 0 for value in rx_values)

        cache = block["cache"]
        cache_enabled = int(cache["cache_check"].isChecked())
        if cache_enabled:
            # Кэш-режим: ответ — последний закэшированный кадр, поля
            # tx_id/dlc/data/rtr записи не используются прошивкой.
            # Каждая строка кэша — своя запись (src_* её фильтра).
            # Пустые поля диапазона — полный диапазон [00, FF]; «X»
            # кодируется инвертированным диапазоном (from=FF > to=00) —
            # прошивка игнорирует байт и обнуляет его в кэше.
            tx_id, tx_extended, tx_dlc, tx_rtr = 0, 0, 0, 0
            tx_data = b""
            row = cache_row or (
                self._collect_cache_row(cache["rows"][0]) if cache["rows"] else {}
            )
            wild = row.get("wild") or [False] * 8
            tx_channel = int(row.get("tx_channel", 0))
            delay_ms = int(row.get("delay_before_send", 0))
            tx_interval_ms = int(row.get("delay_between", 0))
            tx_count = int(row.get("count", 1))
            src_channel = int(row.get("channel", 0))
            src_extended = int(row.get("extended", 0))
            src_id = row.get("id") or 0
            src_dlc = int(row.get("dlc", 0))
            src_from = bytes(
                0xFF if wild[i] else ((v & 0xFF) if v is not None else 0x00)
                for i, v in enumerate(row.get("data_from", [None] * 8))
            )
            src_to = bytes(
                0x00 if wild[i] else ((v & 0xFF) if v is not None else 0xFF)
                for i, v in enumerate(row.get("data_to", [None] * 8))
            )
        else:
            response_rows = block["response"]["rows"]
            response = response_rows[0] if response_rows else None
            if response is None:
                tx_rtr = 0
                tx_id, tx_channel, tx_extended, tx_dlc = 0, 0, 0, 0
                tx_data = b""
                delay_ms = 0
                tx_interval_ms = 0
                tx_count = 0
            else:
                tx_id = self._parse_id(response["id"].text()) or 0
                tx_channel = response["channel"].currentIndex()
                tx_extended = response["bit"].currentIndex()
                tx_dlc = response["dlc"].value()
                tx_data = bytes((value or 0) & 0xFF for value in self._parse_data(response["data"]))
                tx_rtr = int(response["rtr"].isChecked())
                delay_ms = response["delay_before_send"].value()
                tx_interval_ms = response["delay_between"].value()
                tx_count = response["count"].value()
            src_channel = src_extended = src_dlc = 0
            src_id = 0
            src_from = b""
            src_to = b""

        return {
            "enabled": int(block["group"].isChecked()),
            "rx_channel": recv["channel"].currentIndex(),
            "rx_extended": rx_extended,
            "rx_id": recv_id,
            "rx_id_mask": rx_mask,
            "rx_dlc": recv["dlc"].value(),
            "rx_data": rx_data,
            "rx_data_mask": rx_data_mask,
            # rx_rtr: 1 = срабатывать только на RTR-запрос, 0 = любой кадр.
            "rx_rtr": int(recv["rtr"].isChecked()),
            "tx_channel": tx_channel,
            "tx_extended": tx_extended,
            "tx_id": tx_id,
            "tx_dlc": tx_dlc,
            "tx_data": tx_data,
            "tx_rtr": tx_rtr,
            "delay_ms": delay_ms,
            "cache_enabled": cache_enabled,
            "src_channel": src_channel,
            "src_extended": src_extended,
            "src_id": src_id,
            "src_dlc": src_dlc,
            "src_from": src_from,
            "src_to": src_to,
            "tx_interval_ms": tx_interval_ms,
            "tx_count": tx_count,
        }

    def _set_trigger_status(self, index: int, state: str) -> None:
        """Обновляет строку статуса триггера.

        state: 'unknown' | 'enabled' | 'disabled' | 'synced' | 'differs' | 'written'
        """
        label = self._blocks[index]["status"]
        enabled = self._blocks[index]["group"].isChecked()
        on_off = tr("вкл") if enabled else tr("выкл")
        if state == "synced":
            text = tr("Статус: {0}, совпадает с устройством").format(on_off)
            color = "#66BB6A"
        elif state == "written":
            text = tr("Статус: {0}, записан в устройство").format(on_off)
            color = "#66BB6A"
        elif state == "differs":
            text = tr("Статус: {0}, ОТЛИЧАЕТСЯ от устройства").format(on_off)
            color = "#EF5350"
        elif state == "pc":
            text = tr("Статус: вкл, исполняется приложением")
            color = "#FFA726"
        elif state == "enabled":
            text = tr("Статус: включён в устройстве")
            color = "#66BB6A"
        elif state == "disabled":
            text = tr("Статус: выключен")
            color = "#9E9E9E"
        else:
            text = tr("Статус: не читался")
            color = "#9E9E9E"
        label.setText(text)
        label.setStyleSheet(f"color: {color};")

    def _apply_device_trigger(
        self, index: int, group: List[Dict[str, Any]]
    ) -> None:
        values = group[0]
        self._applying_device_state = True
        try:
            self._apply_device_group(index, group)
        finally:
            self._applying_device_state = False
        self._set_trigger_status(index, "enabled" if values["enabled"] else "disabled")

    def _apply_device_group(
        self, index: int, group: List[Dict[str, Any]]
    ) -> None:
        block = self._blocks[index]
        recv = block["recv"]
        values = group[0]
        block["group"].setChecked(bool(values["enabled"]))
        recv["channel"].setCurrentIndex(min(values["rx_channel"], 2))
        recv["bit"].setCurrentIndex(int(values["rx_extended"]))
        recv["id"].setText(int_to_hex(values["rx_id"], 8 if values["rx_extended"] else 3))
        recv["dlc"].setValue(max(1, min(8, values["rx_dlc"] or 8)))
        rx_data_mask = bytes(values.get("rx_data_mask", b"\xff" * 8))
        for i, (edit, value) in enumerate(zip(recv["data"], values["rx_data"])):
            # Байт с нулевой маской — wildcard: показываем «X», а не «00».
            edit.setText("X" if i < len(rx_data_mask) and rx_data_mask[i] == 0 else f"{value:02X}")
        recv["rtr"].setChecked(values.get("rx_rtr", 0) == 1)
        self._set_data_enabled(
            recv["data"], 0 if recv["rtr"].isChecked() else recv["dlc"].value()
        )

        cache = block["cache"]
        cache_enabled = bool(values.get("cache_enabled", 0))
        if cache_enabled:
            # Группа записей → строки кэша. Развёртка group_seq та же,
            # что у фреймов ответа: записи строки (seq 0..127) дают
            # строку, фрагменты (0x80|f) добавляют свой count.
            # Инвертированный диапазон (from>to) показываем как «X».
            row_records: List[Dict[str, Any]] = []
            row_counts: List[int] = []
            for record in group:
                if record.get("group_seq", 0) & GROUP_SEQ_FRAGMENT and row_counts:
                    row_counts[-1] += max(1, record.get("tx_count") or 1)
                else:
                    row_records.append(record)
                    row_counts.append(max(1, record.get("tx_count") or 1))
            cache_rows = []
            for record in row_records:
                src_from = bytes(record.get("src_from", b"\x00" * 8))
                src_to = bytes(record.get("src_to", b"\xff" * 8))
                cache_rows.append({
                    "channel": min(record.get("src_channel", 0), 2),
                    "bit": int(record.get("src_extended", 0)),
                    "id": int_to_hex(
                        record.get("src_id", 0),
                        8 if record.get("src_extended") else 3,
                    ),
                    "dlc": max(1, min(8, record.get("src_dlc", 0) or 8)),
                    "from": " ".join(
                        "X" if src_from[i] > src_to[i] else f"{src_from[i]:02X}"
                        for i in range(8)
                    ),
                    "to": " ".join(
                        "X" if src_from[i] > src_to[i] else f"{src_to[i]:02X}"
                        for i in range(8)
                    ),
                    "tx_channel": min(record["tx_channel"], 2),
                    "delay_before_send": record["delay_ms"],
                    "delay_between": record.get("tx_interval_ms", 0),
                    "count": 1,
                    "next_delay": 0,
                })
            for row_index, count in enumerate(row_counts):
                cache_rows[row_index]["count"] = count
            for row_index in range(1, len(row_records)):
                prev = row_records[row_index - 1]
                prev_end = (
                    prev["delay_ms"]
                    + (row_counts[row_index - 1] - 1)
                    * prev.get("tx_interval_ms", 0)
                )
                gap = max(0, row_records[row_index]["delay_ms"] - prev_end)
                cache_rows[row_index - 1]["next_delay"] = min(gap, 9999)
                cache_rows[row_index]["delay_before_send"] = max(0, gap - 9999)
            self._set_cache_rows(cache, cache_rows)
        else:
            # Группа записей → строки «Фреймы ответа». Записи начала
            # строки (group_seq 0..127) дают фрейм; фрагменты (0x80|f)
            # добавляют свой count к текущей строке (count>255 пишется
            # кусками по 255). Абсолютные задержки записей раскладываем
            # обратно: пауза между строками → next_delay предыдущей,
            # остаток сверх спина (9999 мс) — в скрытый
            # delay_before_send следующей строки, тайминг сохраняется.
            row_records: List[Dict[str, Any]] = []
            row_counts: List[int] = []
            for record in group:
                if record.get("group_seq", 0) & GROUP_SEQ_FRAGMENT and row_counts:
                    row_counts[-1] += max(1, record.get("tx_count") or 1)
                else:
                    row_records.append(record)
                    row_counts.append(max(1, record.get("tx_count") or 1))
            responses = []
            for record in row_records:
                responses.append({
                    "channel": min(record["tx_channel"], 2),
                    "bit": int(record["tx_extended"]),
                    "id": int_to_hex(
                        record["tx_id"], 8 if record["tx_extended"] else 3
                    ),
                    "dlc": min(8, record["tx_dlc"]),
                    "data": " ".join(f"{b:02X}" for b in record["tx_data"]),
                    "rtr": int(record.get("tx_rtr", 0)),
                    "delay_before_send": record["delay_ms"],
                    "delay_between": record.get("tx_interval_ms", 0),
                    "count": 1,
                    "next_delay": 0,
                })
            for row_index, count in enumerate(row_counts):
                responses[row_index]["count"] = count
            # Паузу между строками раскладываем в next_delay предыдущей
            # строки; остаток сверх спина (9999 мс) — в скрытый
            # delay_before_send следующей, тайминг сохраняется точно.
            for row_index in range(1, len(row_records)):
                prev = row_records[row_index - 1]
                prev_end = (
                    prev["delay_ms"]
                    + (row_counts[row_index - 1] - 1)
                    * prev.get("tx_interval_ms", 0)
                )
                gap = max(0, row_records[row_index]["delay_ms"] - prev_end)
                responses[row_index - 1]["next_delay"] = min(gap, 9999)
                responses[row_index]["delay_before_send"] = max(0, gap - 9999)
            self._set_response_rows(block["response"], responses)
        cache["cache_check"].setChecked(cache_enabled)
        self._on_cache_active_changed(index, Qt.CheckState.Checked.value if cache_enabled else Qt.CheckState.Unchecked.value)

    @staticmethod
    def _config_trigger_expandable(trigger: Dict[str, Any]) -> bool:
        """True, если триггер разворачивается в записи МК (одну или
        группу). Развёртке мешает только переполнение расписания —
        суммарная задержка >65 с или >127 строк ответа; такой триггер
        исполняет приложение, в МК пишется томбстоун."""
        if trigger.get("cache"):
            # Строки кэша разворачиваются в записи по тому же
            # расписанию, что и фреймы ответа.
            cache_rows = trigger.get("cache_rows")
            if isinstance(cache_rows, list):
                filled_rows = [
                    r for r in cache_rows
                    if isinstance(r, dict) and hex_to_int(str(r.get("id", ""))) is not None
                ]
                if not filled_rows:
                    return True
                return expand_schedule(filled_rows) is not None
            return True
        filled = [
            r for r in trigger.get("responses") or []
            if isinstance(r, dict) and hex_to_int(str(r.get("id", ""))) is not None
        ]
        if not filled:
            return True
        return expand_schedule(filled) is not None

    @staticmethod
    def _config_trigger_device_representable(trigger: Dict[str, Any]) -> bool:
        """То же правило, что у _is_device_representable, но для записи
        конфигурации (dict) — нужно валидации до сборки виджетов и
        восстановлению PC-only записей при вычитке."""
        if trigger.get("cache"):
            # Кэш-режим целиком поддержан прошивкой (формат v2).
            return True
        filled = [
            r for r in trigger.get("responses") or []
            if isinstance(r, dict) and hex_to_int(str(r.get("id", ""))) is not None
        ]
        if len(filled) > 1:
            return False
        if filled:
            row = filled[0]
            if int(row.get("count") or 1) > 255 or int(row.get("next_delay") or 0) > 0:
                return False
        return True

    def _is_device_representable(self, block: Dict[str, Any]) -> bool:
        """True, если триггер целиком выразим ОДНОЙ записью trigger_t.

        Многофреймовые триггеры сюда не попадают, но на МК всё равно
        пишутся — развёрнутыми в группу записей (_project_block_records).
        Проверка нужна только heal'у: одиночная запись enabled=0 с
        многофреймовым триггером в конфиге — это томбстоун старого
        формата (устройство хранит заглушку, отвечает приложение).
        """
        return self._config_trigger_device_representable(
            self._collect_block_config(block)
        )

    def _project_block_records(self, index: int) -> Optional[List[Dict[str, Any]]]:
        """Раскладывает блок триггера на записи trigger_t для устройства.

        Многофреймовый ответ (несколько строк «Фреймы ответа», пауза
        перед следующим фреймом, count>255) не влезает в одну запись —
        разворачивается в группу записей с общим условием приёма:
        у каждой свой фрейм ответа и абсолютная задержка первой
        отправки, повторы делает прошивка. Записи группы связаны байтом
        group_seq, по которому вычитка собирает блок обратно.

        Возвращает список значений записей ([] — блок пустой) или None,
        если триггер не разворачивается (задержка >65 c, >127 строк) —
        такой исполняет приложение, в МК пишется томбстоун enabled=0.
        """
        values = self._device_trigger_values(index)
        if self._is_empty_trigger(values):
            return []
        block = self._blocks[index]
        if block["cache"]["cache_check"].isChecked():
            # Каждая строка кэша — отдельная запись с собственным
            # src-фильтром и своим слотом кэша в прошивке. Строки
            # связаны group_seq — как у многофреймового ответа.
            cache_rows = [
                self._collect_cache_row(row)
                for row in block["cache"]["rows"]
                if self._parse_id(row["id"].text()) is not None
            ]
            if not cache_rows:
                return [values]
            schedule = expand_schedule(cache_rows)
            if schedule is None:
                return None
            return [
                self._cache_row_record(index, cache_rows[row_index], delay, count, interval, seq)
                for row_index, delay, count, interval, seq in schedule
            ]
        rows = [
            row for row in block["response"]["rows"]
            if self._parse_id(row["id"].text()) is not None
        ]
        if not rows:
            return [values]
        schedule = expand_schedule([
            {
                "delay_before_send": row["delay_before_send"].value(),
                "delay_between": row["delay_between"].value(),
                "count": row["count"].value(),
                "next_delay": row["next_delay"].value(),
            }
            for row in rows
        ])
        if schedule is None:
            return None
        records = []
        for row_index, delay, count, interval, seq in schedule:
            row = rows[row_index]
            record = dict(values)
            record.update({
                "tx_channel": row["channel"].currentIndex(),
                "tx_extended": row["bit"].currentIndex(),
                "tx_id": self._parse_id(row["id"].text()) or 0,
                "tx_dlc": row["dlc"].value(),
                "tx_data": bytes(
                    (v or 0) & 0xFF for v in self._parse_data(row["data"])
                ),
                "tx_rtr": int(row["rtr"].isChecked()),
                "delay_ms": delay,
                "tx_count": count,
                "tx_interval_ms": interval,
                "group_seq": seq,
            })
            records.append(record)
        return records

    def _cache_row_record(
        self,
        index: int,
        cache_row: Dict[str, Any],
        delay: int,
        count: int,
        interval: int,
        seq: int,
    ) -> Dict[str, Any]:
        """Запись trigger_t одной строки кэша: её src-фильтр и канал
        отправки + абсолютный тайминг из расписания expand_schedule."""
        record = self._device_trigger_values(index, cache_row)
        record.update({
            "delay_ms": delay,
            "tx_count": count,
            "tx_interval_ms": interval,
            "group_seq": seq,
        })
        return record

    @staticmethod
    def _is_empty_trigger(values: Dict[str, Any]) -> bool:
        """True, если запись устройства «заводская» — не была настроена."""
        return (
            not values["enabled"]
            and values["rx_id"] == 0
            and values["tx_id"] == 0
            and values.get("src_id", 0) == 0
            and not values.get("cache_enabled", 0)
            and not values.get("rx_rtr", 0)
            and not any(values["rx_data"])
            and not any(values["tx_data"])
        )

    def _clear_block(self, index: int) -> None:
        """Полностью очищает блок (пустой слот устройства — не оставляем
        в полях старые данные из кэша конфигурации)."""
        block = self._blocks[index]
        block["group"].setChecked(False)
        block["cache"]["cache_check"].setChecked(False)
        self._on_cache_active_changed(index, Qt.CheckState.Unchecked.value)
        self._set_row(block["recv"], {}, "recv")
        self._set_response_rows(block["response"], [])
        self._set_cache(block["cache"], {})
        self._set_trigger_status(index, "disabled")

    def _ensure_blocks(self, count: int) -> None:
        """Создаёт блоки до count включительно (для синхронизации/конфига)."""
        while len(self._blocks) < min(count, TRIGGER_COUNT):
            self._add_trigger_block()

    def _read_device_triggers(self) -> List[Dict[str, Any]]:
        """Читает упакованный список триггеров устройства до ответа 0x01
        («за границей списка») — на пустом устройстве вернёт пустой
        список. Пустые записи (старшая прошивка с фиксированными слотами)
        пропускаются, блоки под них не создаются."""
        records: List[Dict[str, Any]] = []
        # Один сеанс на все чтения: reader останавливается один раз,
        # иначе stop/start QThread на каждый запрос растягивал вычитку
        # до десятков секунд.
        with self._serial_manager.control_session():
            for index in range(TRIGGER_COUNT):
                # Вычитка блокирует UI-поток — прокачиваем события между
                # записями, чтобы оверлей «Загрузка настроек» пульсировал,
                # а не выглядел зависшим.
                self.progress_updated.emit(
                    min(100, int((index + 1) * 100 / TRIGGER_COUNT)),
                    tr("Чтение триггеров {0}/{1}").format(index + 1, TRIGGER_COUNT),
                )
                QApplication.processEvents()
                try:
                    payload = self._serial_manager.request_control(CMD_TRIGGER_READ, bytes((index,)))
                    values = unpack_trigger(payload)
                except RuntimeError as exc:
                    if "0x01" in str(exc):
                        break  # конец списка устройства
                    logger.warning("Чтение триггера %d отклонено: %s", index, exc)
                    continue  # ошибка записи — не рвём синхронизацию
                except TimeoutError:
                    # Устройство молчит — дальше читать бессмысленно и
                    # долго (до 70 таймаутов). Пробрасываем наружу, иначе
                    # при сбое связи показывали «триггеров нет», хотя в
                    # МК они есть и исполняются.
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Триггер %d пропущен (битая запись): %s", index, exc)
                    continue
                if self._is_empty_trigger(values):
                    continue
                records.append(values)
        return records

    def sync_from_device(self, force: bool = False) -> bool:
        """Вычитывает триггеры из устройства (вызывается при подключении).

        Блоков ровно столько, сколько записей в устройстве — пустое
        устройство показывает ноль блоков. Блоки, исполняемые МК,
        помечаются device_managed.

        Возвращает True, если содержимое устройства применено к UI,
        False — когда на экране уже есть конфигурация, отличающаяся от
        устройства (загруженный файл, кэш ПК): молча её не затираем,
        иначе автовычитка при каждом подключении перезаписывала бы
        открытую работу оператора. force=True (Ctrl+R) — явный запрос
        на замену, проверка не нужна."""
        records = self._read_device_triggers()
        # Полевая диагностика: по логу видно, что РЕАЛЬНО лежит во Flash —
        # «мнимые» триггеры оказывались либо честно вычитанным содержимым
        # МК, либо сидом из локального config.json.
        logger.info(
            "Вычитка триггеров устройства: %d%s",
            len(records),
            records and " — " + ", ".join(
                f"0x{r.get('rx_id', 0):X}→0x{r.get('tx_id', 0):X}"
                for r in records[:15]
            ) or "",
        )
        if not force and self._blocks and not self._ui_matches_device(records):
            logger.info(
                "Вычитка: устройство (%d записей) отличается от открытой "
                "конфигурации — автозамена пропущена",
                len(records),
            )
            return False

        self._applying_device_state = True
        try:
            groups = self._group_device_records(records)
            self._ensure_blocks(len(groups))
            while len(self._blocks) > len(groups):
                self._remove_block_at(len(self._blocks) - 1)
            for index, group in enumerate(groups):
                self._apply_device_trigger(index, group)
                # Всё, что вычитано из устройства, устройство и
                # исполняет (многофреймовые группы — тоже: они хранятся
                # развёрнутыми). Томбстоуны старых записей поправит heal.
                self._device_managed[index] = True
            # Записи вычитаны из устройства — это подтверждённое
            # состояние, подвески исполнения здесь нет.
            self._pc_suspended = [False] * len(self._blocks)
            self._heal_pc_only_triggers(groups)
            # Индикатор памяти обновляем, а вот config.json НЕ пишем:
            # автовычитка зеркалила состояние МК в локальный файл, и
            # фантомная запись из МК сидила в UI при следующем запуске —
            # «Сохранить» прошивало её обратно. Файл теперь меняется
            # только явными действиями оператора (Сохранить/удаление).
            self._memory_indicator.show_trigger_usage(
                count_configured_triggers(self._collect_config())
            )
        finally:
            self._applying_device_state = False
        return True

    @staticmethod
    def _group_device_records(
        records: List[Dict[str, Any]],
    ) -> List[List[Dict[str, Any]]]:
        """Склеивает записи устройства в группы одного триггера.

        Многофреймовый триггер пишется несколькими записями подряд,
        связанными байтом group_seq: 0 — базовая запись (строка ответа
        0), 1..127 — новая строка ответа, 0x80|f — фрагмент счётчика
        текущей строки. Запись с seq>0 без базовой перед ней считается
        отдельным триггером (защита от чужих/повреждённых данных).
        """
        groups: List[List[Dict[str, Any]]] = []
        for record in records:
            if record.get("group_seq", 0) == 0 or not groups:
                groups.append([record])
            else:
                groups[-1].append(record)
        return groups

    def _heal_pc_only_triggers(self, groups: List[List[Dict[str, Any]]]) -> None:
        """Реанимирует томбстоуны триггеров, исполняемых приложением.

        Триггер, не разворачивающийся в записи trigger_t (суммарная
        задержка >65 с, >127 строк), пишется в МК с enabled=0 — МК его
        не исполняет, это делает приложение. Без восстановления вычитка
        снимала галку с блока: после переподключения ПК переставал
        исполнять живой триггер, а блок выглядел чужим («закрыл
        приложение — устройство перестало работать, в списке непонятные
        записи»). Томбстоун хранит только первый фрейм ответа — блок
        восстанавливаем из config.json, сохранённого при последней
        записи. Развёрнутые группы (несколько записей) томбстоунами не
        являются — их определение полностью во Flash.
        """
        config_triggers = [
            t for t in self._config.get("triggers", []) or []
            if isinstance(t, dict) and not self._config_trigger_is_empty(t)
        ]
        used: Set[int] = set()
        for index, group in enumerate(groups):
            if index >= len(self._blocks) or len(group) != 1:
                continue
            values = group[0]
            if values["enabled"]:
                continue
            cfg_index = self._find_config_trigger(values, config_triggers, used)
            if cfg_index is None or not config_triggers[cfg_index].get("active"):
                continue
            self._apply_config_trigger(index, config_triggers[cfg_index])
            if self._is_device_representable(self._blocks[index]):
                # Влезает в trigger_t — значит enabled=0 это выбор
                # оператора (галка снята при записи), а не томбстоун.
                # Не воскрешаем: выключенный триггер обязан молчать.
                self._apply_device_trigger(index, group)
                continue
            used.add(cfg_index)
            self._device_managed[index] = False
            self._set_trigger_status(index, "pc")
            logger.info(
                "Вычитка: триггер %d (0x%X→0x%X) исполняется приложением — "
                "блок восстановлен из конфигурации",
                index + 1, values["rx_id"], values["tx_id"],
            )

    def _find_config_trigger(
        self,
        values: Dict[str, Any],
        config_triggers: List[Dict[str, Any]],
        used: Set[int],
    ) -> Optional[int]:
        """Индекс записи config.json, из которой сделана запись МК:
        совпадают ID приёма, режим кэша и первый фрейм ответа (запись МК
        хранит только его)."""
        for i, trigger in enumerate(config_triggers):
            if i in used:
                continue
            if (hex_to_int(str(trigger.get("recv_id", ""))) or 0) != values["rx_id"]:
                continue
            if bool(trigger.get("cache")) != bool(values.get("cache_enabled")):
                continue
            if trigger.get("cache"):
                # Первая строка кэша (cache_rows) или легаси-поле cache_id.
                cache_rows = trigger.get("cache_rows")
                first_id = (
                    cache_rows[0].get("id", "")
                    if isinstance(cache_rows, list) and cache_rows
                    else trigger.get("cache_id", "")
                )
                if (hex_to_int(str(first_id)) or 0) != values.get("src_id", 0):
                    continue
            else:
                first_tx = 0
                for row in trigger.get("responses") or []:
                    if isinstance(row, dict):
                        parsed = hex_to_int(str(row.get("id", "")))
                        if parsed is not None:
                            first_tx = parsed
                            break
                if first_tx != values["tx_id"]:
                    continue
            return i
        return None

    def _ui_matches_device(self, records: List[Dict[str, Any]]) -> bool:
        """Сравнивает UI с устройством через сериализованные записи —
        не зависит от порядка блоков и представления полей. Блоки
        проецируются тем же развёртыванием, что и при записи, поэтому
        многофреймовый триггер сравнивается со своей группой записей."""
        local: List[bytes] = []
        for index, block in enumerate(self._blocks):
            try:
                projected = self._project_block_records(index)
                if projected is None:
                    # PC-only блок: на устройстве его томбстоун —
                    # проецируем так же, как при записи.
                    values = self._device_trigger_values(index)
                    values["enabled"] = 0
                    projected = [values]
                for values in projected:
                    if not self._is_empty_trigger(values):
                        local.append(pack_trigger(values))
            except Exception:  # noqa: BLE001
                continue
        try:
            remote = sorted(pack_trigger(r) for r in records)
        except Exception:  # noqa: BLE001
            return False
        return sorted(local) == remote

    def write_to_device(self) -> int:
        """Записывает текущие триггеры в устройство. Возвращает число
        изменённых записей. Вызывается кнопкой «Сохранить» окна настроек.

        После COMMIT записанные слоты перечитываются и сравниваются:
        если прошивка не зафиксировала запись во Flash (стирание/запись
        оборвались), оператор получает ошибку сразу, а не после
        выключения питания.
        """
        errors, warnings = self._validate_config(self._collect_config())
        if errors:
            QMessageBox.warning(
                self,
                tr("Проверка триггеров"),
                tr("Запись отменена — исправьте:\n\n• {0}").format(
                    "\n• ".join(errors)
                ),
            )
            raise TriggerValidationAborted("; ".join(errors))
        if warnings:
            answer = QMessageBox.question(
                self,
                tr("Проверка триггеров"),
                tr("Предупреждения перед записью:\n\n• {0}\n\nВсё равно записать в устройство?").format(
                    "\n• ".join(warnings)
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                raise TriggerValidationAborted("; ".join(warnings))
        changed = 0
        staged: List[Tuple[int, bytes]] = []
        default_payload = pack_trigger({})
        try:
            with self._serial_manager.control_session():
                changed = self._write_to_device_locked(default_payload, staged)
        except Exception:
            # Запись прервана на полпути (таймаут, отказ COMMIT, обрыв
            # порта): состояние Flash неизвестно. Снимаем флаги
            # «исполняется на МК» — приложение берёт триггеры на себя:
            # возможный дубль ответа виден на шине, а молчащий триггер —
            # невидимый сбой (в поле именно так «пропадали» записи).
            self._device_managed = [False] * len(self._blocks)
            raise
        # «Сохранить» подтвердило конфигурацию: неуправляемые МК блоки
        # (сложные ответы, не влезающие в trigger_t) исполняются
        # приложением, подвеска загрузки файла снимается.
        self._pc_suspended = [False] * len(self._blocks)
        self._save_config()
        return changed

    def _write_to_device_locked(
        self, default_payload: bytes, staged: List[Tuple[int, bytes]]
    ) -> int:
        """Тело write_to_device внутри control_session: все команды
        STAGE/READ/COMMIT идут при однократно остановленном reader'е.

        Список на устройстве упакован: пустые блоки в него не попадают
        и Flash не занимают; удалённый оператором триггер исчезает из
        области за счёт итоговой длины в COMMIT. Для старшей прошивки с
        фиксированными слотами хвост гасится пустыми записями."""
        changed = 0
        # Шкала прогресса внутри записи: чтение списка устройства 0–35%,
        # STAGE-записи 35–75%, COMMIT + проверка 75–100%. Окно настроек
        # пересчитывает это в общую шкалу сохранения.
        def _report(percent: int, text: str) -> None:
            self.progress_updated.emit(max(0, min(100, percent)), text)
            QApplication.processEvents()

        # Реальный список устройства (до ответа 0x01 «за границей»).
        device: List[bytes] = []
        for index in range(TRIGGER_COUNT):
            _report(
                int((index + 1) * 35 / TRIGGER_COUNT),
                tr("Чтение триггеров {0}/{1}").format(index + 1, TRIGGER_COUNT),
            )
            try:
                device.append(
                    self._serial_manager.request_control(CMD_TRIGGER_READ, bytes((index,)))
                )
            except RuntimeError as exc:
                if "0x01" in str(exc):
                    break
                raise

        # Автобэкап состояния МК перед перезаписью: если запись пошла
        # не так или оператор пожалел — прежний список триггеров лежит
        # в backups/device_*.json (последние 10).
        try:
            records = []
            for payload in device:
                try:
                    rec = unpack_trigger(payload)
                except ValueError:
                    continue
                records.append(
                    {
                        k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                        for k, v in rec.items()
                    }
                )
            self._config.backup_snapshot(
                "device_before_save",
                {
                    "device_name": self._config.get("device_name", ""),
                    "device_serial": self._config.get("device_serial", "")
                    or self._config.get("serial_number", ""),
                    "triggers": records,
                },
                prefix="device",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить бэкап триггеров устройства: %s", exc)

        # Целевой список: только настроенные (непустые) блоки.
        # Многофреймовый триггер разворачивается в группу записей —
        # устройство исполняет его само, приложение не нужно.
        target: List[Tuple[int, bytes]] = []
        for block_index in range(len(self._blocks)):
            records = self._project_block_records(block_index)
            if records is None:
                # Не разворачивается в записи МК (суммарная задержка
                # >65 с, >127 строк) — исполняет приложение; в МК пишем
                # томбстоун enabled=0, чтобы устройство не дублировало
                # первый фрейм поверх ответов ПК.
                values = self._device_trigger_values(block_index)
                values["enabled"] = 0
                records = [values]
                self._device_managed[block_index] = False
            else:
                self._device_managed[block_index] = True
            for values in records:
                target.append((block_index, pack_trigger(values)))
        if len(target) > TRIGGER_MAX_SLOTS:
            raise RuntimeError(
                tr("Триггеры разворачиваются в {0} записей, на устройстве "
                   "места для {1} — уменьшите число фреймов ответа")
                .format(len(target), TRIGGER_MAX_SLOTS)
            )

        # Полевая диагностика: в логе видно, что именно уходит во Flash —
        # «мнимые» записи после стирания всегда оказывались реальной
        # записью, инициированной «Сохранить» с сидом из кэша ПК. Флаг
        # enabled показываем явно — запись с enabled=0 хранится в МК, но
        # никогда не срабатывает (томбстоун PC-only или выключенный
        # триггер), по логу это раньше не отличить.
        target_desc = []
        for block_index, payload in target[:15]:
            record = unpack_trigger(payload)
            target_desc.append(
                "0x{0:X}→0x{1:X}{2}{3}".format(
                    record.get("rx_id", 0),
                    record.get("tx_id", 0),
                    "" if record.get("enabled") else " (выкл)",
                    "" if self._device_managed[block_index] else " (исполняет ПК)",
                )
            )
        logger.info(
            "Запись триггеров: на устройстве %d, целевых %d — %s",
            len(device), len(target),
            ", ".join(target_desc) or "пусто",
        )

        # Фаза записи: STAGE по целевому списку + гашение хвоста.
        write_total = max(len(target) + max(0, len(device) - len(target)), 1)
        write_step = 0
        for new_index, (block_index, local_payload) in enumerate(target):
            _report(
                35 + int(write_step * 40 / write_total),
                tr("Запись триггеров {0}/{1}").format(write_step + 1, write_total),
            )
            write_step += 1
            # «Вкл, исполняется приложением» — блок активен, но в trigger_t
            # не влез: в МК записан томбстоун enabled=0, отвечает ПК.
            pc_only = (
                self._blocks[block_index]["group"].isChecked()
                and not self._device_managed[block_index]
            )
            remote_payload = device[new_index] if new_index < len(device) else None
            if remote_payload == local_payload:
                self._set_trigger_status(block_index, "pc" if pc_only else "synced")
            else:
                self._serial_manager.request_control(
                    CMD_TRIGGER_STAGE, bytes((new_index,)) + local_payload
                )
                staged.append((new_index, local_payload))
                self._set_trigger_status(block_index, "pc" if pc_only else "written")
                changed += 1

        # Хвост устройства за пределами целевого списка: для старой
        # прошивки гасим слоты пустыми записями, новая отбросит их по
        # длине списка в COMMIT.
        for i in range(len(target), len(device)):
            _report(
                35 + int(write_step * 40 / write_total),
                tr("Запись триггеров {0}/{1}").format(write_step + 1, write_total),
            )
            write_step += 1
            try:
                remote_empty = self._is_empty_trigger(unpack_trigger(device[i]))
            except (ValueError, RuntimeError):
                remote_empty = False
            if not remote_empty:
                self._serial_manager.request_control(
                    CMD_TRIGGER_STAGE, bytes((i,)) + default_payload
                )
                changed += 1

        if changed or len(device) != len(target):
            _report(75, tr("Фиксация во Flash"))
            commit_payload = _commit_payload(
                len(target), self._serial_manager.device_protocol_version()
            )
            self._serial_manager.request_control(CMD_TRIGGER_COMMIT, commit_payload)
            logger.info(
                "COMMIT триггеров: записей=%d, изменено=%d, на устройстве было=%d",
                len(target), changed, len(device),
            )
            verify_total = max(len(staged), 1)
            for v_idx, (index, expected) in enumerate(staged):
                _report(
                    75 + int((v_idx + 1) * 25 / verify_total),
                    tr("Проверка записи {0}/{1}").format(v_idx + 1, verify_total),
                )
                actual = self._serial_manager.request_control(CMD_TRIGGER_READ, bytes((index,)))
                if actual != expected:
                    block_index = target[index][0] if index < len(target) else index
                    if block_index < len(self._blocks):
                        self._set_trigger_status(block_index, "differs")
                    raise RuntimeError(
                        tr("Триггер {0}: проверка записи во Flash не пройдена").format(index + 1)
                    )
        return changed

    def clear_device_managed(self) -> None:
        """Сбрасывает флаги исполнения на МК (при отключении порта)."""
        self._device_managed = [False] * len(self._blocks)

    def _on_trigger_toggled_by_block(self, block: Dict[str, Any], enabled: bool) -> None:
        try:
            index = self._blocks.index(block)
        except ValueError:
            return
        self._on_trigger_toggled(index, enabled)

    def _on_trigger_toggled(self, index: int, enabled: bool) -> None:
        # Запись в устройство теперь происходит только по «Сохранить» —
        # здесь просто помечаем блок изменённым и обновляем статус.
        if self._applying_device_state or index >= len(self._blocks):
            return
        self._device_managed[index] = False
        self._set_trigger_status(index, "enabled" if enabled else "disabled")

    def _on_cache_active_changed(self, index: int, state: int) -> None:
        enabled = state == Qt.CheckState.Checked.value
        block = self._blocks[index]
        self._set_cache_enabled(block, enabled)

    def _set_widget_opacity(self, widget: QWidget, opacity: float) -> None:
        """Устанавливает прозрачность виджета."""
        effect = widget.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(opacity)
        else:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(opacity)
            widget.setGraphicsEffect(effect)

    def _set_cache_enabled(self, block: Dict[str, Any], enabled: bool) -> None:
        """Включает либо блок ответа, либо блок кэша в зависимости от чекбокса."""
        response_group = block["response"]["group"]
        cache_fields = block["cache"]["fields_widget"]

        response_group.setEnabled(not enabled)
        cache_fields.setEnabled(enabled)

        self._set_widget_opacity(response_group, 0.5 if enabled else 1.0)
        self._set_widget_opacity(cache_fields, 1.0 if enabled else 0.5)

    def retranslate_ui(self) -> None:
        """Обновляет статические строки вкладки триггеров."""
        for i, block in enumerate(self._blocks):
            block["group"].setTitle(tr("Триггер {0}").format(i + 1))
            block["cache"]["cache_check"].setText(tr("Автоматическая запись DATA в Кэш"))
            block["response"]["group"].setTitle(tr("Ответ"))
            block["response"]["header_label"].setText(tr("Фреймы ответа"))
            block["response"]["add_button"].setToolTip(tr("Добавить фрейм"))
            for row in block["response"]["rows"]:
                row["remove_button"].setToolTip(tr("Удалить фрейм"))
                row["delay_before_label"].setText(tr("Пауза перед отправкой"))
                row["delay_between_label"].setText(tr("Пауза между пакетами"))
            block["cache"]["group"].setTitle(tr("Кэш"))
            block["cache"]["header_label"].setText(tr("Строки кэша"))
            block["cache"]["add_button"].setToolTip(tr("Добавить строку кэша"))
            for row in block["cache"]["rows"]:
                row["src_label"].setText(tr("Откуда читаем"))
                row["dst_label"].setText(tr("Куда отправляем"))
                row["delay_before_label"].setText(tr("Пауза перед отправкой"))
                row["delay_between_label"].setText(tr("Пауза между пакетами"))
                row["count_label"].setText(tr("Кол-во отправок"))
                row["remove_button"].setToolTip(tr("Удалить строку"))

    def _parse_id(self, text: str) -> Optional[int]:
        return hex_to_int(text.strip())

    def _parse_data(self, edits: List[QLineEdit]) -> List[Optional[int]]:
        result: List[Optional[int]] = []
        for edit in edits:
            text = edit.text().strip()
            if "X" in text.upper():
                # Wildcard: байт не сравнивается (маска 0).
                result.append(None)
            elif text:
                val = hex_to_int(text)
                result.append(val if val is not None else 0)
            else:
                result.append(None)
        return result

    @staticmethod
    def _parse_data_triple(
        edits: List[QLineEdit],
    ) -> Tuple[List[Optional[int]], List[bool]]:
        """Разбор полей данных с wildcard: (значения, флаги «X»).
        Пустое поле — None без флага wild (в диапазонах это граница по
        умолчанию), «X» — None с флагом (байт игнорируется/обнуляется)."""
        values: List[Optional[int]] = []
        wild: List[bool] = []
        for edit in edits:
            text = edit.text().strip()
            if "X" in text.upper():
                values.append(None)
                wild.append(True)
            elif text:
                values.append(hex_to_int(text) or 0)
                wild.append(False)
            else:
                values.append(None)
                wild.append(False)
        return values, wild

    def _build_internal_triggers(self) -> List[Dict[str, Any]]:
        triggers = []
        for i, block in enumerate(self._blocks):
            if not block["group"].isChecked():
                continue
            recv_id = self._parse_id(block["recv"]["id"].text())
            if recv_id is None:
                continue
            triggers.append({
                "index": i,
                "recv_id": recv_id,
                "recv_rtr": int(block["recv"]["rtr"].isChecked()),
                "recv_data": self._parse_data(block["recv"]["data"]),
                "recv_channel": block["recv"]["channel"].currentIndex(),
                "cache": block["cache"]["cache_check"].isChecked(),
                "responses": self._collect_responses(block["response"]["rows"]),
                "cache_rows": self._collect_cache(block["cache"]),
                "device_managed": self._device_managed[i],
                "pc_suspended": self._pc_suspended[i],
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
                "dlc": row["dlc"].value(),
                "data": self._parse_data(row["data"]),
                "rtr": int(row["rtr"].isChecked()),
                "delay_before_send": row["delay_before_send"].value(),
                "delay_between": row["delay_between"].value(),
                "count": row["count"].value(),
                "next_delay": row["next_delay"].value(),
            })
        return result

    def _collect_cache(self, cache: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Строки кэша блока с заданным ID — по одному фильтру источника
        на строку. Строки без ID пропускаются: в прошивку они не
        проецируются, и PC-расписание не должно тратить на них паузы."""
        rows = [self._collect_cache_row(row) for row in cache["rows"]]
        return [row for row in rows if row["id"] is not None]

    def _collect_cache_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Собирает строку кэша в dict: фильтр источника (канал/бит/ID/
        DLC + побайтовый диапазон От/До с флагами wildcard «X») и
        параметры отправки закэшированного кадра."""
        data_from, wild_from = self._parse_data_triple(row["from_data"])
        data_to, wild_to = self._parse_data_triple(row["to_data"])
        # «X» хотя бы в одном из полей пары — байт игнорируется целиком.
        wild = [a or b for a, b in zip(wild_from, wild_to)]
        return {
            "id": self._parse_id(row["id"].text()),
            "channel": row["channel"].currentIndex(),
            "tx_channel": row["tx_channel"].currentIndex(),
            "extended": row["bit"].currentIndex(),
            "data_from": data_from,
            "data_to": data_to,
            "wild": wild,
            "dlc": row["dlc"].value(),
            "delay_before_send": row["delay_before_send"].value(),
            "delay_between": row["delay_between"].value(),
            "count": row["count"].value(),
            "next_delay": row["next_delay"].value(),
        }

    def _load_config(self) -> None:
        triggers = self._config.get("triggers", [])
        # Подключённое устройство — источник истины: его вычитка придёт по
        # sync_from_device. Сид из кэша ПК (config.json) показывал бы
        # УСТАРЕВШИЕ триггеры как текущие — а «Сохранить» до вычитки
        # прошивало их обратно в МК (полевой баг: фантомные 5 триггеров,
        # воскресавшие во Flash после полного стирания через DFU).
        if self._serial_manager.is_open() and not self._config.get("emulation", False):
            if triggers:
                logger.info(
                    "Сид %d триггеров из config.json пропущен — устройство подключено, ждём вычитку",
                    len(triggers),
                )
            return
        self.set_config(triggers if isinstance(triggers, list) else [])

    def _save_config(self) -> None:
        triggers = self._collect_config()
        # Фильтры могли измениться — старый кэш им уже не отвечает.
        self._pc_cache.clear()
        self._config.set("triggers", triggers)
        self._memory_indicator.show_trigger_usage(count_configured_triggers(triggers))

    def _collect_config(self) -> List[Dict[str, Any]]:
        return [self._collect_block_config(block) for block in self._blocks]

    @staticmethod
    def _collect_block_config(block: Dict[str, Any]) -> Dict[str, Any]:
        responses = []
        for row in block["response"]["rows"]:
            responses.append({
                "channel": row["channel"].currentIndex(),
                "bit": row["bit"].currentIndex(),
                "id": row["id"].text(),
                "dlc": row["dlc"].value(),
                "rtr": int(row["rtr"].isChecked()),
                # Пустой байт пишется явным «00» — позиции не съезжают.
                "data": " ".join(
                    e.text() or "00" for e in row["data"][: row["dlc"].value()]
                ),
                "delay_before_send": row["delay_before_send"].value(),
                "delay_between": row["delay_between"].value(),
                "count": row["count"].value(),
                "next_delay": row["next_delay"].value(),
            })
        cache = block["cache"]
        cache_rows = []
        for row in cache["rows"]:
            cache_rows.append({
                "channel": row["channel"].currentIndex(),
                "bit": row["bit"].currentIndex(),
                "id": row["id"].text(),
                "dlc": row["dlc"].value(),
                "tx_channel": row["tx_channel"].currentIndex(),
                # Границы диапазона пишутся явно: пустой От = «00»,
                # пустой До = «FF», wildcard «X» сохраняется как есть.
                "from": " ".join(
                    e.text() or "00" for e in row["from_data"][: row["dlc"].value()]
                ),
                "to": " ".join(
                    e.text() or "FF" for e in row["to_data"][: row["dlc"].value()]
                ),
                "delay_before_send": row["delay_before_send"].value(),
                "delay_between": row["delay_between"].value(),
                "count": row["count"].value(),
                "next_delay": row["next_delay"].value(),
            })
        config = {
            "active": block["group"].isChecked(),
            "cache": block["cache"]["cache_check"].isChecked(),
            "recv_channel": block["recv"]["channel"].currentIndex(),
            "recv_bit": block["recv"]["bit"].currentIndex(),
            "recv_id": block["recv"]["id"].text(),
            "recv_dlc": block["recv"]["dlc"].value(),
            "recv_rtr": int(block["recv"]["rtr"].isChecked()),
            # Пустой байт приёма — wildcard, пишется явным «X».
            "recv_data": " ".join(
                e.text() or "X"
                for e in block["recv"]["data"][: block["recv"]["dlc"].value()]
            ),
            "responses": responses,
            "cache_rows": cache_rows,
        }
        # Дублируем первую строку кэша в легаси-поля — старые версии
        # приложения читают одиночный кэш именно оттуда.
        first = cache_rows[0] if cache_rows else {}
        config.update({
            "cache_channel": first.get("channel", 0),
            "cache_bit": first.get("bit", 0),
            "cache_id": first.get("id", ""),
            "cache_dlc": first.get("dlc", 8),
            "cache_tx_channel": first.get("tx_channel", 0),
            "cache_from_data": first.get("from", ""),
            "cache_to_data": first.get("to", ""),
            "cache_delay_before_send": first.get("delay_before_send", 0),
            "cache_delay_between": first.get("delay_between", 0),
            "cache_count": first.get("count", 1),
        })
        return config

    @staticmethod
    def _config_trigger_is_empty(trigger: Dict[str, Any]) -> bool:
        """Запись конфигурации не несёт настройки: ни условия, ни ответов,
        ни кэша. Старые файлы сохраняли запись на КАЖДЫЙ блок, включая
        пустые — без фильтра они воскресали «пустыми триггерами» поверх
        реальных (полевой баг: «5 блоков, 4 пустых»)."""
        if str(trigger.get("recv_id", "")).strip():
            return False
        if str(trigger.get("cache_id", "")).strip():
            return False
        cache_rows = trigger.get("cache_rows")
        if isinstance(cache_rows, list):
            for row in cache_rows:
                if isinstance(row, dict) and str(row.get("id", "")).strip():
                    return False
        responses = trigger.get("responses", [])
        if isinstance(responses, list):
            for row in responses:
                if isinstance(row, dict) and str(row.get("id", "")).strip():
                    return False
        return True

    @staticmethod
    def _check_id_range(text: str, extended: int, label: str) -> List[str]:
        """ID обязан парситься как hex и лежать в диапазоне кадра:
        11-бит ≤ 0x7FF, 29-бит ≤ 0x1FFFFFFF."""
        value = hex_to_int(text)
        if value is None:
            return [f"{label}: " + tr("ID «{0}» не является HEX").format(text)]
        limit = 0x1FFFFFFF if extended else 0x7FF
        if value > limit:
            kind = tr("расширенного") if extended else tr("стандартного")
            return [
                f"{label}: "
                + tr("ID 0x{0} вне диапазона {1} кадра").format(
                    f"{value:X}", kind
                )
            ]
        return []

    @staticmethod
    def _check_data(text: str, dlc: int, label: str) -> Tuple[List[str], List[str]]:
        """Данные ≤ 8 байт (ошибка — прошивка отрежет молча); длина ≠ DLC
        (предупреждение — шина пошлёт DLC байт, не заявленное число)."""
        errors: List[str] = []
        warnings: List[str] = []
        # Токен «X» — валидный wildcard-байт и тоже считается байтом.
        tokens = [t for t in text.split() if t.strip()]
        data_len = sum(
            1 for t in tokens if hex_to_int(t) is not None or "X" in t.upper()
        )
        if data_len > 8:
            errors.append(
                f"{label}: " + tr("данных {0} байт — максимум 8").format(data_len)
            )
        elif data_len and data_len != dlc:
            warnings.append(
                f"{label}: " + tr("данных {0} байт, а DLC = {1}").format(data_len, dlc)
            )
        if dlc > 8:
            errors.append(f"{label}: " + tr("DLC {0} — максимум 8").format(dlc))
        return errors, warnings

    def _validate_config(
        self, triggers: List[Dict[str, Any]]
    ) -> Tuple[List[str], List[str]]:
        """Проверка триггеров перед записью. Не меняет полей: ошибки
        блокируют запись, предупреждения требуют подтверждения — необычные,
        но допустимые конфигурации остаются возможными."""
        errors: List[str] = []
        warnings: List[str] = []
        rx_seen: Dict[Tuple[int, str], List[int]] = {}
        rx_map: Dict[Tuple[int, str], int] = {}
        tx_map: Dict[Tuple[int, str], int] = {}
        for i, trigger in enumerate(triggers, 1):
            if self._config_trigger_is_empty(trigger):
                continue
            label = tr("Триггер {0}").format(i)
            if not trigger.get("active", True):
                # Полевой баг: заполненный, но выключенный триггер уходил
                # в МК с enabled=0 — хранился, вычитывался, но никогда не
                # срабатывал («устройство перестало работать»).
                warnings.append(
                    f"{label}: "
                    + tr("заполнен, но выключен — будет записан неактивным и исполняться не будет")
                )
            elif not self._config_trigger_expandable(trigger):
                # Триггер не разворачивается в записи МК (задержка >65 с)
                # — на шине отвечает приложение и только пока оно открыто.
                warnings.append(
                    f"{label}: "
                    + tr("исполняется приложением (не помещается в МК) — при закрытии программы перестанет работать")
                )
            rx_text = str(trigger.get("recv_id", "")).strip()
            rx_channel = int(trigger.get("recv_channel", 0))
            if trigger.get("cache"):
                # Строки кэша (новый формат) или легаси-одиночный кэш.
                cache_rows = trigger.get("cache_rows")
                if not isinstance(cache_rows, list):
                    cache_rows = [{
                        "id": trigger.get("cache_id", ""),
                        "bit": trigger.get("cache_bit", 0),
                    }]
                filled = [
                    r for r in cache_rows
                    if isinstance(r, dict) and str(r.get("id", "")).strip()
                ]
                if not filled:
                    warnings.append(
                        f"{label}: " + tr("кэш включён без ID кэшированного кадра")
                    )
                for j, crow in enumerate(filled, 1):
                    errors += self._check_id_range(
                        str(crow["id"]).strip(),
                        int(crow.get("bit", 0)),
                        f"{label} {tr('кэш')} {j}",
                    )
            elif rx_text:
                errors += self._check_id_range(
                    rx_text, int(trigger.get("recv_bit", 0)), label
                )
                key = (rx_channel, rx_text.lower())
                rx_seen.setdefault(key, []).append(i)
                rx_map[key] = i
            else:
                warnings.append(
                    f"{label}: " + tr("нет ID приёма — условие не задано")
                )
            e, w = self._check_data(
                str(trigger.get("recv_data", "")),
                int(trigger.get("recv_dlc", 0)),
                f"{label} {tr('приём')}",
            )
            errors += e
            warnings += w
            for j, response in enumerate(trigger.get("responses", []), 1):
                rlabel = f"{label} {tr('ответ {0}').format(j)}"
                tx_text = str(response.get("id", "")).strip()
                tx_channel = int(response.get("channel", 0))
                if not tx_text:
                    warnings.append(f"{rlabel}: " + tr("нет ID ответа"))
                else:
                    errors += self._check_id_range(
                        tx_text, int(response.get("bit", 0)), rlabel
                    )
                    tx_map.setdefault((tx_channel, tx_text.lower()), i)
                    if (
                        rx_text
                        and tx_channel == rx_channel
                        and tx_text.lower() == rx_text.lower()
                    ):
                        warnings.append(
                            f"{rlabel}: "
                            + tr("ответ повторяет ID приёма — самопетля на одном канале")
                        )
                e, w = self._check_data(
                    str(response.get("data", "")),
                    int(response.get("dlc", 0)),
                    rlabel,
                )
                errors += e
                warnings += w
                if response.get("rtr") and str(response.get("data", "")).strip():
                    warnings.append(
                        f"{rlabel}: " + tr("RTR-ответ с данными — шина их проигнорирует")
                    )
        for (channel, can_id), indices in rx_seen.items():
            if len(indices) > 1:
                warnings.append(
                    tr("Дублирующийся ID приёма 0x{0} на CAN{1} — триггеры {2}").format(
                        can_id.upper(), channel + 1, ", ".join(map(str, indices))
                    )
                )
        pingpong_seen: Set[frozenset] = set()
        for key, rx_i in rx_map.items():
            if key in tx_map and tx_map[key] != rx_i:
                # A принимает то, что шлёт B — опасно только при
                # встречной связи: проверяем, отвечает ли rx_i в приём
                # tx_map[key].
                other = tx_map[key]
                other_rx = next(
                    (k for k, v in rx_map.items() if v == other), None
                )
                if other_rx and other_rx in tx_map and tx_map[other_rx] == rx_i:
                    pair = frozenset((rx_i, other))
                    if pair in pingpong_seen:
                        continue
                    pingpong_seen.add(pair)
                    warnings.append(
                        tr("Пинг-понг: триггер {0} отвечает в приём триггера {1} и наоборот — сработает ограничение эха").format(
                            rx_i, other
                        )
                    )
        return errors, warnings

    def set_config(
        self, triggers: List[Dict[str, Any]], suspend_execution: bool = False
    ) -> None:
        """Загружает конфигурацию триггеров из списка.

        Данные пришли из файла — состояние устройства неизвестно, поэтому
        флаги исполнения на МК сбрасываются (пока пользователь не нажмёт
        «Сохранить» и не запишет их в устройство, ответы при подключении
        обрабатывает приложение).

        suspend_execution=True («Загрузить конфигурацию»): приложение не
        исполняет эти триггеры и само — файл только заполняет поля, на шине
        устройство молчит до «Сохранить».
        """
        dropped = len(triggers)
        triggers = [t for t in triggers
                    if isinstance(t, dict) and not self._config_trigger_is_empty(t)]
        dropped -= len(triggers)
        logger.info(
            "Загружено триггеров в редактор: %d (активных %d)%s%s",
            len(triggers),
            sum(1 for t in triggers if t.get("active", True)),
            f", отброшено пустых {dropped}" if dropped else "",
            ", исполнение подвешено до «Сохранить»" if suspend_execution else "",
        )
        self._applying_device_state = True
        try:
            # Блоков ровно столько, сколько триггеров в конфигурации —
            # лишние удаляются (пустой список = ноль блоков).
            while len(self._blocks) > len(triggers):
                self._remove_block_at(len(self._blocks) - 1)
            self._ensure_blocks(len(triggers))
            self._device_managed = [False] * len(self._blocks)
            self._pc_suspended = [suspend_execution] * len(self._blocks)
            for i in range(len(self._blocks)):
                self._apply_config_trigger(i, triggers[i])
        finally:
            self._applying_device_state = False
        self._update_add_trigger_button()
        self._memory_indicator.show_trigger_usage(count_configured_triggers(triggers))

    def _apply_config_trigger(self, index: int, trigger: Dict[str, Any]) -> None:
        """Применяет запись конфигурации к блоку — все поля + активность.

        `active` по умолчанию True: старые файлы/выгрузки ключа не имели,
        и «триггер есть» надо трактовать как «включён» — иначе вся
        загруженная конфигурация уходила в МК с enabled=0 (полевой баг:
        записанные триггеры «не работали» — лежали во Flash мёртвыми).
        """
        block = self._blocks[index]
        self._applying_device_state = True
        try:
            block["group"].setChecked(bool(trigger.get("active", True)))
            cache_active = bool(trigger.get("cache", False))
            block["cache"]["cache_check"].setChecked(cache_active)
            self._on_cache_active_changed(index, Qt.CheckState.Checked.value if cache_active else Qt.CheckState.Unchecked.value)

            self._set_row(block["recv"], trigger, "recv")
            recv_rtr = int(trigger.get("recv_rtr", 0))
            block["recv"]["rtr"].setChecked(bool(recv_rtr))
            self._set_data_enabled(
                block["recv"]["data"], 0 if recv_rtr else block["recv"]["dlc"].value()
            )
            self._set_response_rows(block["response"], trigger.get("responses", []))
            self._set_cache(block["cache"], trigger)
        finally:
            self._applying_device_state = False

    @staticmethod
    def _set_data_fields(
        edits: List[QLineEdit], text: str, allow_x: bool = False
    ) -> None:
        """Заполняет байтовые поля из строки токенов позиционно: токен i
        попадает в поле i («X» — только в wildcard-полях, пустой/битый
        токен очищает поле)."""
        tokens = str(text).split()
        for i, edit in enumerate(edits):
            token = tokens[i] if i < len(tokens) else ""
            if allow_x and "X" in token.upper():
                edit.setText("X")
            else:
                value = hex_to_int(token)
                edit.setText(f"{value:02X}" if value is not None else "")

    def _set_row(self, row: Dict[str, Any], data: Dict[str, Any], prefix: str) -> None:
        row["channel"].setCurrentIndex(int(data.get(f"{prefix}_channel", 0)))
        row["bit"].setCurrentIndex(int(data.get(f"{prefix}_bit", 0)))
        row["id"].setText(str(data.get(f"{prefix}_id", "")))
        row["dlc"].setValue(int(data.get(f"{prefix}_dlc", 8)))
        self._set_data_fields(row["data"], str(data.get(f"{prefix}_data", "")), allow_x=True)
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
        self._set_data_fields(response["data"], str(data.get("data", "")))
        response["rtr"].setChecked(bool(data.get("rtr", 0)))
        self._set_data_enabled(response["data"], 0 if response["rtr"].isChecked() else response["dlc"].value())
        response["delay_before_send"].setValue(int(data.get("delay_before_send", 0)))
        response["delay_between"].setValue(int(data.get("delay_between", data.get("delay", 0))))
        response["count"].setValue(int(data.get("count", 1)))
        response["next_delay"].setValue(int(data.get("next_delay", 0)))

    def _set_cache(self, cache: Dict[str, Any], data: Dict[str, Any]) -> None:
        rows = data.get("cache_rows")
        if not isinstance(rows, list):
            # Легаси-конфиг: одиночный кэш в плоских полях cache_*.
            rows = [{
                "channel": data.get("cache_channel", 0),
                "bit": data.get("cache_bit", 0),
                "id": data.get("cache_id", ""),
                "dlc": data.get("cache_dlc", 8),
                "tx_channel": data.get("cache_tx_channel", 0),
                "from": data.get("cache_from_data", ""),
                "to": data.get("cache_to_data", ""),
                "delay_before_send": data.get("cache_delay_before_send", 0),
                "delay_between": data.get("cache_delay_between", data.get("cache_delay", 0)),
                "count": data.get("cache_count", 1),
                "next_delay": 0,
            }]
        self._set_cache_rows(cache, rows)

    def _set_cache_rows(self, cache: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
        """Заполняет динамический список строк кэша — как _set_response_rows."""
        widget_rows = cache["rows"]
        for r, row in enumerate(widget_rows):
            data = rows[r] if r < len(rows) else {}
            self._set_cache_row(row, data)
        while len(widget_rows) > len(rows) and len(widget_rows) > 1:
            self._remove_cache_row(cache, widget_rows[-1])
        for r in range(len(widget_rows), len(rows)):
            self._add_cache_row(cache, self._font)
            self._set_cache_row(cache["rows"][-1], rows[r])

    def _set_cache_row(self, row: Dict[str, Any], data: Dict[str, Any]) -> None:
        row["channel"].setCurrentIndex(int(data.get("channel", 0)))
        row["tx_channel"].setCurrentIndex(int(data.get("tx_channel", 0)))
        row["bit"].setCurrentIndex(int(data.get("bit", 0)))
        row["id"].setText(str(data.get("id", "")))
        row["dlc"].setValue(max(1, min(8, int(data.get("dlc", 8)))))
        self._set_data_fields(row["from_data"], str(data.get("from", "")), allow_x=True)
        self._set_data_fields(row["to_data"], str(data.get("to", "")), allow_x=True)
        row["delay_before_send"].setValue(int(data.get("delay_before_send", 0)))
        row["delay_between"].setValue(int(data.get("delay_between", data.get("delay", 0))))
        row["count"].setValue(max(1, int(data.get("count", 1))))
        row["next_delay"].setValue(int(data.get("next_delay", 0)))
        self._set_data_enabled(row["from_data"], row["dlc"].value())
        self._set_data_enabled(row["to_data"], row["dlc"].value())

    def _data_from_response(self, response: Dict[str, Any]) -> bytes:
        """Формирует байты данных фрейма ответа с учётом DLC."""
        dlc = int(response["dlc"])
        parsed = response["data"]
        data = bytearray(dlc)
        for i in range(dlc):
            if i < len(parsed) and parsed[i] is not None:
                data[i] = parsed[i] & 0xFF
        return bytes(data)

    def _send_frame(
        self,
        can_id: int,
        data: bytes,
        channel_index: int,
        rtr: bool = False,
        dlc: Optional[int] = None,
    ) -> None:
        """Отправляет один CAN-кадр в указанный канал."""
        if not self._serial_manager.is_open():
            return
        if channel_index == 0:
            self._serial_manager.send_data(pack_can_frame(1, can_id, data, rtr=rtr, dlc=dlc))
        elif channel_index == 1:
            self._serial_manager.send_data(pack_can_frame(2, can_id, data, rtr=rtr, dlc=dlc))
        else:
            self._serial_manager.send_data(pack_can_frame(1, can_id, data, rtr=rtr, dlc=dlc))
            self._serial_manager.send_data(pack_can_frame(2, can_id, data, rtr=rtr, dlc=dlc))

    def process_frame(self, frame: Dict[str, Any]) -> None:
        frame_id = int(frame["id"])
        frame_channel = int(frame["channel"])
        data = bytes(frame["data"])

        triggers = self._build_internal_triggers()
        for trigger in triggers:
            # Кэш пополняется у всех триггеров независимо от «Приём» —
            # кадр может быть источником данных для одного триггера и
            # условием срабатывания для другого.
            self._update_cache(
                trigger, frame_id, frame_channel, data, bool(frame.get("extended"))
            )
        for trigger in triggers:
            if trigger.get("device_managed"):
                # Триггер записан во Flash и исполняется самим МК —
                # не дублируем ответ со стороны приложения.
                continue
            if trigger.get("pc_suspended"):
                # Блок пришёл из «Загрузить конфигурацию» и ещё не
                # записан в устройство кнопкой «Сохранить» — на шине
                # молчим, файл лишь заполняет поля.
                continue
            if not self._match_condition(
                trigger, frame_id, frame_channel, data, bool(frame.get("rtr"))
            ):
                continue
            if trigger["cache"]:
                self._send_cached_frames(trigger)
            else:
                self._send_responses(trigger)

    def _match_condition(
        self,
        trigger: Dict[str, Any],
        frame_id: int,
        frame_channel: int,
        data: bytes,
        frame_rtr: bool = False,
    ) -> bool:
        if trigger["recv_id"] != frame_id:
            return False
        if trigger.get("recv_rtr") and not frame_rtr:
            # Режим «только RTR-запрос»: обычные кадры не срабатывают.
            return False
        recv_channel = int(trigger["recv_channel"])
        if recv_channel != 2 and recv_channel + 1 != frame_channel:
            return False
        if trigger.get("recv_rtr"):
            return True  # RTR-кадр не несёт Data — сравнивать нечего
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
            cumulative += response["delay_before_send"]
            data = self._data_from_response(response)
            count = max(1, response["count"])
            can_id = response["id"]
            channel = response["channel"]
            rtr = bool(response.get("rtr"))
            dlc = int(response["dlc"])
            for j in range(count):
                if cumulative == 0:
                    self._send_frame(can_id, data, channel, rtr=rtr, dlc=dlc)
                else:
                    QTimer.singleShot(
                        cumulative,
                        lambda cid=can_id, d=data, ch=channel, r=rtr, dl=dlc:
                            self._send_frame(cid, d, ch, rtr=r, dlc=dl),
                    )
                if j < count - 1:
                    cumulative += response["delay_between"]
            if i < len(trigger["responses"]) - 1:
                cumulative += response["next_delay"]

    def _send_cached_frames(self, trigger: Dict[str, Any]) -> None:
        """Отправляет кэшированные кадры по строкам — как _send_responses:
        у каждой строки своя пауза перед отправкой, пауза между пакетами
        и количество; между строками — «Пауза перед следующим».
        Строка без заполненного кэша пропускается (как s_cache_valid=0)."""
        cumulative = 0
        rows = trigger["cache_rows"]
        for i, row in enumerate(rows):
            cumulative += row["delay_before_send"]
            cached = self._pc_cache.get((trigger["index"], i))
            count = max(1, row["count"])
            channel = row["tx_channel"]
            if cached is not None:
                can_id = cached["id"]
                data = cached["data"]
                for j in range(count):
                    if cumulative == 0:
                        self._send_frame(can_id, data, channel)
                    else:
                        QTimer.singleShot(
                            cumulative,
                            lambda cid=can_id, d=data, ch=channel: self._send_frame(cid, d, ch),
                        )
                    if j < count - 1:
                        cumulative += row["delay_between"]
            else:
                # Кэш пуст — отправок нет, но позиция строки в расписании
                # сохраняется: следующая строка ждёт её слот как обычно.
                cumulative += (count - 1) * row["delay_between"]
            if i < len(rows) - 1:
                cumulative += row["next_delay"]

    def _update_cache(
        self,
        trigger: Dict[str, Any],
        frame_id: int,
        frame_channel: int,
        data: bytes,
        extended: bool = False,
    ) -> None:
        """Сохраняет кадр в кэши всех подошедших строк — один входящий
        кадр может пополнить несколько строк кэша. Wildcard-байты («X»)
        при захвате обнуляются — в ответе на их месте уйдёт 0x00."""
        if not trigger["cache"]:
            return
        for row_index, row in enumerate(trigger["cache_rows"]):
            if row["id"] is None or row["id"] != frame_id:
                continue
            if bool(row.get("extended", 0)) != extended:
                continue
            src_channel = int(row["channel"])
            if src_channel != 2 and src_channel + 1 != frame_channel:
                continue
            dlc = row["dlc"]
            wild = row["wild"]
            if not self._cache_src_matches(data, row["data_from"], row["data_to"], wild, dlc):
                continue
            # Кэшируется кадр целиком (как s_cache в прошивке); нулями
            # становятся только wildcard-байты внутри диапазона src_dlc.
            captured = bytearray(data[:8])
            for j in range(min(dlc, len(captured))):
                if j < len(wild) and wild[j]:
                    captured[j] = 0
            self._pc_cache[(trigger["index"], row_index)] = {
                "id": frame_id,
                "data": bytes(captured),
                "channel": frame_channel,
            }

    @staticmethod
    def _cache_src_matches(
        data: bytes,
        data_from: List[Optional[int]],
        data_to: List[Optional[int]],
        wild: List[bool],
        dlc: int,
    ) -> bool:
        """Побайтовая проверка [От, До]: байт i подходит, если
        От[i] ≤ data[i] ≤ До[i]; «X» (wild) — байт игнорируется.
        Порт src_matches() прошивки: там «X» кодируется from>to."""
        for i in range(dlc):
            if i < len(wild) and wild[i]:
                continue
            lo = data_from[i] if i < len(data_from) and data_from[i] is not None else 0x00
            hi = data_to[i] if i < len(data_to) and data_to[i] is not None else 0xFF
            b = data[i] if i < len(data) else 0
            if b < lo or b > hi:
                return False
        return True

    def create_trigger_from_packet(self, packet: Dict[str, object]) -> None:
        """Создаёт первый триггер из пакета мониторинга."""
        if not self._blocks:
            if self._add_trigger_block() is None:
                return
        block = self._blocks[0]
        block["group"].setChecked(True)
        can_id = int(packet["id"])
        block["recv"]["id"].setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        block["recv"]["bit"].setCurrentIndex(1 if can_id > 0x7FF else 0)
        if "dlc" in packet:
            block["recv"]["dlc"].setValue(packet["dlc"])
        bytes_data = bytes(packet["data"])
        for d, edit in enumerate(block["recv"]["data"]):
            edit.setText(f"{bytes_data[d]:02X}" if d < len(bytes_data) else "")
        logger.info("Триггер создан из пакета ID=0x%X", can_id)
