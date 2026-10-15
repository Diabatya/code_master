"""CM-N14: PC-runtime ГЛ исполняет ТОЧНЫЙ резолвнутый снимок,
записанный в камень, — не повторный резолв по черновым настройкам
выхода вкладки «Переменные».

Сценарий ревью: записать программу с «из настроек выхода» (ШИМ
50 Гц/25%), затем в черновике переменной поменять параметры
(999 Гц/99%) — до следующей подтверждённой записи runtime обязан
отрабатывать старые 50/25; новое поколение исполнения инвалидирует
отложенные действия старого снимка.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.aux_channels import resolve_rule_actions
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
    """Заглушка вкладки «Переменные»: настройки выходов меняются
    «вживую» — имитация правки черновика после записи программы."""

    def __init__(self) -> None:
        self.aux: list[dict[str, Any]] = [
            {"type": "aux", "channel": 2, "out_mode": "pwm",
             "pwm_freq": 50, "pwm_duty": 25},
        ]

    def aux_channel_configs(self) -> list[dict[str, Any]]:
        return self.aux

    def export_config(self) -> dict[str, Any]:
        return {"read": [], "control": []}

    # Методы, которые дергают виджеты строк редактора.
    def variable_names(self, *_a: Any, **_kw: Any) -> list[str]:
        return []

    def variable_config(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {}

    def live_frame(self, *_a: Any, **_kw: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        # Любой прочий метод вкладки — безопасная пустышка.
        return lambda *_a, **_kw: None


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    widget._aux_calls: list[tuple] = []
    widget._serial_manager.send_aux = (  # type: ignore[method-assign]
        lambda ch, mode, **kw: widget._aux_calls.append(
            (ch, mode, kw)
        ) or True
    )
    widget._variables_tab = _VarsStub()
    yield widget
    widget.deleteLater()


def _aux_rule() -> dict[str, Any]:
    return {
        "title": "AUX", "active": True,
        "events": [{"type": "frame", "channel": 2, "extended": False,
                    "id": "123", "dlc": 0, "data": "",
                    "fire_limit": 0}],
        "conditions": [{"type": "none"}],
        "actions": [{
            "aux_enabled": True, "aux_use_var": True,
            "aux_channel": 2, "aux_mode": "on",
        }],
    }


def _rx_frame(can_id: int = 0x123) -> dict[str, Any]:
    return {"id": can_id, "channel": 1, "data": b"",
            "extended": False, "rtr": False}


def test_runtime_executes_committed_snapshot_not_draft(tab) -> None:
    """Записанный снимок (ШИМ 50/25) исполняется и после правки
    черновых настроек выхода (999/99) — до новой записи в МК."""
    vars_stub: _VarsStub = tab._variables_tab  # type: ignore[assignment]
    rule = _aux_rule()
    # Путь settings_window: резолв по настройкам → запись в МК →
    # commit_runtime получает ТОЧНЫЙ записанный снимок.
    resolved = resolve_rule_actions(rule, vars_stub.aux_channel_configs())
    assert resolved["actions"][0]["aux_resolved"] is True
    tab.set_config([rule])
    tab.commit_runtime([resolved])
    tab.set_device_flex_state("no-tail")

    # Оператор правит черновые настройки выхода — без записи.
    vars_stub.aux = [
        {"type": "aux", "channel": 2, "out_mode": "pwm",
         "pwm_freq": 999, "pwm_duty": 99},
    ]
    tab.process_frame(_rx_frame())
    assert tab._aux_calls, "действие доп. канала не отработало"
    _ch, _mode, kw = tab._aux_calls[0]
    assert kw["pwm_freq_hz"] == 50 and kw["pwm_duty_pct"] == 25

    # Новая подтверждённая запись — новые параметры вступают в силу.
    resolved2 = resolve_rule_actions(rule, vars_stub.aux_channel_configs())
    tab.commit_runtime([resolved2])
    tab.process_frame(_rx_frame())
    _ch, _mode, kw = tab._aux_calls[-1]
    assert kw["pwm_freq_hz"] == 999 and kw["pwm_duty_pct"] == 99


def test_snapshot_deep_copied_from_commit(tab) -> None:
    """Снимок не разделяет dict'ы действий с источником: правка
    переданного списка после commit исполнение не меняет."""
    vars_stub: _VarsStub = tab._variables_tab  # type: ignore[assignment]
    rule = _aux_rule()
    resolved = resolve_rule_actions(rule, vars_stub.aux_channel_configs())
    tab.set_config([rule])
    tab.commit_runtime([resolved])
    # Мутация исходного списка после commit — runtime не задет.
    resolved["actions"][0]["aux_pwm_freq"] = 777
    tab.set_device_flex_state("no-tail")
    tab.process_frame(_rx_frame())
    _ch, _mode, kw = tab._aux_calls[0]
    assert kw["pwm_freq_hz"] == 50


def test_device_readback_rules_marked_resolved(tab) -> None:
    """Правила, вычитанные из камня (verified), помечаются
    aux_resolved — вычитанные параметры уже применённые."""
    rule = _aux_rule()
    resolved = resolve_rule_actions(
        rule, tab._variables_tab.aux_channel_configs()
    )
    # Имитация вычитки: JSON пришёл без маркера (блоб старой версии),
    # но с применёнными параметрами.
    device_rules = [
        {**resolved, "actions": [
            {k: v for k, v in a.items() if k != "aux_resolved"}
            for a in resolved["actions"]
        ]},
    ]
    tab.apply_device_flex_analysis(
        {"state": "verified", "rules": device_rules, "managed": []}
    )
    assert tab._runtime_rules[0]["actions"][0]["aux_resolved"] is True
    # Правка черновика не влияет на исполнение вычитанного снимка.
    vars_stub: _VarsStub = tab._variables_tab  # type: ignore[assignment]
    vars_stub.aux = [
        {"type": "aux", "channel": 2, "out_mode": "pwm",
         "pwm_freq": 999, "pwm_duty": 99},
    ]
    tab.process_frame(_rx_frame())
    _ch, _mode, kw = tab._aux_calls[-1]
    assert kw["pwm_freq_hz"] == 50 and kw["pwm_duty_pct"] == 25
