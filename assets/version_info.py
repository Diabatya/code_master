"""Версионные метаданные Windows (version resource) для PyInstaller EXE."""

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(1, 0, 0, 0),
        prodvers=(1, 0, 0, 0),
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
                    "040904B0",
                    [
                        StringStruct("CompanyName", "КОД МАСТЕР"),
                        StringStruct("ProductName", "Код Мастер"),
                        StringStruct("FileDescription", "Код Мастер — прошивка STM32 и работа с CAN"),
                        StringStruct("InternalName", "CodeMaster"),
                        StringStruct("OriginalFilename", "CodeMaster.exe"),
                        StringStruct("FileVersion", "1.0.0.0"),
                        StringStruct("ProductVersion", "1.0.0.0"),
                        StringStruct("LegalCopyright", "© 2026 КОД МАСТЕР"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", 0x409, 1200)]),
    ],
)
