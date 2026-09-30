"""Гибкая логика: схемы событий/условий/действий, семантика
ИЛИ-событий и И-условий, исполнение доп. каналов (CMD_AUX_SET)."""

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.serial_manager import SerialManager
import ui.flexible_logic_tab as fl_module
from ui.flexible_logic_tab import FlexibleLogicTab


class _FakeConfig:
    """Изолирует тест от реального config.json разработчика."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    yield widget
    widget.deleteLater()


def _frame_event(can_id: str = "123") -> dict[str, Any]:
    return {
        "type": "frame", "channel": 2, "extended": False,
        "id": can_id, "dlc": 8, "data": "", "fire_limit": 1,
    }


def test_aux_action_roundtrip(tab) -> None:
    """Действие «Доп канал» с импульсами/ШИМ пишется и читается
    обратно без потерь (схема flexible_rules)."""
    rule = {
        "title": "P1", "active": True,
        "events": [_frame_event()],
        "conditions": [{"type": "aux", "channel": 2, "state": 1}],
        "action": {
            "aux_enabled": True, "aux_channel": 3, "aux_mode": "pulse",
            "aux_delay": 250, "aux_pulse_on": 120, "aux_pulse_off": 80,
            "aux_pulse_count": 7, "aux_pwm_freq": 400,
            "aux_pwm_duty": 25, "aux_pwm_time": 3000,
        },
    }
    tab.set_config([rule])
    out = tab.get_config()
    action = out[0]["action"]
    assert action["aux_channel"] == 3 and action["aux_mode"] == "pulse"
    assert action["aux_delay"] == 250 and action["aux_pulse_count"] == 7
    assert action["aux_pwm_duty"] == 25 and action["aux_pwm_time"] == 3000
    cond = out[0]["conditions"][0]
    assert cond == {"type": "aux", "channel": 2, "state": 1}


def test_multiple_events_or_semantics(tab) -> None:
    """Несколько событий — ИЛИ: программа стартует по любому."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab.set_config([{
        "title": "OR", "active": True,
        "events": [_frame_event("123"), _frame_event("456")],
        "conditions": [{"type": "none"}],
        "action": {
            "frame_enabled": True, "channel": 0, "id": "777",
            "dlc": 1, "data": "AA", "count": 1,
        },
    }])
    tab.process_frame({"id": 0x456, "channel": 1, "data": b"",
                       "extended": False, "rtr": False})
    assert sent, "второе событие не запустило программу (ИЛИ)"
    tab.process_frame({"id": 0x123, "channel": 1, "data": b"",
                       "extended": False, "rtr": False})
    assert len(sent) == 2, "первое событие не запустило программу"


def test_multiple_conditions_and_semantics(tab) -> None:
    """Несколько условий — И: действия только когда выполнены все."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab._static_states["Дверь"] = 0
    tab.set_config([{
        "title": "AND", "active": True,
        "events": [_frame_event()],
        "conditions": [
            {"type": "static", "var": "Дверь", "state": 1},
            {"type": "aux", "channel": 1, "state": 1},
        ],
        "action": {
            "frame_enabled": True, "channel": 0, "id": "777",
            "dlc": 1, "data": "AA", "count": 1,
        },
    }])
    # fire_limit=1: одинаковая DATA стреляет один раз — разные кадры,
    # чтобы каждая попытка проходила событие.
    def frame(n: int) -> dict[str, Any]:
        return {"id": 0x123, "channel": 1, "data": bytes((n,)),
                "extended": False, "rtr": False}

    # Ни одно условие не выполнено.
    tab.process_frame(frame(1))
    assert sent == []
    # Выполнено одно из двух — всё равно нет действия (И).
    tab._static_states["Дверь"] = 1
    tab.process_frame(frame(2))
    assert sent == []
    # Оба — программа срабатывает.
    tab._aux_states[1] = 1
    tab.process_frame(frame(3))
    assert sent, "условия И не соблюдены — действие не выполнено"


def test_aux_action_sends_cmd_and_mirrors_state(tab) -> None:
    """Действие «Вкл доп канал» шлёт CMD_AUX_SET и обновляет
    ПК-зеркало состояния для условий «активен/не активен»."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab.set_config([{
        "title": "AUX", "active": True,
        "events": [_frame_event()],
        "conditions": [{"type": "none"}],
        "action": {"aux_enabled": True, "aux_channel": 2,
                   "aux_mode": "on"},
    }])
    tab.process_frame({"id": 0x123, "channel": 1, "data": b"",
                       "extended": False, "rtr": False})
    assert sent, "CMD_AUX_SET не отправлен"
    packet = sent[0]
    assert packet[0] == 0xD3 and packet[1] == 15
    assert packet[2] == 2 and packet[3] == 1  # channel=2, mode=on
    assert tab._aux_states[2] == 1


def test_legacy_rule_migration_unchanged(tab) -> None:
    """Старый формат (id/mask/condition_data → resp_*) мигрирует
    в событие-фрейм + действие-фрейм — регрессия."""
    tab.set_config([{
        "id": "123", "mask": "FF", "condition_data": "AA",
        "resp_id": "777", "resp_data": "BB", "resp_mask": "FF",
        "resp_channel": "1",
    }])
    out = tab.get_config()
    rule = out[0]
    assert rule["events"][0]["type"] == "frame"
    assert rule["events"][0]["id"] == "123"
    assert rule["action"]["frame_enabled"] is True
    assert rule["action"]["id"] == "777"


def test_aux_event_fires_on_state_change(tab) -> None:
    """Событие «Доп канал N активен» срабатывает при смене состояния
    канала, а не ждёт следующего CAN-кадра."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab.set_config([{
        "title": "AUX-EVENT", "active": True,
        "events": [{"type": "aux", "channel": 1, "state": 1}],
        "conditions": [{"type": "none"}],
        "action": {
            "frame_enabled": True, "channel": 0, "id": "777",
            "dlc": 1, "data": "AA", "count": 1,
        },
    }])
    tab._fire_aux_events()  # канал не активен — тишина
    assert sent == []
    tab._aux_states[1] = 1
    tab._fire_aux_events()  # канал включился — фронт
    assert sent, "событие «Доп канал активен» не сработало"
