#!/usr/bin/env python3
"""Собирает образы Flash для релиза: полный (DFU) и app-only (CDC).

Полный образ (out.hex/out.bin) покрывает 0x08000000..0x0803FFFF:
bootloader + страница данных устройства (0x08008000, тип/s/n/версия) +
страница метаданных application (0x08008800, APP1) + код application
(0x08009000+). Оператор прошивает пустой МК одним файлом через DFU —
это релизный артефакт «2 CAN DFU».

Флаги --cdc-hex/--cdc-bin дополнительно выписывают образ без области
загрузчика: версионная запись на странице данных устройства, метаданные
и код application. Он обновляет прошивку кнопкой «Обновить» (USB CDC /
UART) на устройствах, где загрузчик уже установлен — артефакт
«2 CAN CDC».

Если передан --version, в страницу данных устройства (смещение DEVICE_CONFIG_VER_OFFSET)
пишется версионная запись «VER1» с номером релиза — приложение она
нужна, чтобы устройство могло отчитаться о версии ПО. Первый блок
страницы (имя/s/n) остаётся стёртым (0xFF): при записи через
flash_firmware страница объединяется с существующей на устройстве,
поэтому идентичность не затирается, а версия обновляется.

Использование:
    python3 make_full_image.py <bootloader.hex> <app.hex> <out.hex> [out.bin]
        [--cdc-hex path] [--cdc-bin path] [--version 1.2.3]
"""

import argparse
import sys
from pathlib import Path

from intelhex import IntelHex

# core/ лежит у корня репозитория — добавляем его в путь, чтобы
# разметка версионной записи не дублировалась.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bootloader_hex")
    parser.add_argument("app_hex")
    parser.add_argument("out_hex")
    parser.add_argument("out_bin", nargs="?")
    parser.add_argument("--cdc-hex", default="", dest="cdc_hex")
    parser.add_argument("--cdc-bin", default="", dest="cdc_bin")
    parser.add_argument("--version", default="")
    args = parser.parse_args()

    image = IntelHex(args.bootloader_hex)
    app = IntelHex(args.app_hex)
    # У обоих файлов разные records стартового адреса — при объединении
    # сбрасываем, входная точка не нужна для прошивки по адресам.
    image.start_addr = None
    app.start_addr = None
    image.merge(app, overlap="error")

    ver_fragment = b""
    version = args.version.strip().lstrip("v")
    if version:
        from core.stm32_info import (
            DEVICE_CONFIG_PAGE_ADDR,
            DEVICE_CONFIG_VER_OFFSET,
            build_device_config_page,
        )

        # Имя/серийник оператор задаст при прошивке — в образ кладём
        # только версионную запись (VER_OFFSET), первый блок страницы
        # остаётся стёртым (0xFF), иначе устройство прочитало бы
        # валидную конфиг-запись с пустыми именем и серийником.
        page = build_device_config_page("", "", fw_version=version)
        ver_fragment = page[
            DEVICE_CONFIG_VER_OFFSET : DEVICE_CONFIG_VER_OFFSET + 32
        ]
        image.puts(DEVICE_CONFIG_PAGE_ADDR + DEVICE_CONFIG_VER_OFFSET, ver_fragment)
        print(f"full image: версия ПО прошивки v{version}", flush=True)

    image.write_hex_file(args.out_hex)
    if args.out_bin:
        image.tofile(args.out_bin, format="bin")

    # «2 CAN CDC»: app-only образ для кнопки «Обновить» — метаданные и
    # код application плюс версионная запись на странице данных
    # устройства (идентичность merge не трогает). Загрузчик не входит.
    if args.cdc_hex or args.cdc_bin:
        cdc = IntelHex(args.app_hex)
        cdc.start_addr = None
        if ver_fragment:
            cdc.puts(
                DEVICE_CONFIG_PAGE_ADDR + DEVICE_CONFIG_VER_OFFSET, ver_fragment
            )
        if args.cdc_hex:
            cdc.write_hex_file(args.cdc_hex)
        if args.cdc_bin:
            cdc.tofile(args.cdc_bin, format="bin")
        print(f"cdc image: app-only -> {args.cdc_hex or args.cdc_bin}", flush=True)

    segments = list(image.segments())
    total = sum(end - start for start, end in segments)
    print(
        f"full image: {len(segments)} segments, {total} bytes -> {args.out_hex}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
