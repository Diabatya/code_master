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


class _FakeVariablesTab:
    """Стаб вкладки «Переменные» для кэш-переменных — только то,
    что читает рантайм ГЛ (export_config/set_flag_live)."""

    def __init__(self, read: list[dict[str, Any]] | None = None) -> None:
        self._read = read or []

    def export_config(self) -> dict[str, Any]:
        return {"read": self._read, "control": [], "aux": []}

    def variable_names(
        self, column: str, var_type: Any = None
    ) -> list[str]:
        data = self.export_config()
        names = []
        for cfg in data.get(column) or []:
            if var_type is None or cfg.get("type") == var_type:
                names.append(str(cfg.get("name", "")).strip())
        return names

    def set_flag_live(self, _name: str, _value: int) -> None:
        pass


_CACHE_VAR = {
    "type": "cache", "name": "КЭШ1",
    "id": "150", "byte_from": 1, "byte_to": 2,
    "extended": False, "storage": "ram",
}

# Легаси-схема «ID от–до» — при загрузке сжимается до нижней
# границы диапазона.
_CACHE_VAR_LEGACY = {
    "type": "cache", "name": "КЭШЛ",
    "id_from": "160", "id_to": "1FF",
    "extended": False, "storage": "ram",
}


def _rx_frame(can_id: int = 0x150, channel: int = 1,
              data: bytes = b"\x11\x22") -> dict[str, Any]:
    return {"id": can_id, "channel": channel, "data": data,
            "extended": False, "rtr": False}


def test_cache_var_capture_and_events(tab) -> None:
    """«Кэш переменная»: кадр с ID переменной пишет выбранные байты
    «от–до» в буфер 1 (ОЗУ) и ставит фронты «Приход DATA КЭШ»/
    «Записалась»; другой ID или невыбранный канал — тишина."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab._variables_tab = _FakeVariablesTab(
        [_CACHE_VAR, _CACHE_VAR_LEGACY]
    )
    tab.set_config([{
        "title": "CAP", "active": True,
        "events": [{
            "type": "cachevar", "var": "КЭШ1",
            "cache_op": "rx", "channel": 1,
        }],
        "conditions": [{"type": "none"}],
        "action": {
            "frame_enabled": True, "channel": 0, "id": "777",
            "dlc": 1, "data": "AA", "count": 1,
        },
    }])
    # Канал 2 — событие «Приход DATA КЭШ» настроено на CAN1.
    tab.process_frame(_rx_frame(channel=2))
    assert sent == []
    assert not (tab._cache_bufs.get("КЭШ1") or {}).get("buf1"), \
        "захват ограничен каналами событий «Приход DATA КЭШ»"
    # Канал 1, нужный ID — захват байт «от–до» в буфер 1 + событие.
    tab.process_frame(_rx_frame(channel=1, data=b"\xAA\xBB"))
    assert sent, "событие «Приход DATA КЭШ» не сработало"
    buf1 = tab._cache_bufs["КЭШ1"]["buf1"]
    assert buf1["id"] == 0x150 and bytes(buf1["data"]) == b"\xAA\xBB"
    # Следующий кадр того же ID перезаписывает буфер 1.
    tab.process_frame(_rx_frame(data=b"\x01\x02\x03"))
    assert bytes(tab._cache_bufs["КЭШ1"]["buf1"]["data"]) == b"\x01\x02"
    # Другой ID — буфер 1 не трогаем.
    tab.process_frame(_rx_frame(can_id=0x300, data=b"\xFF"))
    assert bytes(tab._cache_bufs["КЭШ1"]["buf1"]["data"]) == b"\x01\x02"
    # Легаси-переменная «ID от–до» захватывает нижнюю границу.
    tab.process_frame(_rx_frame(can_id=0x160, data=b"\x77"))
    assert tab._cache_bufs["КЭШЛ"]["buf1"]["id"] == 0x160
    # …но не середину диапазона — схемы «от–до» больше нет.
    tab.process_frame(_rx_frame(can_id=0x1AA, data=b"\x88"))
    assert tab._cache_bufs["КЭШЛ"]["buf1"]["id"] == 0x160


def test_cache_var_commit_condition_erase(tab) -> None:
    """«Записать КЭШ» переносит буфер 1 → 2 и обнуляет буфер 1;
    условия «Записан/Не записан КЭШ» читают буфер 2; «Стереть КЭШ»
    обнуляет буфер 2 и ставит фронт «Стирание»."""
    tab._variables_tab = _FakeVariablesTab([_CACHE_VAR])
    tab.set_config([{
        "title": "W", "active": True,
        "events": [{
            "type": "cachevar", "var": "КЭШ1",
            "cache_op": "rx", "channel": 1,
        }],
        "conditions": [{"type": "none"}],
        "actions": [{
            "type": "cachevar", "cachevar": "КЭШ1", "cv_op": "commit",
        }],
    }])
    cond_filled = {"type": "cachevar", "var": "КЭШ1",
                   "cache_op": "filled"}
    cond_empty = {"type": "cachevar", "var": "КЭШ1",
                  "cache_op": "empty"}
    assert tab._condition_passed(cond_empty)
    assert not tab._condition_passed(cond_filled)
    # Приход кадра → захват в буфер 1 + действие «Записать КЭШ».
    tab.process_frame(_rx_frame(data=b"\x55"))
    assert tab._cache_bufs["КЭШ1"]["buf1"] is None
    buf2 = tab._cache_bufs["КЭШ1"]["buf2"]
    assert buf2 is not None and bytes(buf2["data"])[:1] == b"\x55"
    assert tab._condition_passed(cond_filled)
    assert not tab._condition_passed(cond_empty)
    # «Стереть КЭШ» — буфер 2 обнуляется, условия переворачиваются.
    tab._run_cachevar_action("КЭШ1", {"cv_op": "erase"})
    assert tab._cache_bufs["КЭШ1"]["buf2"] is None
    assert tab._condition_passed(cond_empty)


def test_cache_var_send_and_schema_roundtrip(tab) -> None:
    """«Отправить КЭШ» шлёт кадр буфера 2 в выбранный CAN N раз;
    схемы события/условия/действия «Кэш переменная» переживают
    set_config → get_config."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab._variables_tab = _FakeVariablesTab([_CACHE_VAR])
    tab._cache_bufs["КЭШ1"] = {
        "buf1": None,
        "buf2": {"id": 0x150, "data": b"\xDE\xAD",
                 "extended": False, "channel": 1},
    }
    rule = {
        "title": "SEND", "active": True,
        "events": [_frame_event("123")],
        "conditions": [{
            "type": "cachevar", "var": "КЭШ1", "cache_op": "filled",
        }],
        "actions": [{
            "type": "cachevar", "cachevar": "КЭШ1",
            "cv_op": "send", "cv_channel": 1,
            "cv_count": 3, "cv_pause": 0,
        }],
    }
    tab.set_config([rule])
    out = tab.get_config()
    assert out[0]["events"][0]["type"] == "frame"
    cond = out[0]["conditions"][0]
    assert cond["type"] == "cachevar" and cond["cache_op"] == "filled"
    act = out[0]["actions"][0]
    assert act["type"] == "cachevar" and act["cv_op"] == "send"
    assert act["cachevar"] == "КЭШ1" and act["cv_count"] == 3
    tab.process_frame(_rx_frame(can_id=0x123))
    assert len(sent) == 3, "«Отправить КЭШ» должен отправить 3 кадра"


