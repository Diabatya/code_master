"""ГЛ-рантайм по переменной «Угол руля (кратность)» — регрессии
CM-N3: единый контракт извлечения RAW с отображением, невалидные
значения (конфликт/no-class/no-calibration/неполный кадр) и
просроченные данные НЕ удовлетворяют численным условиям."""

from __future__ import annotations

import sys
import time
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

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


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    widget.set_device_flex_state("no-tail")
    yield widget
    widget.deleteLater()


class _Vars:
    """Подмена вкладки «Переменные» для рантайма ГЛ."""

    def __init__(self, defs: list[dict[str, Any]]) -> None:
        self._defs = defs

    def export_config(self) -> dict[str, Any]:
        return {"read": self._defs, "control": []}


def _steering_var(**steering_kw) -> dict[str, Any]:
    steering = {
        "enabled": True,
        "version": 2,
        "signs": {
            "pos": {4: "div", 8: "not"},   # 4/12/20 → «+»
            "neg": {8: "div"},             # 8/16/24 → «−»
        },
        "neutral": 0,
        "left_is_negative": True,
        "calib": [(4, 10.0), (8, 20.0), (12, 30.0), (16, 40.0)],
        "interpolate": True,
    }
    steering.update(steering_kw)
    return {
        "name": "angle", "type": "dynamic",
        "id": "0x100", "bytes": [0],
        "out_range": "steering", "steering": steering,
    }


def _num_cond(var: str, op: str, value: str) -> dict[str, Any]:
    return {"type": "num", "var": var, "op": op, "value": value}


def test_valid_angle_feeds_numeric_condition(tab) -> None:
    tab._variables_tab = _Vars([_steering_var()])
    tab._update_variable_states(0x100, b"\x0C")      # 12 → +30°
    assert tab._dyn_current("angle") == 30.0
    assert tab._condition_passed(_num_cond("angle", "gt", "20"))
    assert not tab._condition_passed(_num_cond("angle", "lt", "20"))


def test_conflict_frame_removes_value(tab) -> None:
    """Конфликтный угол не становится численным значением."""
    var = _steering_var(signs={"pos": {4: "div"}, "neg": {4: "div"}})
    tab._variables_tab = _Vars([var])
    tab._update_variable_states(0x100, b"\x0C")
    assert tab._dyn_current("angle") is None
    assert not tab._condition_passed(_num_cond("angle", "gt", "0"))


def test_no_class_and_no_calib_invalid(tab) -> None:
    tab._variables_tab = _Vars([_steering_var()])
    tab._update_variable_states(0x100, b"\x0C")      # валидное +30°
    assert tab._dyn_current("angle") == 30.0
    tab._update_variable_states(0x100, b"\x02")      # вне классов
    assert tab._dyn_current("angle") is None
    assert not tab._condition_passed(_num_cond("angle", "gt", "0"))


def test_incomplete_frame_invalid(tab) -> None:
    """Неполное поле: частичная сумма байтов не собирается."""
    var = _steering_var()
    var["bytes"] = [0, 7]
    tab._variables_tab = _Vars([var])
    tab._update_variable_states(0x100, b"\x0C")      # DLC<8
    assert tab._dyn_current("angle") is None


def test_unrelated_frame_keeps_value(tab) -> None:
    tab._variables_tab = _Vars([_steering_var()])
    tab._update_variable_states(0x100, b"\x0C")
    tab._update_variable_states(0x200, b"\x00")      # чужой ID
    assert tab._dyn_current("angle") == 30.0


def test_stale_value_fails_condition(tab) -> None:
    """Просроченное значение (stale_ms) условиям не отвечает."""
    tab._variables_tab = _Vars([_steering_var(stale_ms=100)])
    tab._update_variable_states(0x100, b"\x0C")
    assert tab._dyn_current("angle") == 30.0
    # «Прошло» 5 секунд без нового кадра — значение просрочено.
    tab._dyn_ts["angle"] = time.monotonic() - 5.0
    assert tab._dyn_current("angle") is None
    assert not tab._condition_passed(_num_cond("angle", "gt", "0"))


def test_stale_ms_zero_never_expires(tab) -> None:
    tab._variables_tab = _Vars([_steering_var(stale_ms=0)])
    tab._update_variable_states(0x100, b"\x0C")
    tab._dyn_ts["angle"] = time.monotonic() - 3600.0
    assert tab._dyn_current("angle") == 30.0


def test_sblp_extraction_matches_display_contract(tab) -> None:
    """Порядок байтов SBLP «старший справа» един с отображением."""
    var = _steering_var()
    var["bytes"] = [0, 1]
    var["sblp"] = "le"
    var["steering"]["signs"] = {"pos": {}, "neg": {8: "div"}}
    var["steering"]["calib"] = [(520, 40.0)]
    # 08 02 + «старший справа» → 8 + 2·256 = 520 → «−» → −40°.
    tab._variables_tab = _Vars([var])
    tab._update_variable_states(0x100, b"\x08\x02")
    assert tab._dyn_current("angle") == -40.0
