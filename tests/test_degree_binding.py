"""CM-FR09: привязка «Градусы» — эффективные роли «−/0/+».

Колонка «Величина» таблицы привязки — значение на ВЫХОДЕ декодера
(эффективная сторона); в конфиге точки хранятся по сырой стороне,
«Инвертирование» умножает результат один раз. Проверяются: цвета
и подписи нарушений кратности, роли по умолчанию без затирания
ввода, единый отказ _on_accept при невалидной привязке, переворот
инверсии в обе стороны на асимметричных масштабах и round-trip
без тихой потери точек.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

import ui.variables_tab as vt
from core import variable_decode as vd


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


# «−» = кратно 8; «+» = кратно 4 И не кратно 8 (проба ревью).
_STEER = {
    "enabled": True,
    "signs": {"neg": {8: "div"}, "pos": {4: "div", 8: "not"}},
    "inverted": False,
    "stale_ms": 0,
    "unit": "none",
}


def _dialog(qapp, points=None, steering=None, **kw):
    cfg = {
        "type": "dynamic", "name": "Угол", "id": "100", "dlc": 8,
        "from": "01 02", "to": "X",
        "out_range": "degrees",
        "steering": dict(steering if steering is not None else _STEER),
        "points": list(points) if points is not None else [],
    }
    cfg.update(kw)
    dlg = vt.VariableDialog(config=cfg)
    return dlg


def _page(dlg):
    return dlg._num_page


def _cell_color(dlg, row):
    item = _page(dlg).points_table.item(row, 0)
    return item.foreground().color().name().lower()


def _accept(dlg, monkeypatch):
    warned = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.append(True)),
    )
    dlg._on_accept()
    return bool(warned)


_GREEN = "#4caf50"
_RED = "#f44336"
_AMBER = "#ffb74d"


class TestEffectiveSideColors:
    """Цвета кратности — по ЭФФЕКТИВНОЙ стороне (CM-FR09 п.3)."""

    def test_valid_binding_all_green(self, qapp) -> None:
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            assert _cell_color(dlg, 0) == _GREEN  # нейтраль
            assert _cell_color(dlg, 1) == _GREEN  # −45 на коде ÷8
            assert _cell_color(dlg, 2) == _GREEN  # +45 на коде ÷4¬8
        finally:
            dlg.deleteLater()

    def test_no_class_is_red_with_message(self, qapp) -> None:
        """Проба ревью: 0x0A (10) не удовлетворяет ни «−»÷8, ни
        «+»÷4¬8 — нарушение красное и с требуемой подписью, а не
        молчаливый янтарный."""
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x0A, -45], [0x0C, 45]],
        )
        try:
            item = _page(dlg).points_table.item(1, 0)
            assert item.foreground().color().name().lower() == _RED
            assert "кратности" in item.toolTip()
        finally:
            dlg.deleteLater()

    def test_wrong_side_is_red(self, qapp) -> None:
        """0x0C — «+»-класс; отрицательная опорная на нём —
        нарушение."""
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x0C, -45], [0x08, 45]],
        )
        try:
            assert _cell_color(dlg, 1) == _RED
            assert _cell_color(dlg, 2) == _RED
        finally:
            dlg.deleteLater()

    def test_inverted_flips_expected_side(self, qapp) -> None:
        """inverted: stored (0x0C,+45) показывается эффективными −45
        на «+»-коде, stored (0x08,−45) — +45 на «−»-коде: обе
        валидны, рабочее значение совпадает с показанным."""
        steer = dict(_STEER)
        steer["inverted"] = True
        dlg = _dialog(
            qapp, steering=steer,
            points=[[0x00, 0], [0x0C, 45], [0x08, -45]],
        )
        try:
            assert _cell_color(dlg, 1) == _GREEN
            assert _cell_color(dlg, 2) == _GREEN
        finally:
            dlg.deleteLater()

    def test_inverted_violation_red(self, qapp) -> None:
        """Сценарий дефекта: inverted, ячейка «−45» на коде 0x08
        («−»-класс → эффективный вывод здесь всегда > 0) — stored
        (0x08,+45) показывается −45 и недостижим → красный; то же
        для «+45» на «+»-коде 0x0C."""
        steer = dict(_STEER)
        steer["inverted"] = True
        dlg = _dialog(
            qapp, steering=steer,
            points=[[0x00, 0], [0x08, 45], [0x0C, -45]],
        )
        try:
            item = _page(dlg).points_table.item(1, 0)
            assert item.foreground().color().name().lower() == _RED
            assert "кратности" in item.toolTip()
            item2 = _page(dlg).points_table.item(2, 0)
            assert item2.foreground().color().name().lower() == _RED
        finally:
            dlg.deleteLater()


class TestAcceptGate:
    """_on_accept не принимает невалидную привязку (CM-FR09)."""

    def test_no_class_refused(self, qapp, monkeypatch) -> None:
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x0A, -45], [0x0C, 45]],
        )
        try:
            warned = _accept(dlg, monkeypatch)
            assert warned and dlg.result() != dlg.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def test_valid_accepted(self, qapp, monkeypatch) -> None:
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            warned = _accept(dlg, monkeypatch)
            assert not warned
            assert dlg.result() == dlg.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def test_missing_roles_refused(self, qapp, monkeypatch) -> None:
        dlg = _dialog(qapp, points=[[0x00, 0], [0x08, -45]])
        try:
            warned = _accept(dlg, monkeypatch)
            assert warned and not dlg.result()
        finally:
            dlg.deleteLater()

    def test_duplicate_code_refused(self, qapp, monkeypatch) -> None:
        dlg = _dialog(
            qapp,
            points=[[0x00, 0], [0x08, -45], [0x08, -20], [0x0C, 45]],
        )
        try:
            warned = _accept(dlg, monkeypatch)
            assert warned and not dlg.result()
        finally:
            dlg.deleteLater()

    def test_ambiguous_zero_refused(self, qapp, monkeypatch) -> None:
        dlg = _dialog(
            qapp,
            points=[[0x00, 0], [0x04, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            warned = _accept(dlg, monkeypatch)
            assert warned and not dlg.result()
        finally:
            dlg.deleteLater()

    def test_incomplete_point_refused(self, qapp, monkeypatch) -> None:
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            _page(dlg).add_point_row()  # пустая строка — не считается
            # частично заполненная: величина есть, кода нет
            row = _page(dlg).points_table.rowCount()
            _page(dlg).points_table.item(row - 1, 1).setText("-10")
            warned = _accept(dlg, monkeypatch)
            assert warned and not dlg.result()
        finally:
            dlg.deleteLater()


class TestRoleRows:
    """Включение «Градусы» — три роли с пояснениями, ввод цел."""

    def test_roles_added_on_enable(self, qapp) -> None:
        cfg = {
            "type": "dynamic", "name": "V", "id": "100", "dlc": 8,
            "from": "01 02", "to": "X", "out_range": "inf",
            "points": [[0, 0], [100, 500]],
        }
        dlg = vt.VariableDialog(config=cfg)
        try:
            page = _page(dlg)
            idx = page.out_range.findData("degrees")
            page.out_range.setCurrentIndex(idx)
            # Свои строки покрывают роли «0» и «+» — добирается
            # только «−», введённое не трогается.
            assert page.points_table.rowCount() == 3
            assert page.points_table.item(0, 0).text().strip() != ""
            ys = [
                page.points_table.item(r, 1).text()
                for r in range(3)
            ]
            assert ys == ["0", "500", "-45"]
            assert "−" in page.points_table.item(2, 0).toolTip() or \
                "отрицательн" in page.points_table.item(2, 0).toolTip()
        finally:
            dlg.deleteLater()

    def test_roles_added_on_empty(self, qapp) -> None:
        """Пустая таблица → все три роли «−»/«0»/«+» с пояснениями."""
        dlg = _dialog(qapp, points=[])
        try:
            page = _page(dlg)
            ys = [
                page.points_table.item(r, 1).text()
                for r in range(page.points_table.rowCount())
            ]
            assert ys == ["-45", "0", "45"]
        finally:
            dlg.deleteLater()

    def test_roles_not_duplicated(self, qapp) -> None:
        """Полная привязка при повторном выборе режима не получает
        лишних строк."""
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            page = _page(dlg)
            page._ensure_degree_role_rows()
            assert page.points_table.rowCount() == 3
        finally:
            dlg.deleteLater()


class TestInversionConsistency:
    """Инверсия один раз, согласованно: таблица ↔ конфиг ↔ декод
    (CM-FR09, асимметричные масштабы)."""

    def test_roundtrip_inverted(self, qapp) -> None:
        """stored (0x08,−45) + inverted → ячейка +45; read() → −45 —
        сохранение/переоткрытие без потери точек."""
        steer = dict(_STEER)
        steer["inverted"] = True
        dlg = _dialog(
            qapp, steering=steer,
            points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            page = _page(dlg)
            # Эффективное отображение — то, что выдаст декодер.
            assert page.points_table.item(1, 1).text() == "45.0"
            assert page.points_table.item(2, 1).text() == "-45.0"
            stored = dict(page.read_points())
            assert stored[8.0] == -45.0 and stored[12.0] == 45.0
        finally:
            dlg.deleteLater()

    def test_decode_matches_displayed(self, qapp) -> None:
        """Рабочее значение DATA == показанной величине: 0x08 при
        инверсии показывает +45 и декодируется в +45."""
        steer = dict(_STEER)
        steer["inverted"] = True
        dlg = _dialog(
            qapp, steering=steer,
            points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            cfg = _page(dlg).read()
            res = vd.decode_var_value(
                {**cfg, "bytes": [0]}, b"\x08"
            )
            assert res["valid"] and res["value"] == 45.0
            res = vd.decode_var_value(
                {**cfg, "bytes": [0]}, b"\x0C"
            )
            assert res["valid"] and res["value"] == -45.0
        finally:
            dlg.deleteLater()

    def test_roundtrip_not_inverted(self, qapp) -> None:
        dlg = _dialog(
            qapp, points=[[0x00, 0], [0x08, -45], [0x0C, 45]],
        )
        try:
            page = _page(dlg)
            assert page.points_table.item(1, 1).text() == "-45"
            stored = dict(page.read_points())
            assert stored[8.0] == -45.0
        finally:
            dlg.deleteLater()
