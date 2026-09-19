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
