"""Декодер режима «Градусы» по кратности сырого CAN-значения
(CM-F05; прежний «Угол руля» — CM-HW-04, переработан по CM-N2).

Модель кодирования (типовая для датчиков угла в CAN): сырое целое
значение делится на два класса знака угла «+»/«−» таблицами условий
кратности; нейтраль — нулевая точка таблицы привязки (не обязательно
код 0).

Таблицы знаков — ДВЕ НЕЗАВИСИМЫЕ, по десять строк-делителей
2,4,6,…,20; у каждой строки три состояния: «Кратно», «Не кратно»,
строка не участвует. Требования ВЫБРАННЫХ строк внутри таблицы
объединяются по И (AND): класс совпал, только если выполнены все его
условия. Пример: «−» = {кратно 8}; «+» = {кратно 4 И не кратно 8} →
8/16 отрицательные, 12/20 положительные, 10 — вне классов.
Ненастроенная таблица (без выбранных строк) не совпадает ни с чем;
совпадение обеих таблиц — «conflict», ни одной — «no-class». Если
делители не заданы ВООБЩЕ — проверка класса не применяется, величина
берётся по всем точкам как единой кусочно-линейной карте (режим
«Градусы» без кратности, миграция прежних конфигов).

Масштаб и нейтраль — из ТАБЛИЦЫ ПРИВЯЗКИ переменной (CM-F05 п.3–4,
CM-F07): точки [сырое, величина]; сторона явно определяется знаком
величины: величина <0 — отрицательная опорная, >0 — положительная,
=0 — нулевая точка (нейтраль; её код имеет приоритет перед
классификацией и исключается из проверки кратности — CM-F06 п.7).
Величина — кусочно-линейно по ветке СВОЕЙ стороны: опорные точки
сортируются по сырому коду, между соседними — линейная интерполяция,
за крайними — экстраполяция последним отрезком (контракт CM-F07
п.9: ветки сторон независимы, общей интерполяции через нейтраль
между разными классами нет).

«Инвертирование» (CM-F05 п.5) меняет положительное/отрицательное
направление и применяется РОВНО ОДИН раз — к уже вычисленной
величине (классификация, проверки таблиц и график остаются
прежними; не инвертируются ни исходные байты, ни сами точки —
CM-F06 п.10). Легаси-ключ ``left_is_negative=True`` соответствует
``inverted=False``.

Поле ``stale_ms`` из диалога удалено (CM-F05 п.6): настройка
сохранена в конфиге при миграции (устаревание продолжает работать
как раньше — политика явно перенесена, а не обнулена); у новых
переменных — 0 (не устаревает). Поле «Таймаут» в UI отсутствует.

Формат конфигурации версионируется:
    version        int    — 3 (текущая)
    signs          {"pos": {d: "div"|"not"}, "neg": {…}} — таблицы
    inverted       bool   — инверсия знака результата
    stale_ms       int    — время жизни значения для условий ГЛ
                            (0 = не устаревает, CM-N3)
    unit           "none"|"celsius"|"fahrenheit" — подпись величины
                            °/°C/°F (семантическая метка, не
                            конвертация — CM-F07)

Легаси-поля при нормализации мигрируются и далее не хранятся:
    neutral        int    — код нейтрали → нулевая точка (x,0)
    calib          [(raw_abs, deg_abs)] — калибровка модуля →
                            пары точек (x,+deg) и (x,−deg)
    left_is_negative bool — → inverted = not left_is_negative
    interpolate    bool   — больше не нужна: интерполяция —
                            единственный контракт
    divisors_pos/neg      — списки версии 1 → таблицы «кратно d»

Результат decode()/decode_raw() — dict:
    valid     bool            — значение годное для отображения и
                              условий ГЛ (invalid/conflict/обрыв
                              кадра/нет точек стороны → False)
    angle     float | None    — знаковые градусы (у нейтрали 0.0)
    raw       int | None      — сырое значение
    raw_hex   str             — HEX-представление сырого значения
    cls       "pos"|"neg"|"neutral"|"none"|"conflict"|"free"|None
    status    str             — машинный код: ok / neutral /
                              no-class / conflict / incomplete /
                              no-points / disabled / no-data

Автономное исполнение на МК НЕ поддерживается: условия ГЛ по
переменным остаются PC-исполнением (компилятор их на камень не
отдаёт) — правило, зависящее от декодированного значения, на камень
не компилируется и валидным не считается без ПК.
"""

