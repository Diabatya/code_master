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
        # Управляемые исходы по региону: dict/None — значение,
        # исключение — бросается (ревью CM-R4: INFO exception/None/
        # неверная длина, вычитка по регионам).
        self.info_outcomes: dict[int, Any] = {}
        self.download_by_region: dict[int, bytes | None] = {}
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
        if region in self.info_outcomes:
            outcome = self.info_outcomes[region]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        # После записи область отдаёт длину последнего блоба.
        for reg, blob in reversed(self.uploads):
            if reg == region:
                return {"base": 0x08010000, "len": len(blob), "gen": 7}
        return None

    def storage_download(self, region: int) -> bytes | None:
        if region in self.download_by_region:
            return self.download_by_region[region]
        return self.download_data

    def storage_peek(self, region: int, length: int) -> bytes | None:
        return self.peek_data


class _FakeTab:
    """Минимальная вкладка: шпионы вызовов вместо реальных виджетов.

    Полный контракт, который SettingsWindow может дёрнуть: отложенный
    _sync_from_device (singleShot 400 мс после конструктора) звонит в
    вкладки — фейк обязан моделировать его целиком, иначе отложенный
    callback стреляет в частично подменённый объект (ревью CM-R4)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.flexb: bool | None = None
        self.flex_state: str | None = None
        self.probe_failed = False
        self.rules: list | None = None
        self.variables: Any = None

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

    def set_device_flex_state(
        self, state: str, managed: list | None = None
    ) -> None:
        self.flex_state = state
        self.probe_failed = False
        # «flexb» — бинарное «камень исполняет»: verified и conflict
        # означают работающий хвост (conflict — владелец камень,
        # хотя содержимое не верифицировано — ревью CM-R7).
        self.flexb = state in ("verified", "conflict")

    def mark_device_flex_probe_failed(self) -> None:
        self.probe_failed = True

    def set_device_flexb(self, present: bool) -> None:
        self.flexb = bool(present)
        self.flex_state = "verified" if present else "no-tail"

    def set_config(
        self, rules: list, *, suspend_execution: bool = False
    ) -> None:
        self.calls.append("set_config")
        self.rules = list(rules)
        self.suspended = suspend_execution

    def import_config(self, variables: Any) -> None:
        self.variables = variables

    def apply_device_flex_analysis(self, analysis: dict) -> None:
        self.flex_state = analysis["state"]
        self.probe_failed = False
        self.flexb = self.flex_state in ("verified", "conflict")

    def reset_device_flex_state(self) -> None:
        self.flex_state = "unknown"
        self.flexb = None
        self.probe_failed = False

    def commit_runtime(self) -> None:
        self.calls.append("commit_runtime")

    # --- контракт _sync_from_device/фоновых коллбеков -------------

    def on_device_session_started(self) -> None:
        self.calls.append("on_device_session_started")

    def sync_from_device(self, force: bool = False) -> bool:
        self.calls.append("sync_from_device")
        return True


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
    # Отложенная авто-вычитка (singleShot 400 мс) планируется, только
    # если порт открыт на момент конструктора. Закрываем её на время
    # создания окна — иначе в тесте, пережившем 400 мс (полный
    # прогон), callback стреляет в уже подменённые вкладки и добавляет
    # чужой critical-диалог до ожидаемого warning (ревью CM-R4).
    serial.open = False
    w = SettingsWindow(serial, None, None)
    serial.open = True
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
    # DeferredDelete и отложенные коллбеки должны исполниться до конца
    # теста — иначе они всплывают внутри СЛЕДУЮЩЕГО теста
    # (ревью CM-R4).
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
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


# ---- CM-R4: расширенные сценарии отказов пути сохранения ----------


def test_var_upload_exception_is_failure(window) -> None:
    """Исключение записи VAR (не только False) — подтверждённый отказ:
    dirty сохранён, подтверждения нет, ГЛ-коммит не вызывался."""
    w, serial, _c, toasts, _d = window
    serial.upload_outcomes[STORAGE_REGION_VAR] = RuntimeError("usb gone")
    assert w._save_current_config() is False
    assert w._save_button.isEnabled()
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls
    # Запись FLEX после сбоя VAR не пыталась исполниться.
    assert not [r for r, _ in serial.uploads if r == STORAGE_REGION_FLEX]


def test_info_none_after_upload_is_failure(window) -> None:
    """storage_info = None после «успешной» записи — размер блоба не
    подтверждён, сохранение отклонено, dirty сохранён."""
    w, serial, _c, toasts, _d = window
    serial.info_outcomes[STORAGE_REGION_VAR] = None
    assert w._save_current_config() is False
    assert w._save_button.isEnabled()
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls


def test_info_exception_after_upload_is_failure(window) -> None:
    """Исключение storage_info на проверке длины — сохранение не
    подтверждается, dirty остаётся."""
    w, serial, _c, toasts, _d = window
    serial.info_outcomes[STORAGE_REGION_VAR] = TimeoutError("info lost")
    assert w._save_current_config() is False
    assert w._save_button.isEnabled()
    assert not toasts


def test_info_wrong_length_is_failure(window) -> None:
    """INFO с неверной длиной — область приняла НЕ наш блоб: отказ,
    dirty сохранён, подтверждения нет."""
    w, serial, _c, toasts, _d = window
    serial.info_outcomes[STORAGE_REGION_VAR] = {
        "base": 0x08010000, "len": 999999, "gen": 7,
    }
    assert w._save_current_config() is False
    assert w._save_button.isEnabled()
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls


def test_cache_invalidated_on_failure(window) -> None:
    """Сбой записи инвалидирует закэшированный блоб — старое
    содержимое не выдаётся за актуальное."""
    w, serial, _c, _t, _d = window
    key = ("TEST1", STORAGE_REGION_VAR)
    w._blob_cache[key] = (1, b"stale")
    serial.upload_outcomes[STORAGE_REGION_VAR] = False
    assert w._save_current_config() is False
    assert key not in w._blob_cache


def test_retry_after_info_failure(window) -> None:
    """Успешный повтор после отказа INFO — окно не требует
    пересоздания."""
    w, serial, _c, toasts, _d = window
    serial.info_outcomes[STORAGE_REGION_VAR] = None
    assert w._save_current_config() is False
    serial.info_outcomes.clear()
    assert w._save_current_config() is True
    assert toasts


def test_commit_unknown_mismatch_keeps_device_owner(window) -> None:
    """Потерянный COMMIT по FLEX, а вычитка вернула ДРУГОЙ валидный
    FLXB (прежнее содержимое камня): наша запись не засчитана, но
    владелец исполнения — реальное содержимое устройства, а не наш
    черновик: прежние программы по-прежнему исполняет камень (нет ни
    мёртвых программ, ни двойного исполнения — ревью CM-R4 §7)."""
    w, serial, _c, toasts, _d = window
    stale = build_flex_blob(
        [{"title": "Старая программа", "active": False}]
    )
    serial.upload_outcomes[STORAGE_REGION_FLEX] = StorageCommitUnknown()
    serial.download_by_region[STORAGE_REGION_FLEX] = stale
    assert w._save_current_config() is False
    assert not toasts
    assert "commit_runtime" not in w._flexible_tab.calls
    # Перечитывание по факту: в камне валидный FLXB (пусть и не наш) —
    # флаг отражает именно это.
    assert w._flexible_tab.flexb is True


def test_commit_unknown_corrupt_blob_marks_pc_owner(window) -> None:
    """Потерянный COMMIT, а область содержит битый контейнер —
    исполняет ПК (камень такой контейнер отвергнет): флаг честно
    опущен, программы не «мёртвы»."""
    w, serial, _c, _t, _d = window
    corrupt = bytearray(build_flex_blob([]))
    corrupt[4] ^= 0x5A  # версия контейнера битая — хвост не грузится
    serial.upload_outcomes[STORAGE_REGION_FLEX] = StorageCommitUnknown()
    serial.download_by_region[STORAGE_REGION_FLEX] = bytes(corrupt)
    assert w._save_current_config() is False
    assert w._flexible_tab.flexb is False


# ---- CM-R5/R7: владелец исполнения — tri-state --------------------


def _flex_info(length: int, gen: int = 7) -> dict:
    return {"base": 0x08010000, "len": length, "gen": gen}


def test_info_none_keeps_owner_and_marks_unconfirmed(window) -> None:
    """storage_info = None — область НЕ прочитана, а не «пустая»:
    прежний владелец сохраняется (keep-last), состояние
    диагностируется как неподтверждённое (ревью CM-R5)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.set_device_flex_state("verified")      # keep-last: владелец был МК
    serial.info_outcomes[STORAGE_REGION_FLEX] = None
    w._refresh_device_flexb()
    assert tab.flex_state == "verified"        # владелец не слетел на ПК
    assert tab.flexb is True
    assert tab.probe_failed is True            # диагностика неизвестности


