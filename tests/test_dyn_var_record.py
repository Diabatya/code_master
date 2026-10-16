"""Регрессии CM-F08 — «Записать» для динамической переменной:
тот же механизм захвата DATA, что у декодера «Градусы».

Проверяются: фильтрация по своему ID/разрядности, только полностью
покрытое поле, дедуп повторов подряд и уже записанных значений,
RAM-only отписка при «Стоп»/уничтожении, честные подсказки без
ID/байтов.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel

import ui.variables_tab as vt


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _Signal:
    """Минимальный Qt-подобный сигнал для new_can_frames."""

    def __init__(self) -> None:
        self._subs = []

    def connect(self, cb) -> None:
        self._subs.append(cb)

    def disconnect(self, cb) -> None:
        self._subs.remove(cb)

    def emit(self, frames) -> None:
        for cb in list(self._subs):
            cb(frames)


class _Manager:
    def __init__(self) -> None:
        self.new_can_frames = _Signal()


def _row():
    row = SimpleNamespace()
    row.config = {}
    row.live_value = None
    row.live_valid = False
    row.live_raw = None
    row._stale_shown = False
    row._live_text = "—"
    row._last_valid_ts = 0.0
    row._state_label = QLabel("—")
    return row


def _tab(manager):
    tab = vt.VariablesTab.__new__(vt.VariablesTab)
    tab._serial_manager = manager
    tab._last_frames = {}
    return tab


def _page(qapp, kind="dyn", cfg=None):
    manager = _Manager()
    tab = _tab(manager)
    page = vt._ValuePage(
        QFont(), kind, lambda: _row(), get_tab=lambda: tab,
    )
    page.write(dict(cfg or {
        "id": "100", "dlc": 8, "from": "01 02", "to": "X",
        "points": [],
    }))
    return page, manager


def _frame(can_id=0x100, data=b"\x01\x02", ext=False):
    return {
        "id": can_id, "channel": 0, "data": bytes(data),
        "extended": ext, "rtr": False,
    }


def test_record_button_only_on_dynamic(qapp) -> None:
    """Кнопка «Записать» — только у «Динамической переменной»,
    у численной её нет (там запись живёт в диалоге декодера)."""
    dyn, _m = _page(qapp, "dyn")
    num, _m2 = _page(qapp, "numeric")
    try:
        assert hasattr(dyn, "_dyn_record_btn")
        assert not hasattr(num, "_dyn_record_btn")
        assert dyn._dyn_record_btn.text() == "Записать"
    finally:
        dyn.deleteLater()
        num.deleteLater()


def test_capture_fills_binding_rows(qapp) -> None:
    """Кадры своего ID → уникальные значения поля попадают в таблицу
    привязки; чужой ID/разрядность и неполное поле игнорируются."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        assert page._dyn_recording and page._dyn_record_btn.text() == "Стоп"
        manager.new_can_frames.emit([
            _frame(data=b"\xAA\xBB"),
            _frame(can_id=0x200, data=b"\xCC\xDD"),  # чужой ID
            _frame(data=b"\xEE\xFF", ext=True),      # чужая разрядность
            _frame(data=b"\x01"),                    # не покрывает поле
            _frame(data=b"\x11\x22"),
        ])
        bindings = page.read_bindings()
        # Имена ещё не введены → read_bindings их отбрасывает; строки
        # проверяем по значениям (itemChanged нормализует «AA BB» →
        # «AABB»).
        raws = {
            vt._parse_data_hex(page.points_table.item(r, 0).text())
            for r in range(page.points_table.rowCount())
        }
        assert 0xAABB in raws and 0x1122 in raws
        assert 0xCCDD not in raws and 0xEEFF not in raws
        assert 0x01 not in raws
        assert not bindings  # имён пока нет — строки «сырые»
        assert page._dyn_recorded == 2
        page._stop_dyn_record()
        assert not page._dyn_recording
        assert page._dyn_record_btn.text() == "Записать"
    finally:
        page.deleteLater()


def test_dedupe_consecutive_and_existing(qapp) -> None:
    """Повтор подряд и значение, уже есть в таблице, не плодят строки."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
        manager.new_can_frames.emit([
            _frame(data=b"\x01\x02"),  # повтор подряд
            _frame(data=b"\x01\x02"),
        ])
        rows = page.points_table.rowCount()
        manager.new_can_frames.emit([_frame(data=b"\x03\x04")])
        manager.new_can_frames.emit([_frame(data=b"\x03\x04")])
        texts = [
            vt._parse_data_hex(page.points_table.item(r, 0).text())
            for r in range(page.points_table.rowCount())
        ]
        assert texts.count(0x0102) == 1
        assert texts.count(0x0304) == 1
        assert page.points_table.rowCount() == rows + 1
    finally:
        page.deleteLater()


def test_hints_without_id_or_bytes(qapp) -> None:
    """Без ID/выбранных байтов запись не стартует — честная
    подсказка вместо молчаливого «не пишет»."""
    page, _m = _page(qapp, cfg={
        "id": "", "dlc": 8, "from": "01 02", "to": "X",
    })
    try:
        page._toggle_dyn_record()
        assert not page._dyn_recording
        assert "ID" in page._dyn_record_status.text()
        page.can_id.setText("100")
        for edit in page.data_from:
            edit.setText("X")  # поле не выбрано
        page._toggle_dyn_record()
        assert not page._dyn_recording
        assert "байт" in page._dyn_record_status.text().lower()
    finally:
        page.deleteLater()


def test_stop_unsubscribes(qapp) -> None:
    """«Стоп» и уничтожение страницы снимают подписку — RAM-only."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        assert manager.new_can_frames._subs == [page._on_dyn_frames]
        page._stop_dyn_record()
        assert manager.new_can_frames._subs == []
        # Повторный захват — новая подписка, старой нет.
        page._toggle_dyn_record()
        page._stop_dyn_record()
        assert manager.new_can_frames._subs == []
    finally:
        page.deleteLater()