from __future__ import annotations

from typing import Any

CONFIG_VERSION = 3
# Десять чётных делителей 2..20 — строки таблиц знаков (CM-N2).
DIVISORS = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20)
DIVISOR_MIN = 2
DIVISOR_MAX = 20

STATUS_OK = "ok"
STATUS_NEUTRAL = "neutral"
STATUS_NO_CLASS = "no-class"
STATUS_CONFLICT = "conflict"
STATUS_INCOMPLETE = "incomplete"
STATUS_NO_CALIBRATION = "no-calibration"  # легаси-статус v2
STATUS_NO_POINTS = "no-points"
STATUS_DISABLED = "disabled"
STATUS_NO_DATA = "no-data"

_REQ_DIV = "div"
_REQ_NOT = "not"

UNIT_NONE = "none"
UNIT_CELSIUS = "celsius"
UNIT_FAHRENHEIT = "fahrenheit"
UNIT_SUFFIXES = {
    UNIT_NONE: "°",
    UNIT_CELSIUS: "°C",
    UNIT_FAHRENHEIT: "°F",
}


def unit_suffix(unit: str) -> str:
    """Суффикс величины для контрольных строк и подписей (CM-F07):
    «Не выбрано» → «°», «Цельсий» → «°C», «Фаренгейт» → «°F»."""
    return UNIT_SUFFIXES.get(unit, UNIT_SUFFIXES[UNIT_NONE])


def _norm_unit(value: Any) -> str:
    u = str(value or "").strip().lower()
    if u in (UNIT_CELSIUS, "°c", "c", "цельсий", "cels"):
        return UNIT_CELSIUS
    if u in (UNIT_FAHRENHEIT, "°f", "f", "фаренгейт", "fahr"):
        return UNIT_FAHRENHEIT
    return UNIT_NONE


def _norm_points(points: Any) -> list[list[float]]:
    """Точки привязки [[сырое, величина], …] → список float-пар.

    Мусорные строки выкидываются — повреждённая строка не ломает
    весь декодер. Дубли сырого: последняя точка выигрывает."""
    dedup: dict[float, float] = {}
    for item in points or []:
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        dedup[x] = y
    return [[x, y] for x, y in sorted(dedup.items())]


def _legacy_points(cfg: dict[str, Any]) -> list[list[float]]:
    """Легаси-калибровка/нейтраль → точки привязки (миграция v2→v3).

    ``calib`` задавал |сырое| → |градусы| для обеих сторон: каждая
    точка раскладывается на пару (x,+deg)/(x,−deg); ``neutral`` →
    нулевая точка (x,0) — ровно нулевая опорная новой таблицы."""
    out: list[list[float]] = []
    try:
        neutral = int(cfg.get("neutral", 0) or 0)
    except (TypeError, ValueError):
        neutral = 0
    out.append([float(neutral), 0.0])
    for item in cfg.get("calib") or []:
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        x, y = abs(x), abs(y)
        if x == float(neutral):
            continue  # опорная на коде нейтрали — дубль нулевой
        out.append([x, y])
        if y != 0:
            out.append([x, -y])
    return out


