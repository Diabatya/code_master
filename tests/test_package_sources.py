"""CM-F12: архив исходников прошивки — состав, исключения, манифест."""

from __future__ import annotations

import zipfile

from firmware.tools.package_sources import _collect, _manifest


class TestSourcePackage:
    def test_collect_has_sources_no_artifacts(self) -> None:
        files = {p.as_posix() for p in _collect()}
        posix = [f for f in files]
        # Ключевые исходники обеих прошивок и инструменты.
        assert any(f.endswith("firmware/application/Src/main.c") for f in files)
        assert any(f.endswith("firmware/bootloader/Src/main.c") for f in files)
        assert any(f.endswith("firmware/application/Makefile") for f in files)
        assert any(
            f.endswith("firmware/bootloader/STM32F105RCTx_BOOTLOADER.ld")
            for f in files
        )
        assert any(f.endswith("firmware/PROTOCOL.md") for f in files)
        # Собранные образы, кэши и бинарники в архив не попадают.
        assert not [f for f in posix if "/build/" in f]
        assert not [f for f in posix if "__pycache__" in f]
        assert not [
            f for f in posix
            if f.endswith((".hex", ".bin", ".elf", ".o", ".pyc"))
        ]
        # Пользовательских данных (конфиги/дампы/резервы) нет.
        assert not [f for f in posix if "config.json" in f]

    def test_manifest_marks_source_archive(self) -> None:
        text = _manifest("9.9.9", "deadbeef")
        assert "9.9.9" in text and "deadbeef" in text
        assert "STM32CubeF1" in text
        # Ясно обозначено: это исходники, не образы.
        assert "ИСХОДНЫЙ КОД" in text

    def test_collect_includes_core_for_make_full_image(self) -> None:
        """CM-FR11: make_full_image.py --version импортирует
        core.stm32_info — модуль обязан быть в архиве."""
        files = {p.as_posix() for p in _collect()}
        assert any(f.endswith("core/stm32_info.py") for f in files)
        assert any(f.endswith("core/__init__.py") for f in files)

    def test_manifest_addresses_and_real_instructions(self) -> None:
        text = _manifest("9.9.9", "deadbeef")
        # Код приложения — 0x08009000 (метаданные отдельно);
        # база CDC-образа — VER1 @0x08008400.
        assert "0x08009000" in text
        assert "0x08008400" in text
        assert "0x08008800" in text
        # `git submodule update` в распакованном ZIP не работает —
        # манифест обязан давать реальный способ получить CubeF1.
        assert "НЕ работает" in text
        assert "STM32CubeF1.git" in text
        # Возможность задать GIT_COMMIT при сборке из ZIP.
        assert "GIT_COMMIT" in text
        # CM-FR11-R2: вложенные сабмодули CubeF1 — обязательная
        # recursive-инициализация после checkout пина (plain clone/
        # tarball не содержат gitlink-содержимого).
        assert "submodule update --init" in text
        assert "recursive" in text
        assert "Drivers/STM32F1xx_HAL_Driver" in text
        assert "Middlewares/ST/STM32_USB_Device_Library" in text
        # Путь без git — те же зависимости tarball'ами на
        # gitlink-коммитах.
        assert "stm32f1xx_hal_driver" in text
        assert "stm32-mw-usb-device" in text
        assert "cmsis_device_f1" in text
        # Клон основного проекта — checkout SHA + recursive update.
        assert "git -C code_master checkout deadbeef" in text
        # Независимые make -C команды — cd цепочка не ломает вторую
        # сборку.
        assert "make -C firmware/bootloader" in text
        assert "make -C firmware/application" in text
        # DFU-образ содержит реальные области, а не «покрывает Flash
        # до 0x0803FFFF» — пустые страницы не добавляются.
        assert "покрывает 0x08000000-0x0803FFFF" not in text
        # Host-тесты обеих прошивок — и в архиве, и в манифесте.
        assert "firmware/bootloader/tests" in text
        files = {p.as_posix() for p in _collect()}
        assert any(
            f.endswith(
                "firmware/bootloader/tests/test_bootloader_boundary.c"
            )
            for f in files
        )
        assert any(
            f.endswith("firmware/bootloader/tests/stubs/usbd_cdc.h")
            for f in files
        )

    def test_extracted_zip_make_full_image(self, tmp_path) -> None:
        """CM-FR11: чистая распаковка → make_full_image.py --version
        работает без репозитория (core/ в архиве), CDC-база
        0x08008400, VER1-дубль на странице APP1."""
        import subprocess
        import sys

        from intelhex import IntelHex

        import firmware.tools.package_sources as pkg

        out = tmp_path / "src.zip"
        argv = sys.argv
        sys.argv = [
            "package_sources.py", str(out), "--version", "1.2.3",
        ]
        try:
            assert pkg.main() == 0
        finally:
            sys.argv = argv
        dest = tmp_path / "extract"
        with zipfile.ZipFile(out) as zf:
            zf.extractall(dest)
        # Минимальные HEX-входы на реальных базах.
        bl = IntelHex()
        bl.puts(0x08000000, b"\x01\x02\x03\x04")
        app = IntelHex()
        app.puts(0x08009000, b"\x05\x06\x07\x08")
        bl_hex = dest / "bl.hex"
        app_hex = dest / "app.hex"
        bl.write_hex_file(str(bl_hex))
        app.write_hex_file(str(app_hex))
        res = subprocess.run(
            [
                sys.executable,
                str(dest / "firmware" / "tools" / "make_full_image.py"),
                str(bl_hex), str(app_hex), str(dest / "out.hex"),
                "--cdc-hex", str(dest / "cdc.hex"),
                "--version", "1.2.3",
            ],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stderr
        image = IntelHex(str(dest / "out.hex"))
        cdc = IntelHex(str(dest / "cdc.hex"))
        # VER1 @0x08008400 (страница идентичности, легаси) и дубль
        # на странице метаданных APP1 @0x08008820. Магия пишется
        # little-endian словом — «1REV» в байтах.
        magic = int.from_bytes(b"VER1", "big").to_bytes(4, "little")
        assert bytes(
            image.tobinarray(start=0x08008400, size=4)
        ) == magic
        assert bytes(
            image.tobinarray(start=0x08008820, size=4)
        ) == magic
        assert cdc.minaddr() == 0x08008400

    def test_zip_written(self, tmp_path) -> None:
        import firmware.tools.package_sources as pkg

        out = tmp_path / "src.zip"
        rc = pkg.main.__wrapped__ if hasattr(pkg.main, "__wrapped__") else None
        assert rc is None  # sanity: main не обёрнут
        import sys as _sys
        _argv = _sys.argv
        _sys.argv = ["package_sources.py", str(out), "--version", "1.0.0"]
        try:
            assert pkg.main() == 0
        finally:
            _sys.argv = _argv
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "MANIFEST.txt" in names
        assert any(n.startswith("firmware/") for n in names)
