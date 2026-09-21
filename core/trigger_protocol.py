"""Packing/unpacking for the firmware trigger_t wire payload."""

from __future__ import annotations

import struct
from typing import Any

TRIGGER_MAGIC = 0x54524732
TRIGGER_FORMAT_VERSION = 3
TRIGGER_FORMAT_VERSION_V2 = 2
TRIGGER_SIZE = 90
TRIGGER_SIZE_V2 = 82
# Формат v3 (90 Б): к v2 добавлены rx_fire_limit(H), rx_flags(B),
# src_fire_limit(H), src_flags(B), 2 байта выравнивания — crc8 в конце.
# rx_flags/src_flags, бит 0 = MUTE_ECHO: не реагировать на TX-эхо
# (собственные отправки МК). 0 = слушать шину и эхо (поведение v2).
# *_fire_limit: «кол-во сработок до смены DATA» — N срабатываний на
# неизменной Data, затем защёлка до смены содержимого; 0 = выкл.
_TRIGGER_FORMAT_V2 = "<IBBBIIB8s8sBBIB8sH2sBBBBIB8s8sHBBBB"
_TRIGGER_FORMAT = "<IBBBIIB8s8sBBIB8sH2sBBBBIB8s8sHBBBHBHBBB B".replace(" ", "")
TRIGGER_F_MUTE_ECHO = 0x01

# Хранилище триггеров v3 (см. firmware/PROTOCOL.md и Inc/trigger.h):
# записи упакованы в суффикс пула страниц над config-страницей
# (0x0803E000..0x08040000) и привязаны к ВЕРХУ Flash — область растёт
# вниз по мере добавления, пустое устройство занимает 0 страниц.
# Заголовок "TRGH" (16 байт) в начале области позволяет прошивке найти
# хранилище сканированием — фиксированной области нет.
TRIGGER_POOL_BASE = 0x0803E000
TRIGGER_POOL_SIZE = 0x08040000 - TRIGGER_POOL_BASE  # 8192
TRIGGER_HEADER_SIZE = 16
TRIGGER_SLOT_SIZE = 90
# Лимит по RAM прошивки (70 записей), а не по пулу — см. Inc/trigger.h.
TRIGGER_MAX_SLOTS = 70


def trigger_usage_percent(used_slots: int) -> int:
    """Процент занятой памяти пула триггеров для индикатора «Память».

    Пустое устройство = 0%; каждая запись добавляет свои 82 байта плюс
    заголовок области. Потолок — пул над config-страницей (8 КБ)."""
    used = int(used_slots)
    if used <= 0:
        return 0
    used_bytes = TRIGGER_HEADER_SIZE + min(used, TRIGGER_MAX_SLOTS) * TRIGGER_SLOT_SIZE
    return min(100, round(used_bytes * 100 / TRIGGER_POOL_SIZE))


def count_configured_triggers(triggers: list) -> int:
    """Сколько слотов конфигурации реально занято (для «Память»).

    Слот считается занятым, если триггер включён или в нём заполнены
    поля приёма/ответа — пустые блоки память не занимают. Многофреймовый
    триггер разворачивается в несколько записей (expand_schedule) —
    считаем записи, а не блоки.
    """
    count = 0
    for trigger in triggers or []:
        if not isinstance(trigger, dict):
            continue
        # Кэш-триггер разворачивается по строкам кэша (у каждой свой
        # src-фильтр), обычный — по фреймам ответа.
        if trigger.get("cache"):
            rows = trigger.get("cache_rows")
            if isinstance(rows, list):
                filled = [
                    r for r in rows
                    if isinstance(r, dict) and str(r.get("id", "")).strip()
                ]
            else:  # легаси-одиночный кэш
                filled = [{"id": trigger.get("cache_id", "")}] if str(
                    trigger.get("cache_id", "")
                ).strip() else []
        else:
            filled = [
                r for r in trigger.get("responses") or []
                if isinstance(r, dict) and str(r.get("id", "")).strip()
            ]
        if (
            trigger.get("active")
            or str(trigger.get("recv_id", "")).strip()
            or str(trigger.get("cache_id", "")).strip()
            or filled
        ):
            schedule = expand_schedule(filled)
            count += len(schedule) if schedule is not None else 1
    return count


# Группировка записей одного триггера — байт reserved_pad (смещение 80)
# прошивка хранит как есть: 0 — базовая запись (строка ответа 0 или
# одиночный триггер), 1..127 — начало строки ответа N, 0x80|f —
# фрагмент счётчика (count>255 разбивается на куски по 255 отправок).
GROUP_SEQ_BASE = 0
GROUP_SEQ_FRAGMENT = 0x80


