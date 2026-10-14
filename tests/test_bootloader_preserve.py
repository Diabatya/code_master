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

    def test_corrupt_payload_preserved_verbatim(self) -> None:
        """Битый payload CRC: область сохраняется дословно (warning),
        а не «чинится» и не пропадает молча."""
        sim = FlashSim()
        old_end = self._plant(sim, 3)
        cfg_blob = bytearray(_store_region(CFG_MAGIC, 40, fill=0x42))
        cfg_blob[25] ^= 0xFF  # payload бит — заголовок цел
        sim.erase(old_end, PAGE)
        sim.write(old_end, bytes(cfg_blob))
        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x22))
        try:
            bl.flash_firmware(path, APPLICATION_BASE_ADDR, page_size=PAGE,
                              skip_blank=False, preserve_storage=True)
        finally:
            os.unlink(path)
        new_end = APPLICATION_BASE_ADDR + 4 * PAGE
        assert sim.read(new_end, len(cfg_blob)) == bytes(cfg_blob)


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
