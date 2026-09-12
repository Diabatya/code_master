#!/usr/bin/env python3
"""Собирает единый образ Flash: bootloader + application (+ metadata).

Результат — codemaster_full.hex / codemaster_full.bin, покрывающие
0x08000000..0x0803D7FF. Оператор прошивает пустой МК одним файлом через
DFU, а конфигурацию (имя/серийный номер) приложение дописывает на
config-страницу 0x0803D800 тем же сеансом (см. _prepare_firmware_with_config
в ui/flash_dialog.py).

Использование:
    python3 make_full_image.py <bootloader.hex> <app.hex> <out.hex> [out.bin]
"""

import sys
from pathlib import Path

from intelhex import IntelHex


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 1

    boot_path, app_path, out_hex = sys.argv[1:4]
    image = IntelHex(boot_path)
    app = IntelHex(app_path)
    # У обоих файлов разные records стартового адреса — при объединении
    # сбрасываем, входная точка не нужна для прошивки по адресам.
    image.start_addr = None
    app.start_addr = None
    image.merge(app, overlap="error")

    out_hex_path = Path(out_hex)
    image.write_hex_file(out_hex_path)

    if len(sys.argv) > 4:
        image.tofile(sys.argv[4], format="bin")

    segments = list(image.segments())
    total = sum(end - start for start, end in segments)
    print(
        f"full image: {len(segments)} segments, {total} bytes -> {out_hex_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
