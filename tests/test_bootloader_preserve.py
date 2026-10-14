"""Тесты сохранения маркерного хранилища при обновлении (CM-CDC-01).

Bootloader.flash_firmware с физической симуляцией Flash-массива:
стирание страниц → 0xFF, запись — только гашение битов (как у МК).
Проверяем порядок «инвалидация метаданных → код → хранилище →
метаданные», релокацию CFGH за новым концом кода и отказ до стирания
при нечитаемом хранилище.
"""

from __future__ import annotations

import json
import os
import tempfile
import zlib
from unittest.mock import MagicMock

import pytest

from core import storage_backup
from core.bootloader import Bootloader, BootloaderError
from core.stm32_info import (
    APP_METADATA_MAGIC,
    APP_METADATA_PAGE_ADDR,
    APP_METADATA_PAGE_SIZE,
    APPLICATION_BASE_ADDR,
    BOOTLOADER_BASE_ADDR,
    DEVICE_INFO_PAGE_ADDR,
    EVENT_LOG_ADDR,
    FLASH_END_ADDR,
)
from core.trigger_protocol import crc8

PAGE = 2048
CFG_MAGIC = 0x43464748
FLX_MAGIC = 0x464C5848
VAR_MAGIC = 0x56415248


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path, monkeypatch):
    """Резервы хранилища — в tmp_path, а не в реальный каталог
    пользователя: тесты не должны мусорить в ~/Library/..."""
    monkeypatch.setattr(
        storage_backup, "backup_dir", lambda: tmp_path
    )
    return tmp_path


