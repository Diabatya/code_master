#!/usr/bin/env python3
"""Собирает единый образ Flash: bootloader + application (+ metadata).

Результат — codemaster_full.hex / codemaster_full.bin, покрывающие
0x08000000..0x0803D7FF. Оператор прошивает пустой МК одним файлом через
DFU, а конфигурацию (имя/серийный номер) приложение дописывает на
config-страницу 0x0803D800 тем же сеансом (см. _prepare_firmware_with_config
в ui/flash_dialog.py).

Если передан --version, в начало config-страницы пишется версионная
запись «VER1» с номером релиза — приложение она нужна, чтобы устройство
могло отчитаться о версии ПО; оператор в окне прошивки может переписать
её вручную.

Использование:
    python3 make_full_image.py <bootloader.hex> <app.hex> <out.hex> [out.bin] [--version 1.2.3]
"""

import sys
from pathlib import Path

from intelhex import IntelHex

# core/ лежит у корня репозитория — добавляем его в путь, чтобы
# разметка версионной записи не дублировалась.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    version = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--version" and i + 1 < len(sys.argv):
            version = sys.argv[i + 1].strip().lstrip("v")
            break

    if len(argv) < 3:
        print(__doc__)
        return 1

    boot_path, app_path, out_hex = argv[:3]
    image = IntelHex(boot_path)
    app = IntelHex(app_path)
    # У обоих файлов разные records стартового адреса — при объединении
    # сбрасываем, входная точка не нужна для прошивки по адресам.
    image.start_addr = None
    app.start_addr = None
    image.merge(app, overlap="error")

    if version:
        from core.stm32_info import (
            DEVICE_CONFIG_PAGE_ADDR,
            DEVICE_CONFIG_VER_OFFSET,
            build_device_config_page,
        )

        # Имя/серийник оператор задаст при прошивке — в образ кладём
        # только версионную запись (смещение 32), первый блок страницы
        # остаётся стёртым (0xFF), иначе устройство прочитало бы
        # валидную конфиг-запись с пустыми именем и серийником.
        page = build_device_config_page("", "", fw_version=version)
        image.puts(
            DEVICE_CONFIG_PAGE_ADDR + DEVICE_CONFIG_VER_OFFSET,
            page[DEVICE_CONFIG_VER_OFFSET : DEVICE_CONFIG_VER_OFFSET + 32],
        )
        print(f"full image: версия ПО прошивки v{version}", flush=True)

    out_hex_path = Path(out_hex)
    image.write_hex_file(out_hex_path)

    if len(argv) > 3:
        image.tofile(argv[3], format="bin")

    segments = list(image.segments())
    total = sum(end - start for start, end in segments)
    print(
        f"full image: {len(segments)} segments, {total} bytes -> {out_hex_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
