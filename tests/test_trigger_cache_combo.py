"""Комбо «автозапись DATA в кэш + обычный ответ» одного триггера.

Проецируется в записи-филлеры кэша (cache_enabled=1, src_flags бит
CACHE_ONLY, протокол 8) и обычные ответные записи той же группы.
Раньше режимы были взаимоисключающими (отчёт мастера)."""

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.serial_manager import SerialManager
from core.trigger_protocol import pack_trigger, unpack_trigger
import ui.can_trigger_tab as trigger_module
from ui.can_trigger_tab import CanTriggerTab


class _FakeConfig:
    """Изолирует тест от реального config.json разработчика."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def _cfg_combo() -> dict[str, Any]:
    return {
        "active": True,
        "recv_id": "111",
        "recv_dlc": 1,
        "recv_data": "AA",
        "recv_channel": 0,
        "recv_bit": 0,
        "recv_rtr": 0,
        "cache": 1,
        "cache_rows": [{
            "channel": 0, "bit": 0, "id": "55", "dlc": 2,
            "from": "00 00", "to": "FF FF",
            "tx_channel": 0, "delay_before_send": 0,
            "delay_between": 0, "count": 1, "next_delay": 0,
            "listen_echo": 1, "fire_limit_enabled": 0, "fire_limit": 1,
        }],
        "responses": [
            {"channel": 0, "bit": 0, "id": "222", "dlc": 1, "data": "BB",
             "delay_before_send": 0, "delay_between": 0, "count": 1,
             "next_delay": 0}
        ],
    }


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(trigger_module, "Config", _FakeConfig)
    widget = CanTriggerTab(SerialManager())
    yield widget
    widget.deleteLater()


def _proto(tab: CanTriggerTab, version: int) -> None:
    tab._serial_manager._device_protocol_version = version


def test_combo_projects_filler_and_response(tab) -> None:
    """Кэш + заполненный ответ → филлер CACHE_ONLY + ответная запись."""
    _proto(tab, 8)
    tab.set_config([_cfg_combo()])
    records = tab._project_block_records(0)
    assert records is not None
    wire = [unpack_trigger(pack_trigger(r)) for r in records]
    fillers = [r for r in wire if r["cache_enabled"]]
    responses = [r for r in wire if not r["cache_enabled"]]
    assert len(fillers) == 1 and len(responses) == 1
    assert fillers[0]["src_cache_only"] is True
    assert fillers[0]["src_id"] == 0x55
    assert responses[0]["tx_id"] == 0x222
    assert responses[0]["cache_enabled"] == 0
    # Ответная запись не разрывает группу: group_seq ≠ 0 у второй строки.
    assert responses[0]["group_seq"] == 1


def test_cache_only_keeps_old_semantics(tab) -> None:
    """Кэш без заполненного ответа — прежняя запись (ответ = кэш)."""
    _proto(tab, 8)
    cfg = _cfg_combo()
    cfg["responses"] = []
    tab.set_config([cfg])
    records = tab._project_block_records(0)
    assert records is not None
    wire = [unpack_trigger(pack_trigger(r)) for r in records]
    assert len(wire) == 1
    assert wire[0]["cache_enabled"] == 1
    assert wire[0]["src_cache_only"] is False


def test_combo_needs_protocol_8(tab) -> None:
    """Старая прошивка бита CACHE_ONLY не знает — комбо исполняет ПК."""
    _proto(tab, 7)
    tab.set_config([_cfg_combo()])
    assert tab._project_block_records(0) is None


def test_combo_roundtrip_readback(tab) -> None:
    """Вычитка комбо-группы собирает обратно и кэш-строку, и ответ."""
    _proto(tab, 8)
    tab.set_config([_cfg_combo()])
    records = [
        unpack_trigger(pack_trigger(r))
        for r in tab._project_block_records(0)
    ]
    groups = tab._group_device_records(records)
    assert len(groups) == 1  # seq-сдвиг не разорвал группу
    tab.set_config([])
    tab._add_trigger_block()
    tab._apply_device_group(0, groups[0])
    block = tab._blocks[0]
    assert block["cache"]["cache_check"].isChecked() is True
    assert block["cache"]["rows"][0]["id"].text() == "055"
    assert block["response"]["rows"][0]["id"].text() == "222"


def test_cache_settings_hidden_until_enabled(tab) -> None:
    """Настройки автозаписи видны только при включённой галке."""
    tab.set_config([_cfg_combo()])
    block = tab._blocks[0]
    assert block["cache"]["fields_widget"].isHidden() is False
    block["cache"]["cache_check"].setChecked(False)
    assert block["cache"]["fields_widget"].isHidden() is True
    # Блок «Ответ» не глушится кэшем и обратно.
    assert block["response"]["group"].isEnabled() is True
