"""Тесты core/steering_decoder — декодер режима «Градусы»
по кратности сырого CAN-значения (CM-F05, семантика CM-N2).

Формат конфигурации v3: ДВЕ независимые таблицы знаков, по десять
чётных делителей 2..20 в каждой; строка — «Кратно»/«Не кратно» или
не участвует; выбранные требования внутри таблицы объединяются
по И (AND). Совпадение обеих таблиц — «conflict», ни одной —
«no-class»; делителей нет вовсе — «free» (класс не ограничивает).
Нейтраль — нулевая точка таблицы привязки (передаётся в points и
имеет приоритет перед классификацией); масштаб — кусочно-линейные
ветки сторон от нуля; «Инвертирование» — один знак к результату.
Легаси-поля v2 (neutral/calib/left_is_negative/interpolate)
мигрируются нормализацией в points/inverted.

Фикстура: «+» = {кратно 4 И не кратно 8}; «−» = {кратно 8}
(пример исходного задания): 4/12/20 → «+», 8/16/24 → «−»,
значения вроде 2/6/10 — вне классов. Точки привязки:
(0,0) — нейтраль; ±-опорные наклоном 2.5°/ед.: (4,±10) (8,±20)
(12,±30) (16,±40).
"""

from __future__ import annotations

from core.steering_decoder import (
    DIVISORS,
    STATUS_CONFLICT,
    STATUS_DISABLED,
    STATUS_INCOMPLETE,
    STATUS_NEUTRAL,
    STATUS_NO_CLASS,
    STATUS_NO_DATA,
    STATUS_NO_POINTS,
    STATUS_OK,
    _piecewise,
    classify,
    decode,
    decode_raw,
    extract_raw,
    migration_points,
    normalize_config,
    split_points,
    unit_suffix,
)


def _cfg(**kw) -> dict:
    cfg = {
        "enabled": True,
        "version": 3,
        "signs": {
            "pos": {4: "div", 8: "not"},
            "neg": {8: "div"},
        },
        "inverted": False,
    }
    cfg.update(kw)
    return cfg


def _points() -> list[list[float]]:
    """Ветки наклоном 2.5°/ед. от нейтрали 0.

    Коды — только своего класса (один и тот же сырой не может быть
    опорой обеих сторон): «+» = {4,12,20,28} (кратно 4, не кратно 8),
    «−» = {8,16,24} (кратно 8)."""
    out = [[0.0, 0.0]]
    for x in (4, 12, 20, 28):
        out.append([float(x), x * 2.5])
    for x in (8, 16, 24):
        out.append([float(x), -x * 2.5])
    return out


def _cfg_v2(**kw) -> dict:
    """Легаси-конфиг v2 для проверок миграции."""
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
        assert conf["points"] == []
        assert conf["inverted"] is False
        assert conf["unit"] == "none"
        assert conf["version"] == 3

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
        assert conf["version"] == 3

    def test_legacy_v2_left_is_negative_to_inverted(self) -> None:
        # «−» → отрицательные градусы (умолчание v2) = НЕинверсия.
        assert normalize_config(
            _cfg_v2(left_is_negative=True)
        )["inverted"] is False
        assert normalize_config(
            _cfg_v2(left_is_negative=False)
        )["inverted"] is True

    def test_explicit_inverted_wins(self) -> None:
        conf = normalize_config(
            _cfg_v2(left_is_negative=True, inverted=True)
        )
        assert conf["inverted"] is True

    def test_legacy_calib_neutral_to_points(self) -> None:
        conf = normalize_config(_cfg_v2())
        pts = {(x, y) for x, y in conf["points"]}
        assert (0.0, 0.0) in pts            # neutral → нулевая точка
        assert (4.0, 10.0) in pts           # |raw|→|deg| → обе стороны
        assert (4.0, -10.0) in pts
        assert (16.0, 40.0) in pts
        assert (16.0, -40.0) in pts

    def test_explicit_points_beat_synth(self) -> None:
        conf = normalize_config(_cfg_v2(points=[[4, 99.0]]))
        assert (4.0, 99.0) in {(x, y) for x, y in conf["points"]}

    def test_unit_normalized(self) -> None:
        assert normalize_config({"unit": "celsius"})["unit"] == "celsius"
        assert normalize_config({"unit": "°F"})["unit"] == "fahrenheit"
        assert normalize_config({"unit": "junk"})["unit"] == "none"

    def test_disabled(self) -> None:
        res = decode_raw(10, {"enabled": False})
        assert res["status"] == STATUS_DISABLED
        assert res["valid"] is False


