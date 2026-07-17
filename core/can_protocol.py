"""Упаковка и распаковка CAN-кадров для приложения «Код Мастер».

Каждый кадр начинается с маркера, содержит байт канала, 11-битный ID,
длину данных, сами данные и контрольную сумму XOR.
"""

from typing import Dict, List, Optional


MARKER_TX = 0xBB  # Маркер исходящего кадра
MARKER_RX = 0xAA  # Маркер входящего кадра
MARKER_TX_EXT = 0xBC  # Маркер исходящего кадра с Extended (29-битным) CAN-ID
MARKER_RX_EXT = 0xAB  # Маркер входящего кадра с Extended (29-битным) CAN-ID

# Команды управления и конфигурации USB-моста
CMD_DEVICE_ID = 0x90        # Запрос типа/версии устройства
CMD_DEVICE_ID_RESP = 0x91   # Ответ на запрос ID
CMD_AUTO_SPEED = 0xA0       # Запрос автоопределения скорости CAN
CMD_AUTO_SPEED_RESP = 0xA1  # Ответ с определённой скоростью

# Типы устройств
DEVICE_TYPE_BASIC = 0x00   # Базовое CAN 2.0
DEVICE_TYPE_ANALOG = 0x01  # 2 CAN + (с аналоговыми портами)
DEVICE_TYPE_CAN_FD = 0x02  # 2 CAN FD


def xor_checksum(data: bytes) -> int:
    """Вычисляет XOR-сумму всех байт переданных данных.

    Args:
        data: Байтовая строка, по которой вычисляется сумма.

    Returns:
        Значение контрольной суммы (один байт).
    """
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def pack_can_frame(channel: int, can_id: int, data: bytes) -> bytes:
    """Формирует байтовый кадр для передачи через UART-мост.

    Args:
        channel: Номер канала (0x01 для CAN1, 0x02 для CAN2).
        can_id: 11-битный или 29-битный идентификатор CAN.
        data: Полезные данные, от 0 до 8 байт (CAN 2.0).

    Returns:
        Упакованный байтовый кадр с контрольной суммой.
    """
    data = bytes(data)[:8]
    length = len(data)
    if can_id > 0x7FF:
        marker = MARKER_TX_EXT
        frame = bytes([marker, channel & 0xFF])
        frame += can_id.to_bytes(4, "little")
        frame += bytes([length])
    else:
        marker = MARKER_TX
        frame = bytes([marker, channel & 0xFF, can_id & 0xFF, (can_id >> 8) & 0xFF, length])
    frame += data
    frame += bytes([xor_checksum(frame)])
    return frame


def unpack_can_frame(raw: bytes) -> Optional[Dict[str, object]]:
    """Ищет и распаковывает один CAN-кадр из байтового потока.

    Args:
        raw: Накопленный байтовый буфер, полученный из COM-порта.

    Returns:
        Словарь {'channel': int, 'id': int, 'data': bytes, 'extended': bool} или None,
        если кадр не найден или контрольная сумма не совпадает.
    """
    marker_index = raw.find(bytes([MARKER_RX]))
    extended_marker_index = raw.find(bytes([MARKER_RX_EXT]))

    if marker_index < 0 and extended_marker_index < 0:
        return None

    if marker_index < 0 or (0 <= extended_marker_index < marker_index):
        marker_index = extended_marker_index
        extended = True
    else:
        extended = False

    marker = raw[marker_index]

    if extended:
        # Минимальная длина: маркер + канал + 4 байта ID + длина + 1 байт контрольной суммы
        if len(raw) - marker_index < 8:
            return None
        length = raw[marker_index + 6]
        id_length = 4
    else:
        # Минимальная длина: маркер + канал + 2 байта ID + длина + 1 байт контрольной суммы
        if len(raw) - marker_index < 6:
            return None
        length = raw[marker_index + 4]
        id_length = 2

    # Общая длина: маркер + канал + id + dlc + данные + checksum
    total_length = 4 + id_length + length
    if len(raw) - marker_index < total_length:
        return None

    frame = raw[marker_index : marker_index + total_length]
    received_checksum = frame[-1]
    calculated_checksum = xor_checksum(frame[:-1])

    if received_checksum != calculated_checksum:
        return None

    channel = frame[1]
    if extended:
        can_id = int.from_bytes(frame[2:6], "little")
    else:
        can_id = frame[2] | (frame[3] << 8)
    data = frame[3 + id_length : -1]
    return {"channel": channel, "id": can_id, "data": data, "extended": extended}


def parse_all_frames(raw: bytes) -> tuple[List[Dict[str, object]], bytes]:
    """Извлекает все полные CAN-кадры из буфера.

    Args:
        raw: Байтовый буфер, накопленный из COM-порта.

    Returns:
        Кортеж: список распакованных кадров и оставшийся неполный буфер.
    """
    frames: List[Dict[str, object]] = []
    while True:
        frame = unpack_can_frame(raw)
        if frame is None:
            break
        frames.append(frame)
        # Ищем маркер текущего кадра и сдвигаем буфер на его длину
        marker_index = raw.find(bytes([MARKER_RX_EXT])) if frame["extended"] else raw.find(bytes([MARKER_RX]))
        total_length = (8 if frame["extended"] else 6) + frame["data"].__len__()  # type: ignore[arg-type]
        raw = raw[marker_index + total_length :]
    return frames, raw
