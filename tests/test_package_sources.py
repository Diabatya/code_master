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
