"""Тесты сохранения маркерного хранилища при обновлении (CM-CDC-01).

Bootloader.flash_firmware с физической симуляцией Flash-массива:
стирание страниц → 0xFF, запись — только гашение битов (как у МК).
Проверяем порядок «инвалидация метаданных → код → хранилище →
метаданные», релокацию CFGH за новым концом кода и отказ до стирания
при нечитаемом хранилище.
"""

from __future__ import annotations

import os
import tempfile
import zlib
from unittest.mock import MagicMock

import pytest

from core.bootloader import Bootloader, BootloaderError
from core.stm32_info import (
    APP_METADATA_MAGIC,
    APP_METADATA_PAGE_ADDR,
    APP_METADATA_PAGE_SIZE,
    APPLICATION_BASE_ADDR,
    BOOTLOADER_BASE_ADDR,
    DEVICE_INFO_PAGE_ADDR,
    FLASH_END_ADDR,
)
from core.trigger_protocol import crc8

PAGE = 2048
CFG_MAGIC = 0x43464748
FLX_MAGIC = 0x464C5848


def _store_region(magic: int, payload_len: int, fill: int = 0x5A) -> bytes:
    """Валидная маркерная область на целое число страниц."""
    hdr = bytearray(20)
    hdr[0:4] = magic.to_bytes(4, "little")
    hdr[4:8] = (1).to_bytes(4, "little")
    hdr[8:12] = payload_len.to_bytes(4, "little")
    hdr[16:18] = (1).to_bytes(2, "little")
    hdr[19] = crc8(bytes(hdr[:19]))
    size = -(-(20 + payload_len) // PAGE) * PAGE
    data = hdr + bytes([fill] * (size - 20))
    return bytes(data)


def _app_metadata(image_size: int, crc: int = 0) -> bytes:
    meta = bytearray(APP_METADATA_PAGE_SIZE)
    meta[0:4] = APP_METADATA_MAGIC.to_bytes(4, "little")
    meta[8:12] = image_size.to_bytes(4, "little")
    meta[12:16] = crc.to_bytes(4, "little")
    return bytes(meta)


class FlashSim:
    """Физически верная модель Flash: erase → 0xFF, write — AND."""

    def __init__(self, size: int = FLASH_END_ADDR - BOOTLOADER_BASE_ADDR):
        self.mem = bytearray(b"\xFF" * size)
        self.base = BOOTLOADER_BASE_ADDR
        self.ops: list[tuple[str, int, int]] = []  # (op, addr, size)

    def read(self, addr: int, length: int) -> bytes:
        off = addr - self.base
        return bytes(self.mem[off : off + length])

    def write(self, addr: int, data: bytes) -> None:
        off = addr - self.base
        self.ops.append(("w", addr, len(data)))
        for i, b in enumerate(data):
            self.mem[off + i] &= b  # flash гасит биты

    def erase(self, addr: int, size: int) -> None:
        off = addr - self.base
        self.ops.append(("e", addr, size))
        self.mem[off : off + size] = b"\xFF" * size


def _bl_on_sim(sim: FlashSim) -> Bootloader:
    bl = Bootloader.__new__(Bootloader)
    bl.port = MagicMock()
    bl._progress_callback = None
    bl._stop_requested = False

    def _read(addr: int, length: int) -> bytes:
        return sim.read(addr, length)

    def _write(addr: int, data: bytes) -> None:
        # Запись в симулятор: flash-семантика AND.
        sim.write(addr, data)

    def _erase(addr: int, data: bytes, page_size: int = PAGE,
               skip_blank: bool = True) -> None:
        end = addr + len(data)
        page = addr - addr % page_size
        while page < end:
            seg_lo = max(page, addr)
            seg_hi = min(page + page_size, end)
            seg = data[seg_lo - addr : seg_hi - addr]
            if not (skip_blank and all(b == 0xFF for b in seg)):
                sim.erase(page, page_size)
            page += page_size

    bl.read_memory = _read  # type: ignore[method-assign]
    bl.write_memory = _write  # type: ignore[method-assign]
    bl.erase_pages = _erase  # type: ignore[method-assign]
    # Хэндшейк AN3155 не нужен: тесты флэш-логики, не транспорта.
    bl.reconfigure_for_bootloader = lambda: None  # type: ignore[method-assign]
    bl.enter_bootloader = lambda _timeout=5.0: None  # type: ignore[method-assign]
    bl.sync = lambda: None  # type: ignore[method-assign]
    return bl


def _app_code(size: int, fill: int = 0x11) -> bytes:
    """Образ application с валидными MSP/reset-векторами — иначе
    guess_firmware_base() для .bin отнесёт его к bootloader-образу."""
    code = bytearray([fill] * size)
    code[0:4] = (0x20010000).to_bytes(4, "little")
    code[4:8] = (APPLICATION_BASE_ADDR | 1).to_bytes(4, "little")
    return bytes(code)


def _flash_app(sim: FlashSim, code: bytes) -> None:
    """Записывает код приложения и валидные метаданные."""
    sim.write(APPLICATION_BASE_ADDR, code)
    sim.erase(APP_METADATA_PAGE_ADDR, PAGE)
    crc = zlib.crc32(code)
    sim.write(APP_METADATA_PAGE_ADDR, _app_metadata(len(code), crc))


def _tmp_bin(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.write(fd, data)
    os.close(fd)
    return path


class TestStoragePreserve:
    def test_storage_survives_code_growth(self) -> None:
        """CFGH якорится за концом кода: рост образа на страницу
        переносит область на новую позицию с сохранением данных."""
        sim = FlashSim()
        old_code = _app_code(3 * PAGE, 0x11)          # код до стр. 3
        old_code_end = APPLICATION_BASE_ADDR + 3 * PAGE
        _flash_app(sim, old_code)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_code_end, PAGE)
        sim.write(old_code_end, cfg_blob)

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        new_code = _app_code(4 * PAGE, 0x22)          # код вырос на страницу
        path = _tmp_bin(new_code)
        try:
            regions = bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)

        new_code_end = APPLICATION_BASE_ADDR + 4 * PAGE
        # Код на месте, CFGH переехал за новый конец кода целиком.
        assert sim.read(APPLICATION_BASE_ADDR, len(new_code)) == new_code
        assert sim.read(new_code_end, len(cfg_blob)) == cfg_blob
        # Старая позиция CFGH затёрта хвостом нового кода.
        assert sim.read(old_code_end, PAGE) == new_code[3 * PAGE:]
        # Метаданные записаны ПОСЛЕДНИМИ и валидны (флаг завершённого
        # обновления): последняя запись — в страницу APP1.
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta == APP_METADATA_MAGIC.to_bytes(4, "little")
        writes = [op for op in sim.ops if op[0] == "w"]
        assert APP_METADATA_PAGE_ADDR <= writes[-1][1] < (
            APP_METADATA_PAGE_ADDR + APP_METADATA_PAGE_SIZE
        )
        # Первая запись — гашение magic APP1 (инвалидация до стирания).
        assert writes[0] == ("w", APP_METADATA_PAGE_ADDR, 4)
        # Регион CFGH вернулся в списке записанных — верификация покроет.
        assert (new_code_end, cfg_blob) in regions

    def test_preserve_disabled_loses_storage(self) -> None:
        sim = FlashSim()
        _flash_app(sim, _app_code(3 * PAGE, 0x11))
        old_code_end = APPLICATION_BASE_ADDR + 3 * PAGE
        cfg_blob = _store_region(CFG_MAGIC, 40)
        sim.erase(old_code_end, PAGE)
        sim.write(old_code_end, cfg_blob)

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(5 * PAGE, 0x22))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=False,
            )
        finally:
            os.unlink(path)
        # Хранилище не сохранялось — новый код затёр страницу.
        assert sim.read(old_code_end, 4) != CFG_MAGIC.to_bytes(
            4, "little")

    def test_read_failure_aborts_before_erase(self) -> None:
        """Сбой чтения хранилища — отказ ДО стирания: код и CFGH живы."""
        sim = FlashSim()
        old_code = _app_code(3 * PAGE, 0x11)
        _flash_app(sim, old_code)
        old_code_end = APPLICATION_BASE_ADDR + 3 * PAGE
        cfg_blob = _store_region(CFG_MAGIC, 40)
        sim.erase(old_code_end, PAGE)
        sim.write(old_code_end, cfg_blob)

        bl = _bl_on_sim(sim)
        real_read = bl.read_memory

        def _failing_read(addr: int, length: int) -> bytes:
            if addr >= old_code_end - PAGE:  # хвост сканирования
                raise OSError("io error")
            return real_read(addr, length)

        bl.read_memory = _failing_read  # type: ignore[method-assign]
        sim.ops.clear()
        path = _tmp_bin(_app_code(5 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    preserve_storage=True,
                )
        finally:
            os.unlink(path)
        # Ничего не стёрто и не записано: приложение валидно.
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(APPLICATION_BASE_ADDR, len(old_code)) == old_code
        assert sim.read(old_code_end, len(cfg_blob)) == cfg_blob
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta == APP_METADATA_MAGIC.to_bytes(4, "little")

    def test_metadata_invalidated_before_code_write(self) -> None:
        """Первая запись в зоне обновления — гашение magic APP1."""
        sim = FlashSim()
        _flash_app(sim, _app_code(2 * PAGE, 0x11))
        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(2 * PAGE, 0x33))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False,
            )
        finally:
            os.unlink(path)
        writes = [op for op in sim.ops if op[0] == "w"]
        assert writes[0] == ("w", APP_METADATA_PAGE_ADDR, 4)

    def test_config_only_update_keeps_metadata(self) -> None:
        """Образ только страницы идентификации (без кода) не гасит APP1
        и не пишет синтетические метаданные поверх чужого образа."""
        sim = FlashSim()
        old_code = _app_code(2 * PAGE, 0x11)
        _flash_app(sim, old_code)
        bl = _bl_on_sim(sim)
        sim.ops.clear()
        info_page = bytearray(b"\xFF" * PAGE)
        info_page[0:4] = (0x43464730).to_bytes(4, "little")  # CFG0
        info_page[1024:1028] = (0x56455231).to_bytes(4, "little")  # VER1
        path = _tmp_bin(bytes(info_page))
        try:
            bl.flash_firmware(
                path, DEVICE_INFO_PAGE_ADDR, page_size=PAGE,
                skip_blank=False,
            )
        finally:
            os.unlink(path)
        # Метаданные приложения не инвалидировались и не перезаписаны.
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta == APP_METADATA_MAGIC.to_bytes(4, "little")
        assert not [
            op for op in sim.ops
            if op[1] == APP_METADATA_PAGE_ADDR
        ]
        # Код приложения не пострадал.
        assert sim.read(APPLICATION_BASE_ADDR, len(old_code)) == old_code
