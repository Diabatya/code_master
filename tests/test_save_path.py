"""Путь сохранения настроек в устройство (ревью CM-02).

Общий _save_current_config: ошибка записи блоба не должна
подтверждаться как «записано в устройство», dirty/данные сохраняются,
commit_runtime и device_flexb включаются только после подтверждённой
записи, потерянный COMMIT-ответ разрешается вычиткой.
"""

import json
import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from core.can_protocol import STORAGE_REGION_FLEX, STORAGE_REGION_VAR
from core.flex_compiler import build_flex_blob
from core.serial_manager import StorageCommitUnknown


class _FakeConfig:
    """Изолирует тест от реального config.json; save() под контролем."""

    DEFAULT_CONFIG: dict[str, Any] = {}

    def __init__(self) -> None:
        self._data: dict[str, Any] = {"device_serial": "TEST1"}
        self.save_result = True
        self.save_calls = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def set_bulk(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def all(self) -> dict[str, Any]:
        return dict(self._data)

    def import_data(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def reset_to_defaults(self) -> None:
        self._data.clear()

    def save(self) -> bool:
        self.save_calls += 1
        return self.save_result

    def save_to_file(self, path: str) -> None:
        pass


class _FakeSerialManager(QObject):
    """SerialManager-заглушка с управляемыми исходами storage_upload."""

    connection_changed = Signal(bool)
    connecting = Signal()
    critical_error = Signal(str)
    device_identified = Signal()
    error_occurred = Signal(str)
    new_can_frame = Signal(int, bytes, int)
    new_can_frames = Signal(list)
    reconnect_scheduled = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.open = True
        self.proto = 12
        self.upload_outcomes: dict[int, Any] = {}
        self.uploads: list[tuple[int, bytes]] = []
        self.download_data: bytes | None = None
        self.peek_data: bytes | None = None
        self.info_len: dict[int, int] = {}

    def is_open(self) -> bool:
        return self.open

    def current_port_name(self) -> str:
        return "FAKE"

    def device_protocol_version(self) -> int:
        return self.proto

    def storage_upload(self, region: int, data: bytes) -> bool:
        self.uploads.append((region, bytes(data)))
        outcome = self.upload_outcomes.get(region, True)
        if isinstance(outcome, Exception):
            raise outcome
        return bool(outcome)

    def storage_info(self, region: int) -> dict | None:
        # После записи область отдаёт длину последнего блоба.
        for reg, blob in reversed(self.uploads):
            if reg == region:
                return {"base": 0x08010000, "len": len(blob), "gen": 7}
        return None

    def storage_download(self, region: int) -> bytes | None:
        return self.download_data

    def storage_peek(self, region: int, length: int) -> bytes | None:
        return self.peek_data


class _FakeTab:
    """Минимальная вкладка: шпионы вызовов вместо реальных виджетов."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.flexb: bool | None = None

    def _save_config(self) -> None:
        self.calls.append("save_config")

    def write_to_device(self) -> int:
        self.calls.append("write_to_device")
        return 0

    def apply_can_settings_to_device(self) -> None:
        self.calls.append("apply_can")

    def export_config(self) -> dict:
        return {"variables": []}

    def get_config(self) -> list:
        return []

    def set_device_flexb(self, present: bool) -> None:
        self.flexb = bool(present)

    def commit_runtime(self) -> None:
        self.calls.append("commit_runtime")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def window(qapp, monkeypatch):
    """SettingsWindow с фейковым окружением и записанными диалогами."""
    import ui.settings_window as sw_module
    from ui.settings_window import SettingsWindow

    monkeypatch.setattr(sw_module, "Config", _FakeConfig)
    dialogs: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: dialogs.append(("warning", a))),
    )
    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda *a, **k: dialogs.append(("critical", a))),
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    toasts: list[str] = []
    monkeypatch.setattr(
        sw_module, "show_toast",
        lambda _w, text: toasts.append(str(text)),
    )

    serial = _FakeSerialManager()
    w = SettingsWindow(serial, None, None)
    config = w._config
    config.set("device_serial", "TEST1")
    # Вкладки заменяем шпионами — реальные тяжелы и не нужны для
    # наблюдаемого контракта save-path.
    w._trigger_tab = _FakeTab()
    w._flexible_tab = _FakeTab()
    w._variables_tab = _FakeTab()
    w._monitor_tab = _FakeTab()
    w._gateway_tab = _FakeTab()
    # Индикатор «есть несохранённые изменения» — реальный dirty.
    w._save_button.setEnabled(True)
    yield w, serial, config, toasts, dialogs
    w.deleteLater()
    from ui.memory_indicator import MemoryIndicator
    MemoryIndicator.shutdown_poller()


def test_var_upload_false_is_failure(window) -> None:
    w, serial, _c, toasts, dialogs = window
    serial.upload_outcomes[STORAGE_REGION_VAR] = False
    assert w._save_current_config() is False
    assert w._save_button.isEnabled()             # dirty сохранён
    assert not toasts                             # нет «записано»
    assert "commit_runtime" not in w._flexible_tab.calls
    assert dialogs and dialogs[0][0] == "warning"


def test_flex_upload_false_is_failure(window) -> None:
    w, serial, _c, toasts, _d = window
    serial.upload_outcomes[STORAGE_REGION_FLEX] = False
    assert w._save_current_config() is False
    assert not toasts
    assert w._flexible_tab.flexb is not True
    assert "commit_runtime" not in w._flexible_tab.calls


def test_flex_upload_timeout_is_failure(window) -> None:
    w, serial, _c, toasts, _d = window
    serial.upload_outcomes[STORAGE_REGION_FLEX] = TimeoutError("boom")
    assert w._save_current_config() is False
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls


def test_commit_unknown_resolved_by_readback(window) -> None:
    """COMMIT-ответ потерян, но вычитка подтверждает блоб — запись
    засчитывается, программа переходит на МК."""
    w, serial, _c, toasts, _d = window
    rules = w._flexible_tab.get_config()
    serial.upload_outcomes[STORAGE_REGION_FLEX] = StorageCommitUnknown()
    serial.download_data = build_flex_blob(rules)
    assert w._save_current_config() is True
    assert w._flexible_tab.flexb is True
    assert "commit_runtime" in w._flexible_tab.calls
    assert toasts


def test_commit_unknown_unreadable_is_failure(window) -> None:
    """COMMIT-ответ потерян и область не читается — сохранение не
    подтверждается; владелец исполнения определяется перечитыванием."""
    w, serial, _c, toasts, _d = window
    serial.upload_outcomes[STORAGE_REGION_FLEX] = StorageCommitUnknown()
    serial.download_data = None
    serial.peek_data = None
    assert w._save_current_config() is False
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls


def test_old_protocol_reported_as_unsupported(window) -> None:
    """Протокол < 11 — явный отказ записи VAR/FLEX, без подтверждения
    полного сохранения в устройство."""
    w, serial, _c, toasts, dialogs = window
    serial.proto = 10
    assert w._save_current_config() is False
    assert not toasts
    assert dialogs and dialogs[0][0] == "warning"


def test_proto11_writes_plain_json_flex(window) -> None:
    """Прошивка протокола 11 хранит блобы, но FLXB не исполняет —
    в область ГЛ уходит прежний чистый JSON, исполнение на ПК."""
    w, serial, _c, toasts, _d = window
    serial.proto = 11
    assert w._save_current_config() is True
    flex_upload = [b for r, b in serial.uploads if r == STORAGE_REGION_FLEX]
    assert flex_upload, "блоб ГЛ не записан"
    blob = flex_upload[-1]
    assert not blob.startswith(b"FLXB")
    assert json.loads(blob.decode("utf-8"))["rules"] == []
    assert w._flexible_tab.flexb is False


def test_full_success_marks_clean_and_commits(window) -> None:
    w, serial, _c, toasts, _d = window
    assert w._save_current_config() is True
    assert not w._save_button.isEnabled()         # dirty снят
    assert w._flexible_tab.flexb is True
    assert "commit_runtime" in w._flexible_tab.calls
    assert "записаны" in toasts[-1]


def test_offline_save_is_local(window) -> None:
    w, serial, _c, toasts, _d = window
    serial.open = False
    assert w._save_current_config() is True
    assert serial.uploads == []
    assert toasts and "устройство" not in toasts[-1]


def test_emulation_no_device_claim(window) -> None:
    """Эмулятор: порт открыт, но записи в МК нет — тост не должен
    утверждать запись в физическое устройство."""
    w, serial, config, toasts, _d = window
    config.set("emulation", True)
    assert w._save_current_config() is True
    assert serial.uploads == []
    assert toasts and "устройство" not in toasts[-1]


def test_file_save_failure_blocks_confirm(window) -> None:
    w, serial, config, toasts, _d = window
    config.save_result = False
    assert w._save_current_config() is False
    assert serial.uploads == []
    assert not toasts
    assert w._save_button.isEnabled()


def test_retry_after_failure(window) -> None:
    """Повторная попытка после сбоя работает без перезапуска окна."""
    w, serial, _c, toasts, _d = window
    serial.upload_outcomes[STORAGE_REGION_VAR] = False
    assert w._save_current_config() is False
    serial.upload_outcomes.clear()
    assert w._save_current_config() is True
    assert toasts
