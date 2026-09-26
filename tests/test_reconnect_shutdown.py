"""Регрессия: close_port — финальное закрытие. После него реконнект не
планируется и не выполняется, пока порт явно не переоткрыт через
open_port (полевая жалоба: «при закрытии приложения что-то
отрабатывает» — поздний реконнект воскрешал порт, запускал вычитку и
зеркалил триггеры устройства в config.json уже на выходе).
"""

from core.serial_manager import SerialManager


def test_close_port_blocks_reconnect_scheduling() -> None:
    """После close_port таймер реконнекта не взводится."""
    manager = SerialManager()
    manager._auto_reconnect = True
    manager._last_port_name = "COM_TEST"
    manager.close_port()
    manager._schedule_reconnect()
    assert manager._reconnect_timer is None


def test_do_reconnect_noop_after_close() -> None:
    """Поздний вызов _do_reconnect (таймер успел сработать до
    _stop_reconnect_timer) после финального закрытия — тихий no-op."""
    manager = SerialManager()
    manager._auto_reconnect = True
    manager._last_port_name = "COM_TEST"
    manager.close_port()
    manager._do_reconnect()
    assert not manager.is_open()


def test_expect_reboot_does_not_reconnect_after_close() -> None:
    """Команда, сорвавшаяся в expect_reboot во время закрытия, не
    запускает цикл переподключения."""
    manager = SerialManager()
    manager._auto_reconnect = True
    manager._last_port_name = "COM_TEST"
    manager.close_port()
    manager.expect_reboot()
    manager._do_reconnect()
    assert not manager.is_open()
    assert manager._reconnect_timer is None


def test_open_port_silent_device_not_connected(monkeypatch) -> None:
    """Порт физически открылся, но устройство не ответило ни на
    CMD_DEVICE_ID, ни на SYSTEM_INFO (зомби-хендл Windows после отвала
    USB): это НЕ подключение — порт закрывается и циклический опрос
    продолжается («после долгой потери связи не определяется»)."""
    import serial as pyserial
    from PySide6.QtWidgets import QApplication

    from core import serial_manager as sm_mod

    if QApplication.instance() is None:
        QApplication([])  # QTimer требует event loop для isActive()

    class _SilentPort:
        def __init__(self, **_kw) -> None:
            self.port = "COM_SILENT"
            self.closed = False

        @property
        def is_open(self) -> bool:
            return not self.closed

        @property
        def dtr(self) -> bool:  # noqa: D102 - имитация pyserial
            return True

        @property
        def rts(self) -> bool:  # noqa: D102
            return True

        def close(self) -> None:
            self.closed = True

        def reset_input_buffer(self) -> None:
            pass

        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, _size: int = 1) -> bytes:
            return b""

        @property
        def in_waiting(self) -> int:
            return 0

    port = _SilentPort()
    monkeypatch.setattr(pyserial, "Serial", lambda **kw: port)
    # read() молчит — перекрываем таймауты, чтобы тест не ждал секунды
    monkeypatch.setattr(sm_mod.SerialManager, "request_control",
                        lambda self, *a, **kw: (_ for _ in ()).throw(TimeoutError()))

    manager = SerialManager()
    connected = []
    manager.connection_changed.connect(connected.append)

    ok = manager.open_port("COM_SILENT", 115200, emulation=False, auto_reconnect=True)

    assert not ok
    assert not manager.is_open()
    assert not manager._device_identified
    assert connected and all(c is False for c in connected)
    # Циклический опрос вооружён — устройство ответит при появлении.
    assert manager._reconnect_timer is not None
    assert manager._reconnect_timer.isActive()
