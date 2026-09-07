"""Юнит-тесты протокольной логики core/bootloader.py без реального COM-порта.

Не открывают настоящий serial.Serial: используют unittest.mock, чтобы
проверить erase_pages()/skip_blank и Bootloader.open() отдельно от
физического порта — см. CURSOR_FIX_PROMPT.md пункты 3.2 и 3.3.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.bootloader import ACK, Bootloader
from core.firmware_utils import validate_application_vector
from core.stm32_info import (
    APPLICATION_BASE_ADDR,
    BOOTLOADER_BASE_ADDR,
    DEVICE_CONFIG_PAGE_SIZE,
    build_device_config_page,
    merge_device_config_page,
    parse_device_config,
    device_config_crc8,
)


def test_application_vector_preflight_rejects_invalid_images() -> None:
    valid = bytearray(8)
    valid[0:4] = (0x20001000).to_bytes(4, "little")
    valid[4:8] = (APPLICATION_BASE_ADDR | 1).to_bytes(4, "little")
    assert validate_application_vector(bytes(valid), APPLICATION_BASE_ADDR)[0]

    invalid = bytearray(valid)
    invalid[4:8] = (0x0803D000).to_bytes(4, "little")
    ok, reason = validate_application_vector(bytes(invalid), APPLICATION_BASE_ADDR)
    assert not ok
    assert "Reset vector" in reason


def test_device_config_page_uses_firmware_layout_and_preserves_fields() -> None:
    existing = bytearray(build_device_config_page("OLD", "123"))
    existing[25:27] = (0x1234).to_bytes(2, "little")
    existing[27:29] = (0x5678).to_bytes(2, "little")
    existing[31] = device_config_crc8(existing[:31])

    updated = merge_device_config_page(
        build_device_config_page("NEW", "456"), bytes(existing)
    )
    parsed = parse_device_config(updated)
    assert parsed is not None
    assert parsed[:2] == ("NEW", "456")
    assert parsed[2:] == (0x1234, 0x5678)
    assert len(updated) == DEVICE_CONFIG_PAGE_SIZE


def _make_bootloader() -> tuple[Bootloader, MagicMock]:
    """Создаёт Bootloader поверх MagicMock-порта (без реального I/O)."""
    port = MagicMock()
    port.timeout = 1.0
    port.read.return_value = bytes([ACK])
    bl = Bootloader(port)
    return bl, port


def test_open_uses_bootloader_uart_settings() -> None:
    """Bootloader.open() должен открывать порт с параметрами AN3155
    (8E1) одинаково для всех вызывающих мест (GUI/CLI)."""
    with patch("core.bootloader.serial.Serial") as serial_ctor:
        fake_port = MagicMock()
        serial_ctor.return_value = fake_port

        bl = Bootloader.open("COM3", baudrate=9600, timeout=2.0)

        serial_ctor.assert_called_once()
        _, kwargs = serial_ctor.call_args
        assert serial_ctor.call_args[0][0] == "COM3"
        assert serial_ctor.call_args[0][1] == 9600
        assert kwargs["timeout"] == 2.0
        assert bl.port is fake_port


def test_erase_pages_skips_blank_pages_by_default() -> None:
    """Страницы, чьи будущие данные полностью 0xFF, не должны стираться
    при skip_blank=True (значение по умолчанию)."""
    bl, port = _make_bootloader()

    page_size = 1024
    data = bytearray(b"\xFF" * (page_size * 3))
    # Только вторая страница содержит непустые данные.
    data[page_size + 10] = 0xAB

    bl.erase_pages(BOOTLOADER_BASE_ADDR, bytes(data), page_size=page_size)

    # send_command(0x44) + extended-erase payload with the page list.
    written = b"".join(call.args[0] for call in port.write.call_args_list)
    # Page 1 (index 1) should be present in the erase payload.
    assert bytes([0x00, 0x01]) in written


def test_erase_pages_skip_blank_false_erases_all_pages() -> None:
    """При skip_blank=False должны стираться все страницы диапазона, даже
    если записываемые в них данные — сплошные 0xFF (полный образ Flash)."""
    bl, port = _make_bootloader()

    page_size = 1024
    data = bytes(b"\xFF" * (page_size * 3))

    bl.erase_pages(BOOTLOADER_BASE_ADDR, data, page_size=page_size, skip_blank=False)

    written = b"".join(call.args[0] for call in port.write.call_args_list)
    # n = 2 (3 pages - 1), followed by page indices 0,1,2.
    assert bytes([0x00, 0x02]) in written  # n encoded big-endian
    for page_index in (0, 1, 2):
        assert bytes([0x00, page_index]) in written


def test_erase_pages_no_pages_does_not_send_command() -> None:
    """Если все данные 0xFF и skip_blank=True, команда стирания вообще не
    должна отправляться на порт (нечего стирать)."""
    bl, port = _make_bootloader()
    data = bytes(b"\xFF" * 2048)

    bl.erase_pages(BOOTLOADER_BASE_ADDR, data, page_size=1024)

    port.write.assert_not_called()


def test_flash_firmware_default_base_is_application_base_addr() -> None:
    """flash_firmware() без явного base_address в файле должен использовать
    APPLICATION_BASE_ADDR (0x08008000), не адрес бутлоадера, чтобы UART/USB
    CDC-прошивка по умолчанию не затирала сам bootloader."""
    import inspect

    sig = inspect.signature(Bootloader.flash_firmware)
    assert sig.parameters["base_address"].default == APPLICATION_BASE_ADDR


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
