"""GUI-регрессии настроек выхода в _AuxDialog (CM-N7): константы
Выкл/Вкл/ШИМ, частота и заполнение сохраняются и читаются обратно,
видны реальное имя пина и причина ограничения, а для ШИМ — фазы
после квантования в сетку целых мс."""

from __future__ import annotations

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import ui.variables_tab as vt


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def _dialog(qapp, config: dict[str, Any] | None = None):
    d = vt._AuxDialog(config=config)
    yield d
    d.deleteLater()


def test_pwm_fields_roundtrip(qapp) -> None:
    dlg = vt._AuxDialog(config={
        "type": "aux", "name": "Вентилятор", "channel": 3,
        "function": "pwm", "out_mode": "pwm",
        "pwm_freq": 250, "pwm_duty": 40,
    })
    cfg = dlg.config
    assert cfg["out_mode"] == "pwm"
    assert cfg["pwm_freq"] == 250 and cfg["pwm_duty"] == 40
    assert cfg["channel"] == 3
    dlg.deleteLater()


def test_mode_switch_shows_pwm_fields(qapp) -> None:
    dlg = vt._AuxDialog(config={"type": "aux", "channel": 3})
    idx = dlg._out_mode.findData("pwm")
    dlg._out_mode.setCurrentIndex(idx)
    dlg._refresh_aux_out()
    assert dlg._pwm_row.isVisibleTo(dlg)
    # Применённая фаза после квантования: 250 Гц → 4 мс, 50% → 2+2.
    dlg._pwm_freq.setValue(250)
    dlg._pwm_duty.setValue(50)
    assert "2+2" in dlg._applied.text()
    dlg._out_mode.setCurrentIndex(dlg._out_mode.findData("on"))
    dlg._refresh_aux_out()
    assert not dlg._pwm_row.isVisibleTo(dlg)
    dlg.deleteLater()


def test_pwm_quantization_shown_not_requested(qapp) -> None:
    """500 Гц 99% квантуется до 100% — подпись честная."""
    dlg = vt._AuxDialog(config={
        "type": "aux", "channel": 3, "out_mode": "pwm",
        "pwm_freq": 500, "pwm_duty": 99,
    })
    assert "2+0" in dlg._applied.text()
    assert "100.0" in dlg._applied.text()
    dlg.deleteLater()


def test_pin_conflict_reason_shown(qapp) -> None:
    """PA14/PD2/PB3 — с причиной ограничения; свободный пин — без."""
    dlg = vt._AuxDialog(config={"type": "aux", "channel": 1})
    assert "PA14" in dlg._pin_label.text()
    assert "SWCLK" in dlg._conflict.text()
    dlg._channel.setValue(3)
    dlg._refresh_aux_out()
    assert dlg._pin_label.text() == "PC10"
    assert not dlg._conflict.isVisibleTo(dlg)
    dlg._channel.setValue(7)
    dlg._refresh_aux_out()
    assert "CAN1" in dlg._conflict.text()
    dlg.deleteLater()


def test_flag_kind_unchanged(qapp) -> None:
    dlg = vt._AuxDialog(config={
        "type": "flag", "name": "Бит", "storage": "flash",
    })
    cfg = dlg.config
    assert cfg["type"] == "flag" and cfg["storage"] == "flash"
    assert "out_mode" not in cfg
    dlg.deleteLater()


def test_row_label_shows_applied_mode(qapp) -> None:
    """Строка переменной показывает пин и применённый режим выхода."""
    from PySide6.QtWidgets import QLabel, QPushButton

    row = vt._VariableRow.__new__(vt._VariableRow)
    row.config = {
        "type": "aux", "channel": 3, "out_mode": "pwm",
        "pwm_freq": 250, "pwm_duty": 50,
    }
    row._name_label = QLabel()
    row._state_label = QLabel()
    row._test_btn = QPushButton()
    vt._VariableRow._refresh_labels(row)
    text = row._state_label.text()
    assert "PC10" in text and "ШИМ" in text
