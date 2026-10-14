"""Поиск маркерных областей хранилища в дампе Flash (CM-CDC-01).

Области VARH/FLXH/EVLH/CFGH и TRGH не имеют фиксированных адресов —
storage.c находит их сканированием страниц за концом кода приложения.
Этот сканер повторяет ту же логику на стороне ПК: используется
сохранением областей при обновлении прошивки (CDC/UART/DFU) — фикс-
адресное предположение теряло данные при росте образа.

Заголовки: store_header_t (20 Б, crc8) у VARH/FLXH/EVLH/CFGH;
TRGH — свой 16-байтовый заголовок; легаси-пул триггеров TRG2 —
по фиксированному адресу без заголовка.
"""

from __future__ import annotations

from core.stm32_info import FLASH_END_ADDR, TRIGGER_REGION_ADDR
from core.trigger_protocol import crc8

STORE_PAGE = 2048
# VARH, FLXH, EVLH, CFGH (настройки приложения сразу за кодом)
STORE_MAGICS = (0x56415248, 0x464C5848, 0x45564C48, 0x43464748)
STORE_MAX_PAYLOAD = 48 * 1024
TRIG_HEADER_MAGIC = 0x54524748  # "TRGH"
TRIG_MAGIC_LEGACY = 0x54524732  # "TRG2"
TRIG_MAX_RECORDS = 70
STORE_MAGIC_CFG = 0x43464748


def find_storage_regions(
    blob: bytes, base_addr: int
) -> list[tuple[int, bytes]]:
    """Сканирует дамп Flash постранично и возвращает [(addr, образ)]
    всех валидных маркерных областей хранилища — для сохранения
    перед стиранием и восстановления после записи."""
    regions: list[tuple[int, bytes]] = []
    for off in range(0, len(blob) - 19, STORE_PAGE):
        magic = int.from_bytes(blob[off : off + 4], "little")
        if magic == TRIG_HEADER_MAGIC:
            version, count = blob[off + 4], blob[off + 5]
            if (
                version in (3, 4)
                and count <= TRIG_MAX_RECORDS
                and crc8(blob[off : off + 15]) == blob[off + 15]
            ):
                stride = 90 if version == 4 else 82
                size = -(-(16 + count * stride) // STORE_PAGE) * STORE_PAGE
                if off + size <= len(blob):
                    regions.append((base_addr + off, blob[off : off + size]))
        elif magic in STORE_MAGICS:
            payload_len = int.from_bytes(blob[off + 8 : off + 12], "little")
            version = int.from_bytes(blob[off + 16 : off + 18], "little")
            if (
                version == 1
                and payload_len <= STORE_MAX_PAYLOAD
                and crc8(blob[off : off + 19]) == blob[off + 19]
            ):
                size = -(-(20 + payload_len) // STORE_PAGE) * STORE_PAGE
                if off + size <= len(blob):
                    regions.append((base_addr + off, blob[off : off + size]))
    # Легаси-пул триггеров v2 — записи без заголовка по фиксированному
    # адресу; сохраняем весь старый диапазон до конца Flash (прошивка
    # при старте смигрирует записи в маркерную область).
    legacy_off = TRIGGER_REGION_ADDR - base_addr
    if (
        0 <= legacy_off <= len(blob) - 4
        and int.from_bytes(blob[legacy_off : legacy_off + 4], "little")
        == TRIG_MAGIC_LEGACY
    ):
        regions.append(
            (TRIGGER_REGION_ADDR, blob[legacy_off : FLASH_END_ADDR - base_addr])
        )
    return regions


def region_is_cfg(blob: bytes) -> bool:
    """Область начинается заголовком CFGH (настройки приложения)."""
    return (
        len(blob) >= 4
        and int.from_bytes(blob[:4], "little") == STORE_MAGIC_CFG
    )
