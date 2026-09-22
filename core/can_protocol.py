"""Упаковка и распаковка CAN-кадров для приложения «Код Мастер».

Каждый кадр начинается с маркера, содержит байт канала, 11-битный ID,
длину данных, сами данные и контрольную сумму XOR.
"""



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
CMD_CAN_SPEED = 0xCE  # Установка бод-рейта CAN-канала (применяется и персистится в МК)
CMD_EVENT_LOG = 0xCF  # Постраничное чтение Flash-журнала событий МК (см. firmware/event_log.h)
CMD_TRIGGER_NAME_READ = 0xD0    # [index] → [len][имя] — из config-страницы МК
CMD_TRIGGER_NAME_WRITE = 0xD1   # [index][len][имя] — в RAM, фиксация по CMD_TRIGGER_NAME_COMMIT
CMD_TRIGGER_NAME_COMMIT = 0xD2  # Записать таблицу имён триггеров во Flash (перезапись config-страницы)

# Типы событий CMD_EVENT_LOG (event_log_type_t в прошивке).
EVLOG_BOOT = 1
EVLOG_CAN_ERROR = 2
EVLOG_CAN_BUSOFF = 3
EVLOG_CAN_BUSOFF_RECOVER = 4
EVLOG_CAN_OVERFLOW = 5
EVLOG_CAN_FIFO_POLL = 6
EVLOG_USB_RESET = 7
EVLOG_USB_DISCONNECT = 8
EVLOG_USB_TX_STALL = 9
EVLOG_USB_RX_OVERFLOW = 10

# Версия протокола, которую ожидает хост. Прошивка отвечает её в
# CMD_SYSTEM_INFO (payload[1]); меньше — функции нового протокола
# (stage/commit триггеров, cfg-команды, смена бод-рейта CAN) на
# устройстве отсутствуют.
EXPECTED_PROTOCOL_VERSION = 3

# Ключи деструктивных команд (протокол v3): прошивка отвергает
# CMD_CFG_WRITE без трейлера A5 5A, CMD_CFG_FACTORY_RESET без "FCLR" и
# CMD_TRIGGER_COMMIT с total=0 без байта-ключа — мусорный байт команды
# из рассинхрона CDC-потока (пересмотр байтов после битого кадра) не
# может больше стереть триггеры, переписать идентичность или сбросить МК.
CFG_WRITE_TRAILER = b"\xA5\x5A"
FACTORY_RESET_KEY = b"FCLR"
TRIGGER_CLEAR_ALL_KEY = 0xA5

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
    channel: int, can_id: int, data: bytes, rtr: bool = False, dlc: int | None = None
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


_RX_MARKERS = (MARKER_RX, MARKER_RX_EXT, MARKER_RX_RTR, MARKER_RX_RTR_EXT)
_TX_MARKERS = (MARKER_TX, MARKER_TX_EXT, MARKER_TX_RTR, MARKER_TX_RTR_EXT)
_EXT_MARKERS = (MARKER_RX_EXT, MARKER_TX_EXT, MARKER_RX_RTR_EXT, MARKER_TX_RTR_EXT)
_RTR_MARKERS = (MARKER_RX_RTR, MARKER_TX_RTR, MARKER_RX_RTR_EXT, MARKER_TX_RTR_EXT)


def unpack_can_frame(raw: bytes, tx: bool = False) -> dict[str, object] | None:
    """Ищет и распаковывает один CAN-кадр из байтового потока.

    Args:
        raw: Накопленный байтовый буфер, полученный из COM-порта.
        tx: False — искать кадры МК→ПК (маркеры 0xAA-0xAD);
            True — кадры ПК→МК (маркеры 0xBB-0xBE). Нужно для
            COM-логгера в режиме прослушивания, где видны оба направления.

    Returns:
        Словарь {'channel': int, 'id': int, 'data': bytes, 'extended': bool,
        'rtr': bool, 'dlc': int, 'tx_echo': bool, 'raw': bytes} или None,
        если кадр не найден или контрольная сумма не совпадает.
        tx_echo=True — кадр ранее отправлен самим МК (ответ триггера или
        ретрансляция кадра ПК): bxCAN себя не слышит, прошивка возвращает
        собственные передачи в RX-поток с битом 7 байта channel=1
        (протокол v2+; младшие 7 бит — номер канала 1/2).
    """
    markers = _TX_MARKERS if tx else _RX_MARKERS
    marker_index = -1
    marker = 0
    for m in markers:
        idx = raw.find(bytes([m]))
        if idx >= 0 and (marker_index < 0 or idx < marker_index):
            marker_index = idx
            marker = m

    if marker_index < 0:
        return None

    extended = marker in _EXT_MARKERS
    rtr = marker in _RTR_MARKERS
    id_length = 4 if extended else 2
    length_offset = 6 if extended else 4
    header_length = 3 + id_length  # marker + channel + id + dlc

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

    channel_byte = frame[1]
    channel = channel_byte & 0x7F
    can_id = int.from_bytes(frame[2:6], "little") if extended else frame[2] | (frame[3] << 8)
    data = frame[header_length:-1] if not rtr else b""
    return {
        "channel": channel,
        "id": can_id,
        "data": data,
        "dlc": length,
        "extended": extended,
        "rtr": rtr,
        "tx_echo": bool(channel_byte & 0x80),
        "raw": frame,
    }


def parse_all_frames(raw: bytes, tx: bool = False) -> tuple[list[dict[str, object]], bytes]:
    """Извлекает все полные CAN-кадры из буфера.

    Args:
        raw: Байтовый буфер, накопленный из COM-порта.
        tx: False — кадры МК→ПК; True — кадры ПК→МК (см. unpack_can_frame).

    Returns:
        Кортеж: список распакованных кадров и оставшийся неполный буфер.
    """
    frames: list[dict[str, object]] = []
    while True:
        frame = unpack_can_frame(raw, tx=tx)
        if frame is None:
            # Кадр не собрался. Возможны два случая:
            #  1) данных ещё недостаточно — ждём следующую порцию;
            #  2) маркер найден, но кадр битый (не сошлась контрольная сумма
            #     или некорректный DLC) — тогда пропускаем этот байт-маркер,
            #     иначе буфер навсегда застрянет на повреждённом байте и будет
            #     расти без ограничений, а новые кадры перестанут разбираться.
            marker_index = _find_marker(raw, tx=tx)
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
        marker_index = _find_marker(raw, tx=tx)
        total_length = len(frame["raw"])  # type: ignore[arg-type]
        raw = raw[marker_index + total_length :]
    return frames, raw


def _find_marker(raw: bytes, tx: bool = False) -> int:
    """Возвращает индекс ближайшего маркера кадра или -1, если маркеров нет."""
    markers = _TX_MARKERS if tx else _RX_MARKERS
    result = -1
    for m in markers:
        idx = raw.find(bytes([m]))
        if idx >= 0 and (result < 0 or idx < result):
            result = idx
    return result


def _is_incomplete(raw: bytes, marker_index: int) -> bool:
    """True, если от маркера ещё не пришло достаточно байт для полного кадра."""
    marker = raw[marker_index]
    extended = marker in _EXT_MARKERS
    rtr = marker in _RTR_MARKERS
    id_length = 4 if extended else 2
    length_offset = 6 if extended else 4
    available = len(raw) - marker_index
    header = 3 + id_length  # marker + channel + id + dlc
    if available < header:
        return True
    length = raw[marker_index + length_offset]
    if length > 8:
        # Заведомо битый кадр — ждать бессмысленно
        return False
    data_length = 0 if rtr else length
    return available < (header + data_length + 1)
