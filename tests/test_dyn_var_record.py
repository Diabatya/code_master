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

from PySide6.QtCore import QEvent, Qt
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
        # CM-FR08-R2: захват подписывается на смену/отключение
        # устройства — сигнал должен существовать у менеджера.
        self.connection_changed = _Signal()


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


def test_dedupe_adjacent_only(qapp) -> None:
    """Дедуп только подряд идущего значения: возврат к прежнему
    значению — новая строка истории (A,B,A → A,B,A; CM-FR10)."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([
            _frame(data=b"\x01\x02"),
            _frame(data=b"\x01\x02"),  # повтор подряд — дедуп
            _frame(data=b"\x03\x04"),
            _frame(data=b"\x01\x02"),  # возврат — новая строка
        ])
        assert _history_hex(page) == ["01 02", "03 04", "01 02"]
        assert page._dyn_recorded == 3
    finally:
        page.deleteLater()


def test_stop_start_preserves_history(qapp) -> None:
    """«Стоп»/повторный «Записать» НЕ очищают историю (CM-FR10):
    захват продолжает журнал, дедуп действует через границу
    старта, очистка — только явной «Очистить»."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
        page._stop_dyn_record()
        page._toggle_dyn_record()
        manager.new_can_frames.emit([
            _frame(data=b"\x01\x02"),  # подряд к прежнему — дедуп
            _frame(data=b"\x03\x04"),
        ])
        assert _history_hex(page) == ["01 02", "03 04"]
        assert page._dyn_recorded == 2
        # Явный сброс — история пуста, дедуп-опора снята.
        page._dyn_clear_btn.click()
        assert _history_hex(page) == [] and page._dyn_recorded == 0
        manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
        assert _history_hex(page) == ["01 02"]
        page._stop_dyn_record()
    finally:
        page.deleteLater()


def test_rtr_and_malformed_frames_are_not_samples(qapp) -> None:
    """RTR и битые кадры не становятся образцами (CM-FR07)."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([
            _frame(data=b"\xAA\xBB", ) | {"rtr": True},
            {"id": 0x100, "channel": 0, "extended": False,
             "rtr": False, "data": "не байты"},
            {"id": "мусор", "channel": 0, "extended": False,
             "rtr": False, "data": b"\xAA\xBB"},
            _frame(data=b"\x11\x22"),
        ])
        assert _history_hex(page) == ["11 22"]
        page._stop_dyn_record()
    finally:
        page.deleteLater()


def test_history_limit_stops_with_status(qapp) -> None:
    """Потолок временной истории — явный стоп со статусом
    (CM-FR07): записи дальше лимита не молча плодятся."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        page._dyn_history.clear()
        for i in range(page.DYN_HISTORY_LIMIT + 3):
            manager.new_can_frames.emit([
                _frame(data=bytes([i % 256, i // 256]))
            ])
        assert page._dyn_history.count() == page.DYN_HISTORY_LIMIT
        assert not page._dyn_recording
        assert "лимит" in page._dyn_record_status.text().lower()
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


def test_parent_dialog_close_stops_recording(qapp) -> None:
    """Закрытие РОДИТЕЛЬСКОГО модального окна (не только прямой
    «Стоп») останавливает захват и отписывает от кадров
    (CM-FR08): OK, Отмена и крестик идут через done()."""
    manager = _Manager()
    tab = _tab(manager)
    dlg = vt.VariableDialog(config={
        "type": "dyn_cache", "id": "100", "dlc": 8,
        "from": "01 02", "to": "X",
        "points": [[0x0102, "a"]],
    })
    dlg._var_tab = tab  # родитель-QWidget подменить нельзя — поле напрямую
    try:
        page = dlg._dyn_page
        page._toggle_dyn_record()
        assert page._dyn_recording
        assert manager.new_can_frames._subs == [page._on_dyn_frames]
        dlg.reject()
        assert not page._dyn_recording
        assert manager.new_can_frames._subs == []
    finally:
        dlg.deleteLater()


def test_source_change_stops_dyn_record(qapp) -> None:
    """CM-FR08-R2: источник зафиксирован на старте — смена ID (и
    любой части ключа) посреди записи останавливает сессию с явным
    статусом; кадры нового источника не попадают в журнал."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
        assert _history_hex(page) == ["01 02"]
        page.can_id.setText("200")          # оператор сменил ID
        manager.new_can_frames.emit([
            _frame(can_id=0x200, data=b"\x03\x04"),
        ])
        assert not page._dyn_recording
        assert "источник" in page._dyn_record_status.text()
        # Журнал старой сессии не смешался с новым источником.
        assert _history_hex(page) == ["01 02"]
        # Подписки сняты — запись не живёт после стопа.
        assert manager.new_can_frames._subs == []
        assert manager.connection_changed._subs == []
    finally:
        page.deleteLater()


def test_device_disconnect_stops_dyn_record(qapp) -> None:
    """CM-FR08-R2: отключение устройства во время захвата — стоп
    со статусом; подписки на кадры и сигнал подключения сняты."""
    page, manager = _page(qapp)
    try:
        page._toggle_dyn_record()
        assert manager.new_can_frames._subs == [page._on_dyn_frames]
        assert manager.connection_changed._subs == [
            page._on_dyn_device_changed
        ]
        manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
        manager.connection_changed.emit(False)   # устройство ушло
        assert not page._dyn_recording
        assert "отключено" in page._dyn_record_status.text()
        assert manager.new_can_frames._subs == []
        assert manager.connection_changed._subs == []
        # История сессии остаётся на просмотр.
        assert _history_hex(page) == ["01 02"]
    finally:
        page.deleteLater()


def test_dyn_deferred_delete_stops_recording(qapp) -> None:
    """Разрушение страницы без явного «Стоп» (deleteLater) —
    захват останавливается и отписывается (CM-FR08-R2, реальный
    путь destroyed Qt)."""
    page, manager = _page(qapp)
    page._toggle_dyn_record()
    assert manager.new_can_frames._subs == [page._on_dyn_frames]
    page.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    # Виджет мёртв — проверяем побочный эффект: подписок нет,
    # поздняя эмиссия никому не доставляется и не падает.
    assert manager.new_can_frames._subs == []
    manager.new_can_frames.emit([_frame(data=b"\x01\x02")])
