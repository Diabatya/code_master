"""GUI-регрессии _AuxDialog после CM-F09: «Канал» вместо
«Пин (канал)», без физических имён пинов и «Режима выхода»,
варианты функций — только выходные (analog_out/pwm), ширина ~×1.5,
точный текст подсказки, сквозное пронесение легаси out_mode/pwm.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import ui.variables_tab as vt


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def test_dialog_width_and_hint(qapp) -> None:
    """CM-F09 п.2/п.3: ширина ≥630 логических px, точный текст
    подсказки о роли канала."""
    dlg = vt._AuxDialog(config={"type": "aux", "channel": 3})
    try:
        assert dlg.minimumWidth() >= 630
        assert dlg._hint.text() == (
            "Количество выходов и входов и их названия появятся после "
            "программирования каналов в устройство. Здесь "
            "настраивается только роль конкретного канала."
        )
    finally:
        dlg.deleteLater()


def test_no_pin_names_no_out_mode(qapp) -> None:
    """CM-F09 п.4–5/п.8: ни имён пинов, ни предупреждения SWCLK,
    ни настройки «Режим выхода»/ШИМ-полей в диалоге нет."""
    dlg = vt._AuxDialog(config={
        "type": "aux", "channel": 1, "out_mode": "pwm",
        "pwm_freq": 250, "pwm_duty": 40,
    })
    try:
        for attr in ("_pin_label", "_conflict", "_out_mode",
                     "_pwm_row", "_pwm_freq", "_pwm_duty", "_applied"):
            assert not hasattr(dlg, attr), attr
        # Ни один виджет диалога не показывает физическое имя пина.
        from PySide6.QtWidgets import QCheckBox, QLabel
        texts = [
            w.text()
            for cls in (QLabel, QCheckBox)
            for w in dlg.findChildren(cls)
        ]
        assert not any(
            pin in t for t in texts
            for pin in ("PA14", "PA15", "PC10", "PC11", "PC12",
                        "PD2", "PB3", "SWCLK")
        )
    finally:
        dlg.deleteLater()


def test_functions_are_outputs_only(qapp) -> None:
    """CM-F09 п.7: «Дискретный вход»/«Дискретный выход» убраны —
    все семь каналов — выходы."""
    dlg = vt._AuxDialog(config={"type": "aux", "channel": 3})
    try:
        items = [
            dlg._function.itemData(i)
            for i in range(dlg._function.count())
        ]
        assert items == ["analog_out", "pwm"]
        labels = [
            dlg._function.itemText(i)
            for i in range(dlg._function.count())
        ]
        assert not any("вход" in t.lower() for t in labels)
    finally:
        dlg.deleteLater()


def test_config_roundtrip_channel_function(qapp) -> None:
    dlg = vt._AuxDialog(config={
        "type": "aux", "name": "Вентилятор", "channel": 3,
        "function": "pwm",
    })
    try:
        cfg = dlg.config
        assert cfg["type"] == "aux" and cfg["channel"] == 3
        assert cfg["function"] == "pwm" and cfg["name"] == "Вентилятор"
        # Новый диалог не создаёт черновые поля настроек выхода.
        assert "out_mode" not in cfg and "pwm_freq" not in cfg
    finally:
        dlg.deleteLater()


def test_legacy_out_fields_passthrough(qapp) -> None:
    """CM-F09 п.9: сохранённые out_mode/pwm_* проносятся сквозь
    диалог неизменными — до миграции действий в снимок сигнал
    не меняется."""
    dlg = vt._AuxDialog(config={
        "type": "aux", "channel": 2, "function": "analog_out",
        "out_mode": "pwm", "pwm_freq": 250, "pwm_duty": 40,
    })
    try:
        cfg = dlg.config
        assert cfg["out_mode"] == "pwm"
        assert cfg["pwm_freq"] == 250 and cfg["pwm_duty"] == 40
    finally:
        dlg.deleteLater()


def test_flag_kind_unchanged(qapp) -> None:
    dlg = vt._AuxDialog(config={
        "type": "flag", "name": "Бит", "storage": "flash",
    })
    try:
        cfg = dlg.config
        assert cfg["type"] == "flag" and cfg["storage"] == "flash"
        assert "out_mode" not in cfg
    finally:
        dlg.deleteLater()


def test_row_label_shows_channel_not_pin(qapp) -> None:
    """Строка переменной — «Канал №N · функция», без имени пина
    и применённого режима выхода (CM-F09 п.5)."""
    from PySide6.QtWidgets import QLabel, QPushButton

    row = vt._VariableRow.__new__(vt._VariableRow)
    row.config = {
        "type": "aux", "channel": 3, "function": "pwm",
        "out_mode": "pwm", "pwm_freq": 250, "pwm_duty": 50,
    }
    row._name_label = QLabel()
    row._state_label = QLabel()
    row._test_btn = QPushButton()
    vt._VariableRow._refresh_labels(row)
    text = row._state_label.text()
    assert "Канал №3" in text and "PC10" not in text
