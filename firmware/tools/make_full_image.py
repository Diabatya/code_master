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

Если передан --version, пишется версионная запись «VER1» с номером
релиза — приложение она нужна, чтобы устройство могло отчитаться о
версии ПО. CM-F03: актуальное место VER1 — слот на странице
метаданных APP1 (смещение 32); копия на странице данных устройства
(смещение DEVICE_CONFIG_VER_OFFSET) остаётся для миграции и старых
прошивок. Первый блок страницы (имя/s/n) остаётся стёртым (0xFF):
идентичность устройства присваивается только первичным
программированием (DFU/ST-Link), а не универсальным релизным
образом; CDC-путь страницу идентичности вообще не пишет — VER1 из
образа переносится на страницу APP1.

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
            APP_METADATA_PAGE_ADDR,
            APP_METADATA_VER_OFFSET,
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
        # CM-F03: VER1 дублируется на страницу метаданных APP1
        # (смещение 32) — актуальное место для прошивок протокола ≥15;
        # копия на странице идентичности остаётся источником миграции
        # и совместимости со старыми прошивками.
        image.puts(
            APP_METADATA_PAGE_ADDR + APP_METADATA_VER_OFFSET, ver_fragment
        )
        print(f"full image: версия ПО прошивки v{version}", flush=True)

    image.write_hex_file(args.out_hex)
    if args.out_bin:
        image.tofile(args.out_bin, format="bin")

    # «2 CAN CDC»: app-only образ для кнопки «Обновить» — метаданные и
    # код application плюс версионная запись VER1 (страница данных
    # устройства @1024 для миграции/совместимости + слот страницы
    # метаданных @32 — актуальное место протокола ≥15). Страница
    # идентичности при CDC-обновлении не пишется. Загрузчик не входит.
    if args.cdc_hex or args.cdc_bin:
        cdc = IntelHex(args.app_hex)
        cdc.start_addr = None
        if ver_fragment:
            cdc.puts(
                DEVICE_CONFIG_PAGE_ADDR + DEVICE_CONFIG_VER_OFFSET, ver_fragment
            )
            cdc.puts(
                APP_METADATA_PAGE_ADDR + APP_METADATA_VER_OFFSET, ver_fragment
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
