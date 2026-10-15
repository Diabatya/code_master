"""GUI-регрессии CM-N18/CM-N19: настоящий clicked() кнопки «Прошить» и
кнопки «Восстановить из резервной копии…».

CM-N18: clicked(bool) не должен попадать в explicit_backup_regions —
обычная прошивка обязана передавать None, direct-config путь сохранён,
чекбокс сохранения работает как раньше.
CM-N19: явный резерв без файла прошивки — понятный отказ в UI до
worker'а; bool/мусор на границе FlashWorker/Bootloader — ошибка до
операций с устройством.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import ui.flash_dialog as fd
from core.bootloader import BootloaderError


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _WorkerRecorder(QObject):
    """Безопасная замена FlashWorker: сигналы настоящие (connect не
    падает), start() только регистрирует — аппаратных операций нет."""

    log_line = Signal(str)
    progress = Signal(int)
    finished = Signal(bool, str)

    made: list[_WorkerRecorder] = []

    def __init__(self, files, method, config, **kwargs):
        super().__init__(kwargs.pop("parent", None))
        self.files = files
        self.method = method
        self.kwargs = kwargs
        self.started = False
        _WorkerRecorder.made.append(self)

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return False


@pytest.fixture()
def dialog(qapp, monkeypatch):
    """Настоящий FlashDialog с безопасными подменами всего, что трогает
    аппаратуру/ФС."""
    _WorkerRecorder.made.clear()
    monkeypatch.setattr(fd, "FlashWorker", _WorkerRecorder)
    monkeypatch.setattr(fd, "_firmware_image_version", lambda _p: None)

    sm = MagicMock()
    sm.is_open.return_value = False
    dlg = fd.FlashDialog(sm)
    monkeypatch.setattr(
        dlg, "_collect_files_for_flash", lambda: ["/tmp/fake_app.bin"]
    )
    monkeypatch.setattr(
        dlg, "_warn_base_address_mismatch", lambda *_a: True
    )
    yield dlg
    dlg.deleteLater()


def test_ordinary_click_passes_none(dialog) -> None:
    """CM-N18 воспроизведение: настоящий clicked(bool) раньше передавал
    False как explicit_backup_regions → TypeError в merge. Теперь
    обычная прошивка обязана передать None."""
    dialog._flash_button.click()
    assert len(_WorkerRecorder.made) == 1
    worker = _WorkerRecorder.made[0]
    assert worker.kwargs["explicit_backup_regions"] is None
    assert worker.started


def test_ordinary_click_direct_config_path(dialog, monkeypatch) -> None:
    """Через ту же кнопку прямой путь записи конфигурации вызывается и
    завершает операцию без старта worker — CM-N18 не должен его
    сломать."""
    called = {"n": 0}

    def _direct(_method):
        called["n"] += 1
        return True

    monkeypatch.setattr(dialog, "_try_direct_config_write", _direct)
    dialog._flash_button.click()
    assert called["n"] == 1
    assert not _WorkerRecorder.made


def test_ordinary_click_respects_preserve_checkbox(
    dialog, monkeypatch
) -> None:
    """Чекбокс «Сохранять данные хранилища» управляет preserve и сам по
    себе не включает явный резерв."""
    # «Прошить» выключается на время операции и включается по finished —
    # рекордер его не эмитит, завершаем вручную (info-бокс глушим).
    monkeypatch.setattr(
        fd.QMessageBox, "information", staticmethod(lambda *a, **kw: None)
    )
    dialog._preserve_triggers_checkbox.setChecked(True)
    dialog._flash_button.click()
    assert _WorkerRecorder.made[-1].kwargs["preserve_triggers"] is True
    assert _WorkerRecorder.made[-1].kwargs["explicit_backup_regions"] is None
    _WorkerRecorder.made[-1].finished.emit(True, "ok")

    dialog._preserve_triggers_checkbox.setChecked(False)
    dialog._flash_button.click()
    assert _WorkerRecorder.made[-1].kwargs["preserve_triggers"] is False
    assert _WorkerRecorder.made[-1].kwargs["explicit_backup_regions"] is None


def test_restore_button_confirm_routes_list(dialog, monkeypatch) -> None:
    """Настоящая кнопка восстановления после подтверждения передаёт
    выбранный список в worker по имени параметра."""
    regions = [(0x0803B800, b"\xAA" * 64)]
    monkeypatch.setattr(
        dialog, "_pick_backup_manifest",
        lambda: (regions, "storage-x.json"),
    )
    dialog._files_list.addItem("/tmp/fake_app.bin")
    dialog._restore_backup_button.click()
    assert len(_WorkerRecorder.made) == 1
    assert (
        _WorkerRecorder.made[0].kwargs["explicit_backup_regions"]
        == regions
    )


def test_restore_button_cancel_starts_no_worker(dialog, monkeypatch) -> None:
    """Отмена выбора/подтверждения — worker не создаётся, устройство не
    трогается."""
    monkeypatch.setattr(dialog, "_pick_backup_manifest", lambda: None)
    dialog._files_list.addItem("/tmp/fake_app.bin")
    dialog._restore_backup_button.click()
    assert not _WorkerRecorder.made


def test_restore_button_without_firmware_refuses(
    dialog, monkeypatch
) -> None:
    """CM-N19: явный резерв без файла прошивки — понятный отказ ДО выбора
    манифеста (config-only HEX резерв не применяет)."""
    warnings: list[str] = []
    monkeypatch.setattr(
        fd.QMessageBox, "warning",
        staticmethod(lambda *a, **kw: warnings.append(str(a[2]))),
    )
    pick_called = {"n": 0}

    def _pick():
        pick_called["n"] += 1
        return ([(0x0803B800, b"\xAA" * 16)], "m.json")

    monkeypatch.setattr(dialog, "_pick_backup_manifest", _pick)
    assert dialog._files_list.count() == 0
    dialog._restore_backup_button.click()
    assert warnings, "должно быть понятное предупреждение"
    assert "прошивки" in warnings[0] or "application" in warnings[0]
    assert pick_called["n"] == 0  # до выбора манифеста дело не дошло
    assert not _WorkerRecorder.made


def test_flash_worker_rejects_bool_backup(qapp) -> None:
    """Граница FlashWorker: bool (как у clicked) отвергается понятной
    ошибкой — до конструирования рабочего состояния."""
    config = MagicMock()
    with pytest.raises(ValueError, match="explicit_backup_regions"):
        fd.FlashWorker(
            ["/tmp/x.bin"], "uart", config,
            explicit_backup_regions=False,
        )
    with pytest.raises(ValueError, match="explicit_backup_regions"):
        fd.FlashWorker(
            ["/tmp/x.bin"], "uart", config,
            explicit_backup_regions="/path/to.json",
        )


def test_flash_firmware_rejects_bool_backup() -> None:
    """Граница Bootloader: bool/мусор в explicit_backup — BootloaderError
    до чтения файла/подключения, не TypeError в глубине merge."""
    from core.bootloader import Bootloader

    bl = Bootloader.__new__(Bootloader)
    for bad in (False, True, "backup.json", 42, {"a": 1}):
        with pytest.raises(BootloaderError, match="explicit_backup"):
            bl.flash_firmware(
                "/nonexistent.bin", explicit_backup=bad
            )
