"""Тесты set_can_speed: команда CMD_CAN_SPEED должна нести канал и бод-рейт
в проволочном формате, отклонять неподдерживаемые значения и возвращать
фактически применённый бод из ответа устройства."""

import pytest

from core.can_protocol import CMD_CAN_SPEED
from core.serial_manager import SerialManager
from tests.test_request_control import _ScriptedPort, _cmd_response, _manager_with_port


def test_set_can_speed_payload_and_applied_baud() -> None:
    """[0xCE][3][ch][baud LE]; ответ несёт фактический бод u16 LE."""
    port = _ScriptedPort(_cmd_response(CMD_CAN_SPEED, 0, bytes([0xFA, 0x00])))  # 250
    manager = _manager_with_port(port)

    applied = manager.set_can_speed(1, 250)

    assert applied == 250
    assert port.writes == [bytes([CMD_CAN_SPEED, 3, 1, 0xFA, 0x00])]


def test_set_can_speed_independent_channels() -> None:
    """CAN1 и CAN2 программируются разными бод-рейтами независимо."""

    class _EchoPort(_ScriptedPort):
        """Отвечает на каждую команду записанным в ней бод-рейтом."""

        def write(self, data: bytes) -> int:
            super().write(data)
            self._rx += _cmd_response(CMD_CAN_SPEED, 0, bytes(data[3:5]))
            return len(data)

    port = _EchoPort(b"")
    manager = _manager_with_port(port)

    assert manager.set_can_speed(1, 500) == 500
    assert manager.set_can_speed(2, 250) == 250
    assert port.writes[0] == bytes([CMD_CAN_SPEED, 3, 1, 0xF4, 0x01])
    assert port.writes[1] == bytes([CMD_CAN_SPEED, 3, 2, 0xFA, 0x00])


def test_set_can_speed_rejects_unsupported_baud() -> None:
    """Скорость вне таблицы бит-тайминга отклоняется на хосте — команда
    на устройство не уходит вообще."""
    port = _ScriptedPort(b"")
    manager = _manager_with_port(port)

    with pytest.raises(ValueError, match="Неподдерживаемая скорость"):
        manager.set_can_speed(1, 33)
    with pytest.raises(ValueError, match="Неподдерживаемая скорость"):
        manager.set_can_speed(2, 800)
    assert port.writes == []


def test_set_can_speed_all_supported_rates() -> None:
    """Каждый бод-рейт из таблицы bxCAN принимается без ошибки."""

    class _EchoPort(_ScriptedPort):
        def write(self, data: bytes) -> int:
            super().write(data)
            self._rx += _cmd_response(CMD_CAN_SPEED, 0, bytes(data[3:5]))
            return len(data)

    port = _EchoPort(b"")
    manager = _manager_with_port(port)
    for kbps in SerialManager.SUPPORTED_CAN_BAUD_KBPS:
        assert manager.set_can_speed(1, kbps) == kbps
    assert len(port.writes) == len(SerialManager.SUPPORTED_CAN_BAUD_KBPS)
