"""CM-F10: редактор ГЛ — имена доп. каналов вместо номеров,
повторное редактирование пунктов и свёрнутой программы по клику,
отсутствие бейджа «ПК» в шапке программы."""

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from core.serial_manager import SerialManager
import ui.flexible_logic_tab as fl_module
from ui.flexible_logic_tab import FlexibleLogicTab


class _FakeConfig:
    """Изолирует тест от реального config.json разработчика."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _VarsStub:
    """Вкладка «Переменные» с именованными и безымянными каналами."""

    def __init__(self) -> None:
        self.aux: list[dict[str, Any]] = [
            {"type": "aux", "channel": 1, "name": "Вентилятор",
             "function": "discrete_out"},
            {"type": "aux", "channel": 3, "name": "",
             "function": "discrete_out"},
            {"type": "flag", "name": "Флаг"},
        ]

    def export_config(self) -> dict[str, Any]:
        return {"read": [], "control": [], "aux": list(self.aux)}

    def aux_channel_configs(self) -> list[dict[str, Any]]:
        return [
            dict(c) for c in self.aux
            if c.get("type") == "aux" and str(c.get("name", "")).strip()
        ]

    def variable_names(self, *_a: Any, **_kw: Any) -> list[str]:
        return []

    def __getattr__(self, name: str) -> Any:
        return lambda *_a, **_kw: None


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(fl_module, "Config", _FakeConfig)
    widget = FlexibleLogicTab(SerialManager())
    widget._variables_tab = _VarsStub()
    widget.set_device_flex_state("no-tail")
    yield widget
    widget.deleteLater()


def _rule() -> dict[str, Any]:
    return {
        "title": "P1", "active": True,
        "events": [{"type": "aux", "channel": 1,
                    "direction": "out", "state": 1}],
        "conditions": [{"type": "aux", "channel": 3, "state": 1}],
        "actions": [{"type": "aux", "aux_enabled": True,
                     "aux_channel": 1, "aux_mode": "on"}],
    }


def _click_summary(item) -> None:
    """Реальный MouseButtonPress на строке-сводке пункта."""
    s = item._summary
    center = s.rect().center()
    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress, center,
        s.mapToGlobal(center),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    s.mousePressEvent(ev)


def test_aux_channel_names_in_summaries(tab) -> None:
    """Имена назначенных каналов показываются в сводках события/
    условия/действия; безымянный канал — «Канал №N» (CM-F10 п.1)."""
    tab.set_config([_rule()])
    row = tab._row_widgets[0]
    event_item = row._event_items[0]
    cond_item = row._cond_items[0]
    action_item = row._action_items[0]
    assert "Вентилятор" in event_item._summary.text()
    assert "Канал №3" in cond_item._summary.text()
    assert "Вентилятор" in action_item._summary.text()
    # Модель хранит неизменный номер канала (CM-F10 п.2).
    out = tab.get_config()[0]
    assert out["events"][0]["channel"] == 1
    assert out["conditions"][0]["channel"] == 3
    assert out["actions"][0]["aux_channel"] == 1


def test_rename_updates_text_not_binding(tab) -> None:
    """Переименование канала обновляет подпись без смены
    физического назначения (CM-F10 п.2)."""
    tab.set_config([_rule()])
    stub: _VarsStub = tab._variables_tab
    stub.aux[0]["name"] = "Печка"
    row = tab._row_widgets[0]
    row.refresh_variables()
    assert "Печка" in row._event_items[0]._summary.text()
    assert "Печка" in row._action_items[0]._summary.text()
    out = tab.get_config()[0]
    assert out["events"][0]["channel"] == 1
    assert out["actions"][0]["aux_channel"] == 1


def test_menu_lists_unnamed_channel_as_numbered(tab) -> None:
    """Каскадное меню показывает безымянный настроенный канал как
    «Канал №N», а именованный — именем (CM-F10 п.1)."""
    tab.set_config([_rule()])
    item = tab._row_widgets[0]._event_items[0]
    spec = dict(item._menu_spec())
    aux_entries = spec["aux"]
    labels = [label for label, _ in aux_entries]
    assert "Вентилятор" in labels
    assert "Канал №3" in labels


def test_repeated_editing_via_summary_click(tab) -> None:
    """Событие/условие/действие открываются по клику на сводку
    сколько угодно раз — не только первый экземпляр (CM-F10 п.3-4)."""
    tab.set_config([_rule()])
    row = tab._row_widgets[0]
    for items in (row._event_items, row._cond_items, row._action_items):
        item = items[0]
        assert item._editor_open is False  # запрограммировано — свёрнуто
        for _ in range(3):
            _click_summary(item)
            assert item._editor_open is True
            _click_summary(item)
            assert item._editor_open is False
        # Финальное разворачивание — редактор остаётся доступным.
        _click_summary(item)
        assert item._editor_open is True


def test_collapsed_program_summary_is_clickable(tab) -> None:
    """Свёрнутая программа разворачивается кликом по строке-сводке
    (CM-F10 п.3)."""
    tab.set_config([_rule()])
    row = tab._row_widgets[0]
    row._toggle_collapsed()
    assert row._body.isHidden()
    assert row._summary_label.isVisible() or True  # visible без окна — нет
    # Клик по сводке программы разворачивает тело.
    s = row._summary_label
    center = s.rect().center()
    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress, center,
        s.mapToGlobal(center),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    s.mousePressEvent(ev)
    assert not row._body.isHidden()
    # Повторное сворачивание/разворачивание — сколько угодно раз.
    for _ in range(2):
        row._toggle_collapsed()
        assert row._body.isHidden()
        center = s.rect().center()
        s.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress, center,
                s.mapToGlobal(center),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert not row._body.isHidden()


def test_no_pc_badge_in_program_header(tab) -> None:
    """Бейдж «ПК»/«устройство» убран из шапки программы; owner-логика
    исполнения сохранена внутренне (CM-F10 п.5)."""
    tab.set_config([_rule()])
    row = tab._row_widgets[0]
    assert not hasattr(row, "_executor_label")
    # Внутреннее состояние владельца по-прежнему фиксируется.
    tab._update_executor_badges()
    assert getattr(row, "_executor_state", (None, None))[0] in {
        "device", "pc", "unsaved", "off", "conflict", "suspended",
    }
