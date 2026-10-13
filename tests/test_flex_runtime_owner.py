"""Владелец исполнения ГЛ на реальной FlexibleLogicTab
(ревью CM-R9/R10/R11):

- холодный старт = «unknown» → ПК-исполнение приостановлено;
- «conflict» блокирует все пути (кадр, «Включение устройства»,
  отложенные QTimer-действия);
- «verified» атомарно применяет rules+managed из той же проверки;
- «conflict» не затирает исполняемый снимок конфигурацией из JSON;
- смена сессии/устройства сбрасывает владение и убивает
  запланированные действия старой сессии.
"""

import json
import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.flex_compiler import analyze_device_flex, build_flex_blob
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
    """Свежая вкладка БЕЗ установленного состояния владельца —
    как при запуске приложения (начальное «unknown», CM-R10)."""
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    widget._sent: list[bytes] = []
    widget._serial_manager.send_data = widget._sent.append
    yield widget
    widget.deleteLater()


def _frame_rule(
    event_id: str = "123",
    *,
    title: str = "P",
    **action_over: Any,
) -> dict[str, Any]:
    """Простая программа «кадр → отправить кадр»."""
    action: dict[str, Any] = {
        "frame_enabled": True, "channel": 0, "id": "777",
        "dlc": 1, "data": "AA", "count": 1,
    }
    action.update(action_over)
    return {
        "title": title, "active": True,
        "events": [{"type": "frame", "channel": 2, "extended": False,
                    "id": event_id, "dlc": 0, "data": "",
                    "fire_limit": 0}],
        "conditions": [{"type": "none"}],
        "action": action,
    }


def _rx_frame(can_id: int = 0x123) -> dict[str, Any]:
    return {"id": can_id, "channel": 1, "data": b"",
            "extended": False, "rtr": False}


