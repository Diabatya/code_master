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
            # Код 0x10 (16) с требованием «кратно 6» — валидный HEX,
            # но нарушение кратности (жёлтое, отличное от красного).
            tbl.toggle_record()
            tbl.capture(b"\x10")
            r = tbl.table.rowCount() - 1
            tbl.table.item(r, tbl.COL_NUM).setText("6")
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
