"""Тесты защиты областей записи и порядка обновления application.

Покрывают пункты чек-листа 1.3/1.5:
- validate_write_region() — границы writable-диапазона AN3155;
- flash_firmware() — инвалидация метаданных до стирания и запись
  метаданных последними («флаг завершённого обновления»).
"""

from __future__ import annotations

import binascii
import struct
from unittest.mock import MagicMock

import pytest

from core.bootloader import Bootloader
from core.firmware_utils import validate_write_region
from core.stm32_info import (
    APP_METADATA_MAGIC,
    APP_METADATA_PAGE_ADDR,
    APP_METADATA_VERSION,
    APPLICATION_BASE_ADDR,
    DEVICE_CONFIG_PAGE_ADDR,
    DEVICE_CONFIG_PAGE_SIZE,
    FLASH_END_ADDR,
    TRIGGER_REGION_ADDR,
    build_app_metadata,
    build_device_config_page,
    merge_device_config_page,
    parse_device_config,
    parse_legacy_device_config,
)


# ---------------------------------------------------------------------------
# validate_write_region — границы из чек-листа 1.5
# ---------------------------------------------------------------------------


def test_write_region_rejects_bootloader_addr() -> None:
    ok, reason = validate_write_region(0x08000000, 256)
    assert not ok
    assert "application" in reason


def test_write_region_allows_app_start() -> None:
    ok, _ = validate_write_region(APPLICATION_BASE_ADDR, 1024)
    assert ok


def test_write_region_allows_config_page() -> None:
    ok, _ = validate_write_region(DEVICE_CONFIG_PAGE_ADDR, DEVICE_CONFIG_PAGE_SIZE)
    assert ok


def test_write_region_rejects_trigger_region() -> None:
    ok, reason = validate_write_region(TRIGGER_REGION_ADDR, 256)
    assert not ok
    assert "триггер" in reason


def test_write_region_rejects_image_overflowing_into_triggers() -> None:
    ok, _ = validate_write_region(APPLICATION_BASE_ADDR, TRIGGER_REGION_ADDR - APPLICATION_BASE_ADDR + 1)
    assert not ok


def test_write_region_rejects_app_image_crossing_config_page() -> None:
    """Application-образ не должен частично перекрывать config-страницу —
    иначе прошивка затрёт имя/serial устройства."""
    size = DEVICE_CONFIG_PAGE_ADDR - APPLICATION_BASE_ADDR + 16
    ok, reason = validate_write_region(APPLICATION_BASE_ADDR, size)
    assert not ok
    assert "config" in reason


def test_write_region_allows_full_app_with_metadata() -> None:
    """Образ code+metadata, заканчивающийся ровно на границе config-страницы."""
    size = DEVICE_CONFIG_PAGE_ADDR - APPLICATION_BASE_ADDR
    ok, _ = validate_write_region(APPLICATION_BASE_ADDR, size)
    assert ok


def test_write_region_rejects_beyond_flash_end() -> None:
    ok, _ = validate_write_region(FLASH_END_ADDR - 4, 8)
    assert not ok


# ---------------------------------------------------------------------------
# build_app_metadata — формат записи APP1 (как в bl_metadata_is_valid)
# ---------------------------------------------------------------------------