def _conflict_blob(
    tail_rules: list[dict[str, Any]],
    json_rules: list[dict[str, Any]],
) -> bytes:
    """Валидный FLXB: хвост скомпилирован из tail_rules, а JSON
    описывает json_rules — геометрия цела, соответствия нет."""
    blob = build_flex_blob(tail_rules)
    json_len = int.from_bytes(blob[8:12], "little")
    tail = blob[12 + json_len:]
    json_part = json.dumps(
        {"kind": "code_master_flex_logic", "format": 1,
         "rules": json_rules},
        ensure_ascii=False,
    ).encode("utf-8")
    return (
        b"FLXB" + bytes((1, len(tail) // 90, 0, 0))
        + len(json_part).to_bytes(4, "little") + json_part + tail
    )


# ------------------------------------------------------------------
# CM-R10: начальное «unknown» и сбой зондирования
# ------------------------------------------------------------------


def test_cold_start_unknown_blocks_frame_execution(tab) -> None:
    """До первого подтверждённого зондирования владелец неизвестен —
    кадровое событие НЕ отправляет команду."""
    assert tab.device_flex_state() == ("unknown", False)
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())
    assert tab._sent == []


def test_cold_start_unknown_blocks_boot_event(tab) -> None:
    """«Включение устройства» в состоянии unknown не исполняется:
    если камень живёт под хвостом FLXB, он отработал её сам."""
    rule = _frame_rule()
    rule["events"] = [{"type": "power"}]
    tab.set_config([rule])
    tab.notify_device_ready()
    assert tab._sent == []


def test_probe_failure_does_not_promote_unknown(tab) -> None:
    """Сбой чтения области при unknown — остаётся unknown с флагом
    «не подтверждён» (CM-R5/R10): keep-last работает только для
    подтверждённого владения, исполнение не разблокируется."""
    tab.set_config([_frame_rule()])
    tab.mark_device_flex_probe_failed()
    assert tab.device_flex_state() == ("unknown", True)
    tab.process_frame(_rx_frame())
    assert tab._sent == []


def test_probe_failure_keeps_confirmed_owner(tab) -> None:
    """Обратная сторона keep-last: verified-владение этой сессии
    переживает разовый сбой вычитки — НЕ managed-программы
    продолжают исполняться на ПК."""
    hw = _frame_rule(event_id="200", title="HW")
    pc = _frame_rule(event_id="321", title="PC")
    pc["conditions"] = [{"type": "aux", "channel": 1, "state": 1}]
    analysis = analyze_device_flex(build_flex_blob([hw, pc]))
    assert analysis["state"] == "verified"
    tab.apply_device_flex_analysis(analysis)
    tab.mark_device_flex_probe_failed()
    assert tab.device_flex_state() == ("verified", True)
    # HW — на камне (managed): ПК молчит.
    tab.process_frame(_rx_frame(0x200))
    assert tab._sent == []
    # PC — на ПК (условие ПРИ на камне не выражается): исполняется.
    tab._aux_states[1] = 1
    tab.process_frame(_rx_frame(0x321))
    assert len(tab._sent) == 1


# ------------------------------------------------------------------
# CM-R9: conflict блокирует всё, включая уже запланированное
# ------------------------------------------------------------------


def test_conflict_blocks_process_frame_and_boot(tab) -> None:
    """После conflict владелец «камень исполняет что-то иное» —
    ПК не включается поверх работающего хвоста."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())
    assert len(tab._sent) == 1

    analysis = analyze_device_flex(
        _conflict_blob(
            [_frame_rule(event_id="555", title="OTHER")],
            [_frame_rule(event_id="666", title="ALIEN")],
        )
    )
    assert analysis["state"] == "conflict"
    tab.apply_device_flex_analysis(analysis)
    tab._sent.clear()

    tab.process_frame(_rx_frame())
    boot = _frame_rule(title="BOOT")
    boot["events"] = [{"type": "power"}]
    tab.set_config([boot])
    tab.notify_device_ready()
    assert tab._sent == []


def test_delayed_action_scheduled_before_conflict_does_not_fire(
    tab,
) -> None:
    """Действие с «Задержкой», запланированное при подтверждённом
    владении ПК, отбрасывается гейтом в момент срабатывания
    QTimer — переход в conflict не даёт ему уйти на шину."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=120)])
    tab.process_frame(_rx_frame())
    tab.apply_device_flex_analysis({"state": "conflict",
                                    "rules": None, "managed": []})
    QTest.qWait(220)
    assert tab._sent == []


# ------------------------------------------------------------------
# CM-R11: verified синхронизирует снимок; conflict его не трогает
# ------------------------------------------------------------------


def test_verified_applies_rules_and_managed_atomically(tab) -> None:
    """Verified-вычитка применяет rules и managed ИЗ ТОЙ ЖЕ проверки:
    исполняемый снимок становится содержимым камня, а не прежним."""
    old = _frame_rule(event_id="123", title="OLD")
    tab.set_config([old])
    tab.set_device_flex_state("no-tail")

    new = _frame_rule(event_id="555", title="NEW")
    analysis = analyze_device_flex(build_flex_blob([new]))
    assert analysis["state"] == "verified"
    tab.apply_device_flex_analysis(analysis)

    assert tab.device_flex_state() == ("verified", False)
    assert tab._runtime_rules[0]["events"][0]["id"] == "555"
    assert tab._runtime_rules[0]["title"] == "NEW"
    assert tab._device_managed == {0}
    # Событие managed-программы — ПК молчит (исполняет камень).
    tab.process_frame(_rx_frame(0x555))
    assert tab._sent == []


def test_conflict_preserves_runtime_snapshot(tab) -> None:
    """Conflict: JSON устройства НЕ импортируется как доказанный
    runtime — исполняемый снимок остаётся прежним, managed пуст,
    исполнение приостановлено."""
    tab.set_config([_frame_rule()])
    tab.set_device_flex_state("no-tail")
    before = list(tab._runtime_rules)

    analysis = analyze_device_flex(
        _conflict_blob(
            [_frame_rule(event_id="555", title="OTHER")],
            [_frame_rule(event_id="666", title="ALIEN")],
        )
    )
    assert analysis["state"] == "conflict"
    tab.apply_device_flex_analysis(analysis)

    assert tab.device_flex_state() == ("conflict", False)
    assert tab._runtime_rules == before
    assert tab._device_managed == set()
    tab.process_frame(_rx_frame())
    assert tab._sent == []


# ------------------------------------------------------------------
# CM-R10: смена сессии/устройства
# ------------------------------------------------------------------


def test_session_reset_invalidates_state_and_pending(tab) -> None:
    """Разрыв связи/смена устройства: владение → unknown, managed
    очищается, запланированные таймеры прошлой сессии не стреляют."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=150)])
    tab.process_frame(_rx_frame())
    # Сессия закрылась до срабатывания таймера.
    tab.reset_device_flex_state()
    assert tab.device_flex_state() == ("unknown", False)
    assert tab._device_managed == set()
    QTest.qWait(240)
    assert tab._sent == []
    # Новый кадр в незондированной сессии — под тем же гейтом.
    tab.process_frame(_rx_frame())
    assert tab._sent == []


def test_confirmed_state_allows_execution(tab) -> None:
    """Контрольный позитив: подтверждённый «no-tail» владелец —
    ПК исполняет программы как раньше."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())
    assert len(tab._sent) == 1
