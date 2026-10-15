"""CM-N16: после штатного обновления настоящий production
Storage_Init (firmware/application/Src/storage.c, собранный на хосте)
выбирает ЦЕЛОСТНУЮ копию области — а не битую старшую по поколению,
оставшуюся физически вне зоны стирания обновления.

Пайплайн повторяет сценарий ревью дословно: настоящий
Bootloader.flash_firmware на FlashSim → итоговый дамп Flash (256 КиБ
от 0x08000000) → харнесс test_storage_select.c --dump → настоящие
Storage_Init/Storage_CfgRead/Storage_Read/Storage_RegionInfo.

Харнесс собирается cc прямо в тесте (нужен host-компилятор); на
macOS/arm64 — x86_64-сборка под Rosetta (нижние 4 ГиБ зарезервированы
pagezero, mmap по реальному адресу Flash возможен только в 32-битной
addressing-совместимой сборке x86_64).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from test_bootloader_preserve import (
    CFG_MAGIC,
    FLX_MAGIC,
    PAGE,
    VAR_MAGIC,
    FlashSim,
    _app_code,
    _bl_on_sim,
    _flash_app,
    _store_region,
    _tmp_bin,
)
from core import storage_backup
from core.stm32_info import APPLICATION_BASE_ADDR

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_SRC = os.path.join(
    REPO, "firmware", "application", "tests", "test_storage_select.c"
)
# Харнесс компилирует _app_code_end = 0x0800B000 → сценарии строим под
# образ ровно в 4 страницы (code_end = APPLICATION_BASE_ADDR + 4*PAGE).
CODE_END = APPLICATION_BASE_ADDR + 4 * PAGE


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path, monkeypatch):
    """Резервы хранилища — в tmp_path, не в реальный каталог."""
    monkeypatch.setattr(
        storage_backup, "backup_dir", lambda: tmp_path
    )
    return tmp_path


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> str:
    """Сборка харнесса настоящего storage.c один раз на модуль."""
    cc = (
        shutil.which("cc")
        or shutil.which("gcc")
        or shutil.which("clang")
    )
    if not cc:
        pytest.skip("нет host-компилятора C для харнесса storage.c")
    out = str(
        tmp_path_factory.mktemp("harness") / "test_storage_select"
    )
    cmd = [cc]
    if sys.platform == "darwin":
        # arm64: pagezero 4 ГиБ не даёт mmap на 0x08000000 — собираем
        # x86_64 с уменьшенным pagezero и запускаем под Rosetta.
        cmd += ["-arch", "x86_64", "-Wl,-pagezero_size,0x4000"]
    cmd += [
        "-I", os.path.join(REPO, "firmware/application/tests/stubs"),
        "-I", os.path.join(REPO, "firmware/application/Inc"),
        "-I", os.path.join(REPO, "firmware/application/Src"),
        HARNESS_SRC,
        "-o", out,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def _mcu_select(harness: str, mem: bytes, tmp_path) -> str:
    """Дамп → настоящий Storage_Init; возвращает вывод SEL-строк."""
    dump = tmp_path / "flash_dump.bin"
    dump.write_bytes(mem)
    cmd = [harness, "--dump", str(dump)]
    if sys.platform == "darwin":
        cmd = ["arch", "-x86_64"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed: {r.stderr}"
    return r.stdout


def _flash_4pages(sim: FlashSim) -> None:
    """Штатное обновление образом в 4 страницы с сохранением."""
    bl = _bl_on_sim(sim)
    path = _tmp_bin(_app_code(4 * PAGE, 0x22))
    try:
        bl.flash_firmware(
            path, APPLICATION_BASE_ADDR, page_size=PAGE,
            skip_blank=False, preserve_storage=True,
        )
    finally:
        os.unlink(path)


def _plant(sim: FlashSim, addr: int, blob: bytes) -> None:
    sim.erase(addr, PAGE * (len(blob) // PAGE))
    sim.write(addr, blob)


def _corrupt(blob: bytes, off: int = 25) -> bytes:
    """Битый payload при валидном CRC8 заголовка (обрыв записи)."""
    b = bytearray(blob)
    b[off] ^= 0xFF
    return bytes(b)


class TestMcuSelectAfterFlash:
    """Дамп после штатного flash_firmware → выбор настоящим МК."""

    def test_corrupt_cfg_survivor_not_selected(
        self, harness: str, tmp_path
    ) -> None:
        """Карта ревью CM-N16: целая CFGH gen=2 на каноничной
        странице + битая gen=3 выше (вне зоны записи). До фикса МК
        выбирал битую старшую → Storage_CfgRead=0 после «успешного»
        обновления."""
        sim = FlashSim()
        _flash_app(sim, _app_code(4 * PAGE))
        # Целая копия — на каноничной странице code_end.
        _plant(sim, CODE_END, _store_region(CFG_MAGIC, 80, gen=2))
        # Битая старшая — выше, адрес обновление не трогает.
        bad_addr = CODE_END + 4 * PAGE
        _plant(sim, bad_addr,
               _corrupt(_store_region(CFG_MAGIC, 80, gen=3)))
        _flash_4pages(sim)
        # Планировщик ПК её вытеснил из плана, но физически битая
        # копия остаётся — только МК-контракт целостности спасает.
        out = _mcu_select(harness, bytes(sim.mem), tmp_path)
        assert f"SEL cfg base=0x{CODE_END:08X} gen=2 read=1" in out, out

    def test_corrupt_var_flex_survivors_not_selected(
        self, harness: str, tmp_path
    ) -> None:
        """То же для VARH/FLXH: битые старшие копии вне зоны записи
        не вытесняют целые — исполняемая ГЛ и переменные выбраны
        реально, а не «присутствуют в дампе»."""
        sim = FlashSim()
        _flash_app(sim, _app_code(4 * PAGE))
        # FLXH: целая gen=2 сразу за страницей CFGH, битая gen=4
        # высоко в зазоре.
        flex_ok = CODE_END + PAGE
        _plant(sim, flex_ok, _store_region(FLX_MAGIC, 120, gen=2))
        _plant(sim, CODE_END + 40 * PAGE,
               _corrupt(_store_region(FLX_MAGIC, 120, gen=4)))
        # VARH: целая gen=2 у верха Flash, битая gen=3 ниже неё.
        var_ok = 0x0803F800
        _plant(sim, var_ok, _store_region(VAR_MAGIC, 96, gen=2))
        _plant(sim, var_ok - 4 * PAGE,
               _corrupt(_store_region(VAR_MAGIC, 96, gen=3)))
        _flash_4pages(sim)
        out = _mcu_select(harness, bytes(sim.mem), tmp_path)
        assert f"SEL var base=0x{var_ok:08X} gen=2" in out, out
        assert f"SEL flex base=0x{flex_ok:08X} gen=2" in out, out

    def test_single_corrupt_cfg_honest_absent(
        self, harness: str, tmp_path
    ) -> None:
        """Единственная битая копия → область отсутствует у МК:
        CfgFoundBase=0/read=0 — «успешное восстановление» не
        заявляется подстановкой дефолтов."""
        sim = FlashSim()
        _flash_app(sim, _app_code(4 * PAGE))
        _plant(sim, CODE_END + 6 * PAGE,
               _corrupt(_store_region(CFG_MAGIC, 48, gen=5)))
        # preserve_storage=True → отказ до стирания (битый блок без
        # альтернативы); шьём БЕЗ сохранения — данные теряются честно.
        bl = _bl_on_sim(sim)
        path = _tmp_bin(_app_code(4 * PAGE, 0x33))
        try:
            bl.flash_firmware(
                path, APPLICATION_BASE_ADDR, page_size=PAGE,
                skip_blank=False, preserve_storage=False,
            )
        finally:
            os.unlink(path)
        out = _mcu_select(harness, bytes(sim.mem), tmp_path)
        assert "SEL cfg base=0x00000000" in out, out
        assert "read=0" in out, out
