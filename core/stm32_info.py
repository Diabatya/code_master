"""Справочные данные о моделях STM32, используемых в прошивке."""


# Единая точка истины для базовых адресов Flash (см. CURSOR_FIX_PROMPT.md 3.4):
# раньше UART/USB CDC-путь (`ui/flash_dialog.py`) использовал магический
# литерал 0x08008000, а ST-Link/DFU-путь — 0x08000000, каждый в нескольких
# местах, без единого источника истины.
#
# BOOTLOADER_BASE_ADDR — начало Flash приложения нашего собственного STM32
# bootloader'а (`firmware/bootloader/`) и полного образа (бутлоадер+приложение),
# который заливается через ST-Link/DFU.
BOOTLOADER_BASE_ADDR = 0x08000000
BOOTLOADER_SIZE = 0x8000

# --- Новая карта Flash (отчёт мастера) -------------------------------------
# Идентификация устройства (тип/s/n/версия ПО) перенесена В НАЧАЛО области
# за bootloader'ом — читается и пишется отсюда при подключении и
# программировании. Конфигурации упакованы у КОНЦА Flash без зазоров —
# наполнение упирается в последние адреса, поэтому карта масштабируется
# на камни с большей Flash без переделки:
#
#   0x08000000–0x08007FFF  bootloader (32 KB)
#   0x08008000–0x080087FF  DEVICE INFO — CFG0+CEX0+TNM0+VER1 (стр. 16)
#   0x08008800–0x08008FFF  APP metadata APP1 (стр. 17)
#   0x08009000–…           application code (со стр. 18 до конца образа)
#   …–0x0803FFFF          маркерное хранилище (storage.c): области без
#                          фиксированных адресов, ищутся по заголовкам —
#                          FLXH (гибкая логика, за концом кода, растёт
#                          вверх), TRGH (триггеры, плавает в зазоре),
#                          EVLH (журнал, под переменными), VARH
#                          (переменные, от конца Flash растут вниз).
#   Легаси-адреса (читаются один раз при миграции, затем стираются):
#   0x0803C000–0x0803DFFF  старый пул журнала (стр. 120–123)
#   0x0803E000–0x0803FFFF  старый пул триггеров TRG2 (стр. 124–127)
DEVICE_INFO_PAGE_ADDR = 0x08008000
DEVICE_CONFIG_PAGE_ADDR = DEVICE_INFO_PAGE_ADDR
DEVICE_CONFIG_PAGE_SIZE = 2048
APP_METADATA_PAGE_ADDR = 0x08008800
APP_METADATA_PAGE_SIZE = 2048
# APPLICATION_BASE_ADDR — начало кода приложения, куда UART/USB CDC
# bootloader-протокол пишет firmware приложения без служебных страниц.
APPLICATION_BASE_ADDR = 0x08009000
# Старая раскладка — читается один раз для миграции на новое место.
DEVICE_CONFIG_PAGE_ADDR_LEGACY = 0x0803D800
APP_METADATA_PAGE_ADDR_LEGACY = 0x0803D000
LEGACY_CONFIG_PAGE_ADDR = 0x0803F800
APP_METADATA_MAGIC = 0x41505031
APP_METADATA_VERSION = 1
# Граница записи AN3155: bootloader разрешает сырые команды записи только
# до 0x0803C000 — верхние 16 КБ под маркерным хранилищем недоступны для
# mass-write/page-erase и меняются только командами CMD_STORAGE_*/CMD_*.
# Совпадает с легаси-адресом пула журнала — отсюда имя константы.
EVENT_LOG_ADDR = 0x0803C000
EVENT_LOG_SIZE = 8192
# Легаси-пул триггеров v2 (записи TRG2 без заголовка) — читается прошивкой
# один раз при миграции в маркерную область TRGH, затем стирается.
# Использовать только как адрес миграции, не как текущее хранилище.
TRIGGER_REGION_ADDR = 0x0803E000
TRIGGER_REGION_SIZE = 8192
FLASH_END_ADDR = 0x08040000


