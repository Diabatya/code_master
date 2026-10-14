"""Тесты протокола 13: aux-записи в FLXB v2 (CM-HW-03).

Событие «доп. канал включился/выключился» → RX_AUX_EVENT
(rx_channel=1..7, rx_dlc=фронт); условие «доп. канал работает» →
COND_AUX (src_channel/src_dlc); действие «доп. канал» → TX_AUX
(tx_channel/tx_dlc=режим, tx_data=параметры как CMD_AUX_SET_EXT).
"""

from __future__ import annotations

import json

from core.flex_compiler import (
    FLEX_BLOB_VERSION,
    FLEX_BLOB_VERSION_AUX,
    build_flex_blob,
    compile_rule,
    executor_info,
    flexb_header_valid,
    parse_flex_blob,
)
from core.trigger_protocol import (
    pack_trigger,
    record_fields_valid,
    unpack_trigger,
)


def _frame_event(can_id: str = "0x100") -> dict:
    return {
        "type": "frame", "id": can_id, "extended": False,
        "data": "", "channel": 2,
    }


def _aux_rule(**kw) -> dict:
    rule = {
        "active": True,
        "title": "aux",
        "events": [{"type": "aux", "channel": 3, "state": 1}],
        "actions": [
            {"type": "aux", "aux_channel": 5, "aux_mode": "on"}
        ],
    }
    rule.update(kw)
    return rule


class TestAuxCompile:
    def test_aux_event_record_proto13(self) -> None:
        recs = compile_rule(_aux_rule(), allow_aux=True)
        assert recs is not None and len(recs) == 1
        rec = recs[0]
        assert rec["rx_aux_event"] is True
        assert rec["rx_channel"] == 3
        assert rec["rx_dlc"] == 1  # «включился»
        assert rec["tx_aux"] is True
        assert rec["tx_channel"] == 5
        assert rec["tx_dlc"] == 1  # AUX_MODE_ON

    def test_aux_event_off_edge(self) -> None:
        recs = compile_rule(
            _aux_rule(events=[{"type": "aux", "channel": 2, "state": 0}]),
            allow_aux=True,
        )
        assert recs[0]["rx_dlc"] == 2  # «выключился»

    def test_aux_rejected_proto12(self) -> None:
        # Прошивка протокола 12 aux-записей не понимает — на ПК.
        assert compile_rule(_aux_rule(), allow_aux=False) is None
        info = executor_info([_aux_rule()], protocol=12)
        assert info[0]["managed"] is False

    def test_aux_event_invalid_channel(self) -> None:
        bad = _aux_rule(
            events=[{"type": "aux", "channel": 8, "state": 1}]
        )
        assert compile_rule(bad, allow_aux=True) is None

    def test_overlapping_aux_events_rejected(self) -> None:
        # Два одинаковых фронта одного канала — двойная сработка
        # на камне против ИЛИ-брейка ПК → остаётся на ПК.
        rule = _aux_rule(events=[
            {"type": "aux", "channel": 3, "state": 1},
            {"type": "aux", "channel": 3, "state": 1},
        ])
        assert compile_rule(rule, allow_aux=True) is None

    def test_opposite_edges_no_overlap(self) -> None:
        rule = _aux_rule(events=[
            {"type": "aux", "channel": 3, "state": 1},
            {"type": "aux", "channel": 3, "state": 0},
        ])
        recs = compile_rule(rule, allow_aux=True)
        assert recs is not None and len(recs) == 2
        assert {r["rx_dlc"] for r in recs} == {1, 2}


class TestAuxCondition:
    def test_cond_aux_encoded(self) -> None:
        rule = _aux_rule(
            events=[_frame_event()],
            conditions=[{"type": "aux", "channel": 6, "state": 1}],
        )
        recs = compile_rule(rule, allow_aux=True)
        rec = recs[0]
        assert rec["cond_aux"] is True
        assert rec["src_channel"] == 6
        assert rec["src_dlc"] == 1  # «работает»

    def test_cond_aux_state_off(self) -> None:
        rule = _aux_rule(
            events=[_frame_event()],
            conditions=[{"type": "aux", "channel": 6, "state": 0}],
        )
        rec = compile_rule(rule, allow_aux=True)[0]
        assert rec["cond_aux"] is True
        assert rec["src_dlc"] == 2  # «не работает»

    def test_cond_aux_rejected_proto12(self) -> None:
        rule = _aux_rule(
            events=[_frame_event()],
            conditions=[{"type": "aux", "channel": 1, "state": 1}],
        )
        assert compile_rule(rule, allow_aux=False) is None


