"""Регрессия: автовычитка при подключении не должна молча затирать
открытую конфигурацию содержимым устройства (полевая жалоба:
«подтянул хлам и перезаписал всё»)."""

import sys
from typing import Any, Dict, List

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
        self._data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def _cfg_trigger(recv_id: str = "111", tx_id: str = "222") -> Dict[str, Any]:
    return {
        "active": True,
        "recv_id": recv_id,
        "recv_dlc": 1,
        "recv_data": "AA",
        "recv_channel": 0,
        "recv_bit": 0,
        "recv_rtr": 0,
        "responses": [
            {"channel": 0, "bit": 0, "id": tx_id, "dlc": 1, "data": "BB", "delay": 0}
        ],
    }


def _device_records(tab: CanTriggerTab, indexes: List[int]) -> List[Dict[str, Any]]:
    """Записи устройства в том виде, в каком их вернёт вычитка."""
    records = []
    for index in indexes:
        values = tab._device_trigger_values(index)
        if not tab._is_device_representable(tab._blocks[index]):
            values["enabled"] = 0
        records.append(unpack_trigger(pack_trigger(values)))
    return records


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


def test_matching_device_state_applies(tab) -> None:
    """UI совпадает с устройством — вычитка применяется штатно."""
    tab.set_config([_cfg_trigger()])
    records = _device_records(tab, [0])
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True


def test_diverged_device_state_keeps_ui(tab) -> None:
    """Устройство расходится с открытой конфигурацией — экран сохранён,
    автозамена пропущена."""
    tab.set_config([_cfg_trigger()])
    other = _device_records(tab, [0])[0]
    other["rx_id"] = 0x333
    records = [unpack_trigger(pack_trigger(other))]
    tab._read_device_triggers = lambda: records

    assert tab.sync_from_device() is False
    assert len(tab._blocks) == 1
    assert tab._blocks[0]["recv"]["id"].text() == "111"


def test_force_read_replaces_ui(tab) -> None:
    """Ctrl+R (force) — оператор явно просит замену, устройство применяется."""
    tab.set_config([_cfg_trigger()])
    other = _device_records(tab, [0])[0]
    other["rx_id"] = 0x333
    records = [unpack_trigger(pack_trigger(other))]
    tab._read_device_triggers = lambda: records

    assert tab.sync_from_device(force=True) is True
    assert len(tab._blocks) == 1
    assert tab._blocks[0]["recv"]["id"].text() == "333"


def test_empty_ui_filled_from_device(tab) -> None:
    """Пустой экран (свежая вкладка) заполняется истиной устройства —
    прежнее поведение для чистого запуска."""
    tab.set_config([_cfg_trigger()])
    records = _device_records(tab, [0])
    tab.set_config([])
    assert len(tab._blocks) == 0
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1
