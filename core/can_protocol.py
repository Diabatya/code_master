"""Упаковка и распаковка CAN-кадров для приложения «Код Мастер».

Каждый кадр начинается с маркера, содержит байт канала, 11-битный ID,
длину данных, сами данные и контрольную сумму XOR.
"""

from typing import Dict, List, Optional


MARKER_TX = 0xBB  # Маркер исходящего кадра
MARKER_RX = 0xAA  # Маркер входящего кадра
MARKER_TX_EXT = 0xBC  # Маркер исходящего кадра с Extended (29-битным) CAN-ID
MARKER_RX_EXT = 0xAB  # Маркер входящего кадра с Extended (29-битным) CAN-ID
MARKER_TX_RTR = 0xBD       # Маркер исходящего RTR-кадра (Standard)
MARKER_RX_RTR = 0xAC       # Маркер входящего RTR-кадра (Standard)
MARKER_TX_RTR_EXT = 0xBE   # Маркер исходящего RTR-кадра (Extended)
MARKER_RX_RTR_EXT = 0xAD   # Маркер входящего RTR-кадра (Extended)

# Команды управления и конфигурации USB-моста
CMD_DEVICE_ID = 0x90        # Запрос типа/версии устройства
CMD_DEVICE_ID_RESP = 0x91   # Ответ на запрос ID
CMD_DEVICE_INFO = 0x92      # Запрос расширенной информации устройства
CMD_DEVICE_INFO_RESP = 0x93 # Ответ с серийным номером и объёмом памяти
CMD_AUTO_SPEED = 0xA0       # Запрос автоопределения скорости CAN
CMD_AUTO_SPEED_RESP = 0xA1  # Ответ с определённой скоростью
CMD_CFG_READ = 0xC0
CMD_CFG_WRITE = 0xC1
CMD_CFG_FACTORY_RESET = 0xC2
CMD_TRIGGER_READ = 0xC3
CMD_TRIGGER_WRITE = 0xC4
CMD_TRIGGER_ENABLE = 0xC5
CMD_CAN_STATS = 0xC7
CMD_TRIGGER_STATS = 0xC8
CMD_SYSTEM_INFO = 0xC9
CMD_TRIGGER_STAGE = 0xCA
CMD_TRIGGER_COMMIT = 0xCB
CMD_USB_STATS = 0xCC
CMD_CAN_MODE = 0xCD  # Управление режимом CAN (Normal/Silent) и терминатором

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


def pack_can_frame(
    channel: int, can_id: int, data: bytes, rtr: bool = False, dlc: Optional[int] = None
) -> bytes:
    """Формирует байтовый кадр для передачи через UART-мост.

    Args:
        channel: Номер канала (0x01 для CAN1, 0x02 для CAN2).
        can_id: 11-битный или 29-битный идентификатор CAN.
        data: Полезные данные, от 0 до 8 байт (CAN 2.0). Для RTR-кадра
            игнорируется, используется только `dlc`.
        rtr: Если True — сформировать Remote Transmission Request.
        dlc: Значение DLC (0..8). Для RTR задаёт запрашиваемую длину.

    Returns:
        Упакованный байтовый кадр с контрольной суммой.
    """
    data = bytes(data)[:8]
    if rtr and dlc is None:
        length = 0
    elif dlc is not None:
        length = max(0, min(8, dlc))
    else:
        length = len(data)
    if can_id > 0x7FF:
        marker = MARKER_TX_RTR_EXT if rtr else MARKER_TX_EXT
        frame = bytes([marker, channel & 0xFF])
        frame += can_id.to_bytes(4, "little")
        frame += bytes([length])
    else:
        marker = MARKER_TX_RTR if rtr else MARKER_TX
        frame = bytes([marker, channel & 0xFF, can_id & 0xFF, (can_id >> 8) & 0xFF, length])
    if not rtr:
        frame += data[:length]
    frame += bytes([xor_checksum(frame)])
    return frame


