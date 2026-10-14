"""CM-N11: отображение живых значений в «Переменных» не оставляет
залипшее прошлое число при невалидном/неполном/просроченном кадре.

Настоящий ``VariablesTab._on_can_frames`` + настоящий
``_VariableRow.set_live_value``/``mark_stale`` на лёгких строках:
live_value=None при invalid, статус текстом, нейтраль 0° ≠ invalid,
stale_ms помечает «(устарело)» и на строке, и в рантайме ГЛ.
"""

from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

import ui.variables_tab as vt


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _Col:
    """Лёгкая колонка — то, что читает _on_can_frames/_mark_stale_rows."""

    def __init__(self, rows):
        self._rows = rows


def _row(config):
    """Строка с настоящими set_live_value/mark_stale — только виджеты,
    которые методы реально трогают."""
    row = vt._VariableRow.__new__(vt._VariableRow)
    row.config = config
    row.live_value = None
    row.live_valid = False
    row.live_raw = None
    row._stale_shown = False
    row._live_text = "—"
    row._last_valid_ts = 0.0
    row._state_label = QLabel("—")
    return row


def _tab(rows):
    """VariablesTab с настоящими _on_can_frames/_mark_stale_rows."""
    tab = vt.VariablesTab.__new__(vt.VariablesTab)
    tab._read_col = _Col(rows)
    tab._ctrl_col = _Col([])
    tab._aux_col = _Col([])
    tab._last_frames = {}
    tab._frame_seq = 0
    return tab


def _frame(can_id=0x100, data=b"\x00\x00", channel=0):
    return {
        "id": can_id, "channel": channel, "data": bytes(data),
        "extended": False, "rtr": False,
    }


_STEER = {
    "enabled": True,
    "signs": {"pos": {4: "div"}, "neg": {6: "div"}},
    "neutral": 0,
    "calib": [[16, 45], [20, 60]],
    "interpolate": False,
    "left_is_negative": True,
    "stale_ms": 0,
}


def _var_cfg(**kw):
    cfg = {
        "type": "dynamic", "name": "Угол", "id": "100",
        "bytes": [0, 1], "sblp": "be",
        "out_range": "steering", "steering": dict(_STEER),
    }
    cfg.update(kw)
    return cfg


class TestLiveValueInvalidation:
    def test_valid_then_incomplete_clears_value(self, qapp) -> None:
        """Валидный кадр → угол; неполный кадр — live_value=None,
        статус «неполный кадр», а не старое число."""
        row = _row(_var_cfg())
        tab = _tab([row])
        # raw 16 кратно 4 → pos, калибровка 16→45°.
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        assert row.live_value == pytest.approx(45.0)
        assert row.live_valid
        assert "45.0" in row._state_label.text()
        # Кадр на 1 байт — поле [0,1] не покрыто.
        tab._on_can_frames([_frame(data=b"\x00")])
        assert row.live_value is None
        assert not row.live_valid
        assert "непол" in row._state_label.text().lower()

    def test_valid_then_conflict_clears_value(self, qapp) -> None:
        """Значение, совпадающее с обеими таблицами знаков —
        конфликт, прошлый угол не показывается."""
        steer = dict(_STEER)
        # raw 12 кратно и 4, и 6 — conflict.
        row = _row(_var_cfg(steering=steer))
        tab = _tab([row])
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        assert row.live_valid
        tab._on_can_frames([_frame(data=b"\x00\x0C")])
        assert row.live_value is None
        assert not row.live_valid
        assert "конфликт" in row._state_label.text().lower()

    def test_neutral_zero_is_valid_not_invalid(self, qapp) -> None:
        """Нейтраль → 0.0° — валидный ноль, отличимый от invalid."""
        row = _row(_var_cfg())
        tab = _tab([row])
        tab._on_can_frames([_frame(data=b"\x00\x00")])
        assert row.live_value == 0.0
        assert row.live_valid
        # Текст — угол, а не статус ошибки.
        assert "0.0" in row._state_label.text()

    def test_no_class_shows_status(self, qapp) -> None:
        """raw вне обеих таблиц — «вне классов», live_value=None."""
        row = _row(_var_cfg())
        tab = _tab([row])
        # 7 не кратно ни 4, ни 6 → no-class.
        tab._on_can_frames([_frame(data=b"\x00\x07")])
        assert row.live_value is None
        assert not row.live_valid
        assert "вне класс" in row._state_label.text().lower()

    def test_invalid_then_valid_recovers(self, qapp) -> None:
        """После невалидного кадра новый валидный снова показывает
        угол — статусная строка не залипает."""
        row = _row(_var_cfg())
        tab = _tab([row])
        tab._on_can_frames([_frame(data=b"\x00")])
        assert not row.live_valid
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        assert row.live_valid and row.live_value == pytest.approx(45.0)
        assert "45.0" in row._state_label.text()

    def test_stale_marks_value_after_timeout(self, qapp) -> None:
        """stale_ms: прошёл таймаут без валидного кадра — показанное
        число помечается «(устарело)», не выдаётся за актуальное."""
        steer = dict(_STEER)
        steer["stale_ms"] = 100
        row = _row(_var_cfg(steering=steer))
        tab = _tab([row])
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        assert row.live_valid and not row._stale_shown
        # Симулируем устаревший кадр — сдвигаем отметку в прошлое.
        row._last_valid_ts = time.monotonic() - 10
        tab._mark_stale_rows()
        assert row._stale_shown
        assert "устарел" in row._state_label.text().lower()
        # live_value остаётся (это явно помеченное старое), но
        # рантайм ГЛ его уже не использует — политика свежести едина.
        assert row.live_value == pytest.approx(45.0)

    def test_degrees_route_uses_decoder_when_enabled(self, qapp) -> None:
        """«Градусы» с включённой кратностью — декодер тот же
        (маршрут из задания); без декодера — таблица точек."""
        row = _row(_var_cfg(out_range="degrees"))
        tab = _tab([row])
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        assert row.live_valid
        assert row.live_value == pytest.approx(45.0)
        assert "45.0" in row._state_label.text()
        # Без включённого декодера «Градусы» работают по точкам
        # графика — неизвестное семейство не показывается (не invalid-
        # статус декодера, а просто отсутствие вывода).
        row2 = _row(_var_cfg(
            out_range="degrees",
            steering={"enabled": False},
            points=[],
        ))
        tab2 = _tab([row2])
        tab2._on_can_frames([_frame(data=b"\x00\x10")])
        # points пустые и steering выключен — строка не трогается.
        assert row2.live_value is None
