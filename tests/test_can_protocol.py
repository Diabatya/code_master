"""Тесты парсера CAN-кадров core/can_protocol.py.

Регрессия: в unpack_can_frame/parse_all_frames длина заголовка была на 1 байт
больше фактической (4 + id_length вместо 3 + id_length), из-за чего кадр,
лежащий в буфере точно по границе, считался неполным, а при догоняющих байтах
захватывал чужой байт и падал по checksum — приём в приложении не работал.
"""

from core.can_protocol import (
    MARKER_RX,
    parse_all_frames,
    pack_can_frame,
    unpack_can_frame,
    xor_checksum,
)


def _rx_frame(channel: int, can_id: int, data: bytes) -> bytes:
    """Собирает кадр МК→ПК так же, как firmware send_can_frame()."""
    frame = bytes([MARKER_RX, channel, can_id & 0xFF, (can_id >> 8) & 0xFF, len(data)]) + data
    return frame + bytes([xor_checksum(frame)])


def test_unpack_tx_frame_exact_buffer() -> None:
    frame = pack_can_frame(1, 0x123, b"\x01\x02\x03")
    parsed = unpack_can_frame(frame, tx=True)
    assert parsed is not None
    assert parsed["channel"] == 1
    assert parsed["id"] == 0x123
    assert parsed["data"] == b"\x01\x02\x03"
    assert parsed["dlc"] == 3
    assert parsed["raw"] == frame


def test_unpack_rx_frame_exact_buffer() -> None:
    frame = _rx_frame(2, 0x456, b"\xAB\xCD")
    parsed = unpack_can_frame(frame)
    assert parsed is not None
    assert parsed["channel"] == 2
    assert parsed["id"] == 0x456
    assert parsed["data"] == b"\xAB\xCD"
    assert parsed["raw"] == frame


def test_parse_frames_amid_service_traffic() -> None:
    frame = _rx_frame(1, 0x100, b"\x11\x22")
    chunk = bytes([0xC7, 0x01, 0x01]) + frame + bytes([0xCC])
    frames, leftover = parse_all_frames(chunk)
    assert len(frames) == 1
    assert frames[0]["id"] == 0x100
    assert leftover == b""


def test_parse_frame_split_across_chunks() -> None:
    frame = _rx_frame(1, 0x321, b"\xAA")
    first, leftover = parse_all_frames(frame[:4])
    assert first == []
    frames, leftover = parse_all_frames(leftover + frame[4:])
    assert len(frames) == 1
    assert frames[0]["id"] == 0x321
    assert leftover == b""


def test_parse_tx_direction_separately() -> None:
    tx = pack_can_frame(2, 0x700, b"")
    rx = _rx_frame(1, 0x123, b"")
    tx_frames, _ = parse_all_frames(tx + rx, tx=True)
    rx_frames, _ = parse_all_frames(tx + rx)
    assert len(tx_frames) == 1 and tx_frames[0]["id"] == 0x700
    assert len(rx_frames) == 1 and rx_frames[0]["id"] == 0x123


def test_rtr_frame_has_no_data() -> None:
    tx = pack_can_frame(1, 0x123, b"", rtr=True, dlc=4)
    parsed = unpack_can_frame(tx, tx=True)
    assert parsed is not None
    assert parsed["rtr"] is True
    assert parsed["dlc"] == 4
    assert parsed["data"] == b""


def test_bad_checksum_skips_marker_and_resyncs() -> None:
    bad = _rx_frame(1, 0x100, b"\x00")[:-1] + b"\xFF"  # битая checksum
    good = _rx_frame(1, 0x200, b"\x01")
    frames, leftover = parse_all_frames(bad + good)
    assert len(frames) == 1
    assert frames[0]["id"] == 0x200
