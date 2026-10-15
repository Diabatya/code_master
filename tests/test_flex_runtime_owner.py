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
    # Блоб собран под протокол 12: aux-условие на камне не
    # выражается → программа остаётся на ПК (на proto 13 она
    # компилировалась бы в запись COND_AUX).
    analysis = analyze_device_flex(
        build_flex_blob([hw, pc], protocol=12)
    )
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


# ------------------------------------------------------------------
# CM-R12: поколение исполнения — таймеры старой сессии/снимка молчат
# ------------------------------------------------------------------


def test_old_session_exec_delay_silent_after_reconfirm(tab) -> None:
    """Запланированное сессии N: reset → no-tail сессии N+1 ДО
    таймера — команда старой сессии не уходит через новый
    разрешённый транспорт."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=80)])
    tab.process_frame(_rx_frame())
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    QTest.qWait(160)
    assert tab._sent == []


def test_old_session_frame_delay_silent_after_reconfirm(tab) -> None:
    """«Пауза перед отправкой»/count/between — QTimer-цепочка
    действия, привязана к поколению тем же способом."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(delay=80, count=2, between=20)])
    tab.process_frame(_rx_frame())
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    QTest.qWait(180)
    assert tab._sent == []


def test_old_session_aux_delay_silent_after_reconfirm(tab) -> None:
    """aux_delay — команда доп канала старой сессии не уходит."""
    aux: list = []
    tab._serial_manager.send_aux = lambda *a, **kw: aux.append((a, kw))
    tab.set_device_flex_state("no-tail")
    rule = _frame_rule(aux_delay=80)
    rule["action"].update(
        frame_enabled=False, aux_enabled=True,
        aux_channel=1, aux_mode="on",
    )
    tab.set_config([rule])
    tab.process_frame(_rx_frame())
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    QTest.qWait(160)
    assert aux == []
    assert tab._sent == []


def test_exec_delay_with_abort_dies_on_reconfirm(tab) -> None:
    """exec_delay + «Прервать если»: запись жила в _pending_actions —
    гейт поколения срабатывает до прерывателя и busy."""
    tab.set_device_flex_state("no-tail")
    rule = _frame_rule(exec_delay=80)
    rule["action"]["abort"] = {
        "type": "frame", "id": "999", "channel": 2,
        "dlc": 0, "data": "",
    }
    tab.set_config([rule])
    tab.process_frame(_rx_frame())
    assert tab._pending_actions  # с abort — зарегистрировано
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    QTest.qWait(160)
    assert tab._sent == []


def test_cache_send_action_dies_on_reconfirm(tab) -> None:
    """«Отправить КЭШ» — буферный кадр старой сессии не уходит."""
    tab.set_device_flex_state("no-tail")
    tab._cache_bufs["C"] = {
        "buf1": None,
        "buf2": {"id": 0x99, "data": b"\x01", "channel": 0},
    }
    rule = _frame_rule()
    rule["action"].update(
        frame_enabled=False, cachevar="C", cv_op="send",
        cv_count=1, cv_pause=30, cv_channel=0,
    )
    tab.set_config([rule])
    tab.process_frame(_rx_frame())
    # Отправки уходят с паузой n*30 — первая немедленно.
    assert len(tab._sent) == 1
    # Запланированная вторая (count=2) умрёт со сменой сессии:
    rule2 = _frame_rule()
    rule2["action"].update(
        frame_enabled=False, cachevar="C", cv_op="send",
        cv_count=2, cv_pause=80, cv_channel=0,
    )
    tab._sent.clear()
    tab.set_config([rule2])
    tab.process_frame(_rx_frame())
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    QTest.qWait(200)
    assert len(tab._sent) == 1  # только немедленная, отложенная мертва


def test_old_callback_does_not_touch_new_busy(tab) -> None:
    """Старый таймер того же rule_index не декрементит _rule_busy
    новой программы — поколение проверяется ДО трога состояния."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=60)])
    tab.process_frame(_rx_frame())
    assert tab._rule_busy.get(0) == 1
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    # Тот же индекс 0, новое поколение: busy нового действия.
    tab.set_config([_frame_rule(exec_delay=250)])
    tab.process_frame(_rx_frame())
    assert tab._rule_busy.get(0) == 1
    QTest.qWait(120)  # старый таймер сработал бы здесь
    assert tab._rule_busy.get(0) == 1  # занятость новой сессии цела
    QTest.qWait(220)
    assert len(tab._sent) == 1  # новая команда исполнилась


def test_rapid_resets_long_timer_from_first_session(tab) -> None:
    """Несколько быстрых reset/подтверждений: длинный таймер самой
    первой сессии не возвращается; новая сессия работает."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(delay=150)])
    tab.process_frame(_rx_frame())
    for _ in range(3):
        tab.reset_device_flex_state()
        tab.set_device_flex_state("no-tail")
    tab.process_frame(_rx_frame())  # новая сессия: своё расписание
    QTest.qWait(240)
    assert len(tab._sent) == 1


