"""Тесты core/steering_decoder — декодер угла поворота руля
по кратности сырого значения (CM-HW-04, семантика CM-N2).

Формат конфигурации v2: ДВЕ независимые таблицы знаков, по десять
чётных делителей 2..20 в каждой; строка — «Кратно»/«Не кратно» или
не участвует; выбранные требования внутри таблицы объединяются
по И (AND). Совпадение обеих таблиц — «conflict», ни одной —
«no-class». Нейтраль — точный код → 0°; градусы только из
калибровки (точка или интерполяция — без экстраполяции).

Фикстура: «+» = {кратно 4 И не кратно 8}; «−» = {кратно 8}
(пример исходного задания): 4/12/20 → «+», 8/16/24 → «−»,
значения вроде 2/6/10 — вне классов. Калибровка |raw|:
(4,10°) (8,20°) (12,30°) (16,40°) — наклон 2.5°/ед., интерполяция.
"""

from __future__ import annotations

from core.steering_decoder import (
    DIVISORS,
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
        "version": 2,
        "signs": {
            "pos": {4: "div", 8: "not"},
            "neg": {8: "div"},
        },
        "neutral": 0,
        "left_is_negative": True,
        "calib": [(4, 10.0), (8, 20.0), (12, 30.0), (16, 40.0)],
        "interpolate": True,
    }
    cfg.update(kw)
    return cfg


class TestNormalize:
    def test_defaults_disabled_optin(self) -> None:
        conf = normalize_config(None)
        assert conf["enabled"] is False  # декодер — opt-in
        assert conf["signs"] == {"pos": {}, "neg": {}}
        assert conf["calib"] == []
        assert conf["interpolate"] is False
        assert conf["version"] == 2

    def test_signs_filtered_to_even_divisors(self) -> None:
        conf = normalize_config(
            {"enabled": True,
             "signs": {"pos": {2: "div", 3: "div", 22: "div",
                               "x": "div", 4: "bogus"},
                       "neg": {"8": "not"}}}
        )
        # Нечётные/вне диапазона/мусорные требования выкидываются;
        # строковый ключ приводится к int.
        assert conf["signs"]["pos"] == {2: "div"}
        assert conf["signs"]["neg"] == {8: "not"}
        assert list(DIVISORS) == [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]

    def test_legacy_v1_migrates_to_div_tables(self) -> None:
        # v1: списки объединялись ИЛИ → переносим как «кратно» по AND
        # (строже — осознанная смена семантики, CM-N2).
        conf = normalize_config(
            {"enabled": True,
             "divisors_pos": [4, 6],
             "divisors_neg": [8]}
        )
        assert conf["signs"]["pos"] == {4: "div", 6: "div"}
        assert conf["signs"]["neg"] == {8: "div"}
        assert conf["version"] == 2

    def test_disabled(self) -> None:
        res = decode_raw(10, {"enabled": False})
        assert res["status"] == STATUS_DISABLED
        assert res["valid"] is False


