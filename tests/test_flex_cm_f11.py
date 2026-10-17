"""CM-F11: «вкл постоянно» для Вкл/ШИМ, единая «Задержка» вместо
легаси «Паузы до действия», автономный старт на МК, отмена
устаревших отложенных команд."""

from __future__ import annotations

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.aux_channels import migrate_aux_delay_snapshot
from core.flex_compiler import compile_rule
from core.serial_manager import SerialManager
import ui.flexible_logic_tab as fl_module
from ui.flexible_logic_tab import FlexibleLogicTab


class _FakeConfig:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _VarsStub:
    def export_config(self) -> dict[str, Any]:
        return {"read": [], "control": [], "aux": []}

    def aux_channel_configs(self) -> list[dict[str, Any]]:
        return []

    def variable_names(self, *_a: Any, **_kw: Any) -> list[str]:
        return []

    def __getattr__(self, name: str) -> Any:
        return lambda *_a, **_kw: None


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    widget._variables_tab = _VarsStub()
    widget.set_device_flex_state("no-tail")
    yield widget
    widget.deleteLater()


def _aux_rule(**action_kw: Any) -> dict[str, Any]:
    return {
        "title": "P1", "active": True,
        "events": [{"type": "frame", "id": "0x100", "extended": False,
                    "data": "", "channel": 1}],
        "conditions": [{"type": "none"}],
        "actions": [{
            "type": "aux", "aux_enabled": True,
            "aux_channel": 1, "aux_mode": "on", **action_kw,
        }],
    }


class TestPermCheckboxUI:
    def test_new_on_defaults_permanent(self, tab) -> None:
        """Новое «Вкл» — «вкл постоянно» включено, поле времени
        неактивно и его значение не влияет на выполнение (0 срока)."""
        tab.set_config([_aux_rule()])
        item = tab._row_widgets[0]._action_items[0]
        assert item.aux_on_perm.isChecked() is True
        assert item.aux_on_time.isEnabled() is False
        item.aux_on_time.setValue(777)  # скрытое значение — игнор
        out = tab.get_config()[0]["actions"][0]
        assert out["aux_on_time"] == 0

    def test_timed_on_migrates_to_unchecked(self, tab) -> None:
        """Старое «время включения» T>0 мигрирует в снятую галочку
        с прежним временем (CM-F11: обратная совместимость)."""
        tab.set_config([_aux_rule(aux_on_time=500)])
        item = tab._row_widgets[0]._action_items[0]
        assert item.aux_on_perm.isChecked() is False
        assert item.aux_on_time.isEnabled() is True
        assert item.aux_on_time.value() == 500
        out = tab.get_config()[0]["actions"][0]
        assert out["aux_on_time"] == 500

    def test_pwm_permanent_and_timed(self, tab) -> None:
        """У ШИМ та же галочка: постоянно по умолчанию, снятая —
        «Время включения» уезжает в полезную нагрузку МК."""
        tab.set_config([_aux_rule(aux_mode="pwm")])
        item = tab._row_widgets[0]._action_items[0]
        assert item.aux_pwm_perm.isChecked() is True
        assert item.aux_pwm_time.isEnabled() is False
        out = tab.get_config()[0]["actions"][0]
        assert out["aux_pwm_time"] == 0
        # Таймированный ШИМ восстанавливается с прежним сроком.
        tab.set_config([_aux_rule(aux_mode="pwm", aux_pwm_time=2000)])
        item = tab._row_widgets[0]._action_items[0]
        assert item.aux_pwm_perm.isChecked() is False
        assert item.aux_pwm_time.value() == 2000
        out = tab.get_config()[0]["actions"][0]
        assert out["aux_pwm_time"] == 2000

    def test_no_aux_pause_widget(self, tab) -> None:
        """Отдельной «Паузы до действия» в настройках допканала
        больше нет — задержка одна, общая (CM-F11)."""
        tab.set_config([_aux_rule()])
        item = tab._row_widgets[0]._action_items[0]
        assert not hasattr(item, "aux_delay")
        assert hasattr(item, "exec_delay")


