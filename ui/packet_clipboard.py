"""Кнопки и функции копирования/вставки пакетов ID+DLC+Data."""

import re
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QClipboard, QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QSizePolicy, QStyle, QWidget

from models.logger import get_logger
from models.translations import _ as tr
from models.utils import hex_to_int, int_to_hex

logger = get_logger(__name__)


# Регулярное выражение для парсинга строки пакета.
# Поддерживает ID=0x123, ID=123, DLC=8, DATA=11 22 0x33 и т.д.
_PACKET_RE = re.compile(
    r"ID\s*=\s*(?:0x)?([0-9A-Fa-f]+)" r"(?:.*?DLC\s*=\s*(\d+))?" r"(?:.*?DATA\s*=\s*(.*))?$",
    re.IGNORECASE | re.DOTALL,
)


def _style_clipboard_button(button: QPushButton) -> None:
    """Настраивает внешний вид маленькой кнопки буфера обмена."""
    button.setFixedSize(24, 24)
    button.setFont(QFont("Segoe UI", 8))
    button.setStyleSheet(
        "QPushButton { background-color: palette(button); color: palette(text); border: none; border-radius: 4px; }"
        "QPushButton:hover { background-color: palette(midlight); }"
        "QPushButton:pressed { background-color: palette(mid); }"
    )


def create_clipboard_buttons(
    parent: QWidget,
    id_edit: Any,
    dlc_spin: Optional[Any] = None,
    data_edits: Optional[List[Any]] = None,
    bit_combo: Optional[Any] = None,
    on_paste: Optional[Callable[[], None]] = None,
    data_edit: Optional[Any] = None,
) -> QWidget:
    """Создаёт виджет с кнопками «Копировать» и «Вставить» для пакета.

    Args:
        parent: Родительский виджет.
        id_edit: Поле ввода ID (QLineEdit).
        dlc_spin: Спиннер DLC (QSpinBox). Если None, используется длина data_edits.
        data_edits: Список полей ввода Data (QLineEdit или HexDataEdit).
        bit_combo: Опциональный комбобокс битности (11/29 бит).
        on_paste: Опциональный колбэк после вставки.
        data_edit: Опциональное одиночное поле Data (QLineEdit со строкой байт).
    """
    widget = QWidget(parent)
    layout = QHBoxLayout(widget)
    layout.setSpacing(2)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

    copy_button = QPushButton()
    copy_button.setToolTip(tr("Копировать"))
    copy_button.setIcon(copy_button.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))

    paste_button = QPushButton()
    paste_button.setToolTip(tr("Вставить"))
    paste_button.setIcon(paste_button.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))

    for button in (copy_button, paste_button):
        _style_clipboard_button(button)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.setIconSize(QSize(18, 18))

    copy_button.clicked.connect(
        lambda: _copy_packet(id_edit, dlc_spin, data_edits or [], data_edit)
    )
    paste_button.clicked.connect(
        lambda: _paste_packet(id_edit, dlc_spin, data_edits or [], bit_combo, on_paste, data_edit)
    )

    layout.addWidget(copy_button)
    layout.addWidget(paste_button)
    return widget


def _copy_packet(
    id_edit: Any,
    dlc_spin: Optional[Any],
    data_edits: List[Any],
    data_edit: Optional[Any] = None,
) -> None:
    """Формирует строку ID=0x... DLC=N DATA=... и копирует в буфер обмена."""
    can_id = hex_to_int(id_edit.text())
    id_text = int_to_hex(can_id, 3) if can_id is not None else (id_edit.text().strip() or "0x")
    if not id_text.startswith("0x"):
        id_text = "0x" + id_text

    if dlc_spin is not None:
        if hasattr(dlc_spin, "value"):
            dlc = int(dlc_spin.value())
        else:
            dlc = int(dlc_spin.text())
    elif data_edits:
        dlc = len(data_edits)
    else:
        dlc = 8

    if data_edits:
        dlc = max(0, min(dlc, len(data_edits)))
        data_parts: List[str] = []
        for i in range(dlc):
            text = data_edits[i].text().strip() if i < len(data_edits) else ""
            val = hex_to_int(text)
            if val is not None:
                data_parts.append(f"{val & 0xFF:02X}")
            else:
                data_parts.append("00")
        data_str = " ".join(data_parts)
    elif data_edit is not None:
        data_str = data_edit.text().strip()
        if not data_str:
            data_str = ""
    else:
        data_str = ""

    packet = f"ID={id_text} DLC={dlc} DATA={data_str}"
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(packet)
    logger.debug("Скопирован пакет: %s", packet)


