#!/usr/bin/env python3
"""Append CodeMaster application integrity metadata to Intel HEX/BIN outputs."""

from __future__ import annotations

import binascii
import sys
from pathlib import Path

from intelhex import IntelHex

APP_START = 0x08008000
APP_METADATA_ADDR = 0x0803D000
APP_METADATA_SIZE = 2048
APP_METADATA_MAGIC = 0x41505031  # "APP1"
APP_METADATA_VERSION = 1


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(f"usage: {sys.argv[0]} input.hex output.hex [output.bin]", file=sys.stderr)
        return 2

    source = IntelHex(sys.argv[1])
    segments = source.segments()
    if not segments:
        raise SystemExit("application HEX is empty")
    if min(start for start, _ in segments) < APP_START:
        raise SystemExit("application HEX contains data before APP_START")

    image_end = max(end for _, end in segments)
    if image_end > APP_METADATA_ADDR:
        raise SystemExit("application overlaps the metadata page")
    image_size = image_end - APP_START
    image = bytes(source.tobinarray(start=APP_START, end=image_end - 1))
    crc32 = binascii.crc32(image) & 0xFFFFFFFF

    metadata = bytearray(b"\xFF" * APP_METADATA_SIZE)
    metadata[0:4] = APP_METADATA_MAGIC.to_bytes(4, "little")
    metadata[4:6] = APP_METADATA_VERSION.to_bytes(2, "little")
    metadata[6:8] = b"\x00\x00"
    metadata[8:12] = image_size.to_bytes(4, "little")
    metadata[12:16] = crc32.to_bytes(4, "little")
    source.puts(APP_METADATA_ADDR, bytes(metadata).decode("latin1"))
    source.write_hex_file(sys.argv[2])
    if len(sys.argv) == 4:
        binary_end = APP_METADATA_ADDR + APP_METADATA_SIZE
        Path(sys.argv[3]).write_bytes(
            bytes(source.tobinarray(start=APP_START, end=binary_end - 1))
        )

    print(
        f"application metadata: start=0x{APP_START:08X}, "
        f"size={image_size}, crc32=0x{crc32:08X}, "
        f"metadata=0x{APP_METADATA_ADDR:08X}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