def test_old_session_timer_silent_after_reconfirm_verified(tab) -> None:
    """Смешанные managed/PC: reset → verified-подтверждение новой
    сессии — запланированное старой сессии молчит."""
    hw = _frame_rule(event_id="200", title="HW")
    pc = _frame_rule(event_id="321", title="PC", exec_delay=80)
    analysis = analyze_device_flex(build_flex_blob([hw, pc]))
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [True, False]
    tab.set_config([hw, pc])
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame(0x321))  # PC-программа планирует delay
    tab.reset_device_flex_state()
    tab.apply_device_flex_analysis(analysis)  # verified новой сессии
    QTest.qWait(160)
    assert tab._sent == []


def test_verified_transition_kills_pending_old_runtime(tab) -> None:
    """Смена владельца/снимка до таймера: действие СТАРОЙ версии
    программы не уходит под новым владением (CM-R12 п.5)."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=80)])
    tab.process_frame(_rx_frame())
    new = _frame_rule(event_id="555", title="NEW")
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    QTest.qWait(160)
    assert tab._sent == []


# ------------------------------------------------------------------
# CM-R13: verified атомарно заменяет исполняемые internal rules
# ------------------------------------------------------------------


def test_verified_swaps_internal_rules(tab) -> None:
    """Сначала построен старый runtime обработкой кадра, потом
    verified: старый кадр молчит, новый исполняется."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())  # internal=OLD, dirty=False
    tab._sent.clear()
    new = _frame_rule(
        event_id="555", title="NEW", id="888", exec_delay=1
    )
    analysis = analyze_device_flex(build_flex_blob([new]))
    assert analysis["state"] == "verified"
    tab.apply_device_flex_analysis(analysis)
    assert tab._internal_rules[0]["rule"]["title"] == "NEW"
    assert not tab._rules_dirty
    tab.process_frame(_rx_frame())  # старый ID — молчим
    QTest.qWait(40)
    assert tab._sent == []
    tab.process_frame(_rx_frame(0x555))  # новый — PC-only: шлёт
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_verified_mixed_managed_switches_owners(tab) -> None:
    """OLD [PC-only] → NEW [managed, PC-only]: индексы из анализа —
    managed молчит, PC-only исполняется."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())
    tab._sent.clear()
    hw = _frame_rule(event_id="200", title="HW")
    pc = _frame_rule(event_id="321", title="PC", exec_delay=1, id="888")
    analysis = analyze_device_flex(build_flex_blob([hw, pc]))
    assert analysis["managed"] == [True, False]
    tab.apply_device_flex_analysis(analysis)
    assert tab._device_managed == {0}
    tab.process_frame(_rx_frame(0x200))
    QTest.qWait(40)
    assert tab._sent == []
    tab.process_frame(_rx_frame(0x321))
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_managed_to_pc_transition(tab) -> None:
    """Обратный переход: OLD managed → NEW PC-only — владелец
    сменился, ПК исполняет новый снимок."""
    hw = _frame_rule(event_id="200", title="HW")
    tab.set_config([hw])
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([hw]))
    )
    tab.process_frame(_rx_frame(0x200))
    assert tab._sent == []  # managed — камень молчит для ПК
    pc = _frame_rule(event_id="200", title="HW", exec_delay=1)
    analysis = analyze_device_flex(build_flex_blob([pc]))
    assert analysis["managed"] == [False]
    tab.apply_device_flex_analysis(analysis)
    assert tab._device_managed == set()
    tab.process_frame(_rx_frame(0x200))
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_verified_preserves_draft_and_executes_new(tab) -> None:
    """Readback при dirty-черновике: flexible_rules не затираются,
    исполняется подтверждённый NEW."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule()])
    tab.process_frame(_rx_frame())
    tab._sent.clear()
    tab._config.set("flexible_rules", [_frame_rule(title="DRAFT")])
    new = _frame_rule(event_id="555", title="NEW", exec_delay=1)
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    assert tab._config.get("flexible_rules")[0]["title"] == "DRAFT"
    tab.process_frame(_rx_frame(0x555))
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_pending_old_rule_dies_on_verified(tab) -> None:
    """Отложенное действие старого снимка после verified —
    политика CM-R12: смена поколения гасит его."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_frame_rule(exec_delay=80)])
    tab.process_frame(_rx_frame())
    new = _frame_rule(event_id="555", title="NEW")
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    QTest.qWait(160)
    assert tab._sent == []


def test_boot_event_uses_new_runtime(tab) -> None:
    """«Включение устройства» после verified: исполняется новый
    снимок, а не прежний internal."""
    new = _frame_rule(title="BOOT", exec_delay=1)
    new["events"] = [{"type": "power"}]
    tab.set_config([_frame_rule()])
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    tab.notify_device_ready()
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_aux_event_uses_new_runtime(tab) -> None:
    """Событие «Доп канал» после verified: новый снимок."""
    new = _frame_rule(title="AUX", exec_delay=1)
    new["events"] = [{"type": "aux", "channel": 1, "state": 1}]
    tab.set_config([_frame_rule()])
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    tab._aux_states[1] = 1
    tab._fire_aux_events()
    QTest.qWait(60)
    assert len(tab._sent) == 1


# ------------------------------------------------------------------
# CM-R14: повторная верификация НЕИЗМЕННОГО снимка — не отменяет
# законные команды текущей сессии
# ------------------------------------------------------------------


def test_identical_reverify_keeps_exec_delay(tab) -> None:
    """Пробник ревью: тот же verified-анализ повторно — без reset,
    смены устройства, правил и владельца. Поколение и команда живы."""
    rule = _frame_rule(exec_delay=80)
    analysis = analyze_device_flex(build_flex_blob([rule]))
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [False]
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())
    gen = tab._exec_generation
    tab.apply_device_flex_analysis(analysis)  # идентичное — не смена
    assert tab._exec_generation == gen
    QTest.qWait(160)
    assert len(tab._sent) == 1


def test_identical_reverify_keeps_send_series(tab) -> None:
    """Повторная вычитка МЕЖДУ повторами серии delay/count/between:
    серия сохраняет число и расписание."""
    rule = _frame_rule(exec_delay=10, delay=40, count=2, between=70)
    analysis = analyze_device_flex(build_flex_blob([rule]))
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())
    QTest.qWait(80)  # первая отправка (10+40) уже ушла
    assert len(tab._sent) == 1
    tab.apply_device_flex_analysis(analysis)  # та же вычитка
    QTest.qWait(160)  # вторая на 10+40+70
    assert len(tab._sent) == 2


def test_identical_reverify_keeps_aux_delay(tab) -> None:
    """aux_delay старого поколения НЕ отменяется идентичной
    верификацией."""
    aux: list = []
    tab._serial_manager.send_aux = lambda *a, **kw: aux.append((a, kw))
    rule = _frame_rule(exec_delay=10, aux_delay=60)
    rule["action"].update(
        frame_enabled=False, aux_enabled=True,
        aux_channel=1, aux_mode="on",
    )
    analysis = analyze_device_flex(build_flex_blob([rule]))
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())
    tab.apply_device_flex_analysis(analysis)
    QTest.qWait(160)
    assert len(aux) == 1


def test_probe_failed_then_same_verified_keeps_actions(tab) -> None:
    """mark_probe_failed → успешное чтение ТОГО ЖЕ verified:
    разрешённые действия не теряются, диагностика снимается."""
    rule = _frame_rule(exec_delay=80)
    analysis = analyze_device_flex(build_flex_blob([rule]))
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())
    tab.mark_device_flex_probe_failed()
    assert tab._device_flex_unconfirmed
    gen = tab._exec_generation
    tab.apply_device_flex_analysis(analysis)
    assert not tab._device_flex_unconfirmed
    assert tab._exec_generation == gen
    QTest.qWait(160)
    assert len(tab._sent) == 1


def test_changed_verified_still_invalidates(tab) -> None:
    """Контроль: ДРУГИЕ правила в verified — прежняя команда гасится
    (политика смены контекста сохранена)."""
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([_frame_rule(exec_delay=80)]))
    )
    tab.process_frame(_rx_frame())
    new = _frame_rule(event_id="555", title="NEW", exec_delay=1)
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    QTest.qWait(160)
    assert len(tab._sent) == 0  # старая команда убита сменой снимка
    tab.process_frame(_rx_frame(0x555))
    QTest.qWait(60)
    assert len(tab._sent) == 1


def test_changed_managed_still_invalidates(tab) -> None:
    """managed-набор изменился (правило ушло на камень) — прежняя
    PC-команда отменяется: исполнять её значило бы дубль."""
    pc = _frame_rule(exec_delay=80)
    a1 = analyze_device_flex(build_flex_blob([pc]))
    assert a1["managed"] == [False]
    tab.apply_device_flex_analysis(a1)
    tab.process_frame(_rx_frame())
    # Теперь то же правило компилируемо (exec_delay убран из JSON) —
    # владелец сменился на камень.
    hw = _frame_rule()
    a2 = analyze_device_flex(build_flex_blob([hw]))
    assert a2["managed"] == [True]
    tab.apply_device_flex_analysis(a2)
    QTest.qWait(160)
    assert tab._sent == []


def test_reset_then_same_verified_still_kills_old(tab) -> None:
    """Reset уже сменил поколение: та же verified-вычитка новой
    сессии не воскрешает старые коллбеки и не блокирует новые."""
    rule = _frame_rule(exec_delay=80)
    analysis = analyze_device_flex(build_flex_blob([rule]))
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())          # старый таймер
    tab.reset_device_flex_state()
    tab.apply_device_flex_analysis(analysis)  # новая сессия, тот же блоб
    QTest.qWait(160)
    assert tab._sent == []
    tab.process_frame(_rx_frame())          # новая команда работает
    QTest.qWait(160)
    assert len(tab._sent) == 1


# ------------------------------------------------------------------
# CM-R15: записи старых поколений не копятся в _pending_actions
# ------------------------------------------------------------------


def _abort_rule(exec_delay: int = 30) -> dict[str, Any]:
    rule = _frame_rule(exec_delay=exec_delay)
    rule["action"]["abort"] = {
        "type": "frame", "id": "999", "channel": 2,
        "dlc": 0, "data": "",
    }
    return rule


def test_stale_pending_removed_on_reset(tab) -> None:
    """Пробник ревью: 3× (schedule + reset→no-tail) — живой список
    не держит записи прошлых поколений."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_abort_rule()])
    for _ in range(3):
        tab.process_frame(_rx_frame())
        assert len(tab._pending_actions) == 1
        tab.reset_device_flex_state()
        assert tab._pending_actions == []  # убрано при инвалидации
        tab.set_device_flex_state("no-tail")
        QTest.qWait(60)
    assert tab._pending_actions == []
    assert tab._sent == []