def _paste_packet(
    id_edit: Any,
    dlc_spin: Optional[Any],
    data_edits: List[Any],
    bit_combo: Optional[Any],
    on_paste: Optional[Callable[[], None]],
    data_edit: Optional[Any] = None,
) -> None:
    """Парсит строку из буфера обмена и заполняет поля."""
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return
    text = clipboard.text().strip()
    if not text:
        return

    match = _PACKET_RE.search(text)
    if not match:
        logger.debug("Не удалось распарсить пакет из буфера: %s", text)
        return

    id_str, dlc_str, data_str = match.groups()
    can_id = hex_to_int(id_str) if id_str else None
    if can_id is None:
        return

    # Установка битности
    if bit_combo is not None:
        bit_combo.setCurrentIndex(1 if can_id > 0x7FF else 0)

    # Установка ID
    id_edit.setText(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))

    # Парсим данные
    parsed_bytes: List[int] = []
    if data_str:
        for token in data_str.split():
            token = token.strip().replace("0x", "").replace("0X", "")
            if not token:
                continue
            val = hex_to_int(token)
            if val is not None:
                parsed_bytes.append(val & 0xFF)

    # Определяем DLC
    max_dlc = len(data_edits) if data_edits else 8
    dlc = max_dlc
    if dlc_str:
        try:
            dlc = int(dlc_str)
        except ValueError:
            dlc = max_dlc

    dlc = max(1, min(dlc, max_dlc))

    # Если данных больше, чем DLC, увеличиваем DLC
    if len(parsed_bytes) > dlc and len(parsed_bytes) <= max_dlc:
        dlc = len(parsed_bytes)

    # Устанавливаем DLC (valueChanged вызовет _set_data_enabled)
    if dlc_spin is not None:
        if hasattr(dlc_spin, "setValue"):
            dlc_spin.setValue(dlc)
        else:
            dlc_spin.setText(str(dlc))

    # Заполняем Data
    if data_edits:
        for i, edit in enumerate(data_edits):
            if i < dlc and i < len(parsed_bytes):
                edit.setText(f"{parsed_bytes[i]:02X}")
            else:
                edit.setText("")
    elif data_edit is not None:
        data_tokens = [f"{parsed_bytes[i]:02X}" for i in range(dlc) if i < len(parsed_bytes)]
        data_edit.setText(" ".join(data_tokens))

    if on_paste is not None:
        on_paste()

    logger.debug("Вставлен пакет: ID=0x%X DLC=%d", can_id, dlc)


def parse_packet(text: str) -> Optional[Dict[str, Any]]:
    """Вспомогательная функция для парсинга пакета из строки.

    Возвращает словарь с ключами id, dlc, data или None.
    """
    match = _PACKET_RE.search(text)
    if not match:
        return None
    id_str, dlc_str, data_str = match.groups()
    can_id = hex_to_int(id_str) if id_str else None
    if can_id is None:
        return None
    dlc = 8
    if dlc_str:
        try:
            dlc = int(dlc_str)
        except ValueError:
            dlc = 8
    parsed_bytes: List[int] = []
    if data_str:
        for token in data_str.split():
            token = token.strip().replace("0x", "").replace("0X", "")
            if not token:
                continue
            val = hex_to_int(token)
            if val is not None:
                parsed_bytes.append(val & 0xFF)
    return {"id": can_id, "dlc": dlc, "data": parsed_bytes[:8]}
