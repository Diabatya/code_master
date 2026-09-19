"""Тесты request_control: CAN-кадры в потоке не должны ломать ответы на
команды и не должны теряться (регрессия «Таймаут ответа на команду 0xCA»).
"""

import pytest

from core.can_protocol import (
    CMD_CFG_READ,
    MARKER_RX,
    xor_checksum,
)
from core.serial_manager import SerialManager


class _ScriptedPort:
    """Минимальный порт со «сценарным» RX-потоком для request_control."""

    def __init__(self, rx_stream: bytes, pre_buffered: bytes = b"") -> None:
        # _pre — байты, уже лежащие в буфере драйвера (приехали, пока
        # reader был остановлен): их видит in_waiting, и команда должна
        # дочитать их перед отправкой, не потеряв CAN-кадры.
        self._pre = bytearray(pre_buffered)
        self._rx = bytearray(rx_stream)
        self.writes = []

    def is_open(self) -> bool:
        return True

    def reset_input_buffer(self) -> None:
        # НЕ очищаем сценарный поток — байты уже «прилетели» от МК.
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        # CDC Full-Speed идёт пакетами до 64 Б — отдаём не больше за вызов,
        # как на реальном линке (иначе один read() съедает «будущие» байты).
        size = min(size, 64)
        src = self._pre if self._pre else self._rx
        chunk = bytes(src[:size])
        del src[:size]
        return chunk

    @property
    def in_waiting(self) -> int:
        # usbser.sys-семантика: «в буфере» только то, что уже приехало —
        # основной поток устройство отдаёт позже, при read().
        return len(self._pre)


def _rx_frame(channel: int, can_id: int, data: bytes) -> bytes:
    """CAN-кадр МК→ПК (standard) с корректной контрольной суммой."""
    frame = bytes([MARKER_RX, channel & 0xFF, can_id & 0xFF, (can_id >> 8) & 0xFF, len(data)])
    frame += data
    return frame + bytes([xor_checksum(frame)])


def _cmd_response(command: int, status: int, payload: bytes = b"") -> bytes:
    return bytes([(command | 0x10) & 0xFF, status, len(payload)]) + payload


def _manager_with_port(port: _ScriptedPort) -> SerialManager:
    manager = SerialManager()
    manager._port = port
    # Без reader-потока — он тут только мешает чтению сценарного буфера.
    manager._stop_reader = lambda: None  # type: ignore[method-assign]
    manager._start_reader = lambda: None  # type: ignore[method-assign]
    return manager


def test_request_control_passes_can_frames_through() -> None:
    """CAN-кадр до ответа: парсится, эмитится в new_can_frame, не ломает ответ."""
    payload = bytes([4]) + b"TEST" + bytes([3]) + b"123" + bytes((0x83, 0x04, 0x40, 0x57))
    frame = _rx_frame(1, 0x123, b"\xD0\x11\x22\x33")  # 0xD0 = маркер ответа C0 в данных!
    port = _ScriptedPort(frame + _cmd_response(CMD_CFG_READ, 0, payload))
    manager = _manager_with_port(port)

    received = []
    manager.new_can_frame.connect(received.append)

    result = manager.request_control(CMD_CFG_READ, b"")
    assert result == payload
    assert len(received) == 1
    assert received[0]["id"] == 0x123
    assert received[0]["data"] == b"\xD0\x11\x22\x33"


def test_request_control_ignores_marker_inside_can_data() -> None:
    """Байт 0xD0 внутри данных CAN-кадра не должен давать ложный ответ."""
    frame = _rx_frame(2, 0x456, b"\xD0\x00\xFF\xAA")
    port = _ScriptedPort(frame + _cmd_response(CMD_CFG_READ, 0, b"\x00\x00\x00"))
    manager = _manager_with_port(port)
    result = manager.request_control(CMD_CFG_READ, b"")
    assert result == b"\x00\x00\x00"