def _store_region(
    magic: int, payload_len: int, fill: int = 0x5A, gen: int = 1
) -> bytes:
    """Валидная маркерная область на целое число страниц."""
    hdr = bytearray(20)
    hdr[0:4] = magic.to_bytes(4, "little")
    hdr[4:8] = gen.to_bytes(4, "little")
    hdr[8:12] = payload_len.to_bytes(4, "little")
    payload = bytes([fill] * payload_len)
    hdr[12:16] = zlib.crc32(payload).to_bytes(4, "little")
    hdr[16:18] = (1).to_bytes(2, "little")
    hdr[19] = crc8(bytes(hdr[:19]))
    size = -(-(20 + payload_len) // PAGE) * PAGE
    data = hdr + payload + bytes([0xFF] * (size - 20 - payload_len))
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


class TestStoragePlanCMN1:
    """Регрессии CM-N1: цель пересадки не должна стирать живую область,
    которую образ не накрыл; план учитывает всю карту и поколения."""

    @staticmethod
    def _plant(sim: FlashSim, code_pages: int) -> int:
        """Код + метаданные; возвращает конец кода (адрес)."""
        code = _app_code(code_pages * PAGE, 0x11)
        _flash_app(sim, code)
        return APPLICATION_BASE_ADDR + code_pages * PAGE

    def test_cfgh_relocation_does_not_kill_neighbor_flxh(self) -> None:
        """Сценарий ревью: код 3стр + CFGH + FLXH соседние; новый код 4
        стр → CFGH едет на старую страницу FLXH — FLXH обязан пережить."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42, gen=3)
        flx_blob = _store_region(FLX_MAGIC, 60, fill=0x77, gen=5)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, flx_blob)

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(path, APPLICATION_BASE_ADDR, page_size=PAGE,
                              skip_blank=False, preserve_storage=True)
        finally:
            os.unlink(path)

        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        # Побайтово: обе области живы на каноничных позициях.
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        assert sim.read(new_end + PAGE, len(flx_blob)) == flx_blob
        # Маркеры обеих областей видны при повторном скане.
        from core.storage_scan import find_storage_regions

        tail = sim.read(new_end, FLASH_END_ADDR - new_end)
        kinds = {b[:4] for _a, b in find_storage_regions(tail, new_end)}
        assert CFG_MAGIC.to_bytes(4, "little") in kinds
        assert FLX_MAGIC.to_bytes(4, "little") in kinds

    def test_flxh_chain_moves_together(self) -> None:
        """FLXH-цепочка из двух страниц переезжает целиком за CFGH."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        flx_blob = _store_region(FLX_MAGIC, 2500, fill=0x77)  # 2 стр.
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        for i in range(0, len(flx_blob), PAGE):
            sim.erase(old_end + PAGE + i, PAGE)
            sim.write(old_end + PAGE + i, flx_blob[i : i + PAGE])

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(path, APPLICATION_BASE_ADDR, page_size=PAGE,
                              skip_blank=False, preserve_storage=True)
        finally:
            os.unlink(path)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        assert sim.read(new_end + PAGE, len(flx_blob)) == flx_blob

    def test_image_shrink_moves_cfg_down(self) -> None:
        """Уменьшение образа: CFGH пересаживается ВНИЗ за новый конец."""
        sim = FlashSim()
        old_end = self._plant(sim, 5)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(path, APPLICATION_BASE_ADDR, page_size=PAGE,
                              skip_blank=False, preserve_storage=True)
        finally:
            os.unlink(path)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob

    def test_older_generation_dropped_newest_kept(self) -> None:
        """Два поколения CFGH: переносится новейшее, старое — история."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_old = _store_region(CFG_MAGIC, 40, fill=0x42, gen=1)
        cfg_new = _store_region(CFG_MAGIC, 40, fill=0x99, gen=7)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_old)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, cfg_new)
        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(path, APPLICATION_BASE_ADDR, page_size=PAGE,
                              skip_blank=False, preserve_storage=True)
        finally:
            os.unlink(path)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_new)) == cfg_new

    def test_no_room_aborts_before_erase(self) -> None:
        """Хранилище не влезает за образ — отказ до стирания."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        bl = _bl_on_sim(sim)
        sim.ops.clear()
        # Образ до самого верха записываемой зоны — CFGH некуда деть.
        from core.stm32_info import EVENT_LOG_ADDR

        huge = _app_code(EVENT_LOG_ADDR - APPLICATION_BASE_ADDR, 0x22)
        path = _tmp_bin(huge)
        try:
            with pytest.raises(BootloaderError, match="разместить|места"):
                bl.flash_firmware(path, APPLICATION_BASE_ADDR,
                                  page_size=PAGE, preserve_storage=True)
        finally:
            os.unlink(path)
        # Ничего не стёрто — старый код и CFGH живы.
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == cfg_blob

    def test_varh_occupies_full_span_to_flash_end(self) -> None:
        """CM-N8: VARH резервирует [base, FLASH_END) целиком — цель
        FLXH на его base конфликтует, область пересаживается наверх,
        а не остаётся «висеть» под перезаписью."""
        sim = FlashSim()
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        flx_blob = _store_region(FLX_MAGIC, 60, fill=0x77)
        var_blob = _store_region(VAR_MAGIC, 30, fill=0x55)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, flx_blob)
        # VARH по низкому адресу — сразу за FLXH (раскладка после
        # старых обновлений с укороченным хвостом).
        var_addr = old_end + 2 * PAGE
        sim.erase(var_addr, PAGE)
        sim.write(var_addr, var_blob)

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)

        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        # Цели CFGH/FLXH заняли старые адреса CFGH/FLXH/VARH — VARH
        # пересажен на самый верх записываемой зоны и цел.
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        assert sim.read(new_end + PAGE, len(flx_blob)) == flx_blob
        var_dst = EVENT_LOG_ADDR - len(var_blob)
        assert sim.read(var_dst, len(var_blob)) == var_blob
        # Повторный скан видит VARH как валидную область.
        from core.storage_scan import find_storage_regions

        tail = sim.read(new_end, FLASH_END_ADDR - new_end)
        kinds = {b[:4] for _a, b in find_storage_regions(tail, new_end)}
        assert VAR_MAGIC.to_bytes(4, "little") in kinds

    def test_varh_no_room_aborts_before_erase(self) -> None:
        """VARH конфликтует, а места для пересадки нет — честный
        отказ до стирания, а не молчаливая потеря региона."""
        sim = FlashSim()
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        # VARH на 24 страницы — места за огромным образом не хватает.
        var_blob = _store_region(VAR_MAGIC, 24 * PAGE - 20, fill=0x55)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        var_addr = old_end + PAGE
        for i in range(0, len(var_blob), PAGE):
            sim.erase(var_addr + i, PAGE)
            sim.write(var_addr + i, var_blob[i : i + PAGE])

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        # Образ оставляет 20 страниц до write_limit: CFGH + VARH(24)
        # не помещаются — отказ.
        free_pages = 20
        code_pages = (
            EVENT_LOG_ADDR - APPLICATION_BASE_ADDR
        ) // PAGE - free_pages
        path = _tmp_bin(_app_code(code_pages * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="разместить|VARH"):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    preserve_storage=True,
                )
        finally:
            os.unlink(path)
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(var_addr, len(var_blob)) == var_blob

    def test_oldest_varh_generation_dropped_newest_relocated(self) -> None:
        """Два поколения VARH: в план попадает новейшее, старое —
        dropped (актуальные данные не теряются)."""
        sim = FlashSim()
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        var_old = _store_region(VAR_MAGIC, 30, fill=0x11, gen=1)
        var_new = _store_region(VAR_MAGIC, 30, fill=0x99, gen=5)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, var_old)
        # Новейшая копия VARH лежит наверху (каноничная позиция).
        var_top = EVENT_LOG_ADDR - len(var_new)
        sim.erase(var_top, PAGE)
        sim.write(var_top, var_new)

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)
        # Новейшее поколение живо наверху.
        assert sim.read(var_top, len(var_new)) == var_new

    def test_short_read_aborts_before_erase(self) -> None:
        """Короткий ответ read_memory без исключения — неполный дамп."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        bl = _bl_on_sim(sim)
        real_read = bl.read_memory

        def _short_read(addr: int, length: int) -> bytes:
            return real_read(addr, length)[: max(0, length - 10)]

        bl.read_memory = _short_read  # type: ignore[method-assign]
        sim.ops.clear()
        path = _tmp_bin(_app_code(5 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="неполный"):
                bl.flash_firmware(path, APPLICATION_BASE_ADDR,
                                  page_size=PAGE, preserve_storage=True)
        finally:
            os.unlink(path)
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == cfg_blob

    def test_abort_then_retry_restores(self) -> None:
        """Обрыв на записи кода → повторное обновление: CFGH спасён
        из резервного чтения второй попытки (она видит ещё живую
        область — стирание идёт только под образ)."""
        sim = FlashSim()
        # Страница идентификации — источник отпечатка: без неё
        # принадлежность резерва не доказана и восстановление не
        # применяется автоматически (CM-N13).
        page = bytearray(b"\xFF" * PAGE)
        page[0:12] = b"TESTDEVICE01"
        sim.erase(DEVICE_INFO_PAGE_ADDR, PAGE)
        sim.write(DEVICE_INFO_PAGE_ADDR, bytes(page))
        old_end = self._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)

        bl = _bl_on_sim(sim)
        real_erase = bl.erase_pages
        calls = {"n": 0}

        def _dying_erase(addr: int, data: bytes, **kw: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:  # обрыв на стирании кода
                real_erase(addr, data[: len(data) // 2], **kw)
                raise OSError("port lost")
            real_erase(addr, data, **kw)

        bl.erase_pages = _dying_erase  # type: ignore[method-assign]
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(OSError):
                bl.flash_firmware(path, APPLICATION_BASE_ADDR,
                                  page_size=PAGE, preserve_storage=True)
        finally:
            os.unlink(path)
        # Повторная попытка — запись работает; CFGH пережил обрыв.
        bl2 = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl2.flash_firmware(path, APPLICATION_BASE_ADDR,
                               page_size=PAGE, skip_blank=False,
                               preserve_storage=True)
        finally:
            os.unlink(path)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob

    def test_corrupt_payload_aborts_before_erase(self) -> None:
        """Битый payload CRC без подтверждённого резерва (CM-N12):
        явный отказ в «безопасном сохранении» — обновление отменено
        ДО стирания, APP1 не активируется поверх битых данных.
        «Продолжить без данных» — отдельная явная операция
        (preserve_storage=False)."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = bytearray(_store_region(CFG_MAGIC, 40, fill=0x42))
        cfg_blob[25] ^= 0xFF  # payload бит — заголовок цел
        sim.erase(old_end, PAGE)
        sim.write(old_end, bytes(cfg_blob))
        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="не сошёлся"):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    skip_blank=False, preserve_storage=True,
                )
        finally:
            os.unlink(path)
        # Ничего не стёрто и не записано — устройство не тронуто.
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == bytes(cfg_blob)
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta == APP_METADATA_MAGIC.to_bytes(4, "little")


