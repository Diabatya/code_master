"""Тесты core/firmware_utils.py: fallback-парсер Intel HEX (_parse_intel_hex)
должен отклонять строки с неверной контрольной суммой, а не молча
принимать их (аудит #2)."""

import pytest

from core.firmware_utils import _parse_intel_hex


def _hex_line(count: int, addr: int, rtype: int, payload: bytes, checksum: int) -> str:
    return f":{count:02X}{addr:04X}{rtype:02X}{payload.hex().upper()}{checksum:02X}"


def test_parse_intel_hex_accepts_valid_checksum() -> None:
    payload = bytes([0x11, 0x22, 0x33, 0x44])
    # checksum = -(count + addr_hi + addr_lo + rtype + sum(payload)) & 0xFF
    checksum = (-(4 + 0x08 + 0x00 + 0x00 + sum(payload))) & 0xFF
    line = _hex_line(4, 0x0800, 0x00, payload, checksum)
    data, base = _parse_intel_hex(line + "\n:00000001FF\n")
    assert base == 0x0800
    assert data == payload


def test_parse_intel_hex_rejects_corrupted_checksum() -> None:
    payload = bytes([0x11, 0x22, 0x33, 0x44])
    checksum = (-(4 + 0x08 + 0x00 + 0x00 + sum(payload))) & 0xFF
    bad_checksum = (checksum + 1) & 0xFF  # намеренно испорчен
    line = _hex_line(4, 0x0800, 0x00, payload, bad_checksum)
    with pytest.raises(ValueError, match="контрольная сумма"):
        _parse_intel_hex(line + "\n:00000001FF\n")