def expand_schedule(responses: list) -> list | None:
    """Раскладывает список фреймов ответа в расписание записей trigger_t.

    Возвращает список кортежей (row_index, delay_ms, tx_count,
    tx_interval_ms, group_seq) — по одному на запись МК. Тайминги
    повторяют _send_responses: у каждой записи абсолютная задержка
    первой отправки, повторы делает прошивка по tx_count/tx_interval_ms.
    None — триггер не разворачивается (>127 строк или задержка >65535),
    такой исполняет приложение.
    """
    if not responses or len(responses) > 127:
        return None
    schedule = []
    cumulative = 0
    for row_index, row in enumerate(responses):
        cumulative += max(0, int(row.get("delay_before_send") or 0))
        count = max(1, int(row.get("count") or 1))
        interval = max(0, int(row.get("delay_between") or 0))
        sent = 0
        fragment = 0
        while sent < count:
            chunk = min(255, count - sent)
            delay = cumulative + sent * interval
            if delay > 0xFFFF:
                return None
            seq = row_index if sent == 0 else GROUP_SEQ_FRAGMENT | fragment
            schedule.append((row_index, delay, chunk, interval, seq))
            sent += chunk
            fragment += 1
        cumulative += (count - 1) * interval
        if row_index < len(responses) - 1:
            cumulative += max(0, int(row.get("next_delay") or 0))
    return schedule


def crc8(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ 0x07) & 0xFF if value & 0x80 else (value << 1) & 0xFF
    return value


def pack_trigger(values: dict[str, Any], fmt_version: int = TRIGGER_FORMAT_VERSION) -> bytes:
    """Pack a trigger_t-compatible record and calculate firmware CRC8.

    fmt_version=3 (по умолчанию) — запись 90 Б с новыми полями;
    fmt_version=2 — легаси-запись 82 Б для старых прошивок (новые
    опции на провод не уходят — они выполняются PC-исполнением)."""
    common = (
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
        bytes((fmt_version, TRIGGER_SIZE_V2 if fmt_version == 2 else TRIGGER_SIZE)),
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
        int(values.get("rx_rtr", 0)) & 0xFF,
        int(values.get("group_seq", 0)) & 0xFF,
    )
    if fmt_version == 2:
        raw = bytearray(struct.pack(_TRIGGER_FORMAT_V2, *common, 0))
    else:
        rx_flags = 0 if values.get("rx_listen_echo", True) else TRIGGER_F_MUTE_ECHO
        src_flags = 0 if values.get("src_listen_echo", True) else TRIGGER_F_MUTE_ECHO
        raw = bytearray(
            struct.pack(
                _TRIGGER_FORMAT,
                *common,
                int(values.get("rx_fire_limit", 0)) & 0xFFFF,
                rx_flags,
                int(values.get("src_fire_limit", 0)) & 0xFFFF,
                src_flags,
                0, 0, 0,
            )
        )
    raw[-1] = crc8(raw[:-1])
    return bytes(raw)


def unpack_trigger(payload: bytes) -> dict[str, Any]:
    """Validate and unpack a trigger_t-compatible payload (v2/v3)."""
    if len(payload) == TRIGGER_SIZE:
        values = struct.unpack(_TRIGGER_FORMAT, payload)
        fmt_ok = values[15] in (
            (0, 0),
            bytes((TRIGGER_FORMAT_VERSION, TRIGGER_SIZE)),
        )
        tail = {
            "rx_fire_limit": values[28],
            "rx_listen_echo": not (values[29] & TRIGGER_F_MUTE_ECHO),
            "src_fire_limit": values[30],
            "src_listen_echo": not (values[31] & TRIGGER_F_MUTE_ECHO),
        }
    elif len(payload) == TRIGGER_SIZE_V2:
        values = struct.unpack(_TRIGGER_FORMAT_V2, payload)
        fmt_ok = values[15] in (
            (0, 0),
            bytes((TRIGGER_FORMAT_VERSION_V2, TRIGGER_SIZE_V2)),
        )
        tail = {
            "rx_fire_limit": 0,
            "rx_listen_echo": True,
            "src_fire_limit": 0,
            "src_listen_echo": True,
        }
    else:
        raise ValueError(f"Некорректный размер trigger_t: {len(payload)}")
    if values[0] != TRIGGER_MAGIC:
        raise ValueError("Неверный magic trigger_t")
    if crc8(payload[:-1]) != payload[-1]:
        raise ValueError("Неверный CRC8 trigger_t")
    if not fmt_ok:
        raise ValueError("Неподдерживаемая версия trigger_t")
    result = {
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
        "rx_rtr": values[26],
        "group_seq": values[27],
    }
    result.update(tail)
    return result
