"""Тесты SerialManager.read_event_log/read_full_event_log: разбор ответа
CMD_EVENT_LOG (0xCF) и постраничная сборка полного журнала."""

from core.can_protocol import CMD_EVENT_LOG
from tests.test_request_control import _ScriptedPort, _cmd_response, _manager_with_port


def _record(seq: int, ts_ms: int, etype: int, channel: int, code: int) -> bytes:
    return (
        seq.to_bytes(4, "little")
        + ts_ms.to_bytes(4, "little")
        + bytes((etype & 0xFF, channel & 0xFF, code & 0xFF))
    )


def test_read_event_log_parses_entries() -> None:
    payload = bytes((2,)) + _record(1, 100, 1, 0, 0x08) + _record(2, 250, 3, 1, 0)
    port = _ScriptedPort(_cmd_response(CMD_EVENT_LOG, 0, payload))
    manager = _manager_with_port(port)

    entries = manager.read_event_log(after_seq=0, max_count=23)

    assert entries == [
        {"seq": 1, "timestamp_ms": 100, "type": 1, "channel": 0, "code": 0x08},
        {"seq": 2, "timestamp_ms": 250, "type": 3, "channel": 1, "code": 0},
    ]
    assert port.writes == [bytes([CMD_EVENT_LOG, 5, 0, 0, 0, 0, 23])]


def test_read_full_event_log_paginates_until_short_batch() -> None:
    """Первая страница возвращает ровно 23 записи (полная), значит есть
    ещё — вторая уходит с after_seq=23 и возвращает меньше 23 — конец."""

    class _PagedPort(_ScriptedPort):
        def __init__(self) -> None:
            super().__init__(b"")
            self._call = 0

        def write(self, data: bytes) -> int:
            super().write(data)
            after_seq = int.from_bytes(data[2:6], "little")
            if self._call == 0:
                records = b"".join(_record(after_seq + i + 1, i, 2, 0, 0) for i in range(23))
                self._rx += _cmd_response(CMD_EVENT_LOG, 0, bytes((23,)) + records)
            else:
                records = _record(after_seq + 1, 999, 7, 0xFF, 5)
                self._rx += _cmd_response(CMD_EVENT_LOG, 0, bytes((1,)) + records)
            self._call += 1
            return len(data)

    port = _PagedPort()
    manager = _manager_with_port(port)

    entries = manager.read_full_event_log()

    assert len(entries) == 24
    assert [e["seq"] for e in entries] == list(range(1, 25))
    assert len(port.writes) == 2
    assert int.from_bytes(port.writes[0][2:6], "little") == 0
    assert int.from_bytes(port.writes[1][2:6], "little") == 23


def test_read_full_event_log_stops_on_empty_batch() -> None:
    port = _ScriptedPort(_cmd_response(CMD_EVENT_LOG, 0, bytes((0,))))
    manager = _manager_with_port(port)

    entries = manager.read_full_event_log()

    assert entries == []
