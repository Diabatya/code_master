"""Host-side trigger_t wire-format tests."""

from core.trigger_protocol import TRIGGER_SIZE, pack_trigger, unpack_trigger


def test_trigger_pack_round_trip() -> None:
    payload = pack_trigger({
        "enabled": 1,
        "rx_channel": 1,
        "rx_extended": 1,
        "rx_id": 0x1234567,
        "rx_id_mask": 0x1FFFFFFF,
        "rx_dlc": 2,
        "rx_data": bytes((0x12, 0x34)),
        "rx_data_mask": bytes((0xFF, 0xFF)),
        "tx_channel": 1,
        "tx_extended": 1,
        "tx_id": 0x123456,
        "tx_dlc": 2,
        "tx_data": bytes((0xAA, 0x55)),
        "delay_ms": 25,
    })
    assert len(payload) == TRIGGER_SIZE
    decoded = unpack_trigger(payload)
    assert decoded["enabled"] == 1
    assert decoded["rx_id"] == 0x1234567
    assert decoded["tx_id"] == 0x123456
    assert decoded["delay_ms"] == 25