def test_build_app_metadata_layout() -> None:
    image = bytes(range(64)) * 4
    meta = build_app_metadata(image)
    assert len(meta) == 16
    magic, version, _reserved, size, crc = struct.unpack("<IHHII", meta)
    assert magic == APP_METADATA_MAGIC
    assert version == APP_METADATA_VERSION
    assert size == len(image)
    assert crc == (binascii.crc32(image) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# flash_firmware — порядок: invalidate → erase → code → metadata
# ---------------------------------------------------------------------------


def _flash_app(image: bytes) -> list[int]:
    """Прогоняет flash_firmware поверх моков и возвращает порядок адресов write_memory."""
    port = MagicMock()
    bl = Bootloader(port)
    bl.reconfigure_for_bootloader = MagicMock()
    bl.enter_bootloader = MagicMock()
    bl.sync = MagicMock()
    bl.erase_pages = MagicMock()
    bl.read_memory = MagicMock(return_value=b"\xFF" * DEVICE_CONFIG_PAGE_SIZE)

    write_addrs: list[int] = []
    bl.write_memory = MagicMock(side_effect=lambda addr, data: write_addrs.append(addr))

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(image)
        tmp_path = tmp.name
    try:
        bl.flash_firmware(tmp_path, base_address=APPLICATION_BASE_ADDR)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return write_addrs


def test_flash_firmware_invalidates_metadata_before_erase() -> None:
    image = b"\xAA" * 1024
    writes = _flash_app(image)

    # Первой записью должна быть инвалидация metadata (UPDATE_STARTED).
    assert writes[0] == APP_METADATA_PAGE_ADDR
    # erase_pages вызван после инвалидации — проверяем через порядок моков
    # косвенно: первый write ещё до любого блока кода (0x08008000).
    assert writes[1] == APPLICATION_BASE_ADDR


def test_flash_firmware_writes_metadata_last_for_bare_image() -> None:
    """Образ без страницы метаданных: ПК синтезирует APP1 и пишет последним."""
    image = b"\x55" * 4096
    writes = _flash_app(image)

    assert writes[-1] == APP_METADATA_PAGE_ADDR
    code_writes = [a for a in writes if APPLICATION_BASE_ADDR <= a < APP_METADATA_PAGE_ADDR]
    assert len(code_writes) == 4096 // Bootloader.BLOCK_SIZE


def test_flash_firmware_image_metadata_goes_last() -> None:
    """Образ, содержащий страницу метаданных, пишет её в самом конце."""
    code = b"\x11" * 2048
    meta = build_app_metadata(code)
    image = code + b"\xFF" * (APP_METADATA_PAGE_ADDR - APPLICATION_BASE_ADDR - len(code)) + meta
    writes = _flash_app(image)

    assert writes[0] == APP_METADATA_PAGE_ADDR  # invalidate
    assert writes[-1] == APP_METADATA_PAGE_ADDR  # финальная запись metadata


def test_flash_firmware_rejects_forbidden_region(tmp_path) -> None:
    port = MagicMock()
    bl = Bootloader(port)
    bl.reconfigure_for_bootloader = MagicMock()
    bl.enter_bootloader = MagicMock()
    bl.sync = MagicMock()
    bl.write_memory = MagicMock()
    bl.erase_pages = MagicMock()

    path = tmp_path / "trigger.bin"
    path.write_bytes(b"\xAA" * 256)
    with pytest.raises(Exception, match="триггер"):
        bl.flash_firmware(str(path), base_address=TRIGGER_REGION_ADDR)
    bl.write_memory.assert_not_called()
    bl.erase_pages.assert_not_called()


# ---------------------------------------------------------------------------
# Миграция config (legacy → новый формат) — чек-лист 13
# ---------------------------------------------------------------------------


def test_legacy_config_migration_to_new_format() -> None:
    """Старый формат name@8..17/serial@18..27 переносится в новый APP1-config
    с валидным CRC8 и дефолтными VID/PID."""
    legacy = bytearray(b"\xFF" * DEVICE_CONFIG_PAGE_SIZE)
    legacy[8:13] = b"GATE1"
    legacy[18:24] = b"SN0001"

    parsed_legacy = parse_legacy_device_config(bytes(legacy))
    assert parsed_legacy == ("GATE1", "SN0001")

    migrated = build_device_config_page(*parsed_legacy)
    parsed = parse_device_config(migrated)
    assert parsed is not None
    assert parsed[0] == "GATE1"
    assert parsed[1] == "SN0001"


def test_merge_config_preserves_unknown_bytes() -> None:
    """merge_device_config_page сохраняет все байты вне 32-байтной записи."""
    existing = bytearray(b"\xFF" * DEVICE_CONFIG_PAGE_SIZE)
    existing[64:80] = bytes(range(16))  # будущие поля
    merged = merge_device_config_page(build_device_config_page("N", "S"), bytes(existing))
    assert merged[64:80] == bytes(range(16))
    assert parse_device_config(merged) is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
