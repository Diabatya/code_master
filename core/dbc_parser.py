"""Парсер DBC-файлов для приложения «Код Мастер».

Выбор библиотеки:
- carbus-lib (импортируется как carbus_async) предоставляет низкоуровневый CAN/ISO-TP/UDS
  транспорт, но не умеет парсить DBC-файлы и не имеет декодирования физических значений.
- cantools умеет загружать DBC (Vector CANdb++), декодировать кадры в физические
  значения, работать с factor/offset/единицами и перечислениями.

Поэтому для парсинга DBC и декодирования CAN-данных выбрана библиотека cantools.
carbus-lib оставлена в зависимостях для будущей работы с CAN-устройствами.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cantools

from models.logger import get_logger
from models.utils import hex_to_int

logger = get_logger(__name__)

DBC_FILE_RE = re.compile(r"\.dbc$", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Legacy-совместимый парсер (регулярными выражениями)
# -----------------------------------------------------------------------------

def _parse_int(text: str) -> Optional[int]:
    value = hex_to_int(text)
    if value is not None:
        return value
    try:
        return int(text)
    except ValueError:
        return None


def _parse_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_signal(line: str) -> Optional[Dict[str, object]]:
    match = re.match(
        r"\s*SG_\s+(\S+)\s*:\s*(\d+)\|(\d+)@(\d+)([+-])\s*\(([^,]+),([^)]+)\)\s*\[([^|]+)\|([^]]+)\]\s*\"([^\"]*)\"\s*.*",
        line,
    )
    if not match:
        return None
    name, start, length, byte_order, signed, factor, offset, min_val, max_val, unit = match.groups()
    return {
        "name": name,
        "start": int(start),
        "length": int(length),
        "byte_order": int(byte_order),
        "signed": signed == "-",
        "factor": _parse_float(factor),
        "offset": _parse_float(offset),
        "min": _parse_float(min_val),
        "max": _parse_float(max_val),
        "unit": unit,
        "values": {},
    }


def _extract_value_enum(line: str) -> Optional[Tuple[int, str, Dict[int, str]]]:
    match = re.match(r"VAL_\s+(\d+)\s+(\S+)\s+(.+);", line)
    if not match:
        return None
    can_id, signal_name, rest = match.groups()
    values: Dict[int, str] = {}
    tokens = rest.split()
    for i in range(0, len(tokens) - 1, 2):
        try:
            key = int(tokens[i])
            value = tokens[i + 1].strip('"')
            values[key] = value
        except (ValueError, IndexError):
            continue
    return (_parse_int(can_id) or int(can_id, 0), signal_name, values)


def parse_dbc(filepath: str) -> Dict[int, Dict[str, object]]:
    """Читает DBC-файл через регулярные выражения и возвращает {can_id: {...}}.

    Оставлен для совместимости с компонентами, которые ожидают старый формат.
    """
    path = Path(filepath)
    if not path.exists():
        logger.error("DBC файл не найден: %s", filepath)
        return {}

    messages: Dict[int, Dict[str, object]] = {}
    current_id: Optional[int] = None
    value_enums: List[Tuple[int, str, Dict[int, str]]] = []

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка чтения DBC %s: %s", filepath, exc)
        return {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("BO_"):
            match = re.match(r"BO_\s+(\d+)\s+(\S+)\s*:\s*(\d+)\s+\S+", line)
            if match:
                can_id = _parse_int(match.group(1))
                name = match.group(2)
                dlc = int(match.group(3))
                if can_id is not None:
                    messages[can_id] = {"name": name, "dlc": dlc, "signals": []}
                    current_id = can_id
            continue

        if line.startswith("SG_"):
            signal = _parse_signal(line)
            if signal is not None and current_id is not None:
                messages[current_id]["signals"].append(signal)
            continue

        if line.startswith("VAL_"):
            enum = _extract_value_enum(line)
            if enum:
                value_enums.append(enum)
            continue

    for can_id, signal_name, values in value_enums:
        if can_id not in messages:
            continue
        for signal in messages[can_id]["signals"]:
            if signal["name"] == signal_name:
                signal["values"] = values
                break

    logger.info("DBC (legacy) загружен: %d сообщений", len(messages))
    return messages


# -----------------------------------------------------------------------------
# Основной API на основе cantools
# -----------------------------------------------------------------------------

def load_dbc(filepath: str) -> Optional[cantools.db.Database]:
    """Загружает DBC-файл через cantools.

    Args:
        filepath: Путь к .dbc файлу.

    Returns:
        Объект Database из cantools или None при ошибке.
    """
    path = Path(filepath)
    if not path.exists():
        logger.error("DBC файл не найден: %s", filepath)
        return None
    try:
        db = cantools.database.load_file(str(path))
        logger.info("DBC (cantools) загружен: %d сообщений", len(db.messages))
        return db
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка загрузки DBC через cantools: %s", exc)
        return None


def decode_frame(db: cantools.db.Database, can_id: int, data: bytes) -> Optional[Dict[str, Any]]:
    """Декодирует сырые CAN-данные в физические значения сигналов.

    Args:
        db: Загруженная база cantools.
        can_id: CAN ID кадра.
        data: Байты кадра (до 8 байт).

    Returns:
        Словарь {signal_name: physical_value} или None, если сообщение не найдено.
    """
    if db is None:
        return None
    try:
        message = db.get_message_by_frame_id(can_id)
    except KeyError:
        return None
    try:
        decoded = message.decode(data)
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка декодирования кадра 0x%X: %s", can_id, exc)
        return None

    result: Dict[str, Any] = {}
    for signal_name, raw_value in decoded.items():
        signal = message.get_signal_by_name(signal_name)
        if signal is None:
            result[signal_name] = raw_value
            continue
        phys = signal.scale * raw_value + signal.offset
        unit = signal.unit or ""
        result[signal_name] = {"value": phys, "unit": unit, "raw": raw_value}
    return result


def dbc_to_dict(db: cantools.db.Database) -> Dict[int, Dict[str, object]]:
    """Преобразует базу cantools в формат словаря, совместимого с DBCManager."""
    result: Dict[int, Dict[str, object]] = {}
    if db is None:
        return result
    for message in db.messages:
        result[message.frame_id] = {
            "name": message.name,
            "dlc": message.length,
            "signals": [
                {
                    "name": s.name,
                    "start": s.start,
                    "length": s.length,
                    "byte_order": 1 if s.byte_order == "little_endian" else 0,
                    "signed": bool(s.is_signed),
                    "factor": float(s.scale),
                    "offset": float(s.offset),
                    "min": float(s.minimum) if s.minimum is not None else 0.0,
                    "max": float(s.maximum) if s.maximum is not None else 0.0,
                    "unit": s.unit or "",
                    "values": dict(s.choices) if s.choices else {},
                }
                for s in message.signals
            ],
        }
    return result
