"""Декодер угла поворота руля по кратности сырого CAN-значения
(CM-HW-04, переработан по CM-N2).

Модель кодирования (типовая для датчиков угла в CAN): сырое целое
значение делится на два класса знака угла «+»/«−» таблицами условий
кратности; отдельный код — нейтраль (нулевой угол, не обязательно 0).

Таблицы знаков — ДВЕ НЕЗАВИСИМЫЕ, по десять строк-делителей
2,4,6,…,20; у каждой строки три состояния: «Кратно», «Не кратно»,
строка не участвует. Требования ВЫБРАННЫХ строк внутри таблицы
объединяются по И (AND): класс совпал, только если выполнены все его
условия. Пример исходного задания: «−» = {кратно 8}; «+» = {кратно 4
И не кратно 8} → 8/16 отрицательные, 12/20 положительные, 10 — вне
классов. Ненастроенная таблица (без выбранных строк) не совпадает ни
с чем; совпадение обеих таблиц — «conflict», ни одной — «no-class».

Формат конфигурации версионируется:
    version        int    — 2 (текущая); 1/отсутствие — легаси-поля
                            divisors_pos/divisors_neg (списки, раньше
                            объединялись ИЛИ) мигрируются в таблицы
                            «кратно d» по AND — уже НЕ ИЛИ: повторная
                            проверка конфигурации оператором нужна
                            (семантика изменена намеренно, см. CM-N2)
    signs          {"pos": {d: "div"|"not"}, "neg": {…}} — таблицы
    neutral        int    — код нейтрали (может быть ≠ 0)
    byte_from      int    — первый байт значения в кадре (0..7)
    byte_len       int    — длина поля в байтах (1..8)
    endian         "le"|"be" — порядок байт сырого значения
    left_is_negative bool — направление: True — класс «−» → отриц.
                           градусы (влево), класс «+» → положит.;
                           False — наоборот (у разных авто по-разному)
    calib          [(raw_abs, deg_abs)] — калибровка модуля: сырое
                           значение по модулю → модуль угла. Только
                           явные точки; вне таблицы — «нет калибровки»
    interpolate    bool   — разрешить линейную интерполяцию между
                           точками калибровки (иначе — только точное
                           совпадение |raw| с точкой)
    stale_ms       int    — время жизни декодированного значения для
                           условий ГЛ; просроченное значение не
                           удовлетворяет численным условиям
                           (0 = не устаревает, CM-N3)

Результат decode()/decode_raw() — dict:
    valid     bool            — значение годное для отображения и
                              условий ГЛ (invalid/conflict/обрыв
                              кадра/нет калибровки → False)
    angle     float | None    — знаковые градусы (у нейтрали 0.0)
    raw       int | None      — сырое значение
    raw_hex   str             — HEX-представление сырого значения
    cls       "pos"|"neg"|"neutral"|"none"|"conflict"|None
    status    str             — машинный код: ok / neutral /
                              no-class / conflict / incomplete /
                              no-calibration / disabled / no-data

Автономное исполнение на МК НЕ поддерживается: условия ГЛ по
переменным остаются PC-исполнением (компилятор их на камень не
отдаёт) — правило, зависящее от декодированного угла, на камень
не компилируется и валидным не считается без ПК.
"""

from __future__ import annotations

from typing import Any

CONFIG_VERSION = 2
# Десять чётных делителей 2..20 — строки таблиц знаков (CM-N2).
DIVISORS = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20)
DIVISOR_MIN = 2
DIVISOR_MAX = 20

STATUS_OK = "ok"
STATUS_NEUTRAL = "neutral"
STATUS_NO_CLASS = "no-class"
STATUS_CONFLICT = "conflict"
STATUS_INCOMPLETE = "incomplete"
STATUS_NO_CALIBRATION = "no-calibration"
STATUS_DISABLED = "disabled"
STATUS_NO_DATA = "no-data"

_REQ_DIV = "div"
_REQ_NOT = "not"


