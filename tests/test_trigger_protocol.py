"""Host-side trigger_t wire-format tests."""

from core.trigger_protocol import (
    TRIGGER_MAX_SLOTS,
    TRIGGER_PAGE_SIZE,
    TRIGGER_SIZE,
    TRIGGER_SLOT_SIZE,
    count_configured_triggers,
    pack_trigger,
    trigger_usage_percent,
    unpack_trigger,
)


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


def test_trigger_channel_2_both_cans() -> None:
    """Канал 2 = «CAN1 и CAN2» (на приём — любой, на ответ — оба)."""
    payload = pack_trigger({
        "enabled": 1,
        "rx_channel": 2,
        "rx_id": 0x100,
        "rx_id_mask": 0x7FF,
        "tx_channel": 2,
        "tx_id": 0x200,
        "tx_dlc": 8,
        "tx_data": b"\x01\x02\x03\x04\x05\x06\x07\x08",
        "tx_rtr": 1,
    })
    decoded = unpack_trigger(payload)
    assert decoded["rx_channel"] == 2
    assert decoded["tx_channel"] == 2
    assert decoded["tx_rtr"] == 1
    assert decoded["tx_data"] == b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_trigger_max_slots_fit_page() -> None:
    """37 слотов по 54 байта помещаются в страницу 2 КБ (0x0803E000)."""
    assert TRIGGER_SLOT_SIZE == TRIGGER_SIZE == 54
    assert TRIGGER_MAX_SLOTS == 37
    assert TRIGGER_MAX_SLOTS * TRIGGER_SLOT_SIZE <= TRIGGER_PAGE_SIZE


def test_trigger_usage_percent() -> None:
    assert trigger_usage_percent(0) == 0
    assert trigger_usage_percent(10) == round(10 * 54 * 100 / 2048)  # 26%
    assert trigger_usage_percent(37) == 98  # 1998/2048 = 97.6% → 98
    assert trigger_usage_percent(100) == 98  # слоты клампятся до 37


def test_count_configured_triggers() -> None:
    assert count_configured_triggers([]) == 0
    assert count_configured_triggers([{}, {"active": False, "recv_id": ""}]) == 0
    triggers = [
        {"active": True},
        {"active": False, "recv_id": "123"},
        {"active": False, "recv_id": "", "responses": [{"id": "55"}]},
        {"active": False, "recv_id": "", "responses": [{"id": ""}]},
    ]
    assert count_configured_triggers(triggers) == 3