DEVICE_CONFIG_NAME_MAX = 9
DEVICE_CONFIG_SERIAL_MAX = 10
DEVICE_CONFIG_MAGIC = 0x43464730
DEVICE_CONFIG_FORMAT_VERSION = 1
DEVICE_CONFIG_RECORD_SIZE = 32
DEVICE_CONFIG_DEFAULT_VID = 0x0483
DEVICE_CONFIG_DEFAULT_PID = 0x5740
# Версия ПО хранится отдельным 32-байтным блоком config-страницы
# (магия "VER1"). Смещение 1024 — свободная область за таблицей имён
# триггеров: старая раскладка (offset 32) конфликтовала с ext-записью
# CEX0 и перетиралась ею при первом же сохранении конфигурации.
# Прошивка читает VER1 в RAM при старте и отдаёт хвостом CMD_CFG_READ.
DEVICE_CONFIG_VER_OFFSET = 1024
DEVICE_CONFIG_VER_OFFSET_LEGACY = 32
DEVICE_CONFIG_VER_MAGIC = 0x56455231
DEVICE_CONFIG_VERSION_MAX = 16


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
    existing_page: bytes | None = None,
    fw_version: str | None = None,
) -> bytes:
    """Формирует полную 2-КБ страницу конфигурации firmware.

    При наличии валидной страницы сохраняет VID/PID, reserved и прочие байты
    страницы; изменяются только поля имени, serial и CRC. Если передана
    ``fw_version``, обновляется и версионная запись (смещение 32).
    """
    page = bytearray(existing_page[:DEVICE_CONFIG_PAGE_SIZE] if existing_page else b"\xFF" * DEVICE_CONFIG_PAGE_SIZE)
    if len(page) < DEVICE_CONFIG_PAGE_SIZE:
        page.extend(b"\xFF" * (DEVICE_CONFIG_PAGE_SIZE - len(page)))
    current = parse_device_config(bytes(page))
    if current is None:
        # Невалидная/отсутствующая запись — обновляем только первые 32 байта,
        # остальную страницу (reserved и будущие поля) не трогаем.
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
    if fw_version is not None:
        ver = fw_version.encode("ascii", errors="ignore")[:DEVICE_CONFIG_VERSION_MAX]
        vrec = bytearray(32)
        vrec[0:4] = DEVICE_CONFIG_VER_MAGIC.to_bytes(4, "little")
        vrec[4] = len(ver)
        vrec[5 : 5 + DEVICE_CONFIG_VERSION_MAX] = ver.ljust(
            DEVICE_CONFIG_VERSION_MAX, b"\x00"
        )
        vrec[31] = device_config_crc8(vrec[:31])
        page[DEVICE_CONFIG_VER_OFFSET : DEVICE_CONFIG_VER_OFFSET + 32] = vrec
    return bytes(page)


def parse_device_fw_version(page: bytes) -> str | None:
    """Читает версионную запись «VER1» со страницы конфигурации.

    Проверяет новое смещение (1024), затем legacy (32) — образы до
    переноса шили VER1 туда."""
    for off in (DEVICE_CONFIG_VER_OFFSET, DEVICE_CONFIG_VER_OFFSET_LEGACY):
        if len(page) < off + 32:
            continue
        if int.from_bytes(page[off : off + 4], "little") != DEVICE_CONFIG_VER_MAGIC:
            continue
        ver_len = page[off + 4]
        if ver_len > DEVICE_CONFIG_VERSION_MAX:
            continue
        if device_config_crc8(page[off : off + 31]) != page[off + 31]:
            continue
        return page[off + 5 : off + 5 + ver_len].decode("ascii", errors="ignore")
    return None


def parse_device_config(page: bytes) -> tuple[str, str, int, int] | None:
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


def parse_legacy_device_config(page: bytes) -> tuple[str, str] | None:
    """Разбирает старый GUI-формат name@8..17/serial@18..27."""
    if len(page) < 28:
        return None
    raw_name = page[8:18].rstrip(b"\xFF\x00 ")
    raw_serial = page[18:28].rstrip(b"\xFF\x00 ")
    if not raw_name and not raw_serial:
        return None
    if any(byte < 0x20 or byte > 0x7E for byte in raw_name + raw_serial):
        return None
    return (
        raw_name.decode("ascii", errors="ignore"),
        raw_serial.decode("ascii", errors="ignore"),
    )


def seed_device_config_page(pages: list[bytes]) -> bytes | None:
    """Ищет идентичность устройства среди прочитанных страниц — базу
    для merge_device_config_page, когда на новой странице валидного
    CFG0 нет (DFU-заливка без config-страницы, старая раскладка).

    Первая валидная CFG0-запись возвращается как есть; при её
    отсутствии первый легаси-формат (GUI name@8/serial@18)
    преобразуется в страницу CFG0. Иначе на стёртой/неписанной
    странице серийник терялся и USB отдавал UID-строку вроде
    «48E851863846» вместо заданного оператором номера
    (отчёт мастера)."""
    for page in pages:
        if page and parse_device_config(page) is not None:
            return page
    for page in pages:
        if not page:
            continue
        legacy = parse_legacy_device_config(page)
        if legacy is not None:
            name, serial = legacy
            return build_device_config_page(name, serial)
    return None