def test_info_exception_keeps_owner(window) -> None:
    """Исключение storage_info — та же неизвестность: состояние не
    меняется, помечается probe_failed (ревью CM-R5)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.set_device_flex_state("verified")
    serial.info_outcomes[STORAGE_REGION_FLEX] = TimeoutError("usb gone")
    w._refresh_device_flexb()
    assert tab.flex_state == "verified"
    assert tab.probe_failed is True


def test_empty_info_confirms_empty_region(window) -> None:
    """INFO с len=0 — подтверждённая пустая область: владелец — ПК,
    это факт, а не неизвестность (ревью CM-R5)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.set_device_flex_state("verified")
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(0)
    w._refresh_device_flexb()
    assert tab.flex_state == "empty"
    assert tab.flexb is False
    assert tab.probe_failed is False


def test_download_none_after_info_keeps_owner(window) -> None:
    """INFO валиден, но вычитка вернула None — неизвестность:
    владелец keep-last, состояние неподтверждённое (ревью CM-R5)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.set_device_flex_state("verified")
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(64)
    serial.download_by_region[STORAGE_REGION_FLEX] = None
    w._refresh_device_flexb()
    assert tab.flex_state == "verified"
    assert tab.probe_failed is True


def test_valid_blob_marks_verified(window) -> None:
    """Полная вычитка + валидация: хвост == компиляции JSON —
    владелец «устройство», подтверждённо (ревью CM-R7)."""
    w, serial, _c, _t, _d = window
    blob = build_flex_blob([])
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.download_by_region[STORAGE_REGION_FLEX] = blob
    w._refresh_device_flexb()
    assert w._flexible_tab.flex_state == "verified"
    assert w._flexible_tab.flexb is True


def test_legacy_json_blob_is_no_tail(window) -> None:
    """Старый чисто-JSON блоб — исполняемого хвоста нет: владелец
    ПК (камень не исполняет JSON), состояние «no-tail»."""
    w, serial, _c, _t, _d = window
    blob = json.dumps({"kind": "code_master_flex_logic",
                       "format": 1, "rules": []}).encode("utf-8")
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.download_by_region[STORAGE_REGION_FLEX] = blob
    w._refresh_device_flexb()
    assert w._flexible_tab.flex_state == "no-tail"
    assert w._flexible_tab.flexb is False


def test_tail_mismatch_is_conflict(window) -> None:
    """Валидный контейнер, но хвост ≠ компиляции JSON (чужой хвост):
    камень исполняет ЗАПИСИ — ПК вслепую не включается поверх;
    состояние «conflict» требует повторной записи (ревью CM-R7)."""
    w, serial, _c, _t, _d = window
    blob = bytearray(build_flex_blob([]))
    # Подменяем исполняемый хвост валидной записью от ДРУГИХ правил.
    other = build_flex_blob(
        [{
            "title": "P", "active": True,
            "events": [{"type": "frame", "id": "0x123", "channel": 0,
                        "format": "std", "rtr": False, "dlc": 8,
                        "data": ""}],
            "actions": [{"type": "frame", "id": "0x456", "channel": 0,
                         "format": "std", "rtr": False,
                         "data": "AA BB", "count": 1, "delay": 0,
                         "interval": 0}],
        }]
    )
    blob[5] = other[5]                       # count
    blob = blob[:12 + int.from_bytes(blob[8:12], "little")] + other[
        12 + int.from_bytes(other[8:12], "little"):
    ]
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.download_by_region[STORAGE_REGION_FLEX] = bytes(blob)
    w._refresh_device_flexb()
    assert w._flexible_tab.flex_state == "conflict"
    assert w._flexible_tab.flexb is True     # камень — владелец


def test_download_blobs_info_none_marks_probe_failed(window) -> None:
    """_download_config_blobs при нечитаемой области: владельца не
    меняет, диагностирует неизвестность, конфиг не импортирует
    (ревью CM-R5)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.set_device_flex_state("verified")
    serial.info_outcomes[STORAGE_REGION_FLEX] = None
    serial.info_outcomes[STORAGE_REGION_VAR] = None
    w._download_config_blobs()
    assert tab.flex_state == "verified"
    assert tab.probe_failed is True
    assert tab.rules is None                 # ничего не импортировали


