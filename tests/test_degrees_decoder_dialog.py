"""Регрессии CM-F05/F06/F07 — единый режим «Градусы», диалог
декодера с записью DATA и таблица привязки как источник масштаба.

Проверяются: миграция «steering»→«degrees», live-контракт декода
по веткам сторон (нейтраль = нулевая точка таблицы), инверсия
один раз, обе таблицы записи (Записать/Стоп, 5 колонок, история
>10, дедуп, лимит), валидация HEX/кратности, отказ подтверждения
некорректной настройки, единица «Температура».
"""

from __future__ import annotations

import contextlib
import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import ui.variables_tab as vt
from core import steering_decoder as sd
from core import variable_decode as vd


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


_V2 = {
    "enabled": True,
    "version": 2,
    "signs": {"pos": {4: "div", 8: "not"}, "neg": {8: "div"}},
    "neutral": 0,
    "left_is_negative": True,
    "calib": [[16, 45.0], [20, 60.0]],
    "interpolate": True,
    "stale_ms": 250,
}


def _dialog(qapp, cfg=None, live=None):
    dlg = vt._DegreesDecoderDialog(
        dict(_V2 if cfg is None else cfg), QFont(), live=live
    )
    yield dlg
    dlg.deleteLater()


class TestDialogLayout:
    """CM-F05 п.3/п.7: поля нейтрали/таймаута/калибровки/интерполяции
    удалены; две таблицы записи с ровно 5 колонками (F06 п.5)."""

    def test_columns_and_buttons(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            headers = [
                dlg._tables["neg"].table.horizontalHeaderItem(i).text()
                for i in range(5)
            ]
            assert headers == [
                "DATA 16", "DATA 10", "Кратно", "Числу", "Не кратно"
            ]
            for tbl in dlg._tables.values():
                # 10 фиксированных делителей 2..20.
                assert tbl.table.rowCount() == 10
                divs = [
                    int(tbl.table.item(r, tbl.COL_NUM).text())
                    for r in range(10)
                ]
                assert divs == list(sd.DIVISORS)
                assert tbl.record_btn.text() == "Записать"
                assert "2E7D32" in tbl.record_btn.styleSheet()  # зелёная
        finally:
            for _ in gen:
                pass

    def test_no_legacy_fields(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            assert not hasattr(dlg, "_neutral")
            assert not hasattr(dlg, "_stale")
            assert not hasattr(dlg, "_calib_table")
            assert not hasattr(dlg, "_interp")
            assert not hasattr(dlg, "_dir_left")
        finally:
            for _ in gen:
                pass

    def test_rules_restored_from_v2(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            neg, pos = dlg._tables["neg"], dlg._tables["pos"]
            assert neg._row_req(3) == "div"          # делитель 8
            assert pos._row_req(1) == "div"          # делитель 4
            assert pos._row_req(3) == "not"          # не кратно 8
        finally:
            for _ in gen:
                pass

    def test_inversion_and_unit_roundtrip(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            dlg._inverted.setChecked(True)
            idx = dlg._unit.findData("celsius")
            dlg._unit.setCurrentIndex(idx)
            out = dlg.config()
            assert out["inverted"] is True
            assert out["unit"] == "celsius"
            # v2-ключи не сохраняются; stale_ms мигрирует как есть.
            assert out["stale_ms"] == 250
            for key in ("neutral", "calib", "interpolate",
                        "left_is_negative"):
                assert key not in out
        finally:
            for _ in gen:
                pass


class TestRecording:
    """CM-F06: независимые Записать/Стоп, дедуп повторов, история
    длиннее десяти, RAM-only, лимит записей."""

    def test_record_and_stop(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            neg = dlg._tables["neg"]
            pos = dlg._tables["pos"]
            neg.toggle_record()
            assert neg.record_btn.text() == "Стоп"
            assert "C62828" in neg.record_btn.styleSheet()  # красная
            assert pos.record_btn.text() == "Записать"
            for i in range(15):
                neg.capture(bytes([0x10 + i]))
            # 15 значений → история длиннее десяти (F06 п.3):
            # первые 10 занимают DATA строк делителей, дальше —
            # добавленные строки журнала (CM-FR13).
            assert neg.table.rowCount() == 10 + 5
            # Повтор подряд не плодит строку (F06 п.18).
            neg.capture(b"\x40")
            assert neg.table.rowCount() == 10 + 6
            neg.capture(b"\x40")
            neg.capture(b"\x40")
            assert neg.table.rowCount() == 10 + 6
            neg.stop_record()
            assert neg.record_btn.text() == "Записать"
            # Стоп не меняет правила (F06 п.12).
            assert neg.sign_rules() == {8: "div"}
            # Правая таблица независима — пустая.
            assert pos.table.rowCount() == 10
        finally:
            for _ in gen:
                pass

    def test_history_limit_stops_recording(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            for i in range(tbl.HISTORY_LIMIT + 5):
                tbl.capture(bytes([i % 256, i // 256]))
            assert not tbl.recording
            assert "лимит" in tbl.status.text().lower()
        finally:
            for _ in gen:
                pass

    def test_history_row_decimal(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["pos"]
            tbl.toggle_record()
            tbl.capture(b"\x01\x02")  # 0x0102 = 258
            # Первая запись — DATA-ячейки строки 0 (CM-FR13).
            r = 0
            assert tbl.table.item(r, tbl.COL_HEX).text() == "01 02"
            assert tbl.table.item(r, tbl.COL_DEC).text() == "258"
        finally:
            for _ in gen:
                pass

    def test_history_rows_are_journal_only(self, qapp) -> None:
        """CM-FR06/CM-FR13: добавленные строки истории (за
        пределами десяти) — журнал без активных условий: галочек
        и делителя нет, правило стороны они не меняют; десять
        делителей остаются на своих строках с журнальными DATA."""
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            for i in range(12):
                tbl.capture(bytes([0x10 + i]))
            # Первые десять строк — делители с галочками, DATA
            # занята журналом в порядке прихода.
            for r in range(10):
                assert tbl._req_boxes(r) is not None
                assert (
                    tbl.table.item(r, tbl.COL_NUM).text()
                    == str(list(sd.DIVISORS)[r])
                )
                assert tbl._row_hex(r) == f"{0x10 + r:02X}"
            # Добавленные строки 10–11 — журнал без условий.
            for r in (10, 11):
                assert tbl._req_boxes(r) is None, (
                    "строка истории не должна иметь галочек требования"
                )
                num = tbl.table.item(r, tbl.COL_NUM)
                assert num.text() == ""
                assert not (num.flags() & Qt.ItemFlag.ItemIsEditable)
                assert tbl._row_hex(r) == f"{0x10 + r:02X}"
            # Постоянное правило (кратно 8) не затронуто записью —
            # и ни одна строка истории не порождает правила.
            assert tbl.sign_rules() == {8: "div"}
            assert not tbl.errors()
        finally:
            for _ in gen:
                pass

    def test_stop_start_preserves_journal_and_rules(self, qapp) -> None:
        """Стоп/старт не трогает ни журнал, ни делители; повторное
        «Записать» продолжает ту же историю новыми строками."""
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            tbl.capture(b"\x10")
            tbl.stop_record()
            tbl.capture(b"\x20")          # вне записи — не пишется
            assert tbl.table.rowCount() == 10
            tbl.toggle_record()
            tbl.capture(b"\x20")
            assert tbl.table.rowCount() == 10
            # Журнал продолжается в порядке прихода: «10» в DATA
            # строки 0, «20» — строки 1; делители на своих местах.
            divs = [
                int(tbl.table.item(r, tbl.COL_NUM).text())
                for r in range(10)
            ]
            assert divs == list(sd.DIVISORS)
            assert tbl._row_hex(0) == "10" and tbl._row_hex(1) == "20"
            assert tbl.sign_rules() == {8: "div"}
        finally:
            for _ in gen:
                pass


class TestCaptureContract:
    """CM-FR07: единый контракт извлечения/порядка байтов (live,
    журнал, делители — все big-endian на каноническом значении),
    фильтрация канала/RTR/битых кадров, ограниченная очередь,
    остановка записи при смене источника."""

    def _live(self, **kw):
        state = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0, 1], "sblp": "", "get_frame": None,
        }
        state.update(kw)
        return state

    def _frame(self, data=b"\x01\x02", **kw):
        frame = {
            "id": 0x100, "channel": 0, "extended": False,
            "rtr": False, "data": bytes(data),
        }
        frame.update(kw)
        return frame

    def _feed(self, dlg, frames):
        dlg._on_frames(frames)
        dlg._tick()
        return dlg._tables["neg"]._history

    def _dec(self, dlg, r):
        return dlg._tables["neg"].table.item(r, dlg._tables["neg"].COL_DEC).text()

    def _hex(self, dlg, r):
        return dlg._tables["neg"]._row_hex(r)

    def test_le_decodes_513_not_258(self, qapp) -> None:
        """bytes=[0,1], sblp='le', DATA 01 02 → DATA 10 и делители
        читают 513 (0x0201) — единый контракт с live. CM-FR07-R2:
        DATA 16 показывает байты ПРОВОДА «01 02» как пришли,
        а не переставленные «02 01»."""
        gen = _dialog(qapp, live=self._live(sblp="le"))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            assert self._feed(dlg, [self._frame()]) == 1
            assert self._hex(dlg, 0) == "01 02"
            assert self._dec(dlg, 0) == "513"
        finally:
            for _ in gen:
                pass

    def test_be_and_leading_zeros(self, qapp) -> None:
        """be: 01 02 → 258 как на шине; le 00 01 → 256 (ведущие
        нули — легитимные байты поля, а не мусор)."""
        gen = _dialog(qapp, live=self._live(sblp="be"))
        dlg = next(gen)
        try:
            neg = dlg._tables["neg"]
            neg.toggle_record()
            assert self._feed(dlg, [self._frame()]) == 1
            assert self._dec(dlg, 0) == "258"
            dlg2_gen = _dialog(qapp, live=self._live(sblp="le"))
            dlg2 = next(dlg2_gen)
            try:
                dlg2._tables["neg"].toggle_record()
                self._feed(dlg2, [self._frame(data=b"\x00\x01")])
                # Ведущий нуль провода виден в DATA 16; число — по
                # контракту извлечения (CM-FR07-R2).
                assert self._hex(dlg2, 0) == "00 01"
                assert dlg2._tables["neg"].table.item(
                    0, dlg2._tables["neg"].COL_DEC
                ).text() == "256"
            finally:
                for _ in dlg2_gen:
                    pass
        finally:
            for _ in gen:
                pass

    def test_multi_byte_and_sum_fallback(self, qapp) -> None:
        """le по трём байтам; без SBLP — легаси-сумма байтов, как у
        extract_var_raw рабочего декода."""
        gen = _dialog(qapp, live=self._live(sblp="le", bytes=[0, 1, 2]))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [self._frame(data=b"\x01\x02\x03")])
            assert self._dec(dlg, 0) == "197121"  # 0x030201
        finally:
            for _ in gen:
                pass
        gen = _dialog(qapp, live=self._live(sblp=""))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [self._frame()])
            assert self._dec(dlg, 0) == "3"       # сумма 1+2
            # CM-FR07-R2: журнал показывает окно провода «01 02»,
            # а не HEX суммы «00 03».
            assert self._hex(dlg, 0) == "01 02"
        finally:
            for _ in gen:
                pass

    def test_channel_honored_only_when_selected(self, qapp) -> None:
        """Выбранный канал фильтрует кадры; None — явный «любой»,
        обе шины легитимны (один ID на двух CAN)."""
        gen = _dialog(qapp, live=self._live(channel=0, sblp="be"))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [
                self._frame(channel=1, data=b"\xAA\xBB"),  # чужая шина
                self._frame(channel=0, data=b"\x01\x02"),
            ])
            assert dlg._tables["neg"]._history == 1
            assert self._hex(dlg, 0) == "01 02"
        finally:
            for _ in gen:
                pass
        # «любой» — обе шины пишутся.
        gen = _dialog(qapp, live=self._live(channel=None, sblp="be"))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [
                self._frame(channel=0, data=b"\x01\x02"),
                self._frame(channel=1, data=b"\x03\x04"),
            ])
            assert dlg._tables["neg"]._history == 2
            assert self._hex(dlg, 0) == "01 02"
            assert self._hex(dlg, 1) == "03 04"
        finally:
            for _ in gen:
                pass

    def test_rtr_malformed_short_not_samples(self, qapp) -> None:
        """RTR, не-байтовое DATA, короткий кадр (поле не покрыто) и
        кадр чужой разрядности — не образцы."""
        gen = _dialog(qapp, live=self._live(sblp="be"))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [
                self._frame(rtr=True),
                self._frame(data=b"\xAA\xBB") | {"data": "мусор"},
                self._frame(data=b"\x01"),           # поле не покрыто
                self._frame(extended=True),           # чужая разрядность
                {"id": "мусор", "channel": 0, "extended": False,
                 "rtr": False, "data": b"\x01\x02"},  # битый ID
                self._frame(data=b"\x11\x22"),
            ])
            assert dlg._tables["neg"]._history == 1
            assert self._hex(dlg, 0) == "11 22"
        finally:
            for _ in gen:
                pass

    def test_inactive_buttons_no_pending(self, qapp) -> None:
        """Обе кнопки выключены — очередь записи не копится."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            dlg._on_frames([self._frame(), self._frame()])
            assert dlg._pending == []
        finally:
            for _ in gen:
                pass

    def test_pending_bounded_with_status(self, qapp) -> None:
        """Очередь ограничена; потолок отражён явным статусом."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            dlg._on_frames([
                self._frame(data=bytes([i % 256, i // 256]))
                for i in range(dlg.PENDING_LIMIT + 3)
            ])
            assert len(dlg._pending) == dlg.PENDING_LIMIT
            assert dlg._pending_dropped == 3
            tbl2 = dlg._tables["pos"]
            tbl2.toggle_record()
            dlg._tick()
            assert "пропущено" in tbl2.status.text()
        finally:
            for _ in gen:
                pass

    def test_source_change_stops_recording(self, qapp) -> None:
        """Смена источника посреди записи — стоп с явным статусом и
        очисткой очереди, без смешивания историй (CM-FR07)."""
        live = self._live()
        gen = _dialog(qapp, live=live)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            dlg._tick()                       # источник зафиксирован
            dlg._on_frames([self._frame()])
            live["fid"] = 0x200               # оператор поменял ID
            dlg._tick()
            assert not tbl.recording
            assert "источник" in tbl.status.text()
            assert dlg._pending == []
        finally:
            for _ in gen:
                pass


class TestQueueOwnershipR2:
    """CM-FR07-R2: принадлежность образца и его байты решаются при
    приёме — не при сливе. «Стоп» осушает свой хвост; журнал
    показывает провод и канал приёма; записи — сверху."""

    def _live(self, **kw):
        state = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0, 1], "sblp": "", "get_frame": None,
        }
        state.update(kw)
        return state

    def _frame(self, data=b"\x01\x02", **kw):
        frame = {
            "id": 0x100, "channel": 0, "extended": False,
            "rtr": False, "data": bytes(data),
        }
        frame.update(kw)
        return frame

    def test_stop_drains_own_tail_immediately(self, qapp) -> None:
        """Проба ревью: «Записать»(−) → образец A → «Стоп»(−) до
        ближайшего тика → «Записать»(+) → тик. A — в журнале (−),
        а не потерян и не переложен в (+)."""
        gen = _dialog(qapp, live=self._live(sblp="be"))
        dlg = next(gen)
        try:
            neg, pos = dlg._tables["neg"], dlg._tables["pos"]
            neg.record_btn.click()
            dlg._on_frames([self._frame(data=b"\x0A\x0B")])
            neg.record_btn.click()          # Стоп — до тика
            # Хвост своей сессии долит немедленно, не дожидаясь
            # 100-мс тика.
            assert neg._history == 1
            assert neg._row_hex(0) == "0A 0B"
            assert dlg._pending == []
            pos.record_btn.click()
            dlg._on_frames([self._frame(data=b"\x0C\x0D")])
            dlg._tick()
            # Старый образец не переложен в (+) — та видит только
            # кадры своей сессии.
            assert pos._history == 1
            assert pos._row_hex(0) == "0C 0D"
            assert neg._history == 1
        finally:
            for _ in gen:
                pass

    def test_tick_delivers_by_arrival_tags(self, qapp) -> None:
        """Обе записывают при приходе A → A принадлежит обеим;
        после «Стоп»(−) образец B — только (+). Тик не пересматривает
        владельцев."""
        gen = _dialog(qapp, live=self._live(sblp="be"))
        dlg = next(gen)
        try:
            neg, pos = dlg._tables["neg"], dlg._tables["pos"]
            neg.record_btn.click()
            pos.record_btn.click()
            dlg._on_frames([self._frame(data=b"\x0A\x0B")])
            neg.record_btn.click()          # стоп: A долит в (−)
            assert neg._row_hex(0) == "0A 0B"
            dlg._on_frames([self._frame(data=b"\x0C\x0D")])
            dlg._tick()
            # A — у обеих (обе записывали при приёме), B — только (+).
            # Порядок журнала — приход: A на строке 0, B на строке 1.
            assert pos._history == 2
            assert pos._row_hex(0) == "0A 0B"
            assert pos._row_hex(1) == "0C 0D"
            assert neg._history == 1
        finally:
            for _ in gen:
                pass

    def test_journal_keeps_wire_bytes_and_channel(self, qapp) -> None:
        """le: DATA 16 — байты провода «01 02» как пришли, DATA 10 —
        число по контракту; канал приёма — в подсказке строки."""
        gen = _dialog(qapp, live=self._live(sblp="le"))
        dlg = next(gen)
        try:
            neg = dlg._tables["neg"]
            neg.record_btn.click()
            dlg._on_frames([
                self._frame(channel=1, data=b"\x01\x02"),
            ])
            dlg._tick()
            item = neg.table.item(0, neg.COL_HEX)
            assert item.text() == "01 02"
            assert "CAN2" in item.toolTip()
            assert neg.table.item(0, neg.COL_DEC).text() == "513"
        finally:
            for _ in gen:
                pass


class TestJournalLayoutR3:
    """CM-FR13/CM-FR14: журнал — в порядке прихода сверху вниз;
    первые десять образцов занимают DATA-ячейки строк делителей,
    дальше — строки без условий; делители и галочки не
    сдвигаются и не размножаются; журнальная запись не связана
    с делителем своей строки; дедуп — по окну провода."""

    def _live(self, **kw):
        state = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0], "sblp": "", "get_frame": None,
        }
        state.update(kw)
        return state

    def _frame(self, data=b"\x01", **kw):
        frame = {
            "id": 0x100, "channel": 0, "extended": False,
            "rtr": False, "data": bytes(data),
        }
        frame.update(kw)
        return frame

    def test_arrival_order_top_down(self, qapp) -> None:
        """Проба ревью: пришло 01 → 02 → 03, журнал сверху вниз —
        01, 02, 03 в DATA-ячейках строк 0..2."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            dlg._on_frames([self._frame(b"\x01"), self._frame(b"\x02"),
                            self._frame(b"\x03")])
            dlg._tick()
            assert tbl._history == 3
            assert [tbl._row_hex(r) for r in range(3)] == [
                "01", "02", "03",
            ]
            assert [
                tbl.table.item(r, tbl.COL_DEC).text()
                for r in range(3)
            ] == ["1", "2", "3"]
            # Делители не сдвинулись и не размножились.
            divs = [
                int(tbl.table.item(r, tbl.COL_NUM).text())
                for r in range(10)
            ]
            assert divs == list(sd.DIVISORS)
        finally:
            for _ in gen:
                pass

    def test_layout_after_fifteen_samples(self, qapp) -> None:
        """15 образцов: десять условий на своих строках с журнальными
        DATA; строки 10–14 — добавленный журнал без условий."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            dlg._on_frames([
                self._frame(bytes([0x10 + i])) for i in range(15)
            ])
            dlg._tick()
            assert tbl._history == 15
            assert tbl.table.rowCount() == 15
            for r in range(10):
                assert tbl._req_boxes(r) is not None
                assert (
                    tbl.table.item(r, tbl.COL_NUM).text()
                    == str(list(sd.DIVISORS)[r])
                )
            for r in range(10, 15):
                assert tbl._req_boxes(r) is None
                assert tbl.table.item(r, tbl.COL_NUM).text() == ""
                assert tbl._row_hex(r) == f"{0x10 + r:02X}"
        finally:
            for _ in gen:
                pass

    def test_journal_independent_of_divisor(self, qapp) -> None:
        """CM-FR13 п.5: образец 03 в DATA строки «кратно 2» не
        нарушает настройку — журнал и условие не связаны; галочки
        после захвата работают и взаимоисключают."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            dlg._on_frames([self._frame(b"\x03")])   # 3 — в строку 0
            dlg._tick()
            # Ставим «кратно 2» на строке с образцом 03: журнал —
            # не нарушение правила строки.
            cb_div, cb_not = tbl._req_boxes(0)
            cb_div.setChecked(True)
            assert not tbl.errors()
            # Взаимоисключение и корректный делитель после захвата.
            cb_not.setChecked(True)
            assert not cb_div.isChecked()
            assert tbl.sign_rules()[2] == "not"
            # OK не блокируется журнальным образцом.
            dlg._on_accept()
            assert dlg.result() == QDialog.DialogCode.Accepted
        finally:
            for _ in gen:
                pass

    def test_stop_start_continues_and_close_clears(self, qapp) -> None:
        """Стоп/старт продолжает историю следующей записью; закрытие
        очищает DATA и снимает добавленные строки, правила целы."""
        gen = _dialog(qapp, live=self._live())
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            dlg._on_frames([self._frame(b"\x10")])
            dlg._tick()
            tbl.record_btn.click()                 # Стоп
            tbl.record_btn.click()                 # снова Записать
            dlg._on_frames([self._frame(bytes([0x20 + i]))
                            for i in range(11)])
            dlg._tick()
            assert tbl._history == 12
            assert tbl._row_hex(0) == "10"
            assert tbl._row_hex(11) == "2A"
            dlg.reject()
            # Добавленные строки сняты, DATA условий очищены,
            # делители и правила не потеряны.
            assert tbl.table.rowCount() == 10
            assert tbl._history == 0
            for r in range(10):
                assert tbl._row_hex(r) == ""
                assert tbl.table.item(
                    r, tbl.COL_HEX
                ).data(Qt.ItemDataRole.UserRole) is None
            assert tbl.sign_rules() == {8: "div"}
        finally:
            for _ in gen:
                pass

    def test_dedup_by_wire_window(self, qapp) -> None:
        """Проба ревью CM-FR14: окна 01 02, 01 02, 02 01, 01 02 при
        одинаковой сумме — три строки 01 02, 02 01, 01 02 сверху
        вниз, DATA 10 у всех 3. Подаём настоящие кадры."""
        gen = _dialog(qapp, live=self._live(bytes=[0, 1]))
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            for wire in (b"\x01\x02", b"\x01\x02", b"\x02\x01",
                         b"\x01\x02"):
                dlg._on_frames([self._frame(wire)])
            dlg._tick()
            assert tbl._history == 3
            assert [tbl._row_hex(r) for r in range(3)] == [
                "01 02", "02 01", "01 02",
            ]
            assert [
                tbl.table.item(r, tbl.COL_DEC).text()
                for r in range(3)
            ] == ["3", "3", "3"]
        finally:
            for _ in gen:
                pass

    def test_dedup_wire_window_be_le(self, qapp) -> None:
        """BE/LE: разные окна с одним числом не схлопываются;
        окно провода и число по контракту сохранены."""
        gen = _dialog(qapp, live=self._live(bytes=[0, 1], sblp="le"))
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            dlg._on_frames([self._frame(b"\x01\x02"),
                            self._frame(b"\x02\x01")])
            dlg._tick()
            assert tbl._history == 2
            assert tbl._row_hex(0) == "01 02"
            assert tbl._row_hex(1) == "02 01"
            assert tbl.table.item(0, tbl.COL_DEC).text() == "513"
            assert tbl.table.item(1, tbl.COL_DEC).text() == "258"
        finally:
            for _ in gen:
                pass


class TestValidation:
    """F06 п.8–9: валидация по завершении ввода; невалидный HEX ≠
    нарушение кратности; некорректная настройка не подтверждается."""

    def test_invalid_hex_kept_and_flagged(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            item = tbl.table.item(0, tbl.COL_HEX)
            item.setText("GH")          # commit → валидация
            tbl._revalidate_row(0)
            assert item.text() == "GH"  # не стёрто
            assert tbl.errors(), "невалидный HEX — ошибка"
        finally:
            for _ in gen:
                pass

    def test_divisibility_violation_distinct(self, qapp) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            # Код 0x10 (16) на фиксированной строке «кратно 6» —
            # валидный HEX, но нарушение кратности (жёлтое,
            # отличное от красного).
            r = list(sd.DIVISORS).index(6)
            tbl.table.item(r, tbl.COL_HEX).setText("10")
            cb_div, _cb_not = tbl._req_boxes(r)
            cb_div.setChecked(True)
            tbl._revalidate_row(r)
            errs = tbl.errors()
            assert any("не подходит" in e for e in errs)
        finally:
            for _ in gen:
                pass

    def test_accept_refused_on_errors(self, qapp, monkeypatch) -> None:
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            warned = []
            monkeypatch.setattr(
                QMessageBox, "warning",
                staticmethod(lambda *a, **k: warned.append(True)),
            )
            tbl = dlg._tables["neg"]
            tbl.table.item(0, tbl.COL_HEX).setText("ZZ")
            tbl._revalidate_row(0)
            dlg._on_accept()
            assert warned and not dlg.result()
        finally:
            for _ in gen:
                pass


class TestUnifiedDecode:
    """CM-F05: единый декод для «degrees» и легаси «steering»;
    нейтраль — нулевая точка привязки; инверсия один раз."""

    def _var(self, **kw) -> dict:
        cfg = {
            "type": "dynamic", "id": "0x100", "bytes": [0],
            "out_range": "degrees",
            "steering": dict(_V2),
            "points": [[0, 0], [16, 45.0], [16, -45.0],
                       [20, 60.0], [20, -60.0]],
        }
        cfg.update(kw)
        return cfg

    def test_degrees_decodes_via_binding(self) -> None:
        # 16 кратно 8 → класс «−» → −45° по ветке привязки.
        res = vd.decode_var_value(self._var(), b"\x10")
        assert res["valid"] and res["value"] == -45.0

    def test_legacy_steering_mode_decodes(self) -> None:
        res = vd.decode_var_value(
            self._var(out_range="steering"), b"\x10"
        )
        assert res["valid"] and res["value"] == -45.0

    def test_neutral_from_zero_point(self) -> None:
        res = vd.decode_var_value(self._var(), b"\x00")
        assert res["valid"] and res["value"] == 0.0
        assert res["status"] == "neutral"

    def test_no_zero_point_no_neutral(self) -> None:
        steering = dict(_V2)
        # Без legacy-нейтрали/калибровки нулевая точка не синтезируется.
        for key in ("neutral", "calib"):
            steering.pop(key)
        var = self._var(steering=steering,
                        points=[[16, 45.0], [8, -20.0]])
        # Код 4 кратен 4 и не кратен 8 → «+», а не нейтраль.
        res = vd.decode_var_value(var, b"\x04")
        assert res["status"] == "ok" and res["value"] == 45.0

    def test_inverted_flips_once(self) -> None:
        var = self._var()
        var["steering"]["inverted"] = True
        var["steering"].pop("left_is_negative")
        res = vd.decode_var_value(var, b"\x10")
        assert res["valid"] and res["value"] == 45.0

    def test_conflict_invalid(self) -> None:
        var = self._var()
        var["steering"]["signs"] = {"pos": {4: "div"}, "neg": {4: "div"}}
        res = vd.decode_var_value(var, b"\x10")
        assert not res["valid"] and res["status"] == "conflict"

    def test_unit_carried_to_result(self) -> None:
        var = self._var()
        var["steering"]["unit"] = "fahrenheit"
        res = vd.decode_var_value(var, b"\x10")
        assert res["unit"] == "fahrenheit" and res["value"] == -45.0


class _FramesEmitter(QObject):
    """Настоящий Qt-сигнал потока кадров — для проверки отписки и
    запоздалых сигналов через реальный механизм connect/disconnect
    (CM-FR08-R2), а не фейковый список колбэков."""
    frames = Signal(list)


class _FakeSignal:
    """Минимальный connect/disconnect/emit для device_signal."""

    def __init__(self) -> None:
        self._subs = []

    def connect(self, cb) -> None:
        self._subs.append(cb)

    def disconnect(self, cb) -> None:
        self._subs.remove(cb)

    def emit(self, *args) -> None:
        for cb in list(self._subs):
            cb(*args)


class TestLifecycle:
    """CM-FR08: закрытие декодера завершает его жизнь — отписка от
    кадров и сигнала устройства, стоп таймера, очистка очереди и
    временной истории; финализация идемпотентна, поздние сигналы не
    мутируют закрытый диалог."""

    def _live(self, subs, sig=None, **kw):
        state = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0, 1], "sblp": "", "get_frame": None,
        }
        state.update(kw)

        def _sub(cb):
            subs.append(cb)

            def _unsub():
                if cb in subs:
                    subs.remove(cb)

            return _unsub

        state["subscribe"] = _sub
        state["device_signal"] = sig
        return state

    def test_reject_disconnects_everything(self, qapp) -> None:
        subs, sig = [], _FakeSignal()
        gen = _dialog(qapp, live=self._live(subs, sig))
        dlg = next(gen)
        try:
            assert subs == [dlg._on_frames]
            assert sig._subs == [dlg._on_device_changed]
            assert dlg._live_timer.isActive()
            dlg._tables["neg"].toggle_record()
            dlg.reject()
            assert dlg._closed
            assert not dlg._live_timer.isActive()
            assert subs == [] and sig._subs == []
            assert not dlg._tables["neg"].recording
            # Запоздалый сигнал после закрытия — ничего не меняет.
            dlg._on_frames([{"id": 0x100, "channel": 0, "extended": False,
                             "rtr": False, "data": b"\x01\x02"}])
            assert dlg._pending == []
        finally:
            for _ in gen:
                pass

    def test_reopen_without_subscriber_growth(self, qapp) -> None:
        subs = []
        gen = _dialog(qapp, live=self._live(subs))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            dlg._on_frames([{"id": 0x100, "channel": 0, "extended": False,
                             "rtr": False, "data": b"\x01\x02"}])
            dlg._tick()
            assert dlg._tables["neg"]._history == 1
            dlg.reject()
            # История закрытого диалога очищена финализацией —
            # DATA строк условий пусты, делители на месте.
            assert dlg._tables["neg"].table.rowCount() == 10
            assert dlg._tables["neg"]._row_hex(0) == ""
        finally:
            for _ in gen:
                pass
        gen2 = _dialog(qapp, live=self._live(subs))
        dlg2 = next(gen2)
        try:
            # Ровно один подписчик — у нового окна; история пуста,
            # сохранённые правила на месте (делитель 8).
            assert subs == [dlg2._on_frames]
            assert dlg2._tables["neg"].table.rowCount() == 10
            assert dlg2._tables["neg"]._row_req(3) == "div"
        finally:
            for _ in gen2:
                pass

    def test_finalize_idempotent(self, qapp) -> None:
        subs = []
        gen = _dialog(qapp, live=self._live(subs))
        dlg = next(gen)
        try:
            dlg.stop_all()
            dlg.stop_all()           # повтор — безопасен
            dlg.reject()
            assert dlg._closed and subs == []
        finally:
            for _ in gen:
                pass

    def test_device_disconnect_stops_capture(self, qapp) -> None:
        subs, sig = [], _FakeSignal()
        gen = _dialog(qapp, live=self._live(subs, sig))
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            dlg._on_frames([{"id": 0x100, "channel": 0, "extended": False,
                             "rtr": False, "data": b"\x01\x02"}])
            assert dlg._pending
            sig.emit(False)          # устройство отключилось
            assert not tbl.recording
            assert dlg._pending == []
            assert "отключено" in tbl.status.text()
        finally:
            for _ in gen:
                pass

    def test_done_path_finalizes(self, qapp) -> None:
        """Прямой done() обходит accept/reject/closeEvent — но не
        финализацию (CM-FR08-R2): guard, таймер, записи, отписки —
        те же, что у обычного закрытия."""
        subs, sig = [], _FakeSignal()
        gen = _dialog(qapp, live=self._live(subs, sig))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            dlg.done(1)
            assert dlg._closed
            assert not dlg._live_timer.isActive()
            assert not dlg._tables["neg"].recording
            assert subs == [] and sig._subs == []
        finally:
            for _ in gen:
                pass

    def test_deferred_delete_finalizes(self, qapp) -> None:
        """Разрушение минуя done (deleteLater — как у смерти
        родителя или deferred delete) вызывает ту же финализацию
        из destroyed() (CM-FR08-R2): отписка от настоящего
        Qt-сигнала подтверждается сторонним списком."""
        emitter = _FramesEmitter()
        unsubscribed: list = []

        def _sub(cb):
            emitter.frames.connect(cb)

            def _unsub():
                # Разрушенный получатель уже снят Qt'ом —
                # disconnect кинет; финализация всё равно считается.
                with contextlib.suppress(Exception):
                    emitter.frames.disconnect(cb)
                unsubscribed.append(cb)

            return _unsub

        live = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0, 1], "sblp": "", "get_frame": None,
            "subscribe": _sub, "device_signal": None,
        }
        dlg = vt._DegreesDecoderDialog(dict(_V2), QFont(), live=live)
        dlg._tables["neg"].toggle_record()
        dlg.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # destroyed() → та же финализация: отписка исполнена даже без
        # done()/closeEvent (CM-FR08-R2). Поля мёртвого объекта не
        # трогаем — проверяем побочный эффект.
        assert len(unsubscribed) == 1
        # Поздняя эмиссия реального сигнала никому не доставляется.
        emitter.frames.emit([{"id": 0x100, "channel": 0,
                              "extended": False, "rtr": False,
                              "data": b"\x01\x02"}])

    def test_late_real_signal_after_done(self, qapp) -> None:
        """Запоздалый сигнал настоящего Qt после done() не пишет в
        закрытый диалог и не поднимает исключений (CM-FR08-R2)."""
        emitter = _FramesEmitter()

        def _sub(cb):
            emitter.frames.connect(cb)
            return lambda: emitter.frames.disconnect(cb)

        live = {
            "fid": 0x100, "extended": False, "channel": None,
            "bytes": [0, 1], "sblp": "", "get_frame": None,
            "subscribe": _sub, "device_signal": None,
        }
        gen = _dialog(qapp, live=live)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.record_btn.click()
            emitter.frames.emit([{"id": 0x100, "channel": 0,
                                  "extended": False, "rtr": False,
                                  "data": b"\x01\x02"}])
            assert dlg._pending
            dlg.done(0)
            emitter.frames.emit([{"id": 0x100, "channel": 0,
                                  "extended": False, "rtr": False,
                                  "data": b"\x03\x04"}])
            assert dlg._pending == []
            assert tbl.table.rowCount() == len(sd.DIVISORS)
        finally:
            for _ in gen:
                pass