def test_program_started_event(tab) -> None:
    """Событие «Программа (имя) начала работать»: целевая программа
    прошла фазу событий → слушатель переходит к своим условиям;
    программа не слушает саму себя; старт доезжает один раз."""
    sent: list[bytes] = []
    tab._serial_manager.send_data = sent.append
    tab.set_config([
        {
            "title": "A", "active": True,
            "events": [_frame_event("123")],
            "conditions": [{"type": "none"}],
            "actions": [],
        },
        {
            "title": "B", "active": True,
            "events": [{"type": "program", "var": "A"}],
            "conditions": [{"type": "none"}],
            "actions": [{
                "frame_enabled": True, "channel": 0, "id": "777",
                "dlc": 1, "data": "AA", "count": 1,
            }],
        },
        {
            # Самослушание запрещено — C слушает C.
            "title": "C", "active": True,
            "events": [{"type": "program", "var": "C"}],
            "conditions": [{"type": "none"}],
            "actions": [{
                "frame_enabled": True, "channel": 0, "id": "888",
                "dlc": 1, "data": "BB", "count": 1,
            }],
        },
    ])
    tab.process_frame(_rx_frame(can_id=0x123))
    assert len(sent) == 1, "B должна стартовать на старте A"
    # Повторный проход по тому же старту не перезапускает B.
    tab._fire_aux_events()
    assert len(sent) == 1
    # Новый старт A — B срабатывает снова.
    tab.process_frame(_rx_frame(can_id=0x123, data=b"\x99"))
    assert len(sent) == 2
