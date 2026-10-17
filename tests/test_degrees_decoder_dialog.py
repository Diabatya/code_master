"""Регрессии CM-F05/F06/F07 — единый режим «Градусы», диалог
декодера с записью DATA и таблица привязки как источник масштаба.

Проверяются: миграция «steering»→«degrees», live-контракт декода
по веткам сторон (нейтраль = нулевая точка таблицы), инверсия
один раз, обе таблицы записи (Записать/Стоп, 5 колонок, история
>10, дедуп, лимит), валидация HEX/кратности, отказ подтверждения
некорректной настройки, единица «Температура».
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

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
            # 15 значений → история длиннее десяти (F06 п.3).
            assert neg.table.rowCount() == 10 + 15
            # Повтор подряд не плодит строку (F06 п.18).
            neg.capture(b"\x40")
            assert neg.table.rowCount() == 10 + 16
            neg.capture(b"\x40")
            neg.capture(b"\x40")
            assert neg.table.rowCount() == 10 + 16
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
            r = tbl.table.rowCount() - 1
            assert tbl.table.item(r, tbl.COL_HEX).text() == "01 02"
            assert tbl.table.item(r, tbl.COL_DEC).text() == "258"
        finally:
            for _ in gen:
                pass

    def test_history_rows_are_journal_only(self, qapp) -> None:
        """CM-FR06: строки истории — журнал без активных условий:
        галочек и делителя нет, правило стороны они не меняют,
        и лежат НИЖЕ десяти фиксированных делителей."""
        gen = _dialog(qapp)
        dlg = next(gen)
        try:
            tbl = dlg._tables["neg"]
            tbl.toggle_record()
            for value in (b"\x10", b"\x20"):
                tbl.capture(value)
            for r in (10, 11):
                assert tbl._req_boxes(r) is None, (
                    "строка истории не должна иметь галочек требования"
                )
                num = tbl.table.item(r, tbl.COL_NUM)
                assert num.text() == ""
                assert not (num.flags() & Qt.ItemFlag.ItemIsEditable)
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
            assert tbl.table.rowCount() == 11
            tbl.toggle_record()
            tbl.capture(b"\x20")
            assert tbl.table.rowCount() == 12
            # Журнал и фиксированные делители на своих местах.
            divs = [
                int(tbl.table.item(r, tbl.COL_NUM).text())
                for r in range(10)
            ]
            assert divs == list(sd.DIVISORS)
            assert tbl._row_hex(10) == "10" and tbl._row_hex(11) == "20"
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
        return dlg._tables["neg"].table.rowCount() - 10

    def _dec(self, dlg, r):
        return dlg._tables["neg"].table.item(r, dlg._tables["neg"].COL_DEC).text()

    def _hex(self, dlg, r):
        return dlg._tables["neg"]._row_hex(r)

    def test_le_decodes_513_not_258(self, qapp) -> None:
        """bytes=[0,1], sblp='le', DATA 01 02 → журнал и делители
        читают 513 (0x0201), а не 258 — единый контракт с live."""
        gen = _dialog(qapp, live=self._live(sblp="le"))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            assert self._feed(dlg, [self._frame()]) == 1
            assert self._hex(dlg, 10) == "02 01"
            assert self._dec(dlg, 10) == "513"
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
            assert self._dec(dlg, 10) == "258"
            dlg2_gen = _dialog(qapp, live=self._live(sblp="le"))
            dlg2 = next(dlg2_gen)
            try:
                dlg2._tables["neg"].toggle_record()
                self._feed(dlg2, [self._frame(data=b"\x00\x01")])
                assert self._hex(dlg2, 10) == "01 00"
                assert dlg2._tables["neg"].table.item(
                    10, dlg2._tables["neg"].COL_DEC
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
            assert self._dec(dlg, 10) == "197121"  # 0x030201
        finally:
            for _ in gen:
                pass
        gen = _dialog(qapp, live=self._live(sblp=""))
        dlg = next(gen)
        try:
            dlg._tables["neg"].toggle_record()
            self._feed(dlg, [self._frame()])
            assert self._dec(dlg, 10) == "3"       # сумма 1+2
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
            assert dlg._tables["neg"].table.rowCount() == 11
            assert self._hex(dlg, 10) == "01 02"
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
            assert dlg._tables["neg"].table.rowCount() == 12
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
            assert dlg._tables["neg"].table.rowCount() == 11
            assert self._hex(dlg, 10) == "11 22"
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
            assert dlg._tables["neg"].table.rowCount() == 11
            dlg.reject()
            # История закрытого диалога очищена финализацией.
            assert dlg._tables["neg"].table.rowCount() == 10
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
