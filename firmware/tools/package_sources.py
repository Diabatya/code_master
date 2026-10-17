#!/usr/bin/env python3
"""Архив исходников прошивки для релиза (CM-F12).

Кладёт в zip исходники именно этой ревизии: application и bootloader
(Inc/Src, Makefile, linker scripts, README), host-тесты прошивки,
инструменты сборки/упаковки и документацию протокола. Собранные
образы (build/, *.hex/*.bin/*.elf/*.o), кэши Python и пользовательские
конфиги в архив не попадают.

MANIFEST.txt внутри архива фиксирует: версию релиза, точный SHA
коммита, зафиксированный коммит сабмодуля STM32CubeF1 и способ
получить зависимости (`git submodule update --init`), тулчейн
arm-none-eabi-gcc. Архив — ИСХОДНЫЙ КОД; готовые образы — отдельные
«2 CAN DFU/CDC 111» артефакты релиза.

Использование:
    python3 package_sources.py <out.zip> [--version 1.2.3] [--sha abc]
"""

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Что входит в архив (корни относительно репозитория). Собранные
# артефакты и служебные каталоги отсекаются по имени ниже.
INCLUDE_ROOTS = (
    "firmware/application/Inc",
    "firmware/application/Src",
    "firmware/application/tests",
    "firmware/bootloader/Inc",
    "firmware/bootloader/Src",
    "firmware/bootloader/tests",
    "firmware/tools",
)
INCLUDE_FILES = (
    "firmware/application/Makefile",
    "firmware/application/README.md",
    "firmware/application/STM32F105RCTx_APP.ld",
    "firmware/bootloader/Makefile",
    "firmware/bootloader/README.md",
    "firmware/bootloader/STM32F105RCTx_BOOTLOADER.ld",
    "firmware/PROTOCOL.md",
    "firmware/PINOUT.md",
    "firmware/HARDWARE_TEST_PROCEDURE.md",
    "firmware/FIRMWARE_INPUT_REQUEST.md",
    # make_full_image.py --version импортирует core.stm32_info за
    # адресами/VER1 — без core/ в ZIP упаковка с версией падает
    # (CM-FR11).
    "core/__init__.py",
    "core/stm32_info.py",
    ".gitmodules",
)
# Суффиксы/имена, которые не являются исходным кодом.
EXCLUDE_SUFFIXES = (".hex", ".bin", ".elf", ".o", ".lst", ".map",
                    ".pyc", ".d")
EXCLUDE_DIRS = {"build", "__pycache__"}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        check=True,
    ).stdout.strip()