def _migrate_signs_v1(cfg: dict[str, Any]) -> dict[str, dict[int, str]]:
    """Легаси-списки divisors_pos/divisors_neg → таблицы требований.

    Семантика v1 — ИЛИ по списку; v2 — И по выбранным строкам, поэтому
    перенос механический (каждый делитель → «кратно»): для списков из
    нескольких делителей результат строже старого поведения — честнее
    «недоклассифицировать», чем продолжать широкое ИЛИ (CM-N2)."""
    out: dict[str, dict[int, str]] = {"pos": {}, "neg": {}}
    for sign, key in (("pos", "divisors_pos"), ("neg", "divisors_neg")):
        for v in cfg.get(key) or []:
            try:
                d = int(v)
            except (TypeError, ValueError):
                continue
            if DIVISOR_MIN <= d <= DIVISOR_MAX:
                out[sign][d] = _REQ_DIV
    return out


def normalize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Конфигурация декодера → нормализованный dict с дефолтами.

    Мусорные строки (делитель вне 2..20, неизвестное требование,
    нечисла) выкидываются — повреждённая строка не ломает весь
    декодер. Поля легаси-формата (divisors_pos/neg, version 1)
    мигрируются в signs."""
    cfg = cfg if isinstance(cfg, dict) else {}

    def _signs() -> dict[str, dict[int, str]]:
        raw = cfg.get("signs")
        if isinstance(raw, dict):
            out: dict[str, dict[int, str]] = {"pos": {}, "neg": {}}
            for sign in ("pos", "neg"):
                table = raw.get(sign)
                if not isinstance(table, dict):
                    continue
                for d_key, req in table.items():
                    try:
                        d = int(d_key)
                    except (TypeError, ValueError):
                        continue
                    if d in DIVISORS and req in (_REQ_DIV, _REQ_NOT):
                        out[sign][d] = req
            return out
        return _migrate_signs_v1(cfg)

    def _calib() -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for item in cfg.get("calib") or []:
            try:
                x, y = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            points.append((abs(x), abs(y)))
        # Дубли X: последняя точка выигрывает, сортировка по X.
        dedup: dict[float, float] = {}
        for x, y in points:
            dedup[x] = y
        return sorted(dedup.items())

    try:
        neutral = int(cfg.get("neutral", 0) or 0)
    except (TypeError, ValueError):
        neutral = 0
    try:
        byte_from = int(cfg.get("byte_from", 0) or 0)
        byte_len = int(cfg.get("byte_len", 2) or 0)
    except (TypeError, ValueError):
        byte_from, byte_len = 0, 2
    byte_from = min(max(byte_from, 0), 7)
    byte_len = min(max(byte_len, 1), 8 - byte_from)
    try:
        stale_ms = int(cfg.get("stale_ms", 0) or 0)
    except (TypeError, ValueError):
        stale_ms = 0
    return {
        "enabled": bool(cfg.get("enabled")),
        "version": CONFIG_VERSION,
        "signs": _signs(),
        "neutral": neutral,
        "byte_from": byte_from,
        "byte_len": byte_len,
        "endian": "be" if cfg.get("endian") == "be" else "le",
        "left_is_negative": bool(cfg.get("left_is_negative", True)),
        "calib": _calib(),
        "interpolate": bool(cfg.get("interpolate")),
        "stale_ms": max(stale_ms, 0),
    }


def extract_raw(data: bytes, cfg: dict[str, Any]) -> int | None:
    """Сырое значение из кадра по полю/порядку байт конфига.

    ``None`` — кадр короче заявленного поля («incomplete») или
    данных нет вовсе."""
    if not data:
        return None
    start = cfg["byte_from"]
    end = start + cfg["byte_len"]
    if end > len(data):
        return None
    chunk = bytes(data[start:end])
    # Ведущие нулевые байты в данных — легальны (int() их проглатывает
    # сам): «00 1A» = 0x1A, а не пустое значение.
    return int.from_bytes(
        chunk, "little" if cfg["endian"] == "le" else "big"
    )


def _sign_matches(reqs: dict[int, str], raw: int) -> bool:
    """Все выбранные требования таблицы выполнены (AND).

    Пустая таблица не совпадает ни с чем — иначе ненастроенный знак
    «выигрывал» бы любое значение и классификация превращалась бы в
    «всё — конфликт»."""
    if not reqs:
        return False
    for d, req in reqs.items():
        if req == _REQ_DIV:
            if raw % d != 0:
                return False
        else:  # «не кратно»
            if raw % d == 0:
                return False
    return True


def classify(raw: int, cfg: dict[str, Any]) -> str:
    """Класс значения по таблицам знаков: pos/neg/neutral/none/conflict.

    Нейтраль проверяется первой — её код не участвует в делителях.
    Совпадение обеих таблиц одновременно — конфликт, значение
    невалидно (нельзя молча выбрать сторону)."""
    if raw == cfg["neutral"]:
        return STATUS_NEUTRAL
    signs = cfg["signs"]
    in_pos = _sign_matches(signs["pos"], raw)
    in_neg = _sign_matches(signs["neg"], raw)
    if in_pos and in_neg:
        return STATUS_CONFLICT
    if in_pos:
        return "pos"
    if in_neg:
        return "neg"
    return STATUS_NO_CLASS


def calibrate(abs_raw: int, cfg: dict[str, Any]) -> float | None:
    """Модуль угла по таблице калибровки; ``None`` — «нет калибровки».

    Точное совпадение |raw| с точкой — всегда; между точками —
    только при включённой интерполяции, иначе отказ (выдуманных
    градусов нет). За крайними точками интерполяция НЕ
    экстраполируется — вне диапазона таблицы калибровки нет."""
    points = cfg["calib"]
    if not points:
        return None
    x = float(abs_raw)
    for px, py in points:
        if px == x:
            return py
    if not cfg["interpolate"] or len(points) < 2:
        return None
    if x < points[0][0] or x > points[-1][0]:
        return None
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        if a[0] <= x <= b[0]:
            if b[0] == a[0]:
                return a[1]
            return a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])
    return None


def _result(raw: int | None, conf: dict[str, Any]) -> dict[str, Any]:
    """Общий хвост decode(): класс → калибровка → знаковый угол."""
    raw_hex = f"0x{int(raw):X}" if raw is not None else ""
    cls = classify(int(raw), conf)
    if cls == STATUS_NEUTRAL:
        return {"valid": True, "angle": 0.0, "raw": int(raw),
                "raw_hex": raw_hex, "cls": "neutral",
                "status": STATUS_NEUTRAL}
    if cls in (STATUS_NO_CLASS, STATUS_CONFLICT):
        return {"valid": False, "angle": None, "raw": int(raw),
                "raw_hex": raw_hex, "cls": cls, "status": cls}
    magnitude = calibrate(abs(int(raw)), conf)
    if magnitude is None:
        return {"valid": False, "angle": None, "raw": int(raw),
                "raw_hex": raw_hex, "cls": cls,
                "status": STATUS_NO_CALIBRATION}
    # Направление: класс «−» → отрицательные градусы при
    # left_is_negative, иначе наоборот — ориентация «влево/вправо»
    # у разных авто разная, поэтому выносится в настройку.
    neg_side = -1.0 if conf["left_is_negative"] else 1.0
    angle = magnitude * (neg_side if cls == "neg" else -neg_side)
    return {"valid": True, "angle": angle, "raw": int(raw),
            "raw_hex": raw_hex, "cls": cls, "status": STATUS_OK}


def decode(data: bytes | None,
           cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Кадр → декодированный угол с статусом качества.

    Невалидные и некалиброванные значения возвращают valid=False —
    условия ГЛ по такой переменной не выполняются, отображение
    показывает статус вместо выдуманного числа."""
    conf = normalize_config(cfg)
    if not conf["enabled"]:
        return {"valid": False, "angle": None, "raw": None,
                "raw_hex": "", "cls": None, "status": STATUS_DISABLED}
    raw = extract_raw(data or b"", conf)
    if raw is None:
        status = STATUS_NO_DATA if not data else STATUS_INCOMPLETE
        return {"valid": False, "angle": None, "raw": None,
                "raw_hex": "", "cls": None, "status": status}
    return _result(raw, conf)


def decode_raw(raw: int | None,
               cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Декодирование по уже собранному сырому значению — для
    переменных, где выбор байтов и порядок (SBLP) настраиваются
    полями DATA самой переменной. ``raw=None`` — кадр не покрыл
    поле (incomplete/no-data)."""
    conf = normalize_config(cfg)
    if not conf["enabled"]:
        return {"valid": False, "angle": None, "raw": raw,
                "raw_hex": "", "cls": None, "status": STATUS_DISABLED}
    if raw is None:
        return {"valid": False, "angle": None, "raw": None,
                "raw_hex": "", "cls": None, "status": STATUS_NO_DATA}
    return _result(int(raw), conf)
