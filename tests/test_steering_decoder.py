"""Тесты core/steering_decoder — декодер угла поворота руля
по кратности сырого значения (CM-HW-04).

Класс значения — по делителям 2..20 (независимые «+»/«−» списки);
нейтраль — точный код → 0°; градусы только из калибровки
(точное совпадение или интерполяция внутри диапазона — без
экстраполяции и выдуманных ступеней).

Фикстура: делители «+» = [3], «−» = [4] — непересекающиеся классы
(кратное 3 ≠ кратно 4 и наоборот, кроме кратных 12 → conflict);
калибровка |raw| 0→0°, 12→36°, 24→72° (наклон 3°/ед.).
"""

from __future__ import annotations

from core.steering_decoder import (
    DIVISOR_MAX,
    DIVISOR_MIN,
    STATUS_CONFLICT,
    STATUS_DISABLED,
    STATUS_INCOMPLETE,
    STATUS_NEUTRAL,
    STATUS_NO_CALIBRATION,
    STATUS_NO_CLASS,
    STATUS_NO_DATA,
    STATUS_OK,
    calibrate,
    classify,
    decode,
    decode_raw,
    extract_raw,
    normalize_config,
)


def _cfg(**kw) -> dict:
    cfg = {
        "enabled": True,
        "divisors_pos": [3],
        "divisors_neg": [4],
        "neutral": 0,
        "left_is_negative": True,
        "calib": [(0, 0.0), (12, 36.0), (24, 72.0)],
        "interpolate": True,
    }
    cfg.update(kw)
    return cfg


class TestNormalize:
    def test_defaults_disabled_optin(self) -> None:
        conf = normalize_config(None)
        assert conf["enabled"] is False  # декодер — opt-in
        assert conf["divisors_pos"] == []
        assert conf["divisors_neg"] == []
        assert conf["calib"] == []
        assert conf["interpolate"] is False

    def test_divisors_clamped_to_2_20(self) -> None:
        conf = normalize_config(
            {"enabled": True,
             "divisors_pos": [1, 2, 20, 21, 500],
             "divisors_neg": [0, 4]}
        )
        assert conf["divisors_pos"] == [2, 20]
        assert conf["divisors_neg"] == [4]
        assert DIVISOR_MIN == 2 and DIVISOR_MAX == 20

    def test_disabled(self) -> None:
        res = decode_raw(10, {"enabled": False})
        assert res["status"] == STATUS_DISABLED
        assert res["valid"] is False


