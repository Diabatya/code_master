"""Тест read_system_info: новое поле stack_free_bytes (offset 64..67,
см. App_GetStackFreeBytes() в firmware/application/Src/main.c) читается
только когда прошивка его прислала, старые (короткие) ответы не ломаются."""

from core.can_protocol import CMD_SYSTEM_INFO
from tests.test_request_control import _ScriptedPort, _cmd_response, _manager_with_port


def _base_payload(extra: bytes = b"") -> bytes:
    payload = bytes(16) + bytes(48) + extra
    return payload


def test_read_system_info_parses_stack_free_bytes() -> None:
    payload = _base_payload() + (0x00001234).to_bytes(4, "little")
    port = _ScriptedPort(_cmd_response(CMD_SYSTEM_INFO, 0, payload))
    manager = _manager_with_port(port)

    info = manager.read_system_info()

    assert info["stack_free_bytes"] == 0x1234


def test_read_system_info_without_stack_field_on_old_firmware() -> None:
    payload = _base_payload()  # ровно 64 байта — старая прошивка
    port = _ScriptedPort(_cmd_response(CMD_SYSTEM_INFO, 0, payload))
    manager = _manager_with_port(port)

    info = manager.read_system_info()

    assert "stack_free_bytes" not in info