def test_stale_pending_removed_on_verified_swap(tab) -> None:
    """Замена снимка тоже чистит живой список."""
    rule = _abort_rule(60)
    tab.set_config([rule])
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([rule]))
    )
    tab.process_frame(_rx_frame())
    assert len(tab._pending_actions) == 1
    new = _frame_rule(event_id="555", title="NEW")
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([new]))
    )
    assert tab._pending_actions == []
    QTest.qWait(120)
    assert tab._sent == []


def test_stale_pending_removed_on_conflict(tab) -> None:
    """verified→conflict: pending старого контекста уходит из
    списка сразу, не дожидаясь таймеров."""
    rule = _abort_rule(60)
    tab.set_config([rule])
    tab.apply_device_flex_analysis(
        analyze_device_flex(build_flex_blob([rule]))
    )
    tab.process_frame(_rx_frame())
    assert len(tab._pending_actions) == 1
    other = _frame_rule(event_id="200", title="OTHER")
    conflict = analyze_device_flex(
        _conflict_blob(tail_rules=[rule], json_rules=[other])
    )
    assert conflict["state"] == "conflict"
    tab.apply_device_flex_analysis(conflict)
    assert tab._pending_actions == []


def test_identical_reverify_keeps_pending_list(tab) -> None:
    """Неизменная верификация сохраняет и pending-записи
    (CM-R14 + CM-R15 вместе)."""
    rule = _abort_rule(80)
    analysis = analyze_device_flex(build_flex_blob([rule]))
    tab.set_config([rule])
    tab.apply_device_flex_analysis(analysis)
    tab.process_frame(_rx_frame())
    assert len(tab._pending_actions) == 1
    tab.apply_device_flex_analysis(analysis)
    assert len(tab._pending_actions) == 1
    assert not tab._pending_actions[0]["aborted"]
    QTest.qWait(160)
    assert len(tab._sent) == 1


def test_new_pending_same_index_after_reset(tab) -> None:
    """Новая запись с тем же rule_index после reset живёт в
    чистом списке и исполняется; поздний старый таймер ничего
    не трогает."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_abort_rule(40)])
    tab.process_frame(_rx_frame())
    tab.reset_device_flex_state()
    tab.set_device_flex_state("no-tail")
    tab.process_frame(_rx_frame())  # index 0, новое поколение
    assert len(tab._pending_actions) == 1
    entry = tab._pending_actions[0]
    assert entry["gen"] == tab._exec_generation
    QTest.qWait(90)
    assert len(tab._sent) == 1
    assert tab._pending_actions == []


def test_abort_event_still_cancels_same_generation(tab) -> None:
    """Прерыватель текущего поколения работает — гард CM-R15
    не сломал штатный abort."""
    tab.set_device_flex_state("no-tail")
    tab.set_config([_abort_rule(80)])
    tab.process_frame(_rx_frame())
    tab.process_frame(_rx_frame(0x999))  # событие-прерыватель
    QTest.qWait(140)
    assert tab._sent == []
    assert tab._pending_actions == []
