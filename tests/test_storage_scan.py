"""Тесты core/storage_scan — сканер маркерных областей Flash (CM-CDC-01).

Заголовок store_header_t (20 Б): magic u32 | seq u32 | len u32 |
gen u32 | version u16 | flags u16 | crc8. TRGH — 16-байтовый
заголовок с crc8 по первым 15 байтам.
"""

from __future__ import annotations

from core.stm32_info import FLASH_END_ADDR, TRIGGER_REGION_ADDR
from core.storage_scan import (
    STORE_PAGE,
    TRIG_HEADER_MAGIC,
    TRIG_MAGIC_LEGACY,
    find_storage_regions,
    region_is_cfg,
)
from core.trigger_protocol import crc8

BASE = 0x08020000


def _store_header(magic: int, payload_len: int, version: int = 1) -> bytes:
    hdr = bytearray(20)
    hdr[0:4] = magic.to_bytes(4, "little")
    hdr[4:8] = (1).to_bytes(4, "little")          # seq
    hdr[8:12] = payload_len.to_bytes(4, "little")
    hdr[12:16] = (0).to_bytes(4, "little")        # gen
    hdr[16:18] = version.to_bytes(2, "little")
    hdr[19] = crc8(bytes(hdr[:19]))
    return bytes(hdr)


def _trgh_header(version: int, count: int) -> bytes:
    hdr = bytearray(16)
    hdr[0:4] = TRIG_HEADER_MAGIC.to_bytes(4, "little")
    hdr[4] = version
    hdr[5] = count
    hdr[15] = crc8(bytes(hdr[:15]))
    return bytes(hdr)


def _blank(size: int = 8 * STORE_PAGE) -> bytearray:
    return bytearray(b"\xFF" * size)


class TestStoreRegions:
    def test_finds_store_region(self) -> None:
        blob = _blank()
        payload = b"\x01\x02\x03"
        page = _store_header(0x56415248, len(payload)) + payload
        blob[0 : len(page)] = page
        regions = find_storage_regions(bytes(blob), BASE)
        assert len(regions) == 1
        addr, data = regions[0]
        assert addr == BASE
        assert len(data) == STORE_PAGE  # округление до страницы
        assert data[:4] == (0x56415248).to_bytes(4, "little")

    def test_cfg_region_detected(self) -> None:
        blob = _blank()
        page = _store_header(0x43464748, 64) + b"\xAA" * 64
        blob[STORE_PAGE : STORE_PAGE + len(page)] = page
        regions = find_storage_regions(bytes(blob), BASE)
        assert len(regions) == 1 and region_is_cfg(regions[0][1])

    def test_bad_crc_rejected(self) -> None:
        blob = _blank()
        page = bytearray(_store_header(0x56415248, 4) + b"\x00" * 4)
        page[19] ^= 0xFF  # ломаем crc8
        blob[0 : len(page)] = page
        assert find_storage_regions(bytes(blob), BASE) == []

    def test_bad_version_rejected(self) -> None:
        blob = _blank()
        hdr = bytearray(_store_header(0x56415248, 4, version=9))
        hdr[19] = crc8(bytes(hdr[:19]))
        blob[0 : 24] = bytes(hdr) + b"\x00" * 4
        assert find_storage_regions(bytes(blob), BASE) == []

    def test_oversized_payload_rejected(self) -> None:
        blob = _blank()
        hdr = bytearray(_store_header(0x56415248, 0xFFFFFF))
        hdr[19] = crc8(bytes(hdr[:19]))
        blob[0:20] = bytes(hdr)
        assert find_storage_regions(bytes(blob), BASE) == []

    def test_multiple_regions(self) -> None:
        blob = _blank()
        blob[0:20] = _store_header(0x43464748, 10)
        blob[STORE_PAGE : STORE_PAGE + 20] = _store_header(
            0x464C5848, 100
        )
        regions = find_storage_regions(bytes(blob), BASE)
        addrs = sorted(a for a, _ in regions)
        assert addrs == [BASE, BASE + STORE_PAGE]


class TestTrghRegion:
    def test_trgh_v3(self) -> None:
        blob = _blank()
        hdr = _trgh_header(3, 2)
        blob[0:16] = hdr
        regions = find_storage_regions(bytes(blob), BASE)
        assert len(regions) == 1
        # 16 + 2*82 = 180 → одна страница.
        assert len(regions[0][1]) == STORE_PAGE

    def test_trgh_v4_stride(self) -> None:
        blob = _blank(4 * STORE_PAGE)
        hdr = _trgh_header(4, 50)
        blob[0:16] = hdr
        regions = find_storage_regions(bytes(blob), BASE)
        assert regions
        # 16 + 50*90 = 4516 → 3 страницы.
        assert len(regions[0][1]) == 3 * STORE_PAGE

    def test_trgh_overrun_rejected(self) -> None:
        blob = _blank(STORE_PAGE)
        hdr = _trgh_header(4, 50)  # требует 3 страницы — не влезает
        blob[0:16] = hdr
        assert find_storage_regions(bytes(blob), BASE) == []

    def test_trgh_bad_count(self) -> None:
        blob = _blank()
        hdr = bytearray(_trgh_header(4, 200))  # > TRIG_MAX_RECORDS
        hdr[15] = crc8(bytes(hdr[:15]))
        blob[0:16] = bytes(hdr)
        assert find_storage_regions(bytes(blob), BASE) == []


class TestLegacyTrig:
    def test_legacy_trg2_at_fixed_addr(self) -> None:
        size = FLASH_END_ADDR - BASE
        blob = _blank(size)
        off = TRIGGER_REGION_ADDR - BASE
        blob[off : off + 4] = TRIG_MAGIC_LEGACY.to_bytes(4, "little")
        regions = find_storage_regions(bytes(blob), BASE)
        assert len(regions) == 1
        addr, data = regions[0]
        assert addr == TRIGGER_REGION_ADDR
        assert addr + len(data) == FLASH_END_ADDR

    def test_no_regions_on_blank(self) -> None:
        assert find_storage_regions(bytes(_blank()), BASE) == []
