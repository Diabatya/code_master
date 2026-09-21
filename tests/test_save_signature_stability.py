"""Регрессия: снимок полей окна настроек не должен зависеть от
порядка виджетов в findChildren.

QTabWidget меняет z-order детей при каждом переключении вкладки —
позиционная сигнатура расходилась с эталоном без всякой правки, и
кнопка «Сохранить» мерцала/«скакала» при хождении по вкладкам
(полевая жалоба: «то активна, то пропадает»)."""

import sys
from typing import Any, Dict

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication


class _FakeConfig:
    """Изолирует тест от реального config.json разработчика."""

    DEFAULT_CONFIG: Dict[str, Any] = {}

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def set_bulk(self, values: Dict[str, Any]) -> None:
        self._data.update(values)

    def all(self) -> Dict[str, Any]:
        return dict(self._data)

    def import_data(self, values: Dict[str, Any]) -> None:
        self._data.update(values)

    def reset_to_defaults(self) -> None:
        self._data.clear()

    def save(self) -> None:
        pass

    def save_to_file(self, path: str) -> None:
        pass


class _FakeSerialManager(QObject):
    """Заглушка SerialManager: окно подключает его сигналы и is_open()."""

    connection_changed = Signal(bool)
    connecting = Signal()
    critical_error = Signal(str)
    device_identified = Signal()
    error_occurred = Signal(str)
    new_can_frame = Signal(int, bytes, int)
    reconnect_scheduled = Signal(str)

    def is_open(self) -> bool:
        return False

    def current_port_name(self) -> str:
        return ""


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def _make_window(qapp, monkeypatch):
    """SettingsWindow с фейковым Config — без реального config.json."""
    import ui.settings_window as sw_module
    from ui.settings_window import SettingsWindow

    monkeypatch.setattr(sw_module, "Config", _FakeConfig)
    window = SettingsWindow(_FakeSerialManager(), _FakeConfig(), None)
    yield window
    window.deleteLater()


def test_signature_stable_across_tab_switches(qapp, monkeypatch) -> None:
    """Переключение вкладок не меняет снимок полей."""
    for window in _make_window(qapp, monkeypatch):
        window.show()
        baseline = window._widgets_signature()
        for index in range(window._tabs.count()):
            window._tabs.setCurrentIndex(index)
            qapp.processEvents()
            assert window._widgets_signature() == baseline, (
                f"сигнатура изменилась при переходе на вкладку {index}: "
                "«Сохранить» включится без правок оператора"
            )
