"""Тесты PC-стороны AUX-команд протокола 13 (CM-HW-02).

send_aux маршрутизируется по версии протокола: >=13 → CMD_AUX_SET_EXT
(0xDB) с новой нумерацией 1..7; <=12 → легаси CMD_AUX_SET (0xD3) с
переводом в старую разметку физических пинов, без молчаливой
переадресации несуществующих каналов.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.can_protocol import CMD_AUX_GET, CMD_AUX_MAP, CMD_AUX_SET, CMD_AUX_SET_EXT
from core.serial_manager import SerialManager


def _manager(protocol: int) -> SerialManager:
    sm = SerialManager.__new__(SerialManager)
    sm._proto = protocol  # type: ignore[attr-defined]
    sm.device_protocol_version = lambda: sm._proto  # type: ignore[method-assign]
    sm.send_data = MagicMock(return_value=True)  # type: ignore[method-assign]
    sm.request_control = MagicMock(return_value=b"")  # type: ignore[method-assign]
    return sm


def _packet(sm: SerialManager) -> bytes:
    return sm.send_data.call_args[0][0]


class TestSendAuxRouting:
    def test_proto13_uses_ext_same_channel(self) -> None:
        sm = _manager(13)
        assert sm.send_aux(1, 1) is True
        pkt = _packet(sm)
        assert pkt[0] == CMD_AUX_SET_EXT and pkt[1] == 15
        assert pkt[2] == 1 and pkt[3] == 1

    def test_proto12_maps_to_legacy_channels(self) -> None:
        # Новые 3/4/5/2 = физические PC10/PC11/PC12/PA15 = легаси 1/2/3/4.
        sm = _manager(12)
        for new, legacy in ((3, 1), (4, 2), (5, 3), (2, 4)):
            sm.send_data.reset_mock()
            assert sm.send_aux(new, 1) is True
            pkt = _packet(sm)
            assert pkt[0] == CMD_AUX_SET and pkt[2] == legacy

    def test_proto12_rejects_new_only_pins(self) -> None:
        sm = _manager(12)
        for ch in (1, 6, 7):  # PA14/PD2/PB3 не существовали
            sm.send_data.reset_mock()
            assert sm.send_aux(ch, 1) is False
            sm.send_data.assert_not_called()

    def test_proto12_rejects_on_with_duration(self) -> None:
        # «Вкл на время» легаси-контрактом не выражается — отказ.
        sm = _manager(12)
        assert sm.send_aux(3, 1, pwm_time_ms=500) is False
        sm.send_data.assert_not_called()

    def test_proto12_rejects_zero_duty_pwm(self) -> None:
        sm = _manager(12)
        assert sm.send_aux(3, 3, pwm_freq_hz=100, pwm_duty_pct=0) is False

    def test_proto13_zero_duty_legal(self) -> None:
        sm = _manager(13)
        assert sm.send_aux(3, 3, pwm_freq_hz=100, pwm_duty_pct=0) is True
        pkt = _packet(sm)
        assert pkt[11] == 0  # duty

    def test_payload_layout_ext(self) -> None:
        sm = _manager(13)
        sm.send_aux(
            7, 2,
            pulse_on_ms=100, pulse_off_ms=50, pulse_count=4,
            pwm_freq_hz=0, pwm_duty_pct=0, pwm_time_ms=0,
        )
        pkt = _packet(sm)
        assert pkt[0] == CMD_AUX_SET_EXT and pkt[1] == 15
        p = pkt[2:]
        assert p[0] == 7 and p[1] == 2
        assert int.from_bytes(p[2:4], "little") == 100
        assert int.from_bytes(p[4:6], "little") == 50
        assert int.from_bytes(p[6:8], "little") == 4
        assert int.from_bytes(p[8:10], "little") == 0
        assert p[10] == 0
        assert int.from_bytes(p[11:15], "little") == 0


class TestReadAuxState:
    def test_decode_masks_and_modes(self) -> None:
        sm = _manager(13)
        sm.request_control.return_value = bytes(
            [0x61, 0x0A, 0x02, 1, 0, 2, 3, 0, 1, 0]
        )
        state = sm.read_aux_state()
        assert state == {
            "owned": 0x61,   # PA14|PD2|PB3
            "active": 0x0A,
            "level": 0x02,
            "modes": [1, 0, 2, 3, 0, 1, 0],
        }
        sm.request_control.assert_called_once_with(CMD_AUX_GET, b"")

    def test_proto12_returns_none(self) -> None:
        sm = _manager(12)
        assert sm.read_aux_state() is None
        sm.request_control.assert_not_called()

    def test_short_response_none(self) -> None:
        sm = _manager(13)
        sm.request_control.return_value = b"\x01\x02"
        assert sm.read_aux_state() is None

    def test_command_error_none(self) -> None:
        sm = _manager(13)
        sm.request_control.side_effect = RuntimeError("nack")
        assert sm.read_aux_state() is None


class TestSetAuxPinsMask:
    def test_map_returns_effective_mask(self) -> None:
        sm = _manager(13)
        sm.request_control.return_value = bytes([0x61])
        assert sm.set_aux_pins_mask(0x7F) == 0x61
        sm.request_control.assert_called_once_with(
            CMD_AUX_MAP, b"\x7F"
        )

    def test_proto12_returns_none(self) -> None:
        sm = _manager(12)
        assert sm.set_aux_pins_mask(0x61) is None

    def test_empty_response_none(self) -> None:
        sm = _manager(13)
        sm.request_control.return_value = b""
        assert sm.set_aux_pins_mask(0x61) is None