class TestClassify:
    def test_neutral_exact_match(self) -> None:
        assert classify(0, normalize_config(_cfg())) == STATUS_NEUTRAL

    def test_pos_and_semantics(self) -> None:
        conf = normalize_config(_cfg())
        # «+» требует кратно 4 И не кратно 8 одновременно.
        assert classify(4, conf) == "pos"
        assert classify(12, conf) == "pos"   # 12%4==0, 12%8!=0
        assert classify(20, conf) == "pos"

    def test_neg_table(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(8, conf) == "neg"
        assert classify(16, conf) == "neg"
        assert classify(24, conf) == "neg"

    def test_and_blocks_pos_when_also_mult_of_8(self) -> None:
        conf = normalize_config(_cfg())
        # 8/16/24 кратны 4, но условие «не кратно 8» проваливается —
        # в «+» не попадают (AND, не ИЛИ).
        for raw in (8, 16, 24):
            assert classify(raw, conf) == "neg"

    def test_not_divisor_requirement(self) -> None:
        conf = normalize_config(_cfg(
            signs={"pos": {6: "not"}, "neg": {}}
        ))
        assert classify(7, conf) == "pos"    # 7 % 6 != 0 → «не кратно»
        assert classify(12, conf) == STATUS_NO_CLASS  # 12 % 6 == 0

    def test_empty_table_matches_nothing(self) -> None:
        conf = normalize_config(_cfg(signs={"pos": {}, "neg": {}}))
        assert classify(4, conf) == STATUS_NO_CLASS

    def test_conflict_same_value_in_both(self) -> None:
        conf = normalize_config(_cfg(
            signs={"pos": {4: "div"}, "neg": {4: "div"}}
        ))
        assert classify(8, conf) == STATUS_CONFLICT
        assert classify(2, conf) == STATUS_NO_CLASS

    def test_no_class(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(2, conf) == STATUS_NO_CLASS
        assert classify(6, conf) == STATUS_NO_CLASS   # 6%4!=0, 6%8!=0
        assert classify(10, conf) == STATUS_NO_CLASS

    def test_neutral_checked_first(self) -> None:
        # 0 кратно любому делителю — но нейтраль имеет приоритет.
        conf = normalize_config(_cfg(
            signs={"pos": {2: "div"}, "neg": {2: "div"}}
        ))
        assert classify(0, conf) == STATUS_NEUTRAL

    def test_custom_neutral_code(self) -> None:
        conf = normalize_config(_cfg(neutral=0x7FF))
        assert classify(0x7FF, conf) == STATUS_NEUTRAL
        assert classify(0x7FD, conf) == STATUS_NO_CLASS


class TestCalibrate:
    def test_exact_point(self) -> None:
        conf = normalize_config(_cfg())
        assert calibrate(8, conf) == 20.0

    def test_interpolation(self) -> None:
        conf = normalize_config(_cfg())
        # 10 — середина 8..12 → 20 + 10/2.
        assert calibrate(10, conf) == 25.0

    def test_no_extrapolation_beyond_range(self) -> None:
        # За крайними точками калибровки нет — даже при interp.
        conf = normalize_config(_cfg())
        assert calibrate(2, conf) is None
        assert calibrate(100, conf) is None

    def test_no_interpolation_means_exact_only(self) -> None:
        conf = normalize_config(_cfg(interpolate=False))
        assert calibrate(10, conf) is None
        assert calibrate(8, conf) == 20.0

    def test_no_calib_points(self) -> None:
        conf = normalize_config(_cfg(calib=[]))
        assert calibrate(10, conf) is None

    def test_single_point_exact_only(self) -> None:
        conf = normalize_config(_cfg(calib=[(10, 5.0)]))
        assert calibrate(10, conf) == 5.0
        assert calibrate(11, conf) is None  # одна точка — наклона нет

    def test_unsorted_points_normalized(self) -> None:
        conf = normalize_config(
            _cfg(calib=[(16, 40.0), (4, 10.0), (8, 20.0), (12, 30.0)])
        )
        assert calibrate(10, conf) == 25.0


class TestDecodeRaw:
    def test_ok_pos(self) -> None:
        res = decode_raw(12, _cfg())
        assert res["valid"] and res["status"] == STATUS_OK
        assert res["angle"] == 30.0 and res["cls"] == "pos"
        assert res["raw_hex"] == "0xC"

    def test_neg_class_left_negative(self) -> None:
        res = decode_raw(16, _cfg(left_is_negative=True))
        # 16 → «−»; |16| → 40° → −40°.
        assert res["valid"] and res["cls"] == "neg"
        assert res["angle"] == -40.0

    def test_neg_class_right_positive(self) -> None:
        res = decode_raw(16, _cfg(left_is_negative=False))
        assert res["cls"] == "neg" and res["angle"] == 40.0

    def test_neutral_zero_degrees(self) -> None:
        res = decode_raw(0, _cfg())
        assert res["valid"] and res["angle"] == 0.0
        assert res["status"] == STATUS_NEUTRAL

    def test_no_class_invalid(self) -> None:
        res = decode_raw(10, _cfg())
        assert not res["valid"] and res["status"] == STATUS_NO_CLASS

    def test_conflict_invalid(self) -> None:
        res = decode_raw(8, _cfg(
            signs={"pos": {4: "div"}, "neg": {4: "div"}}
        ))
        assert not res["valid"] and res["status"] == STATUS_CONFLICT

    def test_no_data(self) -> None:
        res = decode_raw(None, _cfg())
        assert res["status"] == STATUS_NO_DATA

    def test_no_calibration(self) -> None:
        res = decode_raw(12, _cfg(calib=[]))
        assert res["status"] == STATUS_NO_CALIBRATION

    def test_out_of_calib_range(self) -> None:
        # 20 — pos, но за пределом калибровки (16).
        res = decode_raw(20, _cfg())
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
        # raw=12 в байтах 0..1 LE: 0x0C 0x00
        res = decode(
            b"\x0C\x00",
            self._frame_cfg(byte_from=0, byte_len=2, endian="le"),
        )
        assert res["valid"] and res["angle"] == 30.0

    def test_decode_incomplete_frame(self) -> None:
        res = decode(
            b"\x0C",
            self._frame_cfg(byte_from=0, byte_len=2),
        )
        assert res["status"] == STATUS_INCOMPLETE

    def test_decode_no_frame(self) -> None:
        res = decode(None, self._frame_cfg(byte_from=0, byte_len=2))
        assert res["status"] == STATUS_NO_DATA

    def test_leading_zeros(self) -> None:
        res = decode(
            b"\x00\x00\x00\x0C",
            self._frame_cfg(byte_from=0, byte_len=4, endian="be"),
        )
        assert res["valid"] and res["angle"] == 30.0

    def test_decode_disabled(self) -> None:
        res = decode(b"\x0C\x00", {"enabled": False})
        assert res["status"] == STATUS_DISABLED


class TestStalePolicy:
    """Политика устаревания значения (stale_ms) — конфиг декодера
    для условий ГЛ (CM-N3)."""

    def test_stale_ms_default_zero(self) -> None:
        conf = normalize_config(_cfg())
        assert conf["stale_ms"] == 0

    def test_stale_ms_normalized(self) -> None:
        conf = normalize_config(_cfg(stale_ms=250))
        assert conf["stale_ms"] == 250

    def test_stale_ms_garbage_and_negative(self) -> None:
        assert normalize_config(_cfg(stale_ms="x"))["stale_ms"] == 0
        assert normalize_config(_cfg(stale_ms=-5))["stale_ms"] == 0
