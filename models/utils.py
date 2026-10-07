"""Вспомогательные функции для приложения «Код Мастер»."""

import re
import shutil
import sys
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir


# Кириллические буквы на тех же физических клавишах раскладки ЙЦУКЕН,
# что и латинские hex-цифры A-F на QWERTY (А↔F, В↔D, С↔C, Е↔T не hex,
# только буквы A-F нужны): оператор случайно печатает кириллицей
# (не переключил раскладку) — вместо молчаливого отказа подменяем на
# нужный латинский символ (отчёт мастера, относится ко всем HEX-полям
# ID/DATA/таблиц привязки во всём приложении).
CYRILLIC_HEX_MAP = {
    "а": "f", "А": "F",
    "в": "d", "В": "D",
    "с": "c", "С": "C",
    "и": "b", "И": "B",
    "у": "e", "У": "E",
    "ф": "a", "Ф": "A",
    # «Ч» стоит на клавише «X» — wildcard-байт в полях DATA, где он
    # разрешён (отчёт мастера: подмена «Ч»→«X» во всех байтовых полях).
    "ч": "x", "Ч": "X",
}


def translate_cyrillic_hex(text: str) -> str:
    """Подменяет кириллические буквы на латинские HEX-цифры по
    раскладке клавиатуры (см. CYRILLIC_HEX_MAP)."""
    if not text:
        return text
    return "".join(CYRILLIC_HEX_MAP.get(ch, ch) for ch in text)


# Полная подмена русской раскладки JCUKEN на латинскую QWERTY —
# для полей, где нужны не только HEX-буквы, но и знаки/цифровые
# символы («ю» под «.», «б» под «,» и т.п.) — оператор, забывший
# переключить раскладку, вводит латиницу, а не мусор (отчёт мастера).
CYRILLIC_LAYOUT_MAP = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t",
    "н": "y", "г": "u", "ш": "i", "щ": "o", "з": "p",
    "х": "[", "ъ": "]",
    "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g",
    "р": "h", "о": "j", "л": "k", "д": "l", "ж": ";", "э": "'",
    "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b",
    "т": "n", "ь": "m", "б": ",", "ю": ".",
    "ё": "`",
    "Й": "Q", "Ц": "W", "У": "E", "К": "R", "Е": "T",
    "Н": "Y", "Г": "U", "Ш": "I", "Щ": "O", "З": "P",
    "Х": "{", "Ъ": "}",
    "Ф": "A", "Ы": "S", "В": "D", "А": "F", "П": "G",
    "Р": "H", "О": "J", "Л": "K", "Д": "L", "Ж": ":", "Э": "\"",
    "Я": "Z", "Ч": "X", "С": "C", "М": "V", "И": "B",
    "Т": "N", "Ь": "M", "Б": "<", "Ю": ">",
    "Ё": "~",
}


def translate_cyrillic_layout(text: str) -> str:
    """Подменяет кириллические символы на то, что стоит на той же
    клавише латинской раскладки (полная карта, не только HEX)."""
    if not text:
        return text
    return "".join(CYRILLIC_LAYOUT_MAP.get(ch, ch) for ch in text)


def hex_to_int(text: str) -> int | None:
    """Преобразует строку с HEX-значением в целое число.

    Args:
        text: Строка, например «1A», «0x1A», «1a» или «1 A» (пробелы
            игнорируются — вставка DATA через буфер обмена часто
            приходит с разделителями между байтами).

    Returns:
        Целое число или None, если строка пустая или некорректная.
    """
    if not text:
        return None
    cleaned = translate_cyrillic_hex(text).strip()
    cleaned = cleaned.replace(" ", "").replace("0x", "").replace("0X", "")
    if not cleaned:
        return None
    try:
        return int(cleaned, 16)
    except ValueError:
        return None


def int_to_hex(value: int, width: int = 2) -> str:
    """Форматирует целое число в HEX-строку заданной длины.

    Args:
        value: Целое число.
        width: Минимальное количество символов (по умолчанию 2).

    Returns:
        HEX-строка в верхнем регистре, например «1A».
    """
    return f"{value:0{width}X}"


def parse_data_bytes(fields: list[str]) -> list[int]:
    """Преобразует список строковых HEX-полей в список байт.

    Пустые строки игнорируются.

    Args:
        fields: Список строк с HEX-значениями байт.

    Returns:
        Список целых чисел от 0 до 255.
    """
    result: list[int] = []
    for field in fields:
        value = hex_to_int(field)
        if value is not None:
            result.append(value & 0xFF)
    return result


def format_data_bytes(data: bytes) -> list[str]:
    """Преобразует байты в список HEX-строк.

    Args:
        data: Байтовая строка длиной до 8 байт.

    Returns:
        Список строк, например ['1A', '2B', '00'].
    """
    return [int_to_hex(b) for b in data]


def hex_string_to_bytes(text: str) -> bytes:
    """Преобразует строку из HEX-символов в байты.

    Пробелы и префикс 0x игнорируются.

    Args:
        text: Строка, например «DE AD BE EF».

    Returns:
        Байтовая строка.
    """
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(cleaned) % 2 != 0:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned)


def bytes_to_hex_string(data: bytes) -> str:
    """Преобразует байты в строку HEX с пробелами.

    Args:
        data: Байтовая строка.

    Returns:
        Строка, например «DE AD BE EF».
    """
    return " ".join(f"{b:02X}" for b in data)


def parse_packet_string(text: str) -> dict[str, Any] | None:
    """Парсит строку вида ID=<hex> DLC=<n> DATA=<hex hex ...>.

    Токен «X» в DATA — wildcard-байт триггеров, в списке data он
    представлен как None (поля без поддержки wildcard пропускают его).

    Args:
        text: Строка из буфера обмена.

    Returns:
        Словарь {"id": int, "dlc": int, "data": List[Optional[int]]}
        или None.
    """
    match = re.match(
        r"ID\s*=\s*([0-9A-Fa-f]+)\s+DLC\s*=\s*(\d+)\s+DATA\s*=\s*([0-9A-Fa-fXx ]+)",
        text.strip(),
    )
    if not match:
        return None
    can_id = hex_to_int(match.group(1))
    dlc = int(match.group(2))
    data_values = match.group(3).strip().split()
    # Позиции сохраняются: «X» → None (wildcard), битый токен → None.
    data: list[int | None] = [
        None if "X" in token.upper() else hex_to_int(token)
        for token in data_values
    ]
    return {"id": can_id, "dlc": dlc, "data": data}


def get_library_root() -> Path:
    """Возвращает путь к папке библиотеки в доступном пользовательском месте.

    При запуске копирует недостающие bundled-ресурсы из папки рядом с кодом.
    """
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    source = bundle_root / "library"
    target = Path(user_data_dir("CodeMaster", appauthor=False, ensure_exists=True)) / "library"
    target.mkdir(parents=True, exist_ok=True)
    if source.exists() and source.is_dir():
        try:
            for item in source.iterdir():
                dest = target / item.name
                if item.is_dir():
                    if not dest.exists():
                        shutil.copytree(item, dest)
                    else:
                        for sub in item.iterdir():
                            sub_dest = dest / sub.name
                            if not sub_dest.exists():
                                if sub.is_dir():
                                    shutil.copytree(sub, sub_dest)
                                else:
                                    shutil.copy2(sub, dest)
                elif not dest.exists():
                    shutil.copy2(item, target)
        except (OSError, shutil.Error):
            pass
    return target
