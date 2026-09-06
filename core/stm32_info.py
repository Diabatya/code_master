"""Справочные данные о моделях STM32, используемых в прошивке."""

from typing import Dict, Optional, Tuple

# Единая точка истины для базовых адресов Flash (см. CURSOR_FIX_PROMPT.md 3.4):
# раньше UART/USB CDC-путь (`ui/flash_dialog.py`) использовал магический
# литерал 0x08008000, а ST-Link/DFU-путь — 0x08000000, каждый в нескольких
# местах, без единого источника истины.
#
# BOOTLOADER_BASE_ADDR — начало Flash приложения нашего собственного STM32
# bootloader'а (`firmware/bootloader/`) и полного образа (бутлоадер+приложение),
# который заливается через ST-Link/DFU.
BOOTLOADER_BASE_ADDR = 0x08000000
# APPLICATION_BASE_ADDR — начало области приложения (после 32 КБ бутлоадера,
# см. firmware/PROTOCOL.md), куда UART/USB CDC bootloader-протокол пишет
# firmware приложения без самого бутлоадера.
APPLICATION_BASE_ADDR = 0x08008000

# Страница конфигурации приложения. Она не является последней страницей
# физической Flash: страницы 124-127 зарезервированы под триггеры.
APP_METADATA_PAGE_ADDR = 0x0803D000
APP_METADATA_PAGE_SIZE = 2048
APP_METADATA_MAGIC = 0x41505031
APP_METADATA_VERSION = 1
DEVICE_CONFIG_PAGE_ADDR = 0x0803D800
DEVICE_CONFIG_PAGE_SIZE = 2048
DEVICE_CONFIG_NAME_MAX = 9
DEVICE_CONFIG_SERIAL_MAX = 10
DEVICE_CONFIG_MAGIC = 0x43464730
DEVICE_CONFIG_FORMAT_VERSION = 1
DEVICE_CONFIG_RECORD_SIZE = 32
DEVICE_CONFIG_DEFAULT_VID = 0x0483
DEVICE_CONFIG_DEFAULT_PID = 0x5740