class TestAuxActionEncoding:
    def test_on_with_duration(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 1, "aux_mode": "on",
            "aux_on_time": 500,
        }])
        rec = compile_rule(rule, allow_aux=True)[0]
        assert rec["tx_dlc"] == 1
        assert int.from_bytes(rec["tx_data"][0:4], "little") == 500

    def test_pulse_params(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 2, "aux_mode": "pulse",
            "aux_pulse_on": 100, "aux_pulse_off": 50,
            "aux_pulse_count": 4,
        }])
        rec = compile_rule(rule, allow_aux=True)[0]
        assert rec["tx_dlc"] == 2
        d = rec["tx_data"]
        assert int.from_bytes(d[0:2], "little") == 100
        assert int.from_bytes(d[2:4], "little") == 50
        assert int.from_bytes(d[4:6], "little") == 4

    def test_pwm_zero_duty_legal(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 3, "aux_mode": "pwm",
            "aux_pwm_freq": 100, "aux_pwm_duty": 0,
        }])
        rec = compile_rule(rule, allow_aux=True)[0]
        assert rec["tx_dlc"] == 3
        assert rec["tx_data"][2] == 0
        assert int.from_bytes(rec["tx_data"][0:2], "little") == 100

    def test_pwm_freq_over_limit_rejected(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 3, "aux_mode": "pwm",
            "aux_pwm_freq": 600, "aux_pwm_duty": 50,
        }])
        assert compile_rule(rule, allow_aux=True) is None

    def test_pwm_duty_over_100_rejected(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 3, "aux_mode": "pwm",
            "aux_pwm_freq": 100, "aux_pwm_duty": 101,
        }])
        assert compile_rule(rule, allow_aux=True) is None

    def test_off_mode(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 7, "aux_mode": "off",
        }])
        rec = compile_rule(rule, allow_aux=True)[0]
        assert rec["tx_dlc"] == 0

    def test_aux_delay_encoded(self) -> None:
        rule = _aux_rule(actions=[{
            "type": "aux", "aux_channel": 3, "aux_mode": "on",
            "aux_delay": 250,
        }])
        assert compile_rule(rule, allow_aux=True)[0]["delay_ms"] == 250


class TestAuxPackUnpack:
    def test_record_roundtrip(self) -> None:
        rec = compile_rule(_aux_rule(), allow_aux=True)[0]
        packed = pack_trigger(rec)
        assert len(packed) == 90
        assert record_fields_valid(packed)
        back = unpack_trigger(packed)
        assert back["rx_aux_event"] is True
        assert back["tx_aux"] is True
        assert back["rx_channel"] == 3 and back["rx_dlc"] == 1
        assert back["tx_channel"] == 5 and back["tx_dlc"] == 1

    def test_cond_flags(self) -> None:
        rule = _aux_rule(
            events=[_frame_event()],
            conditions=[{"type": "aux", "channel": 6, "state": 1}],
        )
        packed = pack_trigger(compile_rule(rule, allow_aux=True)[0])
        back = unpack_trigger(packed)
        assert back["cond_aux"] is True
        assert back["src_channel"] == 6


class TestFlexBlobV2:
    def test_blob_version_2_with_aux(self) -> None:
        blob = build_flex_blob([_aux_rule()], protocol=13)
        assert blob[:4] == b"FLXB"
        assert blob[4] == FLEX_BLOB_VERSION_AUX
        parsed = parse_flex_blob(blob)
        assert parsed is not None
        assert parsed[1] == 1  # одна запись
        assert flexb_header_valid(blob, len(blob))

    def test_blob_version_1_without_aux(self) -> None:
        rule = {
            "active": True, "title": "f",
            "events": [_frame_event()],
            "actions": [{"type": "frame", "id": "0x200",
                         "data": "01 02"}],
        }
        blob = build_flex_blob([rule], protocol=13)
        assert blob[4] == FLEX_BLOB_VERSION

    def test_blob_proto12_no_aux_records(self) -> None:
        # Протокол 12: aux-программа не компилируется — хвост чистый,
        # версия остаётся 1, JSON сохраняет программу для ПК.
        blob = build_flex_blob([_aux_rule()], protocol=12)
        assert blob[4] == FLEX_BLOB_VERSION
        parsed = parse_flex_blob(blob)
        assert parsed is not None
        json_part = json.loads(parsed[0])
        assert json_part["format"] == 2  # разметка каналов новая
        assert len(json_part["rules"]) == 1

    def test_parsed_record_decodes(self) -> None:
        blob = build_flex_blob([_aux_rule()], protocol=13)
        assert parse_flex_blob(blob) is not None
        json_len = int.from_bytes(blob[8:12], "little")
        rec_off = 12 + json_len
        rec = unpack_trigger(blob[rec_off : rec_off + 90])
        assert rec["rx_aux_event"] is True
        assert rec["tx_aux"] is True


class TestExecutorInfoQuota:
    def test_quota_overflow_stays_on_pc(self) -> None:
        # 17 программ по записи — квота 16: последняя на ПК.
        rules = [
            {
                "active": True, "title": f"r{i}",
                "events": [_frame_event(f"0x{0x100 + i:X}")],
                "actions": [{"type": "frame", "id": "0x500",
                             "data": "00"}],
            }
            for i in range(17)
        ]
        info = executor_info(rules, protocol=13)
        managed = [i["managed"] for i in info]
        assert managed.count(True) == 16
        assert managed[-1] is False
        assert "квота" in info[-1]["reason"]
