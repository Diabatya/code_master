"""CM-N15: страница редактора переменной (_ValuePage) повторяет
контракт валидности строки — невалидный/неполный/просроченный кадр
показывает статус, а не залипшее «0.00»; декодер угла работает без
таблицы обычных точек; RAW просроченного кадра помечается.

Тесты гоняют настоящий ``_ValuePage._update_live_label`` +
``_VariableRow`` и проверяют ФАКТИЧЕСКИЙ текст виджета редактора
(live_label/live_hex_label), а не только ``row._state_label``.
"""

from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel

import ui.variables_tab as vt


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _Col:
    def __init__(self, rows):
        self._rows = rows


def _row(config):
    """Строка с настоящими set_live_value/mark_stale."""
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
    tab = vt.VariablesTab.__new__(vt.VariablesTab)
    tab._read_col = _Col(rows)
    tab._ctrl_col = _Col([])
    tab._aux_col = _Col([])
    tab._last_frames = {}
    tab._frame_seq = 0
    return tab


def _page(row, tab, cfg, kind="numeric"):
    """Настоящая страница редактора переменной."""
    page = vt._ValuePage(
        QFont(), kind, lambda: row, get_tab=lambda: tab,
    )
    page.write(cfg)
    return page


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

_NUMERIC = {
    "type": "dynamic", "name": "Угол", "id": "100",
    "bytes": [0, 1], "sblp": "be",
    "out_range": "steering", "steering": dict(_STEER),
}


def _num_cfg(**kw):
    cfg = dict(_NUMERIC)
    cfg.update(kw)
    return cfg


class TestValuePageValidity:
    """Численная страница: контракт invalid/incomplete/stale
    идентичен строке (CM-N15)."""

    def test_valid_shows_decoded_in_editor(self, qapp) -> None:
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        assert "45.0" in page.live_label.text()
        assert "0x10" in page.live_hex_label.text()

    def test_valid_then_incomplete_shows_status(self, qapp) -> None:
        """45.0° → неполный кадр: редактор показывает статус,
        а не залипшее число."""
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        assert "45.0" in page.live_label.text()
        tab._on_can_frames([_frame(data=b"\x00")])
        page._update_live_label()
        assert "непол" in page.live_label.text().lower()
        assert "45.0" not in page.live_label.text()

    def test_valid_then_conflict_shows_status(self, qapp) -> None:
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        tab._on_can_frames([_frame(data=b"\x00\x0C")])  # 12 кратно 4 и 6
        page._update_live_label()
        assert "конфликт" in page.live_label.text().lower()

    def test_valid_then_no_class_shows_status(self, qapp) -> None:
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        tab._on_can_frames([_frame(data=b"\x00\x07")])
        page._update_live_label()
        assert "вне класс" in page.live_label.text().lower()

    def test_valid_then_no_calibration_shows_status(self, qapp) -> None:
        steer = dict(_STEER)
        steer["calib"] = []  # класс есть, калибровки нет
        row = _row(_num_cfg(steering=steer))
        tab = _tab([row])
        page = _page(row, tab, _num_cfg(steering=steer))
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        assert "калибров" in page.live_label.text().lower()

    def test_stale_value_marked_in_editor(self, qapp) -> None:
        """После stale_ms редактор показывает «устарело» и для
        числа, и для RAW — просроченное не выдаётся за свежее."""
        steer = dict(_STEER)
        steer["stale_ms"] = 100
        row = _row(_num_cfg(steering=steer))
        tab = _tab([row])
        page = _page(row, tab, _num_cfg(steering=steer))
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        row._last_valid_ts = time.monotonic() - 10
        tab._mark_stale_rows()
        page._update_live_label()
        assert "устарел" in page.live_label.text().lower()
        assert "устарел" in page.live_hex_label.text().lower()

    def test_invalid_then_valid_recovers(self, qapp) -> None:
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00")])
        page._update_live_label()
        assert "непол" in page.live_label.text().lower()
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        assert "45.0" in page.live_label.text()

    def test_neutral_zero_is_valid_zero(self, qapp) -> None:
        """Нейтраль → 0.0° — валидный ноль, а не прочерк/ошибка."""
        row = _row(_num_cfg())
        tab = _tab([row])
        page = _page(row, tab, _num_cfg())
        tab._on_can_frames([_frame(data=b"\x00\x00")])
        page._update_live_label()
        assert "0.0" in page.live_label.text()
        assert "устарел" not in page.live_label.text().lower()

    def test_degrees_with_decoder_no_points_needed(self, qapp) -> None:
        """«Градусы» с включённой кратностью — значение из калибровки
        декодера, обычные точки не нужны (раньше требовались ≥2)."""
        cfg = _num_cfg(out_range="degrees")
        row = _row(cfg)
        tab = _tab([row])
        page = _page(row, tab, cfg)
        page.points_table.setRowCount(0)  # ни одной обычной точки
        assert len(page.read_points()) < 2
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        page._update_live_label()
        assert "45.0" in page.live_label.text()

    def test_no_points_no_decoder_shows_dash(self, qapp) -> None:
        """Численная без точек и без декодера — прочерк, а не
        выглядящий валидным «0.00»."""
        cfg = _num_cfg(
            out_range="inf", steering={"enabled": False},
            points=[],
        )
        row = _row(cfg)
        tab = _tab([row])
        page = _page(row, tab, cfg)
        page.points_table.setRowCount(0)
        tab._on_can_frames([_frame(data=b"\x00\x10")])
        # Без точек строка не декодирует — значения нет.
        page._update_live_label()
        assert page.live_label.text() != "0.00"


class TestValuePageDynamic:
    """«Динамическая переменная»: имя привязки — тот же контракт
    валидности (CM-N15, смежный маршрут)."""

    _DYN = {
        "type": "dyn_cache", "name": "Сост", "id": "100",
        "from": "0", "to": "0", "bytes": [0],
        "points": [[16, "ВЛЕВО"], [32, "ВПРАВО"]],
    }

    def test_dynamic_binding_name_and_invalidation(self, qapp) -> None:
        row = _row(dict(self._DYN))
        tab = _tab([row])
        page = _page(row, tab, dict(self._DYN), kind="dynamic")
        tab._on_can_frames([_frame(data=b"\x10")])
        page._update_live_label()
        assert page.live_label.text() == "ВЛЕВО"
        # Нет кадра с новым ID → значение остаётся; но пометка
        # устаревания пробрасывается и сюда.
        row.mark_stale()
        page._update_live_label()
        assert "устарел" in page.live_label.text().lower()