def device_config_crc8(data: bytes) -> int:
    """CRC-8 полиномом 0x07 для первых 31 байт device_config_t."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def build_device_config_page(
    name: str,
    serial: str,
    existing_page: Optional[bytes] = None,
) -> bytes:
    """Формирует полную 2-КБ страницу конфигурации firmware.

    При наличии валидной страницы сохраняет VID/PID, reserved и прочие байты
    страницы; изменяются только поля имени, serial и CRC.
    """
    page = bytearray(existing_page[:DEVICE_CONFIG_PAGE_SIZE] if existing_page else b"\xFF" * DEVICE_CONFIG_PAGE_SIZE)
    if len(page) < DEVICE_CONFIG_PAGE_SIZE:
        page.extend(b"\xFF" * (DEVICE_CONFIG_PAGE_SIZE - len(page)))
    current = parse_device_config(bytes(page))
    if current is None:
        page[:] = b"\xFF" * DEVICE_CONFIG_PAGE_SIZE
        vid, pid = DEVICE_CONFIG_DEFAULT_VID, DEVICE_CONFIG_DEFAULT_PID
    else:
        vid, pid = current[2], current[3]

    name_bytes = name.encode("ascii", errors="ignore")[:DEVICE_CONFIG_NAME_MAX]
    serial_bytes = serial.encode("ascii", errors="ignore")[:DEVICE_CONFIG_SERIAL_MAX]
    record = bytearray(32)
    record[0:4] = DEVICE_CONFIG_MAGIC.to_bytes(4, "little")
    record[4] = len(name_bytes)
    record[5:14] = name_bytes.ljust(DEVICE_CONFIG_NAME_MAX, b"\x00")
    record[14] = len(serial_bytes)
    record[15:25] = serial_bytes.ljust(DEVICE_CONFIG_SERIAL_MAX, b"\x00")
    record[25:27] = int(vid).to_bytes(2, "little")
    record[27:29] = int(pid).to_bytes(2, "little")
    record[29] = DEVICE_CONFIG_FORMAT_VERSION
    record[30] = DEVICE_CONFIG_RECORD_SIZE
    record[31] = device_config_crc8(record[:31])
    page[:32] = record
    return bytes(page)


def parse_device_config(page: bytes) -> Optional[Tuple[str, str, int, int]]:
    """Проверяет и разбирает запись device_config_t из страницы Flash."""
    if len(page) < 32 or int.from_bytes(page[:4], "little") != DEVICE_CONFIG_MAGIC:
        return None
    name_len, serial_len = page[4], page[14]
    if name_len > DEVICE_CONFIG_NAME_MAX or serial_len > DEVICE_CONFIG_SERIAL_MAX:
        return None
    if device_config_crc8(page[:31]) != page[31]:
        return None
    if not (
        (page[29] == 0 and page[30] == 0)
        or (page[29] == DEVICE_CONFIG_FORMAT_VERSION and page[30] == DEVICE_CONFIG_RECORD_SIZE)
    ):
        return None
    name = page[5:14][:name_len].decode("ascii", errors="ignore")
    serial = page[15:25][:serial_len].decode("ascii", errors="ignore")
    vid = int.from_bytes(page[25:27], "little")
    pid = int.from_bytes(page[27:29], "little")
    return name, serial, vid, pid


def merge_device_config_page(incoming_page: bytes, existing_page: bytes) -> bytes:
    """Сохраняет аппаратные поля существующей config-страницы при обновлении."""
    incoming = parse_device_config(incoming_page)
    if incoming is None:
        return incoming_page
    return build_device_config_page(incoming[0], incoming[1], existing_page)


# Модель → размер Flash в КБ
STM32_FLASH_SIZES: Dict[str, int] = {
    "STM32F103C8T6": 64,
    "STM32F103RBT6": 128,
    "STM32F105RCT6": 256,
    "STM32F105VCT6": 256,
    "STM32F107VCT6": 256,
    "STM32F205RGT6": 1024,
    "STM32F303CCT6": 256,
    "STM32F407VGT6": 1024,
    "STM32F429ZIT6": 2048,
    "STM32F446RET6": 512,
    "STM32F746ZGT6": 1024,
}

# Модель → размер страницы flash в байтах. Для F1 — 1/2 КБ, F2/F4 — сектора 16+ КБ.
STM32_PAGE_SIZES: Dict[str, int] = {
    "STM32F103C8T6": 1024,
    "STM32F103RBT6": 1024,
    "STM32F105RCT6": 2048,
    "STM32F105VCT6": 2048,
    "STM32F107VCT6": 2048,
    "STM32F205RGT6": 16384,
    "STM32F303CCT6": 2048,
    "STM32F407VGT6": 16384,
    "STM32F429ZIT6": 16384,
    "STM32F446RET6": 16384,
    "STM32F746ZGT6": 16384,
}

# ST-LINK Device ID (например, 0x418) → модель по datasheet
DEVICE_ID_TO_MODEL: Dict[str, str] = {
    "0x410": "STM32F103RBT6",
    "0x412": "STM32F103C8T6",
    "0x413": "STM32F407VGT6",
    "0x414": "STM32F105RCT6",
    "0x418": "STM32F105VCT6",
    "0x421": "STM32F446RET6",
    "0x422": "STM32F303CCT6",
    "0x434": "STM32F429ZIT6",
    "0x440": "STM32F107VCT6",
    "0x449": "STM32F746ZGT6",
    "0x411": "STM32F205RGT6",
}

# Chip ID (из Get ID) → размер Flash в КБ (как строка, т.к. у некоторых чипов диапазон)
CHIP_FLASH_SIZE_KB: Dict[int, str] = {
    0x412: "64/128",
    0x410: "128/256",
    0x414: "256/512",
    0x418: "64/128",
    0x420: "128/256",
    0x430: "1024",
    0x431: "256/512",
    0x432: "512/1024",
    0x433: "1024",
    0x440: "1024",
    0x441: "2048",
    0x442: "512/1024",
    0x444: "512/1024",
    0x445: "1024",
    0x448: "1024",
    0x449: "2048",
    0x450: "1024",
    0x451: "2048",
}
