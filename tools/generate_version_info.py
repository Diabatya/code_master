#!/usr/bin/env python3
"""Генерирует assets/version_info.py из models/version.py.

Запускается в CI перед `pyinstaller build_win.spec`, чтобы
FileVersion/ProductVersion в свойствах CodeMaster.exe всегда совпадали
с VERSION приложения. Раньше файл правился вручную и рассинхронизировался:
filevers=(1,1,17,0), строки "1.1.14.0" — при VERSION=1.1.34.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models.version import VERSION, VERSION_TUPLE  # noqa: E402

OUT = ROOT / "assets" / "version_info.py"


def _u(s: str) -> str:
    """Литерал u'...' с \\uXXXX-экранированием не-ASCII (как в исходном файле)."""
    return "u" + ascii(s)


def main() -> None:
    major, minor, patch = VERSION_TUPLE
    filevers = f"({major}, {minor}, {patch}, 0)"
    version_str = f"{VERSION}.0"
    content = f'''VSVersionInfo(
    ffi=FixedFileInfo(
        filevers={filevers},
        prodvers={filevers},
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    u'040904B0',
                    [
                        StringStruct(u'CompanyName', {_u("КОД МАСТЕР")}),
                        StringStruct(u'ProductName', {_u("Код Мастер")}),
                        StringStruct(u'FileDescription', u'Code Master - STM32 flashing and CAN tool'),
                        StringStruct(u'InternalName', u'CodeMaster'),
                        StringStruct(u'OriginalFilename', u'CodeMaster.exe'),
                        StringStruct(u'FileVersion', u'{version_str}'),
                        StringStruct(u'ProductVersion', u'{version_str}'),
                        StringStruct(u'LegalCopyright', u'Copyright (C) 2026 KOD MASTER'),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct(u'Translation', [0x409, 1200])]),
    ],
)
'''
    OUT.write_text(content, encoding="ascii")
    print(f"{OUT}: FileVersion/ProductVersion = {version_str}")


if __name__ == "__main__":
    main()