def test_request_control_skips_corrupt_can_frame() -> None:
    """Битый кадр (плохая XOR-сумма) пропускается, ответ находится дальше."""
    frame = bytearray(_rx_frame(1, 0x100, b"\x01\x02"))
    frame[-1] ^= 0xFF  # портим контрольную сумму
    port = _ScriptedPort(bytes(frame) + _cmd_response(CMD_CFG_READ, 0, b"\x07"))
    manager = _manager_with_port(port)
    result = manager.request_control(CMD_CFG_READ, b"")
    assert result == b"\x07"


def test_request_control_status_error() -> None:
    port = _ScriptedPort(_cmd_response(CMD_CFG_READ, 1))
    manager = _manager_with_port(port)
    with pytest.raises(RuntimeError, match="статус 0x01"):
        manager.request_control(CMD_CFG_READ, b"")


def test_request_control_timeout() -> None:
    port = _ScriptedPort(b"")
    manager = _manager_with_port(port)
    with pytest.raises(TimeoutError):
        manager.request_control(CMD_CFG_READ, b"", timeout=0.05)


def test_request_control_burst_under_can_flood() -> None:
    """Серия STAGE (>5 триггеров) под потоком CAN-кадров: каждый ответ
    закрыт лавиной кадров — регрессия «Таймаут ответа на команду 0xCA»."""
    from core.can_protocol import CMD_TRIGGER_STAGE

    stage_cmd = bytes([CMD_TRIGGER_STAGE, 83, 0]) + bytes(82)

    class _FloodPort(_ScriptedPort):
        """Отвечает на каждую команду кадрами + ответом — как МК на
        насыщенной шине: между командами летят CAN-кадры, один с 0xDA
        внутри данных."""

        _n = 0

        def write(self, data: bytes) -> int:
            super().write(data)
            i = self._n
            self._n += 1
            self._rx += _rx_frame(1, 0x100 + i, bytes([i & 0xFF] * 8))
            self._rx += _rx_frame(2, 0x200 + i, b"\xDA\x00\x00")
            self._rx += _cmd_response(CMD_TRIGGER_STAGE, 0)
            return len(data)

    port = _FloodPort(b"")
    manager = _manager_with_port(port)

    with manager.control_session():
        for _ in range(8):
            manager.request_control(CMD_TRIGGER_STAGE, stage_cmd[2:])

    # Каждая команда ушла на устройство ровно один раз (без ретраев-таймаутов).
    assert len(port.writes) == 8
    assert all(w == stage_cmd for w in port.writes)


def test_request_control_preserves_frames_buffered_while_reader_stopped() -> None:
    """Кадр, приехавший в паузе между остановкой reader'а и командой,
    не должен уничтожаться сбросом входного буфера — иначе «RX в
    статистике растёт, а мониторинг молчит» (полевая регрессия)."""
    frame = _rx_frame(1, 0x111, b"\x11" * 8)
    port = _ScriptedPort(
        _cmd_response(CMD_CFG_READ, 0, b"\x01"),
        pre_buffered=frame,
    )
    manager = _manager_with_port(port)

    received = []
    manager.new_can_frame.connect(received.append)

    result = manager.request_control(CMD_CFG_READ, b"")
    assert result == b"\x01"
    assert [f["id"] for f in received] == [0x111]


def test_request_control_preserves_frames_after_response() -> None:
    """Кадр, приехавший в одном USB-буре сразу за ответом на команду,
    тоже не теряется — хвост буфера разбирается перед return."""
    frame = _rx_frame(2, 0x222, b"\x22" * 8)
    port = _ScriptedPort(_cmd_response(CMD_CFG_READ, 0, b"\x01") + frame)
    manager = _manager_with_port(port)

    received = []
    manager.new_can_frame.connect(received.append)

    result = manager.request_control(CMD_CFG_READ, b"")
    assert result == b"\x01"
    assert [f["id"] for f in received] == [0x222]
