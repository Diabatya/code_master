"""Host-side trigger_t wire-format tests."""

from core.trigger_protocol import (
    TRIGGER_F_MUTE_ECHO,
    TRIGGER_FORMAT_VERSION,
    TRIGGER_FORMAT_VERSION_V2,
    TRIGGER_HEADER_SIZE,
    TRIGGER_MAX_SLOTS,
    TRIGGER_POOL_SIZE,
    TRIGGER_SIZE,
    TRIGGER_SIZE_V2,
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


def test_trigger_max_slots_fit_pool() -> None:
    """70 записей по 90 байт (формат v3) + заголовок помещаются в пул 8 КБ."""
    assert TRIGGER_SLOT_SIZE == TRIGGER_SIZE == 90
    assert TRIGGER_MAX_SLOTS == 70
    assert TRIGGER_HEADER_SIZE + TRIGGER_MAX_SLOTS * TRIGGER_SLOT_SIZE <= TRIGGER_POOL_SIZE


def test_trigger_usage_percent() -> None:
    assert trigger_usage_percent(0) == 0
    # 16 + 10*90 = 916 байт из 8192 → 11%
    assert trigger_usage_percent(10) == round(916 * 100 / 8192)
    assert trigger_usage_percent(70) == 77  # 6316/8192 = 77.1% → 77
    assert trigger_usage_percent(100) == 77  # записи клампятся до 70


def test_trigger_v3_new_fields_round_trip() -> None:
    """Формат v3 (90 Б): fire_limit + «Слушать отправляемое» (MUTE_ECHO)."""
    payload = pack_trigger({
        "enabled": 1,
        "rx_id": 0x111,
        "rx_id_mask": 0x7FF,
        "rx_fire_limit": 5,
        "rx_listen_echo": False,
        "src_fire_limit": 9999,
        "src_listen_echo": False,
    })
    assert len(payload) == TRIGGER_SIZE
    assert payload[49] == TRIGGER_FORMAT_VERSION
    assert payload[50] == TRIGGER_SIZE
    decoded = unpack_trigger(payload)
    assert decoded["rx_fire_limit"] == 5
    assert decoded["rx_listen_echo"] is False
    assert decoded["src_fire_limit"] == 9999
    assert decoded["src_listen_echo"] is False


def test_trigger_v3_flags_wire_bits() -> None:
    """Флаги на проводе: MUTE_ECHO=1 соответствует «слушать выкл»."""
    payload = pack_trigger({"rx_listen_echo": False, "src_listen_echo": True})
    # rx_flags @83, src_flags @86 (см. trigger.h)
    assert payload[83] == TRIGGER_F_MUTE_ECHO
    assert payload[86] == 0
    assert int.from_bytes(payload[81:83], "little") == 0  # rx_fire_limit
    assert int.from_bytes(payload[84:86], "little") == 0  # src_fire_limit


def test_trigger_v2_pack_for_old_firmware() -> None:
    """fmt_version=2 — легаси-запись 82 Б для прошивок protocol<4."""
    payload = pack_trigger({
        "enabled": 1,
        "rx_id": 0x222,
        "rx_fire_limit": 5,  # новые опции в v2 не уходят
        "rx_listen_echo": False,
    }, fmt_version=TRIGGER_FORMAT_VERSION_V2)
    assert len(payload) == TRIGGER_SIZE_V2
    assert payload[49] == TRIGGER_FORMAT_VERSION_V2
    assert payload[50] == TRIGGER_SIZE_V2
    decoded = unpack_trigger(payload)
    assert decoded["rx_id"] == 0x222
    # Распакованная v2-запись отдаёт новые поля с дефолтами «выкл».
    assert decoded["rx_fire_limit"] == 0
    assert decoded["rx_listen_echo"] is True
    assert decoded["src_fire_limit"] == 0
    assert decoded["src_listen_echo"] is True


def test_trigger_unpack_rejects_bad_size() -> None:
    import pytest
    with pytest.raises(ValueError):
        unpack_trigger(b"\x00" * 85)  # ни 82, ни 90


def test_trigger_cache_fields_round_trip() -> None:
    """Поля кэш-режима (формат v2): src-матчер + параметры повторов."""
    payload = pack_trigger({
        "enabled": 1,
        "rx_channel": 0,
        "rx_id": 0x100,
        "rx_id_mask": 0x7FF,
        "tx_channel": 1,
        "cache_enabled": 1,
        "src_channel": 2,
        "src_extended": 1,
        "src_id": 0x1ABCDEF,
        "src_dlc": 4,
        "src_from": b"\x00\x00\x00\x00",
        "src_to": b"\xFF\xFF\xFF\xFF",
        "tx_interval_ms": 50,
        "tx_count": 5,
        "delay_ms": 10,
    })
    decoded = unpack_trigger(payload)
    assert decoded["cache_enabled"] == 1
    assert decoded["src_channel"] == 2
    assert decoded["src_id"] == 0x1ABCDEF
    assert decoded["src_from"] == b"\x00\x00\x00\x00\x00\x00\x00\x00"
    assert decoded["src_to"] == b"\xFF" * 8
    assert decoded["tx_interval_ms"] == 50
    assert decoded["tx_count"] == 5
    assert decoded["delay_ms"] == 10


def test_trigger_rx_rtr_round_trip() -> None:
    """rx_rtr живёт в бывшем байте reserved_pad — размер записи 82 Б."""
    payload = pack_trigger({
        "enabled": 1,
        "rx_id": 0x100,
        "rx_id_mask": 0x7FF,
        "rx_rtr": 1,
        "tx_id": 0x200,
    })
    assert len(payload) == TRIGGER_SIZE
    decoded = unpack_trigger(payload)
    assert decoded["rx_rtr"] == 1
    assert decoded["tx_rtr"] == 0


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
