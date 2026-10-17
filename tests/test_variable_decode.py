"""Тесты core/variable_decode — единый контракт извлечения RAW и
декодирования численной переменной для отображения и рантайма ГЛ
(CM-N3).

- поле байтов должно быть покрыто кадром целиком — неполное поле
  значения не даёт;
- порядок байтов: сумма (легаси) или SBLP-склейка be/le;
- режим «steering» делегирует декодеру: конфликт/no-class/
  no-calibration → invalid — численные условия не выполняются;
- stale_ms — политика устаревания для steering-переменных.
"""

from __future__ import annotations

from core import variable_decode as vd


def _steering_cfg(**kw) -> dict:
    cfg = {
        "id": "0x100",
        "type": "dynamic",
        "bytes": [0],
        "out_range": "steering",
        "steering": {
            "enabled": True,
            "version": 2,
            "signs": {
                "pos": {4: "div", 8: "not"},
                "neg": {8: "div"},
            },
            "neutral": 0,
            "left_is_negative": True,
            "calib": [(4, 10.0), (8, 20.0), (12, 30.0), (16, 40.0)],
            "interpolate": True,
        },
    }
    cfg.update(kw)
    return cfg


class TestExtractVarRaw:
    def test_sum_bytes(self) -> None:
        raw, complete = vd.extract_var_raw(
            {"bytes": [0, 1]}, b"\x0A\x14"
        )
        assert complete and raw == 30

    def test_sblp_le(self) -> None:
        # «Старший байт справа»: 09 03 → 9 + 3·256 = 777.
        raw, complete = vd.extract_var_raw(
            {"bytes": [0, 1], "sblp": "le"}, b"\x09\x03"
        )
        assert complete and raw == 777

    def test_sblp_be(self) -> None:
        raw, complete = vd.extract_var_raw(
            {"bytes": [0, 1], "sblp": "be"}, b"\x03\x09"
        )
        assert complete and raw == 777

    def test_incomplete_frame_rejected(self) -> None:
        # Байт 7 выбран, кадр DLC=2 — поле неполное: частичную сумму
        # не собираем.
        raw, complete = vd.extract_var_raw(
            {"bytes": [0, 7]}, b"\x09\x03"
        )
        assert not complete and raw is None

    def test_no_field_no_data(self) -> None:
        assert vd.extract_var_raw({"bytes": []}, b"\x01") == (
            None, False
        )
        assert vd.extract_var_raw({"bytes": [0]}, None) == (
            None, False
        )


class TestDecodeVarValue:
    def test_plain_raw(self) -> None:
        res = vd.decode_var_value(
            {"bytes": [0, 1]}, b"\x0A\x14"
        )
        assert res["valid"] and res["raw"] == 30
        assert res["status"] == "ok" and res["raw_hex"] == "0x1E"

    def test_incomplete_is_invalid(self) -> None:
        res = vd.decode_var_value(
            _steering_cfg(bytes=[0, 7]), b"\x0C\x00"
        )
        assert not res["valid"] and res["status"] == "incomplete"

    def test_no_data_is_invalid(self) -> None:
        res = vd.decode_var_value(_steering_cfg(), None)
        assert not res["valid"] and res["status"] == "no-data"

    def test_steering_ok(self) -> None:
        res = vd.decode_var_value(_steering_cfg(), b"\x0C")
        assert res["valid"] and res["value"] == 30.0
        assert res["status"] == "ok"

    def test_steering_conflict_not_numeric(self) -> None:
        # Конфликт таблиц знаков → invalid: конфликтный угол не
        # должен попадать в численный рантайм.
        res = vd.decode_var_value(
            _steering_cfg(steering={
                "enabled": True, "version": 2,
                "signs": {"pos": {4: "div"}, "neg": {4: "div"}},
                "neutral": 0, "calib": [(8, 20.0)],
            }),
            b"\x08",
        )
        assert not res["valid"] and res["status"] == "conflict"
        assert res["value"] is None

    def test_steering_no_points(self) -> None:
        res = vd.decode_var_value(
            _steering_cfg(steering={
                "enabled": True, "version": 2,
                "signs": {"pos": {4: "div"}, "neg": {}},
            }, points=[]),
            b"\x04",
        )
        assert not res["valid"]
        assert res["status"] == "no-points"

    def test_steering_neutral_is_zero(self) -> None:
        res = vd.decode_var_value(_steering_cfg(), b"\x00")
        assert res["valid"] and res["value"] == 0.0


class TestStalePolicy:
    def test_stale_ms_only_steering(self) -> None:
        assert vd.stale_ms({"out_range": "inf"}) == 0
        assert vd.stale_ms(_steering_cfg()) == 0

    def test_stale_ms_from_cfg(self) -> None:
        cfg = _steering_cfg()
        cfg["steering"]["stale_ms"] = 300
        assert vd.stale_ms(cfg) == 300