class TestUnifiedDelayMigration:
    def test_editor_folds_aux_delay_once(self, tab) -> None:
        """Легаси aux_delay складывается в «Задержку» один раз:
        exec_delay + aux_delay — реальный старый старт."""
        tab.set_config([_aux_rule(exec_delay=100, aux_delay=250)])
        item = tab._row_widgets[0]._action_items[0]
        assert item.exec_delay.value() == 350
        out = tab.get_config()[0]["actions"][0]
        assert out["exec_delay"] == 350
        assert out.get("aux_delay", 0) == 0
        # Повторный круг чтения не удваивает задержку.
        tab.set_config(tab.get_config())
        assert (
            tab.get_config()[0]["actions"][0]["exec_delay"] == 350
        )

    def test_snapshot_migration_both_views(self) -> None:
        """migrate_aux_delay_snapshot чинит канонический actions[]
        и легаси-блок action одинаково и идемпотентно."""
        rule = _aux_rule(exec_delay=10, aux_delay=40)
        rule["action"] = {
            "aux_enabled": True, "aux_mode": "on",
            "exec_delay": 5, "aux_delay": 20,
        }
        payload = {"flexible_rules": [rule]}
        once = migrate_aux_delay_snapshot(payload)
        assert once["flexible_rules"][0]["actions"][0]["exec_delay"] == 50
        assert "aux_delay" not in once["flexible_rules"][0]["actions"][0]
        assert once["flexible_rules"][0]["action"]["exec_delay"] == 25
        assert "aux_delay" not in once["flexible_rules"][0]["action"]
        twice = migrate_aux_delay_snapshot(once)
        assert twice == once


class TestCompilerDelay:
    def test_legacy_delays_sum_into_delay_ms(self) -> None:
        """exec_delay + легаси aux_delay — одна автономная delay_ms
        записи (МК отсчитывает сам, программа не уходит на ПК)."""
        rule = _aux_rule(
            aux_channel=3, exec_delay=100, aux_delay=50,
        )
        recs = compile_rule(rule, allow_aux=True)
        assert recs is not None
        assert recs[0]["delay_ms"] == 150
        assert recs[0]["tx_aux"] is True

    def test_delay_overflow_rejected_not_truncated(self) -> None:
        """Суммарная задержка сверх поля u16 — честный отказ на ПК,
        а не молчаливое усечение времени старта."""
        rule = _aux_rule(
            aux_channel=3, exec_delay=60000, aux_delay=60000,
        )
        assert compile_rule(rule, allow_aux=True) is None

    def test_timed_on_duration_autonomous(self) -> None:
        """Срок «вкл на T мс» едет в полезную нагрузку — канал гасит
        сам МК, ПК не нужен."""
        rule = _aux_rule(aux_channel=2, aux_on_time=800)
        rec = compile_rule(rule, allow_aux=True)[0]
        assert int.from_bytes(rec["tx_data"][0:4], "little") == 800
        # Бессрочное «вкл постоянно» — срок 0.
        rule2 = _aux_rule(aux_channel=2)
        rec2 = compile_rule(rule2, allow_aux=True)[0]
        assert int.from_bytes(rec2["tx_data"][0:4], "little") == 0


class TestStaleAuxCallbacks:
    def test_off_cancels_pending_on(self, tab) -> None:
        """OFF, выданный после отложенного ON, отменяет его коллбек —
        устаревший таймер не зажигает канал поверх Выкл (CM-F11)."""
        sent: list[tuple] = []

        def _send_aux(ch, m, **kw):
            sent.append((ch, m))
            return True

        tab._serial_manager.send_aux = _send_aux
        pending_on = {
            "aux_enabled": True, "aux_channel": 2, "aux_mode": "on",
            "aux_delay": 120,
        }
        off = {
            "aux_enabled": True, "aux_channel": 2, "aux_mode": "off",
        }
        tab._run_actions_now(0, pending_on, {})
        assert sent == []  # ON ещё в таймере
        tab._run_actions_now(0, off, {})
        assert sent == [(2, 0)]  # OFF ушёл сразу
        QTest.qWait(200)
        # Устаревший ON не сработал поверх OFF.
        assert sent == [(2, 0)]

    def test_newer_on_replaces_pending(self, tab) -> None:
        """Более новая команда того же канала замещает старую
        отложенную — старый коллбек молчит."""
        sent: list[tuple] = []

        def _send_aux(ch, m, **kw):
            sent.append((ch, m))
            return True

        tab._serial_manager.send_aux = _send_aux
        tab._run_actions_now(0, {
            "aux_enabled": True, "aux_channel": 4,
            "aux_mode": "on", "aux_delay": 150,
        }, {})
        tab._run_actions_now(0, {
            "aux_enabled": True, "aux_channel": 4,
            "aux_mode": "pwm", "aux_pwm_freq": 100,
            "aux_pwm_duty": 50, "aux_delay": 50,
        }, {})
        QTest.qWait(250)
        # ШИМ ушёл; устаревший ON молчит.
        assert sent == [(4, 3)]