def _collect() -> list[Path]:
    files: list[Path] = []
    for root in INCLUDE_ROOTS:
        base = REPO_ROOT / root
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if EXCLUDE_DIRS & set(path.relative_to(base).parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            files.append(path)
    for rel in INCLUDE_FILES:
        path = REPO_ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def _cube_sha() -> str:
    """Зафиксированный коммит сабмодуля STM32CubeF1 — из индекса
    git (работает и без инициализированного сабмодуля)."""
    try:
        line = _git("ls-tree", "HEAD", "firmware/libs/STM32CubeF1")
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "commit":
            return parts[2]
    except subprocess.CalledProcessError:
        pass
    try:
        line = _git(
            "submodule", "status", "firmware/libs/STM32CubeF1"
        ).lstrip(" +-U")
        return line.split()[0]
    except (subprocess.CalledProcessError, IndexError):
        return "(коммит сабмодуля не зафиксирован — см. .gitmodules)"


# Вложенные зависимости CubeF1, нужные для сборки, на коммитах,
# зафиксированных gitlink'ами пина CubeF1 (CM-FR11-R2): путь внутри
# CubeF1 → (репозиторий, SHA).
CUBE_NESTED_DEPS = (
    (
        "Drivers/CMSIS/Device/ST/STM32F1xx",
        "https://github.com/STMicroelectronics/cmsis_device_f1.git",
        "c8e9a4a4f16b6d2cb2a2083cbe5161025280fb22",
    ),
    (
        "Drivers/STM32F1xx_HAL_Driver",
        "https://github.com/STMicroelectronics/stm32f1xx_hal_driver.git",
        "c3f749241cd2d55074b60775c77824d0eb13b931",
    ),
    (
        "Middlewares/ST/STM32_USB_Device_Library",
        "https://github.com/STMicroelectronics/stm32-mw-usb-device.git",
        "e5a58ee2260204bfed632c7c56da0fdb41514a51",
    ),
)


def _manifest(version: str, sha: str) -> str:
    repo_sha = sha or _git("rev-parse", "HEAD")
    cube = _cube_sha()
    ver = version or "VERSION"
    # CM-FR11-R2: nested-зависимости — git submodule update
    # --init --recursive после checkout пина либо те же gitlink-SHA
    # tarball'ами для пути без git.
    nested_tarballs = "\n".join(
        f"      https://github.com/STMicroelectronics/"
        f"{url.rsplit('/', 1)[-1].removesuffix('.git')}"
        f"/archive/{dep_sha}.tar.gz\n"
        f"        -> firmware/libs/STM32CubeF1/{path}"
        for path, url, dep_sha in CUBE_NESTED_DEPS
    )
    return f"""CodeMaster — исходники прошивки (CM-F12)

Версия релиза:        {version or 'не указана'}
Коммит репозитория:   {repo_sha}
STM32CubeF1 (пин):    {cube}
Репозиторий:          https://github.com/Diabatya/code_master.git

Карта Flash STM32F105RCT6:
  0x08000000-0x08007FFF  bootloader (32 КБ)
  0x08008000-0x080087FF  DEVICE INFO (CFG0/TNM0/VER1, стр. 16)
  0x08008800-0x08008FFF  метаданные приложения APP1 (стр. 17)
  0x08009000-0x0803BFFF  код приложения
  0x0803C000-0x0803FFFF  конец Flash (резерв/хранилище)

Зависимости (в ZIP их нет — .git/gitlink не включается, команда
`git submodule update` внутри распакованного архива НЕ работает):
  - arm-none-eabi-gcc + make (GNU Arm Embedded Toolchain);
  - Python 3.9+; для упаковки образов: pip install intelhex;
  - STM32CubeF1 ровно на коммите {cube}
    в каталоге firmware/libs/STM32CubeF1, С ЕГО ВЛОЖЕННЫМИ
    сабмодулями (plain clone/tarball не содержит их содержимое —
    Makefile'ы требуют HAL/CMSIS/USB-библиотеку). Получить:

      git clone https://github.com/STMicroelectronics/STM32CubeF1.git \\
          firmware/libs/STM32CubeF1
      git -C firmware/libs/STM32CubeF1 checkout {cube}
      git -C firmware/libs/STM32CubeF1 submodule update --init \\
          --recursive \\
          Drivers/CMSIS/Device/ST/STM32F1xx \\
          Drivers/STM32F1xx_HAL_Driver \\
          Middlewares/ST/STM32_USB_Device_Library

    или tarball'ами без git (CubeF1 + три вложенные зависимости
    на gitlink-коммитах пина — распаковать поверх пустых каталогов):
      https://github.com/STMicroelectronics/STM32CubeF1/\\
          archive/{cube}.tar.gz
        -> firmware/libs/STM32CubeF1
{nested_tarballs}

    Либо вместо этого ZIP клонируйте проект на зафиксированный SHA:
      git clone https://github.com/Diabatya/code_master.git
      git -C code_master checkout {repo_sha}
      git -C code_master submodule update --init --recursive

Сборка (независимые команды из корня распакованного архива):
  make -C firmware/bootloader   # загрузчик  @0x08000000
  make -C firmware/application  # приложение @0x08009000
                                # (APP1-метаданные генерируются
                                #  отдельно @0x08008800)
  Вне git-репозитория передайте SHA явно, иначе прошивка
  отчитается «unknown»:  make -C firmware/application GIT_COMMIT=<sha>

Упаковка образов релиза:
  python3 firmware/tools/make_full_image.py \\
      firmware/bootloader/build/codemaster_bootloader.hex \\
      firmware/application/build/codemaster_app.hex \\
      "2 CAN DFU ({ver}) 111.hex" "2 CAN DFU ({ver}) 111.bin" \\
      --cdc-hex "2 CAN CDC ({ver}) 111.hex" \\
      --cdc-bin "2 CAN CDC ({ver}) 111.bin" \\
      --version {ver}
  Полный (DFU) образ содержит загрузчик @0x08000000, метаданные
  APP1 @0x08008800 и код @0x08009000 — только реально
  присутствующие области; пустые страницы до конца Flash
  упаковщик не добавляет. CDC-образ начинается с легаси-записи
  VER1 по 0x08008400 (миграция старых прошивок), затем метаданные
  APP1 @0x08008800 и код @0x08009000; страница идентичности в
  CDC-образе отсутствует.

Хост-тесты: firmware/application/tests и firmware/bootloader/tests
(cc + стабы, см. CI .github/workflows/build.yml в основном
репозитории).

Это ИСХОДНЫЙ КОД прошивки тега. Готовые образы для заливки —
отдельные артефакты релиза «2 CAN DFU (VERSION) 111» и
«2 CAN CDC (VERSION) 111»; сервисный образ загрузчика —
«2 CAN bootloader service 0x08000000 (VERSION)».
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_zip")
    parser.add_argument("--version", default="")
    parser.add_argument("--sha", default="")
    args = parser.parse_args()

    files = _collect()
    if not files:
        print("package_sources: ничего не найдено", file=sys.stderr)
        return 1
    out = Path(args.out_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(REPO_ROOT).as_posix())
        zf.writestr("MANIFEST.txt", _manifest(args.version, args.sha))
    total = sum(p.stat().st_size for p in files)
    print(
        f"package_sources: {len(files)} файлов, {total} байт -> {out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
