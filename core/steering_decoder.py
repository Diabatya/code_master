"""Декодер угла поворота руля по кратности сырого CAN-значения
(CM-HW-04).

Модель кодирования (типовая для датчиков угла в CAN): сырое целое
значение делится на классы по делителю — значения, кратные одному
набору делителей, означают поворот в одну сторону, кратные другому —
в другую; отдельный код — нейтраль (нулевой угол, не обязательно 0).

Настройка — таблица 19 строк по делителям 2..20: у каждой строки
независимый выбор класса «+» или «−» (взаимоисключающе, третье
состояние — строка не участвует). Требования по классу объединяются
по ИЛИ внутри класса (значение кратное ХОТЯ БЫ одному делителю
класса принадлежит ему), классы между собой — исключающе:
совпадение в обоих — «конфликт», ни в одном — «вне классов».

Конфигурация декодера (dict):
    enabled        bool   — декодер включён
    divisors_pos   [int]  — делители 2..20 класса «+»
    divisors_neg   [int]  — делители 2..20 класса «−»
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

Результат decode() — dict:
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


def normalize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Конфигурация декодера → нормализованный dict с дефолтами.

    Мусорные делители (вне 2..20, дубли, нечисла) выкидываются —
    повреждённая строка не ломает весь декодер."""
    cfg = cfg if isinstance(cfg, dict) else {}

    def _divisors(key: str) -> list[int]:
        out: list[int] = []
        for v in cfg.get(key) or []:
            try:
                d = int(v)
            except (TypeError, ValueError):
                continue
            if DIVISOR_MIN <= d <= DIVISOR_MAX and d not in out:
                out.append(d)
        return sorted(out)

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
    return {
        "enabled": bool(cfg.get("enabled")),
        "divisors_pos": _divisors("divisors_pos"),
        "divisors_neg": _divisors("divisors_neg"),
        "neutral": neutral,
        "byte_from": byte_from,
        "byte_len": byte_len,
        "endian": "be" if cfg.get("endian") == "be" else "le",
        "left_is_negative": bool(cfg.get("left_is_negative", True)),
        "calib": _calib(),
        "interpolate": bool(cfg.get("interpolate")),
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


def classify(raw: int, cfg: dict[str, Any]) -> str:
    """Класс значения по кратности: pos/neg/neutral/none/conflict.

    Нейтраль проверяется первой — её код не участвует в делителях.
    Попадание в оба класса одновременно — конфликт, значение
    невалидно (нельзя молча выбрать сторону)."""
    if raw == cfg["neutral"]:
        return STATUS_NEUTRAL
    in_pos = any(raw % d == 0 for d in cfg["divisors_pos"])
    in_neg = any(raw % d == 0 for d in cfg["divisors_neg"])
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
    raw_hex = "0x{0:0{1}X}".format(raw, conf["byte_len"] * 2)
    cls = classify(raw, conf)
    if cls == STATUS_NEUTRAL:
        return {"valid": True, "angle": 0.0, "raw": raw,
                "raw_hex": raw_hex, "cls": "neutral",
                "status": STATUS_NEUTRAL}
    if cls in (STATUS_NO_CLASS, STATUS_CONFLICT):
        return {"valid": False, "angle": None, "raw": raw,
                "raw_hex": raw_hex, "cls": cls, "status": cls}
    magnitude = calibrate(abs(raw), conf)
    if magnitude is None:
        return {"valid": False, "angle": None, "raw": raw,
                "raw_hex": raw_hex, "cls": cls,
                "status": STATUS_NO_CALIBRATION}
    # Направление: класс «−» → отрицательные градусы при
    # left_is_negative, иначе наоборот — ориентация «влево/вправо»
    # у разных авто разная, поэтому выносится в настройку.
    neg_side = -1.0 if conf["left_is_negative"] else 1.0
    angle = magnitude * (neg_side if cls == "neg" else -neg_side)
    return {"valid": True, "angle": angle, "raw": raw,
            "raw_hex": raw_hex, "cls": cls, "status": STATUS_OK}


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
    raw_hex = f"0x{int(raw):X}"
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
    neg_side = -1.0 if conf["left_is_negative"] else 1.0
    angle = magnitude * (neg_side if cls == "neg" else -neg_side)
    return {"valid": True, "angle": angle, "raw": int(raw),
            "raw_hex": raw_hex, "cls": cls, "status": STATUS_OK}