def test_download_blobs_empty_region_marks_empty(window) -> None:
    """Подтверждённый len=0 в INFO — область пуста, владелец ПК."""
    w, serial, _c, _t, _d = window
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(0)
    serial.info_outcomes[STORAGE_REGION_VAR] = _flex_info(0)
    w._download_config_blobs()
    assert w._flexible_tab.flex_state == "empty"
    assert w._flexible_tab.probe_failed is False


def test_download_blobs_verified_imports_rules(window) -> None:
    """Валидный FLXB: правила импортируются И владелец — устройство
    (verified), в этом порядке — managed-индексы относятся к
    импортированным правилам (ревью CM-R7)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    rules = [{"title": "P1", "active": True,
              "events": [{"type": "power"}],
              "actions": [{"type": "frame", "id": "0x100",
                           "channel": 0, "data": "01"}]}]
    blob = build_flex_blob(rules)
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.info_outcomes[STORAGE_REGION_VAR] = _flex_info(0)
    serial.download_by_region[STORAGE_REGION_FLEX] = blob
    w._download_config_blobs()
    assert tab.rules == rules
    assert tab.flex_state == "verified"
    assert tab.flexb is True


def test_download_blobs_corrupt_json_sets_conflict(window) -> None:
    """Хвост валиден, а JSON битый — конфигурацию не импортируем
    (старые правила на месте), но владелец — камень: conflict
    (ревью CM-R7)."""
    w, serial, _c, _t, _d = window
    tab = w._flexible_tab
    tab.rules = [{"title": "старые"}]
    blob = bytearray(build_flex_blob([]))
    # Портим JSON-часть (не хвост): контейнер грузится, правила нет.
    blob[12] = ord("!")
    blob[13] = ord("!")
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.info_outcomes[STORAGE_REGION_VAR] = _flex_info(0)
    serial.download_by_region[STORAGE_REGION_FLEX] = bytes(blob)
    w._download_config_blobs()
    assert tab.flex_state == "conflict"
    assert tab.rules == [{"title": "старые"}]   # не затёрли


def test_proto11_area_is_no_tail(window) -> None:
    """Протокол 11 хранит блобы, но хвост не исполняет — владелец ПК
    даже при валидном FLXB в области (ревью CM-R5/R7)."""
    w, serial, _c, _t, _d = window
    serial.proto = 11
    blob = build_flex_blob([])
    serial.info_outcomes[STORAGE_REGION_FLEX] = _flex_info(len(blob))
    serial.download_by_region[STORAGE_REGION_FLEX] = blob
    w._refresh_device_flexb()
    assert w._flexible_tab.flex_state == "no-tail"