def migration_points(cfg: dict[str, Any] | None) -> list[list[float]]:
    """Точки привязки, синтезированные из легаси-полей декодера
    (``calib``/``neutral``) — для переноса в таблицу привязки
    переменной при открытии/загрузке старого конфига (CM-F05).
    Возвращает [] для конфига без легаси-полей."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not (cfg.get("calib") or "neutral" in cfg):
        return []
    return _legacy_points(cfg)


def _migrate_signs_v1(cfg: dict[str, Any]) -> dict[str, dict[int, str]]:
    """Легаси-списки divisors_pos/divisors_neg → таблицы требований.

    Семантика v1 — ИЛИ по списку; v2+ — И по выбранным строкам,
    поэтому перенос механический (каждый делитель → «кратно»): для
    списков из нескольких делителей результат строже старого
    поведения — честнее «недоклассифицировать», чем продолжать
    широкое ИЛИ (CM-N2)."""
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
    """Конфигурация декодера → нормализованный dict v3 с дефолтами.

    Легаси-поля (neutral/calib/left_is_negative/interpolate,
    divisors_pos/neg) мигрируются: точки масштаба складываются в
    ``points`` (явные точки конфига важнее синтезированных),
    направление — в ``inverted``, списки v1 — в ``signs``. Поле
    ``stale_ms`` сохраняется как было (CM-F05 п.6)."""
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

    # Точки: явные из конфига + синтезированные из легаси-
    # калибровки/нейтрали (без дублей — явные точки важнее).
    points = _norm_points(cfg.get("points"))
    have = {(p[0], p[1]) for p in points}
    for p in _legacy_points(cfg) if (
        cfg.get("calib") or "neutral" in cfg
    ) else []:
        if (p[0], p[1]) not in have:
            points.append(p)
            have.add((p[0], p[1]))
    points.sort()

    # «Инвертирование»: явный ключ важнее; легаси
    # left_is_negative=True (класс «−» → отрицательные градусы) —
    # это НЕинвертированное направление по умолчанию.
    inverted = (
        bool(cfg.get("inverted")) if "inverted" in cfg
        else not bool(cfg.get("left_is_negative", True))
    )
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
        "inverted": inverted,
        "unit": _norm_unit(cfg.get("unit")),
        "stale_ms": max(stale_ms, 0),
        "points": points,
        "byte_from": byte_from,
        "byte_len": byte_len,
        "endian": "be" if cfg.get("endian") == "be" else "le",
    }


def split_points(points: Any) -> dict[str, Any]:
    """Точки привязки → ветки по сторонам (CM-F07):
    ``{"zero": [x,0]|None, "neg": [(x,y)…], "pos": [(x,y)…]}`` —
    ветки отсортированы по сырому коду; сторона — по знаку
    величины (явная принадлежность точки к стороне). Дедупликации
    по коду нет: синтезированные из легаси-калибровки пары (x,±deg)
    законно делят один сырой код."""
    zero: list[float] | None = None
    neg: list[tuple[float, float]] = []
    pos: list[tuple[float, float]] = []
    for item in points or []:
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if y < 0:
            neg.append((x, y))
        elif y > 0:
            pos.append((x, y))
        elif zero is None:
            zero = [x, y]
    neg.sort()
    pos.sort()
    return {"zero": zero, "neg": neg, "pos": pos}


def _piecewise(branch: list[tuple[float, float]], raw: float) -> float:
    """Кусочно-линейная величина по ветке стороны.

    Опорные точки отсортированы по сырому коду; внутри — линейная
    интерполяция между соседними, за крайними — экстраполяция
    последним отрезком (единый контракт CM-F07 п.9). Одна опорная
    точка — постоянная величина."""
    if len(branch) == 1:
        return branch[0][1]
    x = float(raw)
    if x <= branch[0][0]:
        (x0, y0), (x1, y1) = branch[0], branch[1]
    elif x >= branch[-1][0]:
        (x0, y0), (x1, y1) = branch[-2], branch[-1]
    else:
        for a, b in zip(branch, branch[1:], strict=False):
            if a[0] <= x <= b[0]:
                (x0, y0), (x1, y1) = a, b
                break
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


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


def classify(
    raw: int, cfg: dict[str, Any], zero_raw: int | None = None
) -> str:
    """Класс значения по таблицам знаков:
    pos/neg/neutral/free/none/conflict.

    Нулевая точка таблицы привязки (``zero_raw``) проверяется
    ПЕРВОЙ — её код исключён из делителей и всегда даёт нейтраль,
    даже если кратен чему-либо (CM-F06 п.7). Совпадение обеих
    таблиц одновременно — конфликт, значение невалидно (нельзя
    молча выбрать сторону). Делители не заданы вовсе — «free»:
    класс не ограничивает величину."""
    if zero_raw is not None and raw == zero_raw:
        return STATUS_NEUTRAL
    signs = cfg["signs"]
    if not signs["pos"] and not signs["neg"]:
        return "free"
    in_pos = _sign_matches(signs["pos"], raw)
    in_neg = _sign_matches(signs["neg"], raw)
    if in_pos and in_neg:
        return STATUS_CONFLICT
    if in_pos:
        return "pos"
    if in_neg:
        return "neg"
    return STATUS_NO_CLASS


def _result(raw: int | None, conf: dict[str, Any]) -> dict[str, Any]:
    """Общий хвост decode(): класс → ветка стороны → инверсия."""
    raw_hex = f"0x{int(raw):X}" if raw is not None else ""
    sides = split_points(conf["points"])
    zero_raw = int(sides["zero"][0]) if sides["zero"] else None
    cls = classify(int(raw), conf, zero_raw)
    if cls == STATUS_NEUTRAL:
        return {"valid": True, "angle": 0.0, "raw": int(raw),
                "raw_hex": raw_hex, "cls": "neutral",
                "status": STATUS_NEUTRAL}
    if cls in (STATUS_NO_CLASS, STATUS_CONFLICT):
        return {"valid": False, "angle": None, "raw": int(raw),
                "raw_hex": raw_hex, "cls": cls, "status": cls}
    if cls == "free":
        # Без делителей — вся таблица одной кусочно-линейной
        # картой (режим «Градусы» без кратности).
        branch = sorted(sides["neg"] + sides["pos"])
        if sides["zero"]:
            branch.append((float(sides["zero"][0]), 0.0))
            branch.sort()
        if not branch:
            return {"valid": False, "angle": None, "raw": int(raw),
                    "raw_hex": raw_hex, "cls": cls,
                    "status": STATUS_NO_POINTS}
        value = _piecewise(branch, float(raw))
    else:
        # Ветка СВОЕЙ стороны: нулевая точка — общий якорь, опорные
        # — только своего класса (ветки независимы, CM-F07 п.9).
        branch = list(sides[cls])
        if sides["zero"]:
            branch.append((float(sides["zero"][0]), 0.0))
        if len(branch) < (2 if sides["zero"] else 1):
            return {"valid": False, "angle": None, "raw": int(raw),
                    "raw_hex": raw_hex, "cls": cls,
                    "status": STATUS_NO_POINTS}
        branch.sort()
        value = _piecewise(branch, float(raw))
    # Инверсия — ровно один раз, к готовой величине (CM-F05 п.5).
    if conf["inverted"]:
        value = -value
    return {"valid": True, "angle": value, "raw": int(raw),
            "raw_hex": raw_hex, "cls": cls, "status": STATUS_OK}


def decode(data: bytes | None,
           cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Кадр → декодированное значение со статусом качества.

    Невалидные значения и значения без опорных точек возвращают
    valid=False — условия ГЛ по такой переменной не выполняются,
    отображение показывает статус вместо выдуманного числа."""
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


def decode_raw(
    raw: int | None,
    cfg: dict[str, Any] | None,
    points: Any | None = None,
) -> dict[str, Any]:
    """Декодирование по уже собранному сырому значению — для
    переменных, где выбор байтов и порядок (SBLP) настраиваются
    полями DATA самой переменной. ``raw=None`` — кадр не покрыл
    поле (incomplete/no-data).

    ``points`` — точки привязки переменной (нейтраль/масштаб,
    CM-F05): передаются отдельно от конфига декодера, поскольку
    таблица привязки живёт в конфиге переменной, а не декодера;
    при отсутствии используются ``points`` самого конфига."""
    merged = dict(cfg) if isinstance(cfg, dict) else {}
    if points is not None:
        merged["points"] = points
    conf = normalize_config(merged)
    if not conf["enabled"]:
        return {"valid": False, "angle": None, "raw": raw,
                "raw_hex": "", "cls": None, "status": STATUS_DISABLED}
    if raw is None:
        return {"valid": False, "angle": None, "raw": None,
                "raw_hex": "", "cls": None, "status": STATUS_NO_DATA}
    return _result(int(raw), conf)
