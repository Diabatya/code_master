"""Packing/unpacking for the firmware trigger_t wire payload."""

from __future__ import annotations

import struct
from typing import Dict, Any

TRIGGER_MAGIC = 0x54524732
TRIGGER_FORMAT_VERSION = 2
TRIGGER_SIZE = 82
_TRIGGER_FORMAT = "<IBBBIIB8s8sBBIB8sH2sBBBBIB8s8sHB2sB"

# Область хранения триггеров во Flash устройства (см. firmware/PROTOCOL.md
# и Inc/trigger.h — значения обязаны совпадать): две страницы по 2 КБ
# с адреса 0x0803E000, записи по 82 байта → максимум 49 слотов.
TRIGGER_PAGE_ADDR = 0x0803E000
TRIGGER_PAGE_SIZE = 4096
TRIGGER_SLOT_SIZE = 82
TRIGGER_MAX_SLOTS = TRIGGER_PAGE_SIZE // TRIGGER_SLOT_SIZE  # 49


def trigger_usage_percent(used_slots: int) -> int:
    """Процент занятой памяти страницы триггеров для индикатора «Память»."""
    used = max(0, min(TRIGGER_MAX_SLOTS, int(used_slots))) * TRIGGER_SLOT_SIZE
    return min(100, round(used * 100 / TRIGGER_PAGE_SIZE))


def count_configured_triggers(triggers: list) -> int:
    """Сколько слотов конфигурации реально занято (для «Память»).

    Слот считается занятым, если триггер включён или в нём заполнены
    поля приёма/ответа — пустые блоки память не занимают.
    """
    count = 0
    for trigger in triggers or []:
        if not isinstance(trigger, dict):
            continue
        responses = trigger.get("responses") or []
        if (
            trigger.get("active")
            or str(trigger.get("recv_id", "")).strip()
            or str(trigger.get("cache_id", "")).strip()
            or any(str(r.get("id", "")).strip() for r in responses if isinstance(r, dict))
        ):
            count += 1
    return count


def crc8(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ 0x07) & 0xFF if value & 0x80 else (value << 1) & 0xFF
    return value


def pack_trigger(values: Dict[str, Any]) -> bytes:
    """Pack a trigger_t-compatible record and calculate firmware CRC8."""
    raw = bytearray(
        struct.pack(
            _TRIGGER_FORMAT,
            TRIGGER_MAGIC,
            int(values.get("enabled", 0)) & 0xFF,
            int(values.get("rx_channel", 0)) & 0xFF,
            int(values.get("rx_extended", 0)) & 0xFF,
            int(values.get("rx_id", 0)) & 0x1FFFFFFF,
            int(values.get("rx_id_mask", 0x7FF)) & 0x1FFFFFFF,
            int(values.get("rx_dlc", 0)) & 0xFF,
            bytes(values.get("rx_data", b""))[:8].ljust(8, b"\x00"),
            bytes(values.get("rx_data_mask", b""))[:8].ljust(8, b"\x00"),
            int(values.get("tx_channel", 0)) & 0xFF,
            int(values.get("tx_extended", 0)) & 0xFF,
            int(values.get("tx_id", 0)) & 0x1FFFFFFF,
            int(values.get("tx_dlc", 0)) & 0xFF,
            bytes(values.get("tx_data", b""))[:8].ljust(8, b"\x00"),
            int(values.get("delay_ms", 0)) & 0xFFFF,
            bytes((TRIGGER_FORMAT_VERSION, TRIGGER_SIZE)),
            int(values.get("tx_rtr", 0)) & 0xFF,
            int(values.get("cache_enabled", 0)) & 0xFF,
            int(values.get("src_channel", 0)) & 0xFF,
            int(values.get("src_extended", 0)) & 0xFF,
            int(values.get("src_id", 0)) & 0x1FFFFFFF,
            int(values.get("src_dlc", 0)) & 0xFF,
            bytes(values.get("src_from", b""))[:8].ljust(8, b"\x00"),
            bytes(values.get("src_to", b"\xff" * 8))[:8].ljust(8, b"\xff"),
            int(values.get("tx_interval_ms", 0)) & 0xFFFF,
            int(values.get("tx_count", 0)) & 0xFF,
            b"\x00\x00",
            0,
        )
    )
    raw[-1] = crc8(raw[:-1])
    return bytes(raw)


def unpack_trigger(payload: bytes) -> Dict[str, Any]:
    """Validate and unpack a trigger_t-compatible payload."""
    if len(payload) != TRIGGER_SIZE:
        raise ValueError(f"Некорректный размер trigger_t: {len(payload)}")
    values = struct.unpack(_TRIGGER_FORMAT, payload)
    if values[0] != TRIGGER_MAGIC:
        raise ValueError("Неверный magic trigger_t")
    if crc8(payload[:-1]) != payload[-1]:
        raise ValueError("Неверный CRC8 trigger_t")
    if not (
        (values[15][0] == 0 and values[15][1] == 0)
        or (values[15][0] == TRIGGER_FORMAT_VERSION and values[15][1] == TRIGGER_SIZE)
    ):
        raise ValueError("Неподдерживаемая версия trigger_t")
    return {
        "enabled": values[1],
        "rx_channel": values[2],
        "rx_extended": values[3],
        "rx_id": values[4],
        "rx_id_mask": values[5],
        "rx_dlc": values[6],
        "rx_data": values[7],
        "rx_data_mask": values[8],
        "tx_channel": values[9],
        "tx_extended": values[10],
        "tx_id": values[11],
        "tx_dlc": values[12],
        "tx_data": values[13],
        "delay_ms": values[14],
        "tx_rtr": values[16],
        "cache_enabled": values[17],
        "src_channel": values[18],
        "src_extended": values[19],
        "src_id": values[20],
        "src_dlc": values[21],
        "src_from": values[22],
        "src_to": values[23],
        "tx_interval_ms": values[24],
        "tx_count": values[25],
    }
