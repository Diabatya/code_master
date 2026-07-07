"""Простой парсер DBC-файлов (Vector CANdb++) для «Код Мастер»."""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models.logger import get_logger
from models.utils import hex_to_int

logger = get_logger(__name__)

DBC_FILE_RE = re.compile(r"\.dbc$", re.IGNORECASE)


def _parse_int(text: str) -> Optional[int]:
    """Парсит целое, включая HEX-строки."""
    value = hex_to_int(text)
    if value is not None:
        return value
    try:
        return int(text)
    except ValueError:
        return None


def _parse_float(text: str) -> float:
    """Парсит float, возвращает 0.0 при ошибке."""
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_signal(line: str) -> Optional[Dict[str, object]]:
    """Парсит одну строку сигнала SG_."""
    # Пример: SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|8000] "rpm" Vector__XXX
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
        "byte_order": int(byte_order),  # 1 = little endian, 0 = big endian
        "signed": signed == "-",
        "factor": _parse_float(factor),
        "offset": _parse_float(offset),
        "min": _parse_float(min_val),
        "max": _parse_float(max_val),
        "unit": unit,
        "values": {},
    }


def _extract_value_enum(line: str) -> Optional[Tuple[int, str, str, Dict[int, str]]]:
    """Парсит строку VAL_ и возвращает (can_id, signal_name, raw_id, enum)."""
    # Пример: VAL_ 123 EngineSpeed 0 "Off" 1 "On" ;
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
    return (_parse_int(can_id) or int(can_id), signal_name, values)


def parse_dbc(filepath: str) -> Dict[int, Dict[str, object]]:
    """Читает DBC-файл и возвращает словарь сообщений.

    Args:
        filepath: Путь к .dbc файлу.

    Returns:
        Словарь {can_id: {"name": str, "dlc": int, "signals": [...]}}.
    """
    path = Path(filepath)
    if not path.exists():
        logger.error("DBC файл не найден: %s", filepath)
        return {}

    messages: Dict[int, Dict[str, object]] = {}
    current_id: Optional[int] = None

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка чтения DBC %s: %s", filepath, exc)
        return {}

    value_enums: List[Tuple[int, str, Dict[int, str]]] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("BO_"):
            # BO_ 123 MessageName: 8 Vector__XXX
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

    # Применяем перечисления к сигналам
    for can_id, signal_name, values in value_enums:
        if can_id not in messages:
            continue
        for signal in messages[can_id]["signals"]:
            if signal["name"] == signal_name:
                signal["values"] = values
                break

    logger.info("DBC загружен: %d сообщений, %d сигналов", len(messages), sum(len(m["signals"]) for m in messages.values()))
    return messages
