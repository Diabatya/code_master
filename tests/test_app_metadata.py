"""Проверка post-build metadata application для bootloader CRC32."""

from __future__ import annotations

import binascii
import subprocess
import sys
from pathlib import Path

from intelhex import IntelHex


ROOT = Path(__file__).resolve().parents[1]
APP_START = 0x08008000
METADATA_ADDR = 0x0803D000


def test_metadata_script_adds_size_and_crc32(tmp_path: Path) -> None:
    source_path = tmp_path / "input.hex"
    output_path = tmp_path / "output.hex"
    data = bytes(range(64))

    source = IntelHex()
    source.puts(APP_START, data.decode("latin1"))
    source.write_hex_file(source_path)

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "firmware/tools/add_app_metadata.py"),
            str(source_path),
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = IntelHex(str(output_path))
    metadata = bytes(result.tobinarray(start=METADATA_ADDR, end=METADATA_ADDR + 15))
    assert int.from_bytes(metadata[0:4], "little") == 0x41505031
    assert int.from_bytes(metadata[4:6], "little") == 1
    assert int.from_bytes(metadata[8:12], "little") == len(data)
    assert int.from_bytes(metadata[12:16], "little") == (binascii.crc32(data) & 0xFFFFFFFF)