class TestClassify:
    def test_neutral_exact_match(self) -> None:
        assert classify(0, normalize_config(_cfg())) == STATUS_NEUTRAL

    def test_pos_family(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(9, conf) == "pos"   # 9 % 3 == 0, 9 % 4 != 0
        assert classify(21, conf) == "pos"

    def test_neg_family(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(8, conf) == "neg"   # 8 % 4 == 0, 8 % 3 != 0
        assert classify(20, conf) == "neg"

    def test_conflict_same_value_in_both(self) -> None:
        conf = normalize_config(_cfg())
        # 24 кратно 3 («+») и 4 («−») одновременно.
        assert classify(24, conf) == STATUS_CONFLICT

    def test_no_class(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(25, conf) == STATUS_NO_CLASS
        assert classify(7, conf) == STATUS_NO_CLASS

    def test_neutral_checked_first(self) -> None:
        # 0 кратно любому делителю — но нейтраль имеет приоритет.
        conf = normalize_config(_cfg(divisors_pos=[2], divisors_neg=[2]))
        assert classify(0, conf) == STATUS_NEUTRAL

    def test_custom_neutral_code(self) -> None:
        conf = normalize_config(_cfg(neutral=0x7FF))
        assert classify(0x7FF, conf) == STATUS_NEUTRAL
        assert classify(0x7FD, conf) == STATUS_NO_CLASS


class TestCalibrate:
    def test_exact_point(self) -> None:
        conf = normalize_config(_cfg())
        assert calibrate(12, conf) == 36.0

    def test_interpolation(self) -> None:
        conf = normalize_config(_cfg())
        # 18 — середина 12..24 → 36 + 36/2.
        assert calibrate(18, conf) == 54.0

    def test_no_extrapolation_beyond_range(self) -> None:
        # За крайними точками калибровки нет — даже при interp.
        conf = normalize_config(_cfg())
        assert calibrate(25, conf) is None
        assert calibrate(100, conf) is None

    def test_no_interpolation_means_exact_only(self) -> None:
        conf = normalize_config(_cfg(interpolate=False))
        assert calibrate(18, conf) is None
        assert calibrate(12, conf) == 36.0

    def test_no_calib_points(self) -> None:
        conf = normalize_config(_cfg(calib=[]))
        assert calibrate(18, conf) is None

    def test_single_point_exact_only(self) -> None:
        conf = normalize_config(_cfg(calib=[(10, 5.0)]))
        assert calibrate(10, conf) == 5.0
        assert calibrate(11, conf) is None  # одна точка — наклона нет

    def test_unsorted_points_normalized(self) -> None:
        conf = normalize_config(
            _cfg(calib=[(24, 72.0), (0, 0.0), (12, 36.0)])
        )
        assert calibrate(18, conf) == 54.0


class TestDecodeRaw:
    def test_ok_pos(self) -> None:
        res = decode_raw(18, _cfg())
        assert res["valid"] and res["status"] == STATUS_OK
        assert res["angle"] == 54.0 and res["cls"] == "pos"
        assert res["raw_hex"] == "0x12"

    def test_neg_class_left_negative(self) -> None:
        res = decode_raw(16, _cfg(left_is_negative=True))
        # 16 % 4 == 0, 16 % 3 != 0 → «−»; |16| → 48° → −48°.
        assert res["valid"] and res["cls"] == "neg"
        assert res["angle"] == -48.0

    def test_neg_class_right_positive(self) -> None:
        res = decode_raw(16, _cfg(left_is_negative=False))
        assert res["cls"] == "neg" and res["angle"] == 48.0

    def test_neutral_zero_degrees(self) -> None:
        res = decode_raw(0, _cfg())
        assert res["valid"] and res["angle"] == 0.0
        assert res["status"] == STATUS_NEUTRAL

    def test_no_class_invalid(self) -> None:
        res = decode_raw(25, _cfg())
        assert not res["valid"] and res["status"] == STATUS_NO_CLASS

    def test_conflict_invalid(self) -> None:
        res = decode_raw(24, _cfg())
        assert not res["valid"] and res["status"] == STATUS_CONFLICT

    def test_no_data(self) -> None:
        res = decode_raw(None, _cfg())
        assert res["status"] == STATUS_NO_DATA

    def test_no_calibration(self) -> None:
        res = decode_raw(18, _cfg(calib=[]))
        assert res["status"] == STATUS_NO_CALIBRATION

    def test_out_of_calib_range(self) -> None:
        # 27 — pos (27%3==0), но за пределом калибровки (24).
        res = decode_raw(27, _cfg())
        assert res["status"] == STATUS_NO_CALIBRATION


class TestExtractAndDecodeFrame:
    def _frame_cfg(self, **kw) -> dict:
        cfg = _cfg()
        cfg.update(kw)
        return cfg

    def test_extract_big_endian(self) -> None:
        conf = normalize_config(
            {"byte_from": 0, "byte_len": 4, "endian": "be"}
        )
        assert extract_raw(b"\x01\x02\x03\x04", conf) == 0x01020304

    def test_extract_little_endian(self) -> None:
        conf = normalize_config(
            {"byte_from": 0, "byte_len": 4, "endian": "le"}
        )
        assert extract_raw(b"\x01\x02\x03\x04", conf) == 0x04030201

    def test_extract_window(self) -> None:
        conf = normalize_config(
            {"byte_from": 1, "byte_len": 2, "endian": "be"}
        )
        assert extract_raw(b"\xAA\x10\x20\xBB", conf) == 0x1020

    def test_extract_incomplete_frame(self) -> None:
        conf = normalize_config(
            {"byte_from": 0, "byte_len": 4, "endian": "be"}
        )
        assert extract_raw(b"\x01\x02", conf) is None

    def test_decode_full_frame_le(self) -> None:
        # raw=18 в байтах 0..1 LE: 0x12 0x00
        res = decode(
            b"\x12\x00",
            self._frame_cfg(byte_from=0, byte_len=2, endian="le"),
        )
        assert res["valid"] and res["angle"] == 54.0

    def test_decode_incomplete_frame(self) -> None:
        res = decode(
            b"\x12",
            self._frame_cfg(byte_from=0, byte_len=2),
        )
        assert res["status"] == STATUS_INCOMPLETE

    def test_decode_no_frame(self) -> None:
        res = decode(None, self._frame_cfg(byte_from=0, byte_len=2))
        assert res["status"] == STATUS_NO_DATA

    def test_leading_zeros(self) -> None:
        res = decode(
            b"\x00\x00\x00\x12",
            self._frame_cfg(byte_from=0, byte_len=4, endian="be"),
        )
        assert res["valid"] and res["angle"] == 54.0

    def test_decode_disabled(self) -> None:
        res = decode(b"\x12\x00", {"enabled": False})
        assert res["status"] == STATUS_DISABLED
