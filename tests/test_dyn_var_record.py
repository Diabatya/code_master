"""Регрессии CM-F08 — «Записать» для динамической переменной:
тот же механизм захвата DATA, что у декодера «Градусы».

Проверяются: фильтрация по своему ID/разрядности, только полностью
покрытое поле, временная история HEX+DEC в порядке прихода,
дедуп повторов/уже пойманных, отсутствие автопревращения истории
в постоянную привязку (перенос — только осознанной кнопкой),
просмотр после «Стоп», RAM-only отписка, честные подсказки без
ID/байтов.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
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


def _history_hex(page) -> list[str]:
    return [
        str(page._dyn_history.item(i).data(Qt.ItemDataRole.UserRole))
        for i in range(page._dyn_history.count())
    ]


def test_record_button_only_on_dynamic(qapp) -> None:
    """Кнопка «Записать» и временная история — только у
    «Динамической переменной», у численной их нет."""
    dyn, _m = _page(qapp, "dyn")
    num, _m2 = _page(qapp, "numeric")
    try:
        assert hasattr(dyn, "_dyn_record_btn")
        assert hasattr(dyn, "_dyn_history")
        assert not hasattr(num, "_dyn_record_btn")
        assert dyn._dyn_record_btn.text() == "Записать"
        assert "2E7D32" in dyn._dyn_record_btn.styleSheet()
    finally:
        dyn.deleteLater()
        num.deleteLater()


def test_capture_goes_to_temp_history(qapp) -> None:
    """Кадры своего ID → временная история (HEX и DEC), а НЕ в
    таблицу привязки: автопревращения захвата в постоянную
    привязку нет (CM-F08)."""
    page, manager = _page(qapp)
    try:
        before = page.points_table.rowCount()
        page._toggle_dyn_record()
        assert page._dyn_recording
        assert page._dyn_record_btn.text() == "Стоп"
        assert "C62828" in page._dyn_record_btn.styleSheet()
        manager.new_can_frames.emit([
            _frame(data=b"\xAA\xBB"),
            _frame(can_id=0x200, data=b"\xCC\xDD"),  # чужой ID
            _frame(data=b"\xEE\xFF", ext=True),      # чужая разрядность
            _frame(data=b"\x01"),                    # не покрывает поле
            _frame(data=b"\x11\x22"),
        ])
        hist = _history_hex(page)
        assert hist == ["AA BB", "11 22"]
        item = page._dyn_history.item(0)
        assert "AA BB" in item.text() and "43707" in item.text()
        # Постоянная таблица привязки не тронута захватом.
        assert page.points_table.rowCount() == before
        assert page._dyn_recorded == 2
        page._stop_dyn_record()
        assert not page._dyn_recording
        assert page._dyn_record_btn.text() == "Записать"
        # История остаётся на просмотр после «Стоп».
        assert _history_hex(page) == ["AA BB", "11 22"]
        assert page._dyn_history_box.isVisibleTo(page)
    finally:
        page.deleteLater()


def test_apply_selected_to_binding(qapp) -> None:
    """«→ В привязку» переносит только выбранное значение строкой
    с пустым именем — осознанный перенос оператора."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([
            _frame(data=b"\xAA\xBB"), _frame(data=b"\x11\x22"),
        ])
        page._stop_dyn_record()
        page._dyn_history.setCurrentRow(1)
        page._dyn_history_to_binding()
        raws = [
            vt._parse_data_hex(page.points_table.item(r, 0).text())
            for r in range(page.points_table.rowCount())
        ]
        assert 0x1122 in raws and 0xAABB not in raws
        # Строка без имени не считается готовой привязкой.
        assert page.read_bindings() == []
    finally:
        page.deleteLater()


def test_dedupe_consecutive_and_seen(qapp) -> None:
    """Повтор подряд и уже пойманное значение не плодят строки
    истории; порядок — по приходу (A,B,A → A,B)."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([
            _frame(data=b"\x01\x02"),
            _frame(data=b"\x01\x02"),  # повтор подряд
            _frame(data=b"\x03\x04"),
            _frame(data=b"\x01\x02"),  # уже поймано ранее
        ])
        assert _history_hex(page) == ["01 02", "03 04"]
        assert page._dyn_recorded == 2
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
        page._toggle_dyn_record()
        page._stop_dyn_record()
        assert manager.new_can_frames._subs == []
    finally:
        page.deleteLater()