class TestStorageBackupCMN9:
    """Регрессии CM-N9: резерв на ПК переживает обрыв обновления —
    повторная попытка восстанавливает области ЭТОГО устройства по
    отпечатку страницы идентификации."""

    @staticmethod
    def _plant_id_page(sim: FlashSim) -> None:
        """Непустая страница идентификации — источник отпечатка."""
        page = bytearray(b"\xFF" * PAGE)
        page[0:12] = b"TESTDEVICE01"
        sim.erase(DEVICE_INFO_PAGE_ADDR, PAGE)
        sim.write(DEVICE_INFO_PAGE_ADDR, bytes(page))

    def test_abort_during_restore_retry_recovers_from_backup(
        self,
    ) -> None:
        """Обрыв на записи восстановления: код уже на месте, области
        хранилища стёрты. Повторный flash_firmware (новый процесс —
        байтов в памяти нет) поднимает CFGH/FLXH из PC-резерва."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        flx_blob = _store_region(FLX_MAGIC, 60, fill=0x77)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, flx_blob)

        bl = _bl_on_sim(sim)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        real_write = bl.write_memory

        def _dying_write(addr: int, data: bytes) -> None:
            if addr == new_end:
                raise OSError("port lost")
            real_write(addr, data)

        bl.write_memory = _dying_write  # type: ignore[method-assign]
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(OSError):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    skip_blank=False, preserve_storage=True,
                )
        finally:
            os.unlink(path)
        # Обе области в живой Flash потеряны: CFGH затёрт кодом,
        # FLXH — стёртой страницей неудавшегося восстановления.
        from core.storage_scan import find_storage_regions

        tail = sim.read(APPLICATION_BASE_ADDR,
                        FLASH_END_ADDR - APPLICATION_BASE_ADDR)
        assert find_storage_regions(
            tail, APPLICATION_BASE_ADDR
        ) == []

        # Повторная попытка — другой объект Bootloader (байтов резерва
        # в памяти нет): области приходят из файла на ПК.
        bl2 = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            regions = bl2.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        assert sim.read(new_end + PAGE, len(flx_blob)) == flx_blob
        written = {b for _a, b in regions}
        assert cfg_blob in written and flx_blob in written

    def test_backup_write_failure_aborts_before_erase(
        self, monkeypatch
    ) -> None:
        """Сбой записи резерва на ПК — обновление отменяется ДО
        первого стирания: без файла восстановиться нечем."""
        sim = FlashSim()
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)

        def _broken_dump(*_a: object, **_kw: object) -> str:
            raise OSError("disk full")

        monkeypatch.setattr(storage_backup, "dump_backup", _broken_dump)
        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="резерв"):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    preserve_storage=True,
                )
        finally:
            os.unlink(path)
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == cfg_blob

    def test_foreign_device_backup_not_applied(self, tmp_path) -> None:
        """Резерв чужого устройства (другой отпечаток) повторная
        попытка не применяет — чужие настройки не заливаются."""
        sim = FlashSim()
        self._plant_id_page(sim)
        TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        # Прямой резерв «чужого» устройства в общий каталог.
        storage_backup.dump_backup(
            cfg_blob + b"\xFF" * 100,
            APPLICATION_BASE_ADDR,
            [(APPLICATION_BASE_ADDR, cfg_blob)],
            {"other-device-fingerprint"},
            directory=tmp_path,
        )
        live = sim.read(DEVICE_INFO_PAGE_ADDR, PAGE)
        found = storage_backup.find_matching_backup(
            {storage_backup.device_fingerprint(live) or ""},
            directory=tmp_path,
        )
        assert found is None

    def test_healthy_flash_does_not_resurrect_missing_kind(
        self,
    ) -> None:
        """CM-N13: исправная Flash (APP1 валиден) с сознательно
        отсутствующим видом области НЕ получает его из истории —
        резерв применяется только при незавершённой операции."""
        sim = FlashSim()
        TestStorageBackupCMN9._plant_id_page(sim)
        TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        old_end = APPLICATION_BASE_ADDR + 3 * PAGE
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        code = _app_code(4 * PAGE, 0x22)
        # Первое обновление создаёт резерв с VARH в манифесте — имитируем
        # «в резерве есть вид, которого на живой Flash давно нет».
        var_blob = _store_region(VAR_MAGIC, 30, fill=0x55)
        var_addr = FLASH_END_ADDR - 2 * PAGE
        tail_bytes = bytearray(b"\xFF" * (var_addr + 2 * PAGE
                                          - APPLICATION_BASE_ADDR))
        tail_bytes[3 * PAGE : 4 * PAGE] = cfg_blob
        tail_bytes[var_addr - APPLICATION_BASE_ADDR:] = var_blob
        storage_backup.dump_backup(
            bytes(tail_bytes),
            APPLICATION_BASE_ADDR,
            [(old_end, cfg_blob), (var_addr, var_blob)],
            {storage_backup.device_fingerprint(
                sim.read(DEVICE_INFO_PAGE_ADDR, PAGE)) or ""},
            image_sha256=__import__("hashlib").sha256(code).hexdigest(),
        )
        bl = _bl_on_sim(sim)
        path = _tmp_bin(code)
        try:
            regions = bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)
        # VARH не воскрешён — вид отсутствовал на здоровой Flash.
        written_kinds = {
            storage_backup.region_kind(b) for _a, b in regions
        }
        assert "var" not in written_kinds
        tail = sim.read(APPLICATION_BASE_ADDR,
                        FLASH_END_ADDR - APPLICATION_BASE_ADDR)
        from core.storage_scan import find_storage_regions
        assert "var" not in {
            storage_backup.region_kind(b)
            for _a, b in find_storage_regions(tail, APPLICATION_BASE_ADDR)
        }


class TestPartialWriteRecoveryCMN12:
    """CM-N12: обрыв ПОСРЕДИ записи области — заголовок с валидным
    CRC8 записан, payload CRC32 не сошёлся. Такой блок не должен
    вытеснять валидную копию из подтверждённого резерва."""

    @staticmethod
    def _plant_id_page(sim: FlashSim) -> None:
        page = bytearray(b"\xFF" * PAGE)
        page[0:12] = b"TESTDEVICE01"
        sim.erase(DEVICE_INFO_PAGE_ADDR, PAGE)
        sim.write(DEVICE_INFO_PAGE_ADDR, bytes(page))

    def test_partial_region_write_retry_restores_from_backup(
        self,
    ) -> None:
        """Сценарий ревью: код 3 стр + CFGH + FLXH; обновление до 4 стр;
        первая попытка создаёт резерв, стирает и пишет CFGH на 32
        байта, затем обрыв. Повторная попытка видит «живой» CFGH с
        валидным заголовком и битым payload — обязана восстановить
        валидный блоб из резерва и довести APP1 до валидного."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 80, fill=0x42)
        flx_blob = _store_region(FLX_MAGIC, 80, fill=0x77)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        sim.erase(old_end + PAGE, PAGE)
        sim.write(old_end + PAGE, flx_blob)

        code = _app_code(4 * PAGE, 0x22)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        bl = _bl_on_sim(sim)
        real_write = bl.write_memory
        failed = {"done": False}

        def _dying_write(addr: int, data: bytes) -> None:
            if addr == new_end and not failed["done"]:
                # Первые 32 байта — заголовок (CRC8 валиден) + кусок
                # payload; CRC32 полного payload не сойдётся.
                real_write(addr, data[:32])
                failed["done"] = True
                raise OSError("port lost")
            real_write(addr, data)

        bl.write_memory = _dying_write  # type: ignore[method-assign]
        path = _tmp_bin(code)
        try:
            with pytest.raises(OSError):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    skip_blank=False, preserve_storage=True,
                )
        finally:
            os.unlink(path)

        # Состояние после обрыва: CFGH виден сканеру (валидный CRC8),
        # но payload бит; FLXH стёрт кодом/стиранием.
        from core.storage_scan import (
            find_storage_regions,
            region_kind,
            region_payload_ok,
        )
        tail = sim.read(
            APPLICATION_BASE_ADDR,
            FLASH_END_ADDR - APPLICATION_BASE_ADDR,
        )
        live = find_storage_regions(tail, APPLICATION_BASE_ADDR)
        kinds = {region_kind(b): region_payload_ok(b) for _a, b in live}
        assert kinds.get("cfg") is False  # «живой», но битый
        assert "flex" not in kinds
        # APP1 инвалиден — операция не завершена.
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta != APP_METADATA_MAGIC.to_bytes(4, "little")

        # Повторная попытка: валидные блобы из PC-резерва, readback.
        bl2 = _bl_on_sim(sim)
        path = _tmp_bin(code)
        try:
            regions = bl2.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        assert sim.read(new_end + PAGE, len(flx_blob)) == flx_blob
        written = {b for _a, b in regions}
        assert cfg_blob in written and flx_blob in written
        meta = sim.read(APP_METADATA_PAGE_ADDR, 4)
        assert meta == APP_METADATA_MAGIC.to_bytes(4, "little")

    def test_corrupt_live_newer_generation_still_restored(
        self,
    ) -> None:
        """Битая живая область со СТАРШИМ поколением не вытесняет
        валидный резерв младшего — целостность важнее поколения."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 80, fill=0x42, gen=2)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)

        code = _app_code(4 * PAGE, 0x22)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        # Резерв содержит валидный gen=2; живая копия после обрыва —
        # gen=3 с битым payload (более новая запись оборвалась).
        fp = storage_backup.device_fingerprint(
            sim.read(DEVICE_INFO_PAGE_ADDR, PAGE)
        ) or ""
        tail_bytes = bytearray(
            b"\xFF" * (old_end - APPLICATION_BASE_ADDR + PAGE)
        )
        tail_bytes[old_end - APPLICATION_BASE_ADDR:] = cfg_blob
        storage_backup.dump_backup(
            bytes(tail_bytes),
            APPLICATION_BASE_ADDR,
            [(old_end, cfg_blob)],
            {fp},
            image_sha256=__import__("hashlib").sha256(code).hexdigest(),
        )
        # Живая Flash: CFGH gen=3, заголовок валиден, payload бит.
        corrupt = bytearray(_store_region(CFG_MAGIC, 80, fill=0x33, gen=3))
        corrupt[25] ^= 0xFF
        sim.erase(old_end, PAGE)
        sim.write(old_end, bytes(corrupt))
        # APP1 инвалиден — операция не завершена.
        sim.erase(APP_METADATA_PAGE_ADDR, PAGE)

        bl = _bl_on_sim(sim)
        path = _tmp_bin(code)
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
            )
        finally:
            os.unlink(path)
        # Победил валидный резерв gen=2, а не битый gen=3.
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob


class TestBackupOwnershipCMN13:
    """CM-N13: резерв применяется только при доказанной
    принадлежности — отпечаток устройства + образ операции."""

    def test_anonymous_backup_never_auto_applied(self, tmp_path) -> None:
        """Анонимный резерв (плата A без страницы идентификации) не
        применяется автоматически — ни на неопознанном устройстве,
        ни на устройстве B с отпечатком, даже будучи единственным
        файлом в каталоге."""
        cfg = _store_region(CFG_MAGIC, 40, fill=0x42)
        storage_backup.dump_backup(
            cfg + b"\xFF" * 100, APPLICATION_BASE_ADDR,
            [(APPLICATION_BASE_ADDR, cfg)],
            set(),  # анонимный резерв
            directory=tmp_path,
        )
        # Неопознанное устройство: отпечатков нет — «единственный
        # файл» не является доказательством принадлежности.
        assert storage_backup.find_matching_backup(
            set(), directory=tmp_path
        ) is None
        # Устройство B с непустым отпечатком — тоже отказ.
        assert storage_backup.find_matching_backup(
            {"fp-board-B"}, directory=tmp_path
        ) is None
        # Явный выбор файла оператором — работает (целостность
        # проверяется всегда).
        manifest = storage_backup.list_backup_manifests(tmp_path)[0]
        regions = storage_backup.load_backup_file(manifest)
        assert regions is not None
        blobs = {storage_backup.region_kind(b) for _a, b in regions}
        assert "cfg" in blobs

    def test_other_image_backup_rejected(self, tmp_path) -> None:
        """Резерв от другой операции (другой образ) не применяется
        даже при совпадающем отпечатке устройства."""
        cfg = _store_region(CFG_MAGIC, 40, fill=0x42)
        storage_backup.dump_backup(
            cfg + b"\xFF" * 100, APPLICATION_BASE_ADDR,
            [(APPLICATION_BASE_ADDR, cfg)],
            {"fp-dev"},
            image_sha256="aa" * 32,
            directory=tmp_path,
        )
        # Тот же образ — находится.
        assert storage_backup.find_matching_backup(
            {"fp-dev"}, image_sha256="aa" * 32, directory=tmp_path
        ) is not None
        # Другой образ операции — отказ.
        assert storage_backup.find_matching_backup(
            {"fp-dev"}, image_sha256="bb" * 32, directory=tmp_path
        ) is None

    def test_corrupt_inline_blob_rejected(self, tmp_path) -> None:
        """Inline blob_b64 с битым sha256 отклоняется так же строго,
        как повреждённый внешний .bin (паритет целостности)."""
        cfg = _store_region(CFG_MAGIC, 40, fill=0x42)
        var = _store_region(VAR_MAGIC, 30, fill=0x55)
        storage_backup.dump_backup(
            cfg + b"\xFF" * 100, APPLICATION_BASE_ADDR,
            [(APPLICATION_BASE_ADDR, cfg),
             (APPLICATION_BASE_ADDR + PAGE, var)],
            {"fp"}, restored_blobs={"var": var},
            directory=tmp_path,
        )
        manifest = storage_backup.list_backup_manifests(tmp_path)[0]
        m = json.loads(manifest.read_text(encoding="utf-8"))
        import base64
        for r in m["regions"]:
            if r.get("origin") == "backup":
                raw = bytearray(base64.b64decode(r["blob_b64"]))
                raw[25] ^= 0xFF
                r["blob_b64"] = base64.b64encode(bytes(raw)).decode()
        manifest.write_text(json.dumps(m), encoding="utf-8")
        assert storage_backup.find_matching_backup(
            {"fp"}, directory=tmp_path
        ) is None

    def test_merge_healthy_live_never_crashes(self) -> None:
        """Здоровая живая область + подходящий резерв при завершённой
        операции: merge ничего не добавляет и НЕ падает
        (ветка «живое валидно, резерв не новее» — раньше UnboundLocalError
        на неинициализированном need). Перепрошивка того же образа —
        частый путь, где резерв всегда находится по image_sha256."""
        cfg = _store_region(CFG_MAGIC, 40, fill=0x42)
        var = _store_region(VAR_MAGIC, 30, fill=0x55)
        live = [(APPLICATION_BASE_ADDR, cfg)]
        bak = [(APPLICATION_BASE_ADDR + PAGE, cfg),
               (APPLICATION_BASE_ADDR + 2 * PAGE, var)]
        merged, restored = storage_backup.merge_backup_regions(
            list(live), bak, interrupted=False,
        )
        assert restored == {}
        assert merged == live
        # Незавершённая операция: отсутствующий вид поднимается,
        # здоровый живой того же поколения не дублируется.
        merged2, restored2 = storage_backup.merge_backup_regions(
            list(live), bak, interrupted=True,
        )
        assert set(restored2) == {"var"}
        kinds = [storage_backup.region_kind(b) for _, b in merged2]
        assert kinds.count("cfg") == 1
        assert kinds.count("var") == 1
        # Незавершённая операция + резерв новее живого — дописываем.
        cfg_new = _store_region(CFG_MAGIC, 40, fill=0x77, gen=2)
        _m3, restored3 = storage_backup.merge_backup_regions(
            list(live),
            [(APPLICATION_BASE_ADDR + PAGE, cfg_new)],
            interrupted=True,
        )
        assert set(restored3) == {"cfg"}


class TestExplicitBackupCMN17:
    """CM-N17: явно выбранный оператором резерв идёт тем же pipeline
    (merge → план → стирание → запись → readback), но принадлежность
    подтверждает выбор оператора — поэтому недостающие виды
    восстанавливаются и на исправной Flash без признака прерванной
    операции. Непригодный резерв — отказ до первого стирания."""

    @staticmethod
    def _plant_id_page(sim: FlashSim) -> None:
        page = bytearray(b"\xFF" * PAGE)
        page[0:12] = b"TESTDEVICE01"
        sim.erase(DEVICE_INFO_PAGE_ADDR, PAGE)
        sim.write(DEVICE_INFO_PAGE_ADDR, bytes(page))

    def test_explicit_backup_restores_missing_kind(self) -> None:
        """Исправная Flash (APP1 валиден, незавершённости нет), VARH
        отсутствует — автоматика его не воскресила бы (CM-N13), но
        явно выбранный оператором резерв — команда на восстановление."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        var_blob = _store_region(VAR_MAGIC, 30, fill=0x55)
        var_src = FLASH_END_ADDR - 2 * PAGE

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            regions = bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
                explicit_backup=[(var_src, var_blob)],
            )
        finally:
            os.unlink(path)
        # VARH записан на канонический якорь storage.c, CFGH за кодом.
        from core.storage_scan import find_storage_regions
        tail = sim.read(APPLICATION_BASE_ADDR,
                        FLASH_END_ADDR - APPLICATION_BASE_ADDR)
        live = dict(find_storage_regions(tail, APPLICATION_BASE_ADDR))
        kinds = {a: storage_backup.region_kind(b) for a, b in live.items()}
        assert "var" in kinds.values()
        assert sim.read(FLASH_END_ADDR - 18 * 1024, len(var_blob)) == var_blob
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob
        written = {b for _a, b in regions}
        assert var_blob in written and cfg_blob in written

    def test_explicit_backup_enables_preserve_without_checkbox(self) -> None:
        """explicit_backup сам включает сохранение хранилища:
        «восстановить данные» — это preserve-операция, состояние
        чекбокса не должно молча отменять выбор оператора."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        var_blob = _store_region(VAR_MAGIC, 30, fill=0x55)

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=False,
                explicit_backup=[(FLASH_END_ADDR - 2 * PAGE, var_blob)],
            )
        finally:
            os.unlink(path)
        from core.storage_scan import find_storage_regions
        tail = sim.read(APPLICATION_BASE_ADDR,
                        FLASH_END_ADDR - APPLICATION_BASE_ADDR)
        kinds = {
            storage_backup.region_kind(b)
            for _a, b in find_storage_regions(tail, APPLICATION_BASE_ADDR)
        }
        assert "var" in kinds and "cfg" in kinds
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == cfg_blob

    def test_explicit_backup_corrupt_payload_aborts_before_erase(
        self,
    ) -> None:
        """Резерв с битым payload CRC32 — отказ до первого стирания:
        «частичное восстановление» выдавало бы повреждённое за
        восстановленное."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        corrupt = bytearray(_store_region(VAR_MAGIC, 30, fill=0x55))
        corrupt[25] ^= 0xFF  # заголовок цел, payload бит

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="непригодна"):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    skip_blank=False, preserve_storage=True,
                    explicit_backup=[
                        (FLASH_END_ADDR - 2 * PAGE, bytes(corrupt))
                    ],
                )
        finally:
            os.unlink(path)
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == cfg_blob

    def test_explicit_backup_no_room_aborts_before_erase(self) -> None:
        """Резерв, который физически не размещается под новым образом —
        отказ до стирания, устройство не тронуто."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_blob = _store_region(CFG_MAGIC, 40, fill=0x42)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_blob)
        # VARH в сто страниц: зазор 4-страничного образа его не вмещает.
        huge_var = _store_region(VAR_MAGIC, 100 * PAGE, fill=0x55)

        bl = _bl_on_sim(sim)
        sim.ops.clear()
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            with pytest.raises(BootloaderError, match="разместить"):
                bl.flash_firmware(
                    path, APPLICATION_BASE_ADDR, page_size=PAGE,
                    skip_blank=False, preserve_storage=True,
                    explicit_backup=[
                        (FLASH_END_ADDR - 100 * PAGE, huge_var)
                    ],
                )
        finally:
            os.unlink(path)
        assert not [op for op in sim.ops if op[0] == "e"]
        assert sim.read(old_end, len(cfg_blob)) == cfg_blob

    def test_explicit_backup_does_not_downgrade_healthy_newer(
        self,
    ) -> None:
        """Явный резерв восстанавливает отсутствующие/повреждённые виды,
        но не откатывает живую копию со СТАРШИМ поколением — полный
        откат настроек делается стиранием, а не «тихим» затираем
        более свежей конфигурации."""
        sim = FlashSim()
        self._plant_id_page(sim)
        old_end = TestStoragePlanCMN1._plant(sim, 3)
        cfg_new = _store_region(CFG_MAGIC, 40, fill=0x42, gen=5)
        sim.erase(old_end, PAGE)
        sim.write(old_end, cfg_new)
        cfg_old = _store_region(CFG_MAGIC, 40, fill=0x99, gen=2)

        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=True,
                explicit_backup=[(old_end, cfg_old)],
            )
        finally:
            os.unlink(path)
        # Живое gen=5 осталось выбранным (целостность+поколение) —
        # резерв gen=2 его не заменил.
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_new)) == cfg_new

    def test_restore_button_cancel_does_not_flash(
        self, monkeypatch
    ) -> None:
        """Отмена на любом шаге выбора (список, подтверждение, битый
        файл) — _on_flash не вызывается: ни чтения устройства, ни
        стирания, ни записи не происходит."""
        from ui.flash_dialog import FlashDialog

        fake = MagicMock()
        fake._pick_backup_manifest.return_value = None  # любой отказ
        FlashDialog._on_restore_from_backup(fake)
        fake._on_flash.assert_not_called()
        fake._log.assert_not_called()

    def test_restore_button_confirm_routes_to_flash(
        self, monkeypatch
    ) -> None:
        """Подтверждённый выбор передаёт области резерва в общий
        _on_flash — не обходной путь и не молчаливый даунгрейд до
        обычной прошивки."""
        from ui.flash_dialog import FlashDialog

        var_blob = _store_region(VAR_MAGIC, 30, fill=0x55)
        picked = ([(0x0803E000, var_blob)], "storage-20250101.json")
        fake = MagicMock()
        fake._pick_backup_manifest.return_value = picked
        FlashDialog._on_restore_from_backup(fake)
        fake._on_flash.assert_called_once_with(
            explicit_backup_regions=picked[0]
        )