class TestMigrationPoints:
    def test_neutral_code_becomes_zero(self) -> None:
        pts = migration_points(_cfg_v2(neutral=0x7FF))
        assert [float(0x7FF), 0.0] in pts

    def test_no_legacy_no_points(self) -> None:
        assert migration_points({"enabled": True}) == []


class TestSplitPoints:
    def test_sides(self) -> None:
        sides = split_points(_points())
        assert sides["zero"] == [0.0, 0.0]
        assert (8.0, -20.0) in sides["neg"]
        assert (16.0, -40.0) in sides["neg"]
        assert (12.0, 30.0) in sides["pos"]

    def test_no_zero(self) -> None:
        sides = split_points([[4, -10], [8, 20]])
        assert sides["zero"] is None


class TestClassify:
    def test_neutral_exact_match(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(0, conf, zero_raw=0) == STATUS_NEUTRAL

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

    def test_no_rules_is_free(self) -> None:
        # Делителей нет — класс не ограничивает («Градусы» без
        # кратности, CM-F05: вся таблица — одна карта).
        conf = normalize_config(_cfg(signs={"pos": {}, "neg": {}}))
        assert classify(4, conf) == "free"

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
        # 0 кратно любому делителю — но нулевая точка имеет
        # приоритет (F06 п.7).
        conf = normalize_config(_cfg(
            signs={"pos": {2: "div"}, "neg": {2: "div"}}
        ))
        assert classify(0, conf, zero_raw=0) == STATUS_NEUTRAL

    def test_custom_neutral_code(self) -> None:
        conf = normalize_config(_cfg())
        assert classify(0x7FF, conf, zero_raw=0x7FF) == STATUS_NEUTRAL
        assert classify(0x7FD, conf, zero_raw=0x7FF) == STATUS_NO_CLASS


class TestPiecewise:
    """Кусочно-линейный контракт ветки (F07 п.9): опорные точки
    отсортированы по сырому, внутри — интерполяция, за крайними —
    экстраполяция последним отрезком."""

    def test_exact_and_interp(self) -> None:
        branch = [(0.0, 0.0), (8.0, 20.0), (12.0, 30.0)]
        assert _piecewise(branch, 8.0) == 20.0
        assert _piecewise(branch, 10.0) == 25.0

    def test_extrapolation_both_ends(self) -> None:
        branch = [(0.0, 0.0), (8.0, 20.0), (16.0, 40.0)]
        # За последней точкой — наклон крайнего отрезка.
        assert _piecewise(branch, 20.0) == 50.0
        assert _piecewise(branch, -4.0) == -10.0

    def test_descending_raw_branch(self) -> None:
        # Сырой код убывает с ростом угла — порядок сортировки по x
        # держит линейность независимо от направления кода.
        branch = [(0x20, 0.0), (0x10, -20.0), (0x08, -40.0)]
        assert _piecewise(sorted(branch), 0x10) == -20.0
        assert _piecewise(sorted(branch), 0x18) == -10.0

    def test_single_anchor_constant(self) -> None:
        assert _piecewise([(5.0, 7.0)], 100.0) == 7.0


class TestDecodeRaw:
    def test_ok_pos(self) -> None:
        res = decode_raw(12, _cfg(), points=_points())
        assert res["valid"] and res["status"] == STATUS_OK
        assert res["angle"] == 30.0 and res["cls"] == "pos"
        assert res["raw_hex"] == "0xC"

    def test_neg_branch_value(self) -> None:
        # 16 → «−»; ветка (0,0)…(16,−40) → −40°.
        res = decode_raw(16, _cfg(), points=_points())
        assert res["valid"] and res["cls"] == "neg"
        assert res["angle"] == -40.0

    def test_inversion_flips_once(self) -> None:
        # «Инвертирование» меняет знак величины ровно один раз —
        # класс и исходные байты не трогаем (F05 п.5, F06 п.10).
        res = decode_raw(16, _cfg(inverted=True), points=_points())
        assert res["valid"] and res["cls"] == "neg"
        assert res["angle"] == 40.0
        res_pos = decode_raw(12, _cfg(inverted=True), points=_points())
        assert res_pos["angle"] == -30.0

    def test_neutral_zero_degrees(self) -> None:
        res = decode_raw(0, _cfg(), points=_points())
        assert res["valid"] and res["angle"] == 0.0
        assert res["status"] == STATUS_NEUTRAL

    def test_zero_point_priority_over_class(self) -> None:
        # Нулевая точка кратна делителям обеих сторон — приоритет
        # нейтрали перед конфликтом (F06 п.7).
        res = decode_raw(
            8,
            _cfg(signs={"pos": {8: "div"}, "neg": {8: "div"}}),
            points=[[8, 0], [4, 10], [4, -10]],
        )
        assert res["valid"] and res["status"] == STATUS_NEUTRAL

    def test_no_class_invalid(self) -> None:
        res = decode_raw(10, _cfg(), points=_points())
        assert not res["valid"] and res["status"] == STATUS_NO_CLASS

    def test_conflict_invalid(self) -> None:
        res = decode_raw(8, _cfg(
            signs={"pos": {4: "div"}, "neg": {4: "div"}}
        ), points=_points())
        assert not res["valid"] and res["status"] == STATUS_CONFLICT

    def test_no_data(self) -> None:
        res = decode_raw(None, _cfg(), points=_points())
        assert res["status"] == STATUS_NO_DATA

    def test_no_points_side(self) -> None:
        # Класс найден, но опорных точек его стороны нет.
        res = decode_raw(
            12, _cfg(),
            points=[[0, 0], [4, -10], [8, -20]],
        )
        assert res["status"] == STATUS_NO_POINTS
        assert not res["valid"]

    def test_free_mode_all_points(self) -> None:
        # Без делителей — вся таблица одной кусочно-линейной картой.
        res = decode_raw(
            6, _cfg(signs={"pos": {}, "neg": {}}),
            points=[[0, 0], [4, -10], [8, 20]],
        )
        assert res["valid"] and res["cls"] == "free"
        assert res["angle"] == 5.0   # x=6: −10 + 30·(6−4)/(8−4)

    def test_three_point_branches(self) -> None:
        # F07 п.9: трёхточечный случай — две ветви от нейтрали к
        # опорным точкам, без перекрёстной интерполяции классов.
        signs = {"pos": {4: "div", 8: "not"}, "neg": {8: "div"}}
        pts = [[0x32, 0], [0x64, 30], [0x30, -30]]
        # 0x64=100: %4==0, %8!=0 → «+»; 0x30=48: %8==0 → «−».
        res_p = decode_raw(0x64, _cfg(signs=signs), points=pts)
        assert res_p["valid"] and res_p["angle"] == 30.0
        res_n = decode_raw(0x30, _cfg(signs=signs), points=pts)
        assert res_n["cls"] == "neg" and res_n["angle"] == -30.0

    def test_points_in_cfg(self) -> None:
        # Точки могут лежать прямо в конфиге декодера (нормализация).
        res = decode_raw(12, _cfg(points=_points()))
        assert res["valid"] and res["angle"] == 30.0


class TestExtractAndDecodeFrame:
    def _frame_cfg(self, **kw) -> dict:
        cfg = _cfg(points=_points())
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
    """Политика устаревания значения (stale_ms) — мигрированная
    настройка декодера для условий ГЛ (CM-N3, CM-F05 п.6:
    поле удалено из диалога, значение сохранено)."""

    def test_stale_ms_default_zero(self) -> None:
        conf = normalize_config(_cfg())
        assert conf["stale_ms"] == 0

    def test_stale_ms_normalized(self) -> None:
        conf = normalize_config(_cfg(stale_ms=250))
        assert conf["stale_ms"] == 250

    def test_stale_ms_garbage_and_negative(self) -> None:
        assert normalize_config(_cfg(stale_ms="x"))["stale_ms"] == 0
        assert normalize_config(_cfg(stale_ms=-5))["stale_ms"] == 0


class TestUnits:
    """«Температура» — семантическая метка единицы, не конвертация
    (CM-F07): суффикс в подписях, число не меняется."""

    def test_suffixes(self) -> None:
        assert unit_suffix("none") == "°"
        assert unit_suffix("celsius") == "°C"
        assert unit_suffix("fahrenheit") == "°F"
        assert unit_suffix("junk") == "°"

    def test_unit_does_not_change_value(self) -> None:
        for unit in ("none", "celsius", "fahrenheit"):
            res = decode_raw(12, _cfg(unit=unit), points=_points())
            assert res["angle"] == 30.0