def unpack_can_frame(raw: bytes) -> Optional[Dict[str, object]]:
    """Ищет и распаковывает один CAN-кадр из байтового потока.

    Args:
        raw: Накопленный байтовый буфер, полученный из COM-порта.

    Returns:
        Словарь {'channel': int, 'id': int, 'data': bytes, 'extended': bool,
        'rtr': bool, 'dlc': int} или None, если кадр не найден или
        контрольная сумма не совпадает.
    """
    rx_markers = (MARKER_RX, MARKER_RX_EXT, MARKER_RX_RTR, MARKER_RX_RTR_EXT)
    marker_index = -1
    marker = 0
    for m in rx_markers:
        idx = raw.find(bytes([m]))
        if idx >= 0 and (marker_index < 0 or idx < marker_index):
            marker_index = idx
            marker = m

    if marker_index < 0:
        return None

    extended = marker in (MARKER_RX_EXT, MARKER_RX_RTR_EXT)
    rtr = marker in (MARKER_RX_RTR, MARKER_RX_RTR_EXT)
    id_length = 4 if extended else 2
    length_offset = 6 if extended else 4
    header_length = 4 + id_length  # marker + channel + id + dlc

    if len(raw) - marker_index < header_length:
        return None

    length = raw[marker_index + length_offset]
    if length > 8:
        return None

    data_length = 0 if rtr else length
    total_length = header_length + data_length
    if len(raw) - marker_index < total_length + 1:  # +1 checksum
        return None

    total_length += 1  # include checksum byte

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
    data = frame[header_length:-1] if not rtr else b""
    return {
        "channel": channel,
        "id": can_id,
        "data": data,
        "dlc": length,
        "extended": extended,
        "rtr": rtr,
    }


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
            # Кадр не собрался. Возможны два случая:
            #  1) данных ещё недостаточно — ждём следующую порцию;
            #  2) маркер найден, но кадр битый (не сошлась контрольная сумма
            #     или некорректный DLC) — тогда пропускаем этот байт-маркер,
            #     иначе буфер навсегда застрянет на повреждённом байте и будет
            #     расти без ограничений, а новые кадры перестанут разбираться.
            marker_index = _find_marker(raw)
            if marker_index < 0:
                # Маркеров нет вовсе — хранить нечего, кроме возможного хвоста
                raw = b""
                break
            if _is_incomplete(raw, marker_index):
                # Ждём остаток кадра, но сначала отбрасываем мусор до маркера
                raw = raw[marker_index:]
                break
            raw = raw[marker_index + 1 :]
            continue
        frames.append(frame)
        marker_index = _find_marker(raw)
        total_length = (8 if frame["extended"] else 6) + len(frame["data"])  # type: ignore[arg-type]
        raw = raw[marker_index + total_length :]
    return frames, raw


def _find_marker(raw: bytes) -> int:
    """Возвращает индекс ближайшего RX-маркера или -1, если маркеров нет."""
    rx_markers = (MARKER_RX, MARKER_RX_EXT, MARKER_RX_RTR, MARKER_RX_RTR_EXT)
    result = -1
    for m in rx_markers:
        idx = raw.find(bytes([m]))
        if idx >= 0 and (result < 0 or idx < result):
            result = idx
    return result


def _is_incomplete(raw: bytes, marker_index: int) -> bool:
    """True, если от маркера ещё не пришло достаточно байт для полного кадра."""
    marker = raw[marker_index]
    extended = marker in (MARKER_RX_EXT, MARKER_RX_RTR_EXT)
    rtr = marker in (MARKER_RX_RTR, MARKER_RX_RTR_EXT)
    id_length = 4 if extended else 2
    length_offset = 6 if extended else 4
    available = len(raw) - marker_index
    header = 4 + id_length  # marker + channel + id + dlc
    if available < header:
        return True
    length = raw[marker_index + length_offset]
    if length > 8:
        # Заведомо битый кадр — ждать бессмысленно
        return False
    data_length = 0 if rtr else length
    return available < (header + data_length + 1)