def merge_device_config_page(incoming_page: bytes, existing_page: bytes) -> bytes:
    """Сохраняет аппаратные поля существующей config-страницы при
    обновлении. Версионная запись VER1 берётся из НОВОГО образа —
    иначе после прошивки на карточке оставалась бы старая версия
    (или пусто), а релизная прошивка несёт свою (отчёт мастера).

    Оверлей не-0xFF байтов образа поверх существующей страницы:
    релизный CDC-образ несёт только VER1 (CFG0 в него не пишется),
    поэтому требование валидного CFG0 у образа отбрасывало версию —
    идентичность устройства при этом затиралась стёртой страницей
    (отчёт мастера: «прошил старой версией — показывает последнюю»)."""
    merged = bytearray(
        existing_page[:DEVICE_CONFIG_PAGE_SIZE]
        if existing_page
        else b"\xFF" * DEVICE_CONFIG_PAGE_SIZE
    )
    if len(merged) < DEVICE_CONFIG_PAGE_SIZE:
        merged.extend(b"\xFF" * (DEVICE_CONFIG_PAGE_SIZE - len(merged)))
    incoming_cfg = parse_device_config(incoming_page)
    for off in range(0, DEVICE_CONFIG_PAGE_SIZE, DEVICE_CONFIG_RECORD_SIZE):
        block = incoming_page[off : off + DEVICE_CONFIG_RECORD_SIZE]
        if len(block) < DEVICE_CONFIG_RECORD_SIZE:
            break
        # VER1 по легаси-смещению 32 переносится ниже в актуальное
        # смещение 1024 — оверлей по старому адресу попал бы в область
        # имён триггеров (TNM0) и испортил бы их записи.
        if (
            off == DEVICE_CONFIG_VER_OFFSET_LEGACY
            and int.from_bytes(block[:4], "little") == DEVICE_CONFIG_VER_MAGIC
        ):
            continue
        for i, byte in enumerate(block):
            if byte != 0xFF:
                merged[off + i] = byte
    existing = parse_device_config(existing_page) if existing_page else None
    merged_cfg = parse_device_config(bytes(merged))
    if existing is not None and merged_cfg is not None and (
        incoming_cfg is not None
        and merged_cfg[2:4] != existing[2:4]
    ):
        # VID/PID — аппаратная идентичность USB-интерфейса: образ
        # собран инструментом без данных устройства и мог принести
        # дефолтные VID/PID — восстанавливаем из старой страницы.
        merged[25:27] = int(existing[2]).to_bytes(2, "little")
        merged[27:29] = int(existing[3]).to_bytes(2, "little")
        merged[31] = device_config_crc8(bytes(merged[:31]))
    # Версию образа нормализуем в актуальное смещение VER1 (образы до
    # переноса хранили её по offset 32 — теперь там имена триггеров).
    ver = parse_device_fw_version(incoming_page)
    if ver is not None:
        enc = ver.encode("ascii", errors="ignore")[:DEVICE_CONFIG_VERSION_MAX]
        vrec = bytearray(32)
        vrec[0:4] = DEVICE_CONFIG_VER_MAGIC.to_bytes(4, "little")
        vrec[4] = len(enc)
        vrec[5 : 5 + DEVICE_CONFIG_VERSION_MAX] = enc.ljust(
            DEVICE_CONFIG_VERSION_MAX, b"\x00"
        )
        vrec[31] = device_config_crc8(bytes(vrec[:31]))
        merged[
            DEVICE_CONFIG_VER_OFFSET : DEVICE_CONFIG_VER_OFFSET + 32
        ] = vrec
    return bytes(merged)


def build_app_metadata(image: bytes) -> bytes:
    """Строит 16-байтную запись метаданных приложения (APP1).

    Записывается bootloader'ом/конфигуратором ПОСЛЕДНИМ шагом обновления —
    пока CRC и размер не записаны, bootloader считает приложение
    невалидным и остаётся в режиме прошивки (см. bl_metadata_is_valid).
    """
    import binascii
    import struct

    return struct.pack(
        "<IHHII",
        APP_METADATA_MAGIC,
        APP_METADATA_VERSION,
        0,  # reserved
        len(image),
        binascii.crc32(image) & 0xFFFFFFFF,
    )


# Модель → размер Flash в КБ
STM32_FLASH_SIZES: dict[str, int] = {
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
STM32_PAGE_SIZES: dict[str, int] = {
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
DEVICE_ID_TO_MODEL: dict[str, str] = {
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
CHIP_FLASH_SIZE_KB: dict[int, str] = {
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
