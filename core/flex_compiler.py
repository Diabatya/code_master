"""Компиляция программ гибкой логики в записи trigger_t — автономное
исполнение ГЛ на STM32 без ПК (протокол 12, отчёт мастера:
«программы ГЛ должны работать без приложения, как триггеры»).

Программа компилируется целиком, когда:
  * все события — «Фрейм» (матч по CAN-кадру) или «Включение
    устройства» (FIRE_ON_BOOT);
  * условий ПРИ нет;
  * все действия — «Фрейм» (отправка CAN-кадра с паузой/кол-вом/
    паузой между отправками);
  * у действий нет «Задержки» выполнения и «Прервать если».

Семантика «X» в DATA действия — байт из кадра события — передаётся
флагом TRIGGER_F_TX_EVENT_DATA (rx_flags бит 5) с маской подстановки
в поле src_from (у скомпилированных записей кэш-фильтр не используется).

Программы с другими событиями/условиями/действиями (переменные,
флаги, доп. каналы, кэш-переменные, команды управления) на камень
не компилируются — их исполняет приложение, как раньше.

Формат контейнера FLXB в полезной нагрузке блоба FLXH:
    [0..3]   b"FLXB"
    [4]      версия контейнера = 1
    [5]      число записей N
    [6..7]   зарезервировано (0)
    [8..11]  длина JSON-конфигурации, u32 LE
    [12..]   JSON (исходная конфигурация для вычитки настройки)
    [12+J..] N записей trigger_t (90 Б с crc8) — исполняемая часть
"""

from __future__ import annotations

import json
from typing import Any

from core import variable_decode
from core.aux_channels import (
    AUX_MAP_VERSION,
    AUX_MAX_DURATION_MS,
    migrate_rules,
)
from core.trigger_protocol import (
    TRIGGER_SIZE,
    pack_trigger,
    record_fields_valid,
)
from models.utils import hex_to_int

FLEX_BLOB_MAGIC = b"FLXB"
FLEX_BLOB_VERSION = 1
# Версия 2: записи с флагами доп. каналов (RX_AUX_EVENT/TX_AUX/
# COND_AUX). Контейнер поднимается до 2 только когда запись реально
# использует aux-флаги — чисто кадровые программы остаются v1 и
# принимаются прошивками протокола 12.
FLEX_BLOB_VERSION_AUX = 2
# Версия 3: автономные переменные — события по динамическим и
# статическим переменным (RX_VAR), псевдо-действие «Переменная»
# (TX_SETFLAG), условие по флагу (COND_FLAG), действия-команды
# «Управления». Снимок задействованных определений кладётся в
# JSON-часть («vars») — вычитка и детерминированная сверка не
# зависят от текущей конфигурации ПК.
FLEX_BLOB_VERSION_VAR = 3
FLEX_BLOB_VERSIONS = (
    FLEX_BLOB_VERSION, FLEX_BLOB_VERSION_AUX, FLEX_BLOB_VERSION_VAR,
)
# Минимальный протокол, где прошивка исполняет aux-записи ГЛ.
FLEX_AUX_PROTOCOL = 13
# Минимальный протокол, где прошивка исполняет записи переменных ГЛ.
FLEX_VAR_PROTOCOL = 14
FLEX_MAX_RECORDS = 16  # должен совпадать с trigger.h
FLEX_MAX_FLAGS = 16  # должен совпадать с trigger.h FLEX_MAX_FLAGS
AUX_CH_COUNT = 7  # должен совпадать с aux_out.h

_FLXB_HEADER_SIZE = 12

# Канал-селектор записи RX_VAR: слушатель фронта статического флага
# (а не CAN-кадра) — должен совпадать с TRIGGER_VAR_CHAN_FLAG.
_VAR_CHAN_FLAG = 0xFF

# Варианты событий/действий, которые умеет движок триггеров.
_EVENT_NONE = "none"
_EVENT_FRAME = "frame"
_EVENT_POWER = "power"
_EVENT_PROGRAM = "program"
_EVENT_AUX = "aux"
_EVENT_DYN = "dyn"
_EVENT_NUM = "num"
_EVENT_STATIC = "static"
_EVENT_FLAG = "flag"
_EVENT_IMPULSE = "impulse"
_ACT_FRAME = "frame"
_ACT_AUX = "aux"
_ACT_VAR = "var"
_COND_NONE = "none"
_COND_AUX = "aux"
_COND_STATIC = "static"
_COND_FLAG = "flag"

# Режимы aux-действия → tx_dlc записи (aux_out.h AUX_MODE_*).
_AUX_MODES = {"off": 0, "on": 1, "pulse": 2, "pwm": 3}


def _tokens(value: Any) -> list[str]:
    """DATA из конфига: строка «AA BB X» либо список токенов
    (старые конфиги сериализовали поля списками)."""
    if isinstance(value, (list, tuple)):
        return [str(t).strip().upper() for t in value]
    return [
        t.strip().upper()
        for t in str(value or "").replace(",", " ").split()
    ]


class _VarScope:
    """Снимок определений переменных для компиляции (FLXB v3).

    Принимает как живой ``export_config()`` вкладки «Переменные»
    (списки read/control/aux), так и вложенный в FLXB снимок
    ``vars`` (словари по именам) — внутренне всё нормализуется в
    name→def отображения."""

    def __init__(self, variables: dict[str, Any] | None):
        variables = variables or {}
        self.read: dict[str, dict[str, Any]] = {}
        self.control: dict[str, dict[str, Any]] = {}
        self.static: dict[str, dict[str, Any]] = {}
        self.flag_vars: dict[str, dict[str, Any]] = {}
        if isinstance(variables.get("read"), dict):
            # Вложенный снимок из JSON-части FLXB v3.
            for key in ("read", "control", "static", "flag_vars"):
                src = variables.get(key) or {}
                if isinstance(src, dict):
                    getattr(self, key).update(
                        {str(k): v for k, v in src.items()
                         if isinstance(v, dict)}
                    )
        else:
            # Живой экспорт вкладки «Переменные».
            for var in variables.get("read") or []:
                if not isinstance(var, dict):
                    continue
                name = str(var.get("name", "")).strip()
                if not name:
                    continue
                self.read[name] = var
                if var.get("type") == _EVENT_STATIC:
                    self.static[name] = var
            for cmd in variables.get("control") or []:
                if not isinstance(cmd, dict):
                    continue
                name = str(cmd.get("name", "")).strip()
                if name and cmd.get("type") == "command":
                    self.control[name] = cmd
            for var in variables.get("aux") or []:
                if not isinstance(var, dict):
                    continue
                name = str(var.get("name", "")).strip()
                if name and var.get("type") == _EVENT_FLAG:
                    self.flag_vars[name] = var
        # Наследие: статические переменные «Управления» давали
        # производные команды «имя → 1»/«имя → 0» (отправка её
        # кадров с value=1/0) — собираем их из read-колонки, как
        # _legacy_to_command на ПК.
        for name, var in self.static.items():
            for want in (1, 0):
                frames = [
                    {
                        "channel": int(fr.get("channel", 0) or 0),
                        "extended": bool(fr.get("extended", False)),
                        "id": str(fr.get("id", "")),
                        "dlc": int(fr.get("dlc", 8) or 8),
                        "data": str(fr.get("data", "")),
                        "rtr": bool(fr.get("rtr", False)),
                        "delay_before_send": int(
                            fr.get("delay_before_send", 0) or 0),
                        "delay_between": int(
                            fr.get("delay_between", 0) or 0),
                        "count": max(1, int(fr.get("count", 1) or 1)),
                        "next_delay": int(fr.get("next_delay", 0) or 0),
                    }
                    for fr in var.get("frames") or []
                    if int(fr.get("value", 1) or 0) == want
                ]
                if frames:
                    self.control.setdefault(
                        f"{name} → {want}",
                        {"type": "command", "name": f"{name} → {want}",
                         "frames": frames, "cache": {}},
                    )

    def flag_def(self, name: str) -> dict[str, Any] | None:
        return self.flag_vars.get(name)


class _FlexCtx:
    """Контекст компиляции одного блоба FLXB: снимок переменных,
    назначенные индексы флагов и определения, попавшие в снимок
    JSON (для вычитки/сверки). Назначение детерминированно —
    по первому обращению в порядке компиляции правил."""

    def __init__(self, variables: dict[str, Any] | None):
        self.scope = _VarScope(variables)
        self.flag_idx: dict[tuple[str, str], int] = {}
        self.static_defs: dict[str, dict[str, Any]] = {}
        self.used_read: dict[str, dict[str, Any]] = {}
        self.used_control: dict[str, dict[str, Any]] = {}
        self.used_static: dict[str, dict[str, Any]] = {}
        self.used_flag_vars: dict[str, dict[str, Any]] = {}

    def _alloc(self, key: tuple[str, str]) -> int | None:
        if key in self.flag_idx:
            return self.flag_idx[key]
        if len(self.flag_idx) >= FLEX_MAX_FLAGS:
            return None
        idx = len(self.flag_idx)
        self.flag_idx[key] = idx
        return idx

    def static_flag(self, name: str,
                    var: dict[str, Any] | None) -> int | None:
        """Флаг статической переменной: None — нет слотов либо
        хранение ПЗУ (автономные флаги живут в ОЗУ МК — сохранение
        в ПЗУ на камне не выражается)."""
        if var is None or str(var.get("storage", "ram")) != "ram":
            return None
        idx = self._alloc(("static", name))
        if idx is not None:
            self.static_defs[name] = var
            self.used_static[name] = var
        return idx

    def user_flag(self, name: str) -> int | None:
        """Флаг «Переменной» (бит ОЗУ/ПЗУ). ПЗУ-хранение на камне
        не восстанавливается — такие флаги остаются на ПК. Флага
        без определения достаточно имени — на ПК бит существует
        и без записи в «Переменных» (_flag_states)."""
        var = self.scope.flag_def(name)
        if var is not None and str(var.get("storage", "ram")) != "ram":
            return None
        idx = self._alloc(("flag", name))
        if idx is not None and var is not None:
            self.used_flag_vars[name] = var
        return idx

    def use_read(self, var: dict[str, Any]) -> None:
        name = str(var.get("name", "")).strip()
        if name:
            self.used_read[name] = var

    def use_control(self, cmd: dict[str, Any]) -> None:
        name = str(cmd.get("name", "")).strip()
        if name:
            self.used_control[name] = cmd

    def ref_read(self, var: dict[str, Any] | None) -> None:
        """Ссылка программы на переменную чтения — фиксируется до
        исхода компиляции (даже неуспешного), иначе снимок vars
        потеряет определение и сверка хвоста в analyze_device_flex
        пойдёт по другой ветке (переменная «есть» → «нет»)."""
        if var is not None:
            self.use_read(var)

    def ref_static(self, var: dict[str, Any] | None) -> None:
        if var is not None:
            name = str(var.get("name", "")).strip()
            if name:
                self.used_static[name] = var

    def ref_flag_def(self, name: str) -> None:
        var = self.scope.flag_def(name)
        if var is not None:
            self.used_flag_vars[name] = var

    def snapshot(self) -> tuple:
        # Откатываются только АЛЛОКАЦИИ флагов (индексы и определения
        # для updater-записей): программа, ушедшая на ПК, не должна
        # оставлять занятые слоты и updater без слушателя. Референсы
        # used_* отказ не откатывает — снимок vars фиксирует сам факт
        # ссылки, чтобы verify-вычитка пошла тем же путём отказа.
        return dict(self.flag_idx), dict(self.static_defs)

    def restore(self, snap: tuple) -> None:
        self.flag_idx, self.static_defs = snap

    def vars_json(self) -> dict[str, Any]:
        """Снимок задействованных определений для JSON-части FLXB —
        вычитка программы и сверка хвоста не зависят от текущей
        конфигурации «Переменных» на ПК."""
        return {
            "flags": {
                f"{kind}:{name}": idx
                for (kind, name), idx in self.flag_idx.items()
            },
            "read": self.used_read,
            "control": self.used_control,
            "static": self.used_static,
            "flag_vars": self.used_flag_vars,
        }


def _var_extraction(
    var: dict[str, Any],
) -> tuple[int, int, int, int] | None:
    """Определение переменной → (offset, len, endian, kind) окна
    извлечения записи RX_VAR.

    kind 0 — dyn_cache (накопление позиций покадрово), kind 1 —
    численная (поле целиком из кадра). Порядок: 0 — старший байт
    в первой позиции списка (BE-окно), 1 — little-endian.
    None — список байтов неконтинуален/режим «сумма»: на камне
    не выражается."""
    used = [
        int(p) for p in (var.get("bytes") or [])
        if isinstance(p, int)
        or str(p).strip().lstrip("-").isdigit()
    ]
    if not used or min(used) < 0 or max(used) > 7:
        return None
    lo = min(used)
    contiguous = used == list(range(lo, lo + len(used)))
    descending = used == list(range(lo + len(used) - 1, lo - 1, -1))
    vtype = var.get("type")
    if vtype == "dyn_cache":
        # PC склеивает raw = (raw<<8)|cache[pos] в порядке списка:
        # возрастающий список — BE-окно, убывающий — LE.
        if contiguous:
            return lo, len(used), 0, 0
        if descending:
            return lo, len(used), 1, 0
        return None
    # Численная переменная: контракт sblp «be»/«le»; без sblp на ПК
    # — сумма байтов, на камне не выражается.
    if not contiguous:
        return None
    sblp = str(var.get("sblp", "") or "")
    if sblp == "be":
        return lo, len(used), 0, 1
    if sblp == "le":
        return lo, len(used), 1, 1
    return None


def _num_raw_threshold(
    var: dict[str, Any], thr: float, want_gt: bool
) -> int | None:
    """Порог численной переменной → порог по сырому значению.

    PC сравнивает интерполированное значение _interpolate(points,
    raw): при монотонно неубывающей карте «value > thr» эквивалентно
    «raw > floor(raw_c)», «value < thr» — «raw < ceil(raw_c)»,
    где raw_c — точка пересечения. Немонотонная карта, порог вне
    диапазона карты и плоские сегменты — на камне не выражаются.

    «Градусы»/руль не компилируются в сырой порог: на ПК значение —
    знаковый декод по веткам сторон с классификацией делителями и
    инверсией (decode_var_value), а не _interpolate по всей таблице;
    тривиальный raw-порог срабатывал бы на чужом классе/конфликте
    там, где ПК даёт invalid. Такие переменные остаются на ПК
    (CM-F05)."""
    if variable_decode.is_steering(var):
        return None
    pts: list[tuple[float, float]] = []
    for p in var.get("points") or []:
        try:
            pts.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not pts:
        return None
    pts.sort()
    ys = [y for _, y in pts]
    if any(b < a for a, b in zip(ys, ys[1:], strict=False)):
        return None  # немонотонная карта — порог на сыром неоднозначен
    y_min, y_max = ys[0], ys[-1]
    if want_gt:
        if thr >= y_max:
            return -1  # never fires — «мёртвая» ветка, как на ПК
        if thr < y_min:
            return None  # всегда истинно — край не кодируется
    else:
        if thr <= y_min:
            return -1  # value < thr никогда — «мёртвая» ветка
        if thr > y_max:
            return None  # всегда истинно
    # Граница по сырому: want_gt — raw > sup{x: y(x) <= thr}
    # (плоский сегмент на уровне порога отодвигает границу к его
    # концу — на ПК value=thr всю площадку, «>» не стреляет);
    # want_lt — raw < inf{x: y(x) >= thr} (первая точка достижения).
    raw_c: float | None = None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if want_gt:
            if y1 <= thr:
                raw_c = float(x1)  # вся точка ещё ≤ thr
            elif y0 <= thr < y1:
                raw_c = x0 + (thr - y0) * (x1 - x0) / (y1 - y0)
                break
            else:
                break
        else:
            if y1 < thr:
                continue
            if y0 < thr <= y1 and y1 != y0:
                raw_c = x0 + (thr - y0) * (x1 - x0) / (y1 - y0)
                break
            if y0 == thr:
                raw_c = float(x0)
                break
            if y0 > thr:
                break
    if raw_c is None:
        # thr лежит правее всех точек: want_gt→never, lt→всегда.
        return -1 if want_gt else None
    import math
    val = math.floor(raw_c) if want_gt else math.ceil(raw_c)
    if not 0 <= val <= 0xFFFFFFFFFFFFFFFF:
        return None
    return int(val)


def _dyn_event_to_rxs(
    event: dict[str, Any],
    var: dict[str, Any],
    ctx: _FlexCtx,
) -> list[dict[str, Any]] | None:
    """Событие «Динамическая переменная» → записи RX_VAR.

    None — не компилируется (режимы извлечения/карта не выражаются).
    [] — «мёртвая» ветка: на ПК такое событие не срабатывает никогда
    (нет переменной/привязки/порога) — семантически точно не давать
    записей вовсе."""
    fid = hex_to_int(str(var.get("id", "")))
    if fid is None:
        return []
    ext = bool(var.get("extended")) or fid > 0x7FF
    extraction = _var_extraction(var)
    if extraction is None:
        return None
    off, ln, endian, kind = extraction
    if kind == 0:
        # dyn_cache: символьное состояние из таблицы привязки.
        # Численно разбираемое значение на ПК сравнивается с
        # строковым состоянием — не срабатывает никогда (здесь и
        # далее PC-семантика _event_fired повторяется точно).
        value = str(event.get("value", "")).strip()
        try:
            float(value.replace(",", "."))
            return []  # число против имени состояния — всегда False
        except ValueError:
            pass
        raws = sorted({
            int(p[0]) for p in var.get("points") or []
            if isinstance(p, (list, tuple)) and len(p) >= 2
            and str(p[1]) == value
            and str(p[0]).strip().lstrip("-").isdigit()
        })
        if not raws:
            return []
        cmps = [(0, raw) for raw in raws]
    else:
        # Численная: на ПК dir=="gt" → «>», остальные (lt/eq) — «<»
        # (_event_fired сравнивает dir буквально с "gt").
        value = str(event.get("value", "")).strip()
        try:
            thr = float(value.replace(",", "."))
        except ValueError:
            return []  # имя состояния против float — всегда False
        want_gt = str(event.get("dir", "gt")) == "gt"
        raw_thr = _num_raw_threshold(var, thr, want_gt)
        if raw_thr is None:
            return None
        if raw_thr < 0:
            return []  # порог недостижим — «мёртвая» ветка
        cmps = [(1 if want_gt else 2, raw_thr)]
    ch = event.get("channel", 2)
    channel = (2 if ch in (None, "", 2) else int(ch)) & 0xFF
    out: list[dict[str, Any]] = []
    for cmp_op, val in cmps:
        mask = bytearray(8)
        mask[0], mask[1], mask[2], mask[3], mask[4] = (
            off, ln, endian, cmp_op, kind
        )
        data = int(val).to_bytes(8, "little")
        out.append({
            "rx_var": True,
            "rx_channel": channel,
            "rx_extended": 1 if ext else 0,
            "rx_id": fid,
            "rx_id_mask": 0x1FFFFFFF if ext else 0x7FF,
            "rx_dlc": 0,
            "rx_data": bytes(data),
            "rx_data_mask": bytes(mask),
            "rx_rtr": 0,
            "rx_listen_echo": False,
            "rx_fire_limit": 0,
        })
    ctx.use_read(var)
    return out


def _static_event_to_rx(
    event: dict[str, Any], ctx: _FlexCtx,
) -> dict[str, Any] | None | str:
    """Событие «Статическая переменная» → слушатель фронта флага.

    None — не компилируется; "" — «мёртвая» ветка (переменной нет);
    dict — rx-часть записи."""
    name = str(event.get("var", "")).strip()
    var = ctx.scope.static.get(name)
    ctx.ref_static(var)
    if var is None:
        return ""  # нет переменной — на ПК фронт не приходит
    idx = ctx.static_flag(name, var)
    if idx is None:
        return None  # ПЗУ-хранение или кончились слоты флагов
    edge = str(event.get("edge", "both"))
    dlc = {"on": 1, "off": 2}.get(edge, 0)
    mask = bytearray(8)
    mask[0] = idx
    return {
        "rx_var": True,
        "rx_channel": _VAR_CHAN_FLAG,
        "rx_extended": 0,
        "rx_id": 0,
        "rx_id_mask": 0,
        "rx_dlc": dlc,
        "rx_data": b"\x00" * 8,
        "rx_data_mask": bytes(mask),
        "rx_rtr": 0,
        "rx_listen_echo": False,
        "rx_fire_limit": 0,
    }


def _flag_event_to_rx(
    event: dict[str, Any], ctx: _FlexCtx,
) -> dict[str, Any] | None | str:
    """Событие «Переменная (бит) стала 1/0» → слушатель фронта."""
    name = str(event.get("var", "")).strip()
    if not name:
        return None
    ctx.ref_flag_def(name)
    idx = ctx.user_flag(name)
    if idx is None:
        return None
    state = int(event.get("state", 1) or 0)
    mask = bytearray(8)
    mask[0] = idx
    return {
        "rx_var": True,
        "rx_channel": _VAR_CHAN_FLAG,
        "rx_extended": 0,
        "rx_id": 0,
        "rx_id_mask": 0,
        "rx_dlc": 1 if state == 1 else 2,
        "rx_data": b"\x00" * 8,
        "rx_data_mask": bytes(mask),
        "rx_rtr": 0,
        "rx_listen_echo": False,
        "rx_fire_limit": 0,
    }


def _impulse_event_to_rx(
    event: dict[str, Any], ctx: _FlexCtx,
) -> dict[str, Any] | None | str:
    """«Импульсная переменная» — совпадение байтов одного кадра:
    это обычный кадровый матч (работает уже на протоколе 12)."""
    name = str(event.get("var", "")).strip()
    var = ctx.scope.read.get(name)
    ctx.ref_read(var)
    if var is None or var.get("type") != "impulse":
        return ""
    fid = hex_to_int(str(var.get("id", "")))
    if fid is None:
        return ""
    ext = bool(var.get("extended")) or fid > 0x7FF
    data = bytearray(8)
    mask = bytearray(8)
    for i, tok in enumerate(
            str(var.get("data", "")).split()[:8]):
        if not tok or tok == "X":
            continue
        value = hex_to_int(tok)
        if value is None:
            continue
        data[i] = value & 0xFF
        mask[i] = 0xFF
    ctx.use_read(var)
    return {
        "rx_channel": 2,
        "rx_extended": 1 if ext else 0,
        "rx_id": fid,
        "rx_id_mask": 0x1FFFFFFF if ext else 0x7FF,
        "rx_dlc": 0,
        "rx_data": bytes(data),
        "rx_data_mask": bytes(mask),
        "rx_rtr": 0,
        "rx_listen_echo": False,
        "rx_fire_limit": 0,
    }


def _event_to_rx(event: dict[str, Any]) -> dict[str, Any] | None:
    """Событие программы → rx-часть записи trigger_t.

    None — событие на камне не выражается. Пустое («Нет») событие
    возвращает {} — вкладки в запись нет, но программа не считается
    некомпилируемой."""
    etype = event.get("type", _EVENT_FRAME)
    if etype in (None, "", _EVENT_NONE):
        return {}
    if etype == _EVENT_POWER:
        # «Включение устройства»: вооружается один раз при запуске МК
        # (rx-поля не используются — как FIRE_ON_BOOT в триггерах).
        return {"rx_fire_on_boot": True, "rx_boot_delay": 0}
    if etype == _EVENT_AUX:
        # «Доп. канал включился/выключился»: переход ЛОГИЧЕСКОГО
        # состояния (OFF↔ON/PWM/PULSE), не электрический фронт ШИМ —
        # на камне событие ставит aux_out (CM-HW-03). rx_channel =
        # канал 1..7, rx_dlc = фронт: 1 — «включился», 2 —
        # «выключился» (коды edge в trigger.c::Trigger_OnAuxEvent).
        try:
            ch = int(event.get("channel", 0) or 0)
            state = int(event.get("state", 1) or 0)
        except (TypeError, ValueError):
            return None
        if not 1 <= ch <= AUX_CH_COUNT or state not in (0, 1):
            return None
        return {
            "rx_aux_event": True,
            "rx_channel": ch & 0xFF,
            "rx_dlc": 1 if state == 1 else 2,
            "rx_listen_echo": False,
        }
    if etype != _EVENT_FRAME:
        return None
    can_id = hex_to_int(str(event.get("id", "")))
    if can_id is None:
        return None
    # ID за пределами 11 бит физически всегда уходит extended-кадром:
    # без флага запись не прошла бы trigger_fields_valid и молча
    # отбрасывалась — программа не исполнялась бы нигде.
    extended = bool(event.get("extended")) or can_id > 0x7FF
    data = bytearray(8)
    mask = bytearray(8)
    for i, tok in enumerate(_tokens(event.get("data"))[:8]):
        if not tok or tok == "X":
            continue
        value = hex_to_int(tok)
        if value is None:
            continue
        data[i] = value & 0xFF
        mask[i] = 0xFF
    # Канал события: 0=CAN1, 1=CAN2, 2=любой — None/прочерк → «любой»
    # («0 or 2» здесь ошибочно превращал бы CAN1 в «любой»).
    ch = event.get("channel", 2)
    # «Кол-во сработок до смены DATA»: на ПК 0/1 — «без ограничения»
    # (_event_fired: limit > 1), а старые конфиги писали 1 — передача
    # 1 в rx_fire_limit запирала бы запись на камне после первой
    # сработки. Нормализуем: ограничением считается только >1.
    fire_limit = int(event.get("fire_limit", 0) or 0)
    rx_flags_extra = {
        # ПК не реагирует на TX-эхо — на камне то же через MUTE_ECHO.
        "rx_listen_echo": False,
        "rx_fire_limit": (fire_limit if fire_limit > 1 else 0) & 0xFFFF,
    }
    return {
        "rx_channel": (2 if ch is None else int(ch)) & 0xFF,
        "rx_extended": 1 if extended else 0,
        "rx_id": can_id,
        "rx_id_mask": 0x1FFFFFFF if extended else 0x7FF,
        # PC-матч не проверяет DLC — 0 = «любая длина».
        "rx_dlc": 0,
        "rx_data": bytes(data),
        "rx_data_mask": bytes(mask),
        "rx_rtr": 1 if event.get("rtr") else 0,
        **rx_flags_extra,
    }


def _aux_action_to_tx(action: dict[str, Any]) -> dict[str, Any] | None:
    """Действие «Доп. канал» → tx-часть записи trigger_t (FLXB v2).

    Раскладка tx_data по режимам — как trigger.c::aux_exec:
      Вкл:  [0..3] срок работы мс (u32 LE; 0 = до «Выкл»)
      Импульсы: [0..1] импульс, [2..3] пауза, [4..5] кол-во (u16),
                [6..7] общий срок (u16, 0 = считать по кол-ву)
      ШИМ:  [0..1] частота Гц, [2] заполнение %, [3..6] срок (u32)
    tx_dlc = AUX_MODE_* (0..3), delay_ms — «пауза до действия»."""
    try:
        ch = int(action.get("aux_channel", 0) or 0)
        mode = _AUX_MODES[str(action.get("aux_mode", "on"))]
        delay = int(action.get("aux_delay", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not 1 <= ch <= AUX_CH_COUNT or not 0 <= delay <= 0xFFFF:
        return None
    data = bytearray(8)
    if mode == 1:  # Вкл — с опциональным сроком «включить на T мс»
        on_time = int(action.get("aux_on_time", 0) or 0)
        if on_time > AUX_MAX_DURATION_MS:
            return None  # та же граница, что у МК (CM-N5)
        data[0:4] = on_time.to_bytes(4, "little")
    elif mode == 2:  # импульсы
        try:
            pulse_on = int(action.get("aux_pulse_on", 0) or 0)
            pulse_off = int(action.get("aux_pulse_off", 0) or 0)
            pulse_count = int(action.get("aux_pulse_count", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not (pulse_on and pulse_off and pulse_count):
            return None
        data[0:2] = pulse_on.to_bytes(2, "little")
        data[2:4] = pulse_off.to_bytes(2, "little")
        data[4:6] = pulse_count.to_bytes(2, "little")
    elif mode == 3:  # ШИМ
        try:
            freq = int(action.get("aux_pwm_freq", 0) or 0)
            duty = int(action.get("aux_pwm_duty", 0) or 0)
        except (TypeError, ValueError):
            return None
        # Зеркало AuxOut_Set: частота 1..500 Гц (программный ШИМ,
        # период ≥ 2 мс), заполнение 0..100 (0% — легально).
        if not 1 <= freq <= 500 or not 0 <= duty <= 100:
            return None
        data[0:2] = freq.to_bytes(2, "little")
        data[2] = duty
        pwm_time = int(action.get("aux_pwm_time", 0) or 0)
        if pwm_time > AUX_MAX_DURATION_MS:
            return None
        data[3:7] = pwm_time.to_bytes(4, "little")
    # mode == 0 («Выкл») — параметры не нужны.
    # delay_ms передаётся без маски — верхний предел поля 0xFFFF
    # проверяет вызывающий вместе с exec_delay (CM-F11: единая
    # «Задержка», усечение молча меняло бы время старта).
    return {
        "tx_aux": True,
        "tx_channel": ch & 0xFF,
        "tx_dlc": mode & 0xFF,
        "tx_data": bytes(data),
        "delay_ms": delay,
        "tx_count": 1,
        "tx_interval_ms": 0,
    }


def _event_to_rxs(
    event: dict[str, Any],
    ctx: _FlexCtx,
    allow_aux: bool,
    allow_var: bool,
) -> list[dict[str, Any]] | None:
    """Событие программы → список rx-частей (ветка может порождать
    несколько записей — например «== имя состояния» по нескольким
    сырым значениям).

    None — ветка на камне не выражается (программа остаётся на ПК).
    [] — «мёртвая» ветка: на ПК она не срабатывает никогда (нет
    переменной/привязки) — записей не даёт, но программу не ломает."""
    etype = event.get("type", _EVENT_FRAME)
    if etype in (_EVENT_DYN, _EVENT_NUM):
        if not allow_var:
            return None
        name = str(event.get("var", "")).strip()
        var = ctx.scope.read.get(name)
        ctx.ref_read(var)
        if var is None or str(var.get("storage", "ram")) != "ram":
            # Нет определения либо хранение ПЗУ — на ПК фронт по
            # отсутствующей переменной не приходит («мёртвая» ветка).
            # ПЗУ-переменные автономно не поддерживаются — честный
            # отказ, а не молчаливое исполнение только на ПК.
            return [] if var is None else None
        return _dyn_event_to_rxs(event, var, ctx)
    if etype == _EVENT_STATIC:
        if not allow_var:
            return None
        rx = _static_event_to_rx(event, ctx)
        return None if rx is None else ([rx] if isinstance(rx, dict) else [])
    if etype == _EVENT_FLAG:
        if not allow_var:
            return None
        rx = _flag_event_to_rx(event, ctx)
        return None if rx is None else ([rx] if isinstance(rx, dict) else [])
    if etype == _EVENT_IMPULSE:
        rx = _impulse_event_to_rx(event, ctx)
        return None if rx is None else ([rx] if isinstance(rx, dict) else [])
    rx = _event_to_rx(event)
    if rx is None:
        return None
    if rx.get("rx_aux_event") and not allow_aux:
        # Aux-событие на прошивке протокола <13 запись не валидирует
        # — FLXB отвергся бы целиком, ломая и кадровые программы.
        return None
    return [rx] if rx else []


def _cmd_frames_to_txs(
    cmd: dict[str, Any], base_delay: int,
) -> list[dict[str, Any]] | None:
    """Команда «Управления» → серия tx-частей записей со свёрнутым
    абсолютным расписанием — как _run_command на ПК: старт кадра k =
    суммарное время предыдущих фреймов + delay_before_send, повторы —
    tx_count/tx_interval_ms («пауза между»), «пауза до следующего»
    входит в сдвиг следующего старта."""
    if (cmd.get("cache") or {}).get("enabled"):
        return None  # автозапись DATA в кэш — только на ПК
    txs: list[dict[str, Any]] = []
    elapsed = base_delay
    for fr in cmd.get("frames") or []:
        fid = hex_to_int(str(fr.get("id", "")))
        if fid is None:
            return None
        extended = bool(fr.get("extended")) or fid > 0x7FF
        rtr = bool(fr.get("rtr", False))
        data = bytearray(8)
        if not rtr:
            tokens = str(fr.get("data", "")).split()
            if any(t and t.upper() == "X" for t in tokens):
                # «X» в команде = подстановка из её кэша — на камне
                # нет кэша команды (только «X из кадра события»).
                return None
            for i, tok in enumerate(tokens[:8]):
                if not tok:
                    continue
                value = hex_to_int(tok)
                data[i] = (value or 0) & 0xFF
        try:
            before = int(fr.get("delay_before_send", 0) or 0)
            count = max(1, int(fr.get("count", 1) or 1))
            between = int(fr.get("delay_between", 0) or 0)
            next_d = int(fr.get("next_delay", 0) or 0)
            dlc = int(fr.get("dlc", 8) or 8)
            channel = int(fr.get("channel", 0) or 0)
        except (TypeError, ValueError):
            return None
        if channel not in (0, 1) or not 0 <= dlc <= 8:
            return None
        start = elapsed + before
        if not (0 <= start <= 0xFFFF and 0 <= between <= 0xFFFF
                and count <= 0xFF):
            return None
        txs.append({
            "tx_channel": channel & 0xFF,
            "tx_extended": 1 if extended else 0,
            "tx_id": fid,
            "tx_dlc": dlc & 0xFF,
            "tx_data": bytes(data),
            "tx_rtr": 1 if rtr else 0,
            "delay_ms": start,
            "tx_count": count,
            "tx_interval_ms": between,
        })
        elapsed = start + (count - 1) * between + next_d
    return txs


def _flag_action_to_tx(
    action: dict[str, Any], ctx: _FlexCtx, base_delay: int,
) -> dict[str, Any] | None | str:
    """Действие «Переменная — вкл/выкл/вкл на N мс» → псевдо-действие
    SETFLAG. "" — битого имени/слотов нет (не компилируется)... на
    самом деле возвращает None при ПЗУ/переполнении; строку-ошибку
    не используем — None."""
    name = str(action.get("flag_var", "")).strip()
    if not name:
        return ""
    if not 0 <= base_delay <= 0xFFFF:
        return None
    ctx.ref_flag_def(name)
    idx = ctx.user_flag(name)
    if idx is None:
        return None
    mode = str(action.get("flag_mode", "on"))
    data = bytearray(8)
    data[0] = idx
    if mode == "off":
        data[1] = 0
    else:
        data[1] = 1
        if mode == "pulse":
            pulse = int(action.get("flag_time", 0) or 0)
            if pulse <= 0 or pulse > 0xFFFFFFFF:
                return None
            data[2:6] = pulse.to_bytes(4, "little")
    return {
        "tx_setflag": True,
        "tx_channel": 0,
        "tx_extended": 0,
        "tx_id": 0,
        "tx_dlc": 0,
        "tx_data": bytes(data),
        "tx_rtr": 0,
        "delay_ms": base_delay,
        "tx_count": 1,
        "tx_interval_ms": 0,
    }


def _action_to_txs(
    action: dict[str, Any],
    ctx: _FlexCtx,
    base_delay: int,
) -> list[dict[str, Any]] | None | str:
    """Действие «Переменные управления» → tx-части фреймов команды.

    "" — команда не найдена: на ПК действие тоже ничего не делает
    (_run_actions_now не находит имени в списке) — «мёртвая» ветка,
    записей не даёт. None — команда есть, но на камне не выражается
    (кэш, «X» из кэша, недопустимый канал, переполнение полей)."""
    name = str(action.get("var", "")).strip()
    if not name:
        return ""
    candidates = [name]
    if "var_value" in action:
        candidates.append(f"{name} → {int(action.get('var_value', 1) or 0)}")
    for cand in candidates:
        cmd = ctx.scope.control.get(cand)
        if cmd is not None:
            ctx.use_control(cmd)
            return _cmd_frames_to_txs(cmd, base_delay)
    # Команда найдена не была — но если имя совпало со статической
    # переменной, производная «имя → 0/1» строится из её
    # определения: референс на статическую фиксируем, чтобы снимок
    # повторял исход (_VarScope восстановит производную команду).
    st = ctx.scope.static.get(name)
    ctx.ref_static(st)
    return ""


def _action_to_tx(action: dict[str, Any]) -> dict[str, Any] | None:
    """Действие «Фрейм» → tx-часть записи + маска подстановки X.

    None — действие на камне не выражается."""
    if action.get("type", _ACT_FRAME) not in (None, "", _ACT_FRAME):
        return None
    if not action.get("frame_enabled", True):
        return {}
    can_id = hex_to_int(str(action.get("id", "")))
    if can_id is None:
        return {}
    extended = bool(action.get("extended")) or can_id > 0x7FF
    data = bytearray(8)
    subst = bytearray(8)
    tokens = _tokens(action.get("data"))
    # PC-семантика (_run_actions_now): payload собирается НА кадре
    # события — каждая позиция без валидного hex-токена (пустая, «X»,
    # нечисловая и позиции за концом списка токенов) берёт байт из
    # пришедшего кадра. Маска подстановки покрывает их все.
    for i in range(8):
        tok = tokens[i] if i < len(tokens) else ""
        value = hex_to_int(tok) if tok and tok != "X" else None
        if value is None:
            subst[i] = 0xFF
        else:
            data[i] = value & 0xFF
    return {
        "tx_channel": int(action.get("channel", 0) or 0) & 0xFF,
        "tx_extended": 1 if extended else 0,
        "tx_id": can_id,
        "tx_dlc": int(action.get("dlc", 8) or 8) & 0xFF,
        "tx_data": bytes(data),
        "tx_rtr": 0,
        "delay_ms": int(action.get("delay", 0) or 0) & 0xFFFF,
        "tx_count": max(1, int(action.get("count", 1) or 1)) & 0xFF,
        "tx_interval_ms": int(action.get("between", 0) or 0) & 0xFFFF,
        "src_from": bytes(subst),
        "src_to": b"\x00" * 8,
        "_subst": any(subst),
    }


def _rule_parts(rule: dict[str, Any]) -> tuple[list, list, list]:
    """События/условия/действия правила с учётом легаси-формата
    (одиночный event/condition/объединённый action)."""
    events = rule.get("events")
    if not isinstance(events, list) or not events:
        events = [rule.get("event") or {}]
    conditions = rule.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        conditions = [rule.get("condition") or {}]
    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        merged = rule.get("action") or {}
        actions = [merged] if merged else []
    return events, conditions, actions


def _rx_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Могут ли два скомпилированных события сработать на одном кадре
    (или на одном старте для «Включение устройства»).

    На ПК события программы объединены по ИЛИ с break — кадр,
    совпавший с двумя событиями, исполняет действия один раз. На камне
    каждое событие — отдельная запись trigger_t: пересекающиеся
    фильтры дали бы двойную отправку, поэтому такие программы не
    компилируются и остаются на исполнении приложения."""
    a_aux = bool(a.get("rx_aux_event"))
    b_aux = bool(b.get("rx_aux_event"))
    if a_aux or b_aux:
        # Aux-события пересекаются только одинаковым фронтом того же
        # канала («канал N включился» дважды в списке). Фронт ON и OFF
        # — разные переходы, кадры шины aux-записи не возбуждают.
        return (
            a_aux and b_aux
            and a["rx_channel"] == b["rx_channel"]
            and a["rx_dlc"] == b["rx_dlc"]
        )
    a_boot = bool(a.get("rx_fire_on_boot"))
    b_boot = bool(b.get("rx_fire_on_boot"))
    if a_boot or b_boot:
        # «Включение устройства» вооружается при старте: два power-
        # события сработали бы оба. power+frame не пересекаются —
        # кадр шины boot-запись не возбуждает.
        return a_boot and b_boot
    a_var = bool(a.get("rx_var"))
    b_var = bool(b.get("rx_var"))
    if a_var or b_var:
        a_flag = a_var and a["rx_channel"] == _VAR_CHAN_FLAG
        b_flag = b_var and b["rx_channel"] == _VAR_CHAN_FLAG
        if a_flag or b_flag:
            # Слушатель фронта флага — не кадровое событие:
            # пересекается только со слушателем того же флага и
            # совместимого фронта (rx_dlc 0 = любой фронт).
            return (
                a_flag and b_flag
                and a["rx_data_mask"][0] == b["rx_data_mask"][0]
                and (
                    a["rx_dlc"] == 0
                    or b["rx_dlc"] == 0
                    or a["rx_dlc"] == b["rx_dlc"]
                )
            )
        var_r = a if a_var else b
        other = b if a_var else a
        # Канал: 2 = любой — пересекается с любым.
        if (
            var_r["rx_channel"] != 2
            and other["rx_channel"] != 2
            and var_r["rx_channel"] != other["rx_channel"]
        ):
            return False
        if var_r["rx_extended"] != other["rx_extended"]:
            return False
        if b_var:
            # dyn-сравнение против dyn-сравнения: предикаты
            # независимы — пересекаются, кроме доказуемо
            # несовместных «eq по одному окну на разные значения».
            if a["rx_id"] != b["rx_id"]:
                return False
            ma, mb = a["rx_data_mask"], b["rx_data_mask"]
            return not (
                ma[3] == 0 and mb[3] == 0
                and ma[:5] == mb[:5]
                and a["rx_data"] != b["rx_data"]
            )
        # dyn-сравнение против кадрового фильтра: подошедший кадр
        # может удовлетворить обоим — пересечение по маске ID.
        if other["rx_rtr"] == 1:
            return False  # переменные живут только на кадрах данных
        masked = other["rx_id_mask"]
        return (var_r["rx_id"] & masked) == (other["rx_id"] & masked)
    # Канал: 2 = любой — пересекается с любым.
    if (
        a["rx_channel"] != 2
        and b["rx_channel"] != 2
        and a["rx_channel"] != b["rx_channel"]
    ):
        return False
    if a["rx_extended"] != b["rx_extended"]:
        return False
    if (a["rx_id"] & a["rx_id_mask"]) != (b["rx_id"] & b["rx_id_mask"]):
        return False
    # rx_rtr: 0 = любой кадр, 1 = только RTR, 2 = только данные.
    if a["rx_rtr"] and b["rx_rtr"] and a["rx_rtr"] != b["rx_rtr"]:
        return False
    # rx_dlc: 0 = любая длина.
    if a["rx_dlc"] and b["rx_dlc"] and a["rx_dlc"] != b["rx_dlc"]:
        return False
    # DATA: позиции, заданные в обоих фильтрах, должны совпадать.
    # В RTR-режиме данные не сравниваются вовсе.
    if a["rx_rtr"] != 1 and b["rx_rtr"] != 1:
        for i in range(8):
            both = a["rx_data_mask"][i] & b["rx_data_mask"][i]
            if both and (a["rx_data"][i] & both) != (b["rx_data"][i] & both):
                return False
    return True


def _rule_events(rule: dict[str, Any]) -> list:
    """События правила с учётом легаси-формата (одиночный «event»)."""
    events = rule.get("events")
    if not isinstance(events, list) or not events:
        events = [rule.get("event") or {}]
    return events


def _compile_rule(
    rule: dict[str, Any],
    allow_aux: bool = False,
    allow_var: bool = False,
    ctx: _FlexCtx | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Программа ГЛ → (записи trigger_t | None, причина отказа | None).

    allow_aux — прошивка протокола >=13 (FLXB v2) умеет события/
    условия/действия доп. каналов; allow_var — протокол >=14
    (FLXB v3): события/условия по переменным и флагам, действия
    «Переменная» и команды «Управления». Причины — для UI-бейджа
    «кто исполняет» (ревью CM-R4/R5)."""
    if not rule.get("active", False):
        return [], None
    if ctx is None:
        ctx = _FlexCtx(None)
    flags_before = set(ctx.flag_idx)
    events, conditions, actions = _rule_parts(rule)

    # Условия, переносимые на камень: «доп. канал работает/не
    # работает» (proto>=13) и «флаг/статическая в состоянии»
    # (proto>=14). Проверка — в момент события, как _condition_passed
    # на ПК. Запись несёт одно условие — комбинации и остальные типы
    # остаются на ПК.
    aux_cond: tuple[int, int] | None = None  # (канал, src_dlc)
    flag_cond: tuple[int, int] | None = None  # (индекс флага, src_dlc)
    for cond in conditions:
        ctype = cond.get("type", _COND_NONE)
        if ctype in (None, "", _COND_NONE):
            continue
        if ctype == _COND_AUX and allow_aux and aux_cond is None:
            try:
                ch = int(cond.get("channel", 0) or 0)
                st = int(cond.get("state", 1) or 0)
            except (TypeError, ValueError):
                return None, "повреждённое условие по доп. каналу — только на ПК"
            if not 1 <= ch <= AUX_CH_COUNT or st not in (0, 1):
                return None, "повреждённое условие по доп. каналу — только на ПК"
            aux_cond = (ch, 1 if st == 1 else 2)
            continue
        if ctype in (_COND_STATIC, _COND_FLAG) and allow_var \
                and flag_cond is None:
            name = str(cond.get("var", "")).strip()
            want = int(cond.get("state", 1) or 0)
            if ctype == _COND_STATIC:
                var = ctx.scope.static.get(name)
                ctx.ref_static(var)
                if var is None:
                    # Отсутствующая статическая читается нулём
                    # (_static_states.get(name, 0)): «==0» — условие
                    # всегда истинно (пропускаем), «==1» — программа
                    # не срабатывает никогда (как на ПК).
                    if want != 0:
                        return [], None
                    continue
                idx = ctx.static_flag(name, var)
            else:
                ctx.ref_flag_def(name)
                idx = ctx.user_flag(name)
                if idx is None and ctx.scope.flag_def(name) is None:
                    # Флага без определения достаточно имени — на ПК
                    # бит существует и читается нулём.
                    if want != 0:
                        return [], None
                    continue
            if idx is None:
                return None, (
                    "условие по ПЗУ-переменной или кончились флаги "
                    "МК — только на ПК"
                )
            flag_cond = (idx, 1 if want == 1 else 2)
            continue
        return None, "условие ПРИ — только на ПК"

    rx_parts: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            return None, "повреждённое событие — только на ПК"
        rxs = _event_to_rxs(event, ctx, allow_aux, allow_var)
        if rxs is None:
            return None, "событие не выражается на камне — только на ПК"
        for rx in rxs:
            rx_parts.append((idx, rx))
    if not rx_parts:
        return [], None
    # Пересекающиеся события: ИЛИ-семантика ПК (break на первом
    # совпавшем) в записях не выражается — на камне сработали бы обе
    # записи и действия исполнились бы дважды. Такая программа
    # остаётся на ПК (ревью CM-03).
    for i in range(len(rx_parts)):
        for j in range(i + 1, len(rx_parts)):
            if _rx_overlap(rx_parts[i][1], rx_parts[j][1]):
                return None, "пересекающиеся события — только на ПК"
    # rx_cond_idx упаковывается в rx_flags битах 2..4 — максимум 7.
    if max(idx for idx, _ in rx_parts) > 7:
        return None, "больше 8 событий — только на ПК"

    tx_parts: list[tuple[int, dict[str, Any]]] = []
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            return None, "повреждённое действие — только на ПК"
        # «Прервать если» — только на ПК. «Задержка» выполнения
        # (exec_delay) сворачивается в delay_ms первого отправления —
        # единая задержка, автономная на МК (CM-F11).
        try:
            exec_delay = int(action.get("exec_delay", 0) or 0)
        except (TypeError, ValueError):
            return None, "задержка выполнения повреждена — только на ПК"
        abort = action.get("abort") or {}
        if abort.get("type") not in (None, "", _EVENT_NONE):
            return None, "прерыватель действия — только на ПК"
        # Действие «Доп. канал» переносится на камень с протокола 13 —
        # до валидации «не-фрейма», чтобы atype=aux доходило сюда.
        atype = action.get("type", _ACT_FRAME)
        if atype == _ACT_AUX or action.get("aux_enabled"):
            if not allow_aux:
                return None, "действие «доп. канал» — только на ПК"
            tx = _aux_action_to_tx(action)
            if tx is None:
                return None, "действие «доп. канал» повреждено — только на ПК"
            # CM-F11: легаси aux_delay и общий exec_delay — это
            # последовательные этапы одного старта, их сумма и есть
            # автономная delay_ms. Сумма сверх поля — честный отказ.
            if tx["delay_ms"] + exec_delay > 0xFFFF:
                return None, "задержка действия не влезает — только на ПК"
            if tx["delay_ms"] + exec_delay:
                tx = dict(tx)
                tx["delay_ms"] += exec_delay
            tx_parts.append((idx, tx))
            continue
        # Действие «Переменная» (бит ОЗУ/ПЗУ) — псевдо-действие
        # SETFLAG (proto>=14). Проверка ДО команды: type «var» с
        # flag_var — это бит, а не «Переменные управления».
        if str(action.get("flag_var", "")).strip():
            if not allow_var:
                return None, "действие «переменная» — только на ПК"
            tx = _flag_action_to_tx(action, ctx, exec_delay)
            if tx is None:
                return None, (
                    "действие «переменная» не выражается — только на ПК"
                )
            if isinstance(tx, dict):
                tx_parts.append((idx, tx))
            continue
        # Действие «Переменные управления» (команда) — серия
        # tx-записей со свёрнутым расписанием (proto>=14). Нет такой
        # команды — на ПК действие тоже ничего не делает («мёртвая»
        # ветка, записей не даёт).
        if atype == _ACT_VAR or str(action.get("var", "")).strip():
            if not allow_var:
                return None, "действие «команда» — только на ПК"
            txs = _action_to_txs(action, ctx, exec_delay)
            if txs is None:
                return None, (
                    "команда «Управления» не выражается на камне "
                    "— только на ПК"
                )
            for tx in txs if isinstance(txs, list) else []:
                tx_parts.append((idx, tx))
            continue
        # Действия не-фреймового типа (кэш и пр.) делают программу
        # некомпилируемой целиком.
        non_frame = (
            str(action.get("cachevar", "")).strip()
            or action.get("cache_enabled")
            or action.get("cv_op")
        )
        if atype not in (None, "", _ACT_FRAME, "none") or non_frame:
            return None, "действие не «отправка кадра» — только на ПК"
        tx = _action_to_tx(action)
        if tx is None:
            return None, "действие не «отправка кадра» — только на ПК"
        if tx:
            if exec_delay:
                if tx["delay_ms"] + exec_delay > 0xFFFF:
                    return None, "задержка действия не влезает — только на ПК"
                tx = dict(tx)
                tx["delay_ms"] += exec_delay
            tx_parts.append((idx, tx))
    if not tx_parts:
        return [], None

    records: list[dict[str, Any]] = []
    for ev_idx, rx in rx_parts:
        for act_idx, tx in tx_parts:
            # get(), не pop(): один tx участвует в записи каждого
            # события — pop без дефолта падал KeyError на второй
            # итерации, и весь блоб ГЛ не писался в камень.
            subst = bool(tx.get("_subst"))
            record: dict[str, Any] = {
                "enabled": 1,
                "rx_channel": 0,
                "rx_extended": 0,
                "rx_id": 0,
                "rx_id_mask": 0,
                "rx_dlc": 0,
                "rx_data": b"\x00" * 8,
                "rx_data_mask": b"\x00" * 8,
                "rx_rtr": 0,
                "rx_listen_echo": False,
                "rx_fire_limit": 0,
                "rx_fire_on_boot": False,
                "rx_boot_delay": 0,
                "rx_cond_idx": ev_idx,
                "tx_channel": 0,
                "tx_extended": 0,
                "tx_id": 0,
                "tx_dlc": 0,
                "tx_data": b"\x00" * 8,
                "tx_rtr": 0,
                "delay_ms": 0,
                "tx_count": 1,
                "tx_interval_ms": 0,
                "cache_enabled": 0,
                "src_channel": 0,
                "src_extended": 0,
                "src_id": 0,
                "src_dlc": 0,
                "src_from": b"\x00" * 8,
                "src_to": b"\x00" * 8,
                "src_listen_echo": True,
                "src_fire_limit": 0,
                "src_cache_only": False,
                "group_seq": act_idx & 0xFF,
            }
            record.update(rx)
            record.update(tx)
            if aux_cond is not None:
                # «При доп. канал N работает/не работает»: src_channel
                # = канал, src_dlc = требуемое логическое состояние
                # (1 работает / 2 не работает) — aux_cond_ok в
                # trigger.c проверяет её в момент события.
                record["cond_aux"] = True
                record["src_channel"] = aux_cond[0]
                record["src_dlc"] = aux_cond[1]
            if flag_cond is not None:
                # «При флаг/статическая ==0/==1»: src_flags бит 5,
                # src_id = индекс бита flags_ram[], src_dlc = 1/2.
                record["cond_flag"] = True
                record["src_id"] = flag_cond[0]
                record["src_dlc"] = flag_cond[1]
            # «X из кадра события» — флаг rx_flags бит 5 + маска
            # подстановки в src_from (у скомпилированных записей
            # кэш-фильтр src_* не используется). Кадр события есть
            # только у кадрового фильтра и «динамической» RX_VAR
            # (rx_channel 0..2); слушатели флагов, aux-фронты и
            # «включение устройства» кадра не имеют.
            non_frame_event = (
                rx.get("rx_aux_event")
                or rx.get("rx_fire_on_boot")
                or (rx.get("rx_var")
                    and rx.get("rx_channel") == _VAR_CHAN_FLAG)
            )
            record["rx_tx_event_data"] = (
                bool(subst) and not non_frame_event
            )
            records.append(record)

    # Updater-записи статических переменных, впервые затронутых
    # этой программой: каждый кадр определения переменной —
    # SETFLAG её бита значением кадра (как _update_variables на
    # ПК: подошедший кадр ставит state=value). Флаги, выделенные
    # ранее скомпилированными программами, updater уже имеют.
    for (kind, name), fidx in ctx.flag_idx.items():
        if kind != "static" or (kind, name) in flags_before:
            continue
        var = ctx.static_defs.get(name) or {}
        for fr in var.get("frames") or []:
            if not isinstance(fr, dict):
                return None, (
                    "повреждённый кадр статической — только на ПК"
                )
            fid = hex_to_int(str(fr.get("id", "")))
            if fid is None:
                continue  # на ПК такой кадр не матчится никогда
            try:
                value = int(fr.get("value", 1) or 0)
            except (TypeError, ValueError):
                return None, (
                    "повреждённое значение статической — только на ПК"
                )
            if value not in (0, 1):
                # На ПК бит статической — произвольное int; фронты
                # «стала 1/0» и условия «==0/==1» осмысленны только
                # для 0/1 — остальное честно остаётся на ПК.
                return None, (
                    "статическая переменная со значением не 0/1 "
                    "— только на ПК"
                )
            data = bytearray(8)
            dmask = bytearray(8)
            for i, tok in enumerate(_tokens(fr.get("data"))[:8]):
                if not tok or tok == "X":
                    continue
                bval = hex_to_int(tok)
                if bval is None:
                    continue  # как _tokens_match: битый токен не
                              # ограничивает матч
                data[i] = bval & 0xFF
                dmask[i] = 0xFF
            ext = bool(fr.get("extended")) or fid > 0x7FF
            tdata = bytearray(8)
            tdata[0] = fidx
            tdata[1] = value
            records.append({
                "enabled": 1,
                # Канал у статических на ПК не проверяется — любой.
                "rx_channel": 2,
                "rx_extended": 1 if ext else 0,
                "rx_id": fid,
                "rx_id_mask": 0x1FFFFFFF if ext else 0x7FF,
                "rx_dlc": 0,
                "rx_data": bytes(data),
                "rx_data_mask": bytes(dmask),
                "rx_rtr": 0,
                "rx_listen_echo": False,
                "rx_fire_limit": 0,
                "rx_fire_on_boot": False,
                "rx_boot_delay": 0,
                "rx_cond_idx": 0,
                "tx_setflag": True,
                "tx_channel": 0,
                "tx_extended": 0,
                "tx_id": 0,
                "tx_dlc": 0,
                "tx_data": bytes(tdata),
                "tx_rtr": 0,
                "delay_ms": 0,
                "tx_count": 1,
                "tx_interval_ms": 0,
                "cache_enabled": 0,
                "src_channel": 0,
                "src_extended": 0,
                "src_id": 0,
                "src_dlc": 0,
                "src_from": b"\x00" * 8,
                "src_to": b"\x00" * 8,
                "src_listen_echo": True,
                "src_fire_limit": 0,
                "src_cache_only": False,
                # Не действие программы — маркер updater-слота
                # (group_seq прошивкой не читается).
                "group_seq": 0xFF,
            })
    return records, None


def compile_rule(
    rule: dict[str, Any],
    allow_aux: bool = False,
    allow_var: bool = False,
    variables: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Программа ГЛ → список записей trigger_t (словари для pack_trigger).

    None — программа на камне не выражается (исполняет приложение).
    [] — программа компилируется, но записей не порождает (неактивна,
    нет рабочих событий или действий). Одиночная компиляция без
    общего контекста: флаги переменных назначаются с нуля — для
    блоба пользоваться compile_rules, не суммой compile_rule."""
    records, _reason = _compile_rule(
        rule, allow_aux, allow_var, _FlexCtx(variables)
    )
    return records


def _compile_all(
    rules: list[dict[str, Any]],
    protocol: int,
    variables: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[tuple[bool, str | None]], _FlexCtx]:
    """Все программы → (записи, [(managed, reason)], контекст).

    Общий _FlexCtx: индексы флагов переменных назначаются
    детерминированно в порядке компиляции программ. Правило,
    ушедшее на ПК (не выразилось или не влезло в квоту), откатывает
    свои выделения флагов — слоты и updater-записи не зависают."""
    allow_aux = protocol >= FLEX_AUX_PROTOCOL
    allow_var = protocol >= FLEX_VAR_PROTOCOL
    ctx = _FlexCtx(variables)

    # Событие «программа <имя> начала работать» существует только в
    # рантайме ПК: программа, ушедшая на камень, стартует без уведомления
    # приложения — слушающая её программа никогда бы не сработала.
    # Целевая программа вместе со слушателем остаётся на ПК
    # (ревью CM-05) — демоция ДО компиляции, чтобы её флаги/апдейтеры
    # не занимали слоты и не попадали в снимок.
    listened = {
        str(event.get("var", "")).strip()
        for rule in rules
        for event in _rule_events(rule)
        if isinstance(event, dict) and event.get("type") == _EVENT_PROGRAM
    }

    records: list[dict[str, Any]] = []
    info: list[tuple[bool, str | None]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            info.append((False, "ошибка конфигурации — только на ПК"))
            continue
        if str(rule.get("title", "")).strip() in listened:
            info.append((
                False,
                "зависит событие «программа начала работать» — "
                "только на ПК",
            ))
            continue
        snap = ctx.snapshot()
        try:
            rule_records, reason = _compile_rule(
                rule, allow_aux, allow_var, ctx
            )
        except Exception:  # noqa: BLE001
            # Битый элемент конфига не должен срывать запись всего
            # блоба — программа остаётся на исполнении приложения.
            rule_records, reason = (
                None, "ошибка конфигурации — только на ПК"
            )
        if rule_records is None:
            ctx.restore(snap)
            info.append((False, reason or "только на ПК"))
            continue
        if len(records) + len(rule_records) > FLEX_MAX_RECORDS:
            ctx.restore(snap)
            info.append((False, "квота записей МК исчерпана (16)"))
            continue
        records.extend(rule_records)
        info.append((True, None))
    return records, info, ctx


def executor_info(
    rules: list[dict[str, Any]],
    protocol: int,
    variables: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Для каждой программы — владелец исполнения и причина
    (UI-бейдж «камень/ПК», ревью CM-R4).

    Ключи записи: ``managed`` (камень исполняет), ``reason``
    (почему программа на ПК / почему не компилируется), ``active``."""
    if protocol < 12:
        return [
            {
                "managed": False,
                "active": bool(rule.get("active")),
                "reason": (
                    f"прошивка без автономной ГЛ (протокол {protocol})"
                ),
            }
            for rule in rules
        ]
    _records, info, _ctx = _compile_all(rules, protocol, variables)
    return [
        {
            "managed": managed and bool(rule.get("active")),
            "active": bool(rule.get("active")),
            "reason": reason,
        }
        for (managed, reason), rule in zip(info, rules, strict=False)
    ]


def compile_rules(
    rules: list[dict[str, Any]],
    protocol: int = 12,
    variables: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[bool]]:
    """Все программы → (плоский список записей, флаги «исполняет МК»).

    protocol >= 13 разрешает записи доп. каналов (FLXB v2), >=14 —
    записи переменных и команд (FLXB v3). Записи укладываются до
    FLEX_MAX_RECORDS; программа, не влезшая целиком, остаётся на
    исполнении приложением."""
    records, info, _ctx = _compile_all(rules, protocol, variables)
    managed = [
        managed and bool(rule.get("active"))
        for (managed, _reason), rule in zip(info, rules, strict=False)
    ]
    return records, managed


def build_flex_blob(
    rules: list[dict[str, Any]],
    protocol: int = 13,
    variables: dict[str, Any] | None = None,
) -> bytes:
    """FLXB-контейнер для блоба FLXH: JSON-конфигурация + хвост из
    скомпилированных записей trigger_t, исполняемых прошивкой.

    protocol — версия протокола целевой прошивки: >=13 компилирует и
    aux-записи, >=14 — записи переменных/команд (FLXB v3);
    младшие пишут чисто кадровые программы (FLXB v1), чтобы блоб
    читался старой прошивкой после отката. ``variables`` — живой
    export_config() вкладки «Переменные»: задействованные
    определения вкладываются в JSON-снимок «vars», по нему
    вычитка повторяет компиляцию побайтово."""
    records, _info, ctx = _compile_all(rules, protocol, variables)
    version = FLEX_BLOB_VERSION
    if protocol >= FLEX_VAR_PROTOCOL and any(
        r.get("rx_var") or r.get("tx_setflag") or r.get("cond_flag")
        for r in records
    ):
        version = FLEX_BLOB_VERSION_VAR
    elif protocol >= FLEX_AUX_PROTOCOL and any(
        r.get("rx_aux_event") or r.get("tx_aux") or r.get("cond_aux")
        for r in records
    ):
        version = FLEX_BLOB_VERSION_AUX
    doc: dict[str, Any] = {
        "kind": "code_master_flex_logic",
        # format — разметка номеров доп. каналов в JSON-правилах
        # (aux_channels.AUX_MAP_VERSION): 2 = новая нумерация
        # 1..7. Не путать с версией контейнера FLXB (байт 4).
        "format": AUX_MAP_VERSION,
        "rules": rules,
    }
    vars_snapshot = ctx.vars_json()
    if any(vars_snapshot[k] for k in vars_snapshot if k != "flags") \
            or vars_snapshot["flags"]:
        # Определения, реально затронутые компиляцией (включая
        # ссылки нескомпилированных программ) — сверка хвоста в
        # analyze_device_flex идёт тем же путём, что и запись.
        doc["vars"] = vars_snapshot
    json_part = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    packed = bytearray()
    packed += FLEX_BLOB_MAGIC
    packed += bytes((version, len(records) & 0xFF, 0, 0))
    packed += len(json_part).to_bytes(4, "little")
    packed += json_part
    for record in records:
        packed += pack_trigger(record)
    return bytes(packed)


def parse_flex_blob(blob: bytes) -> tuple[bytes, int] | None:
    """Разбор контейнера FLXB → (JSON-часть, число записей).

    Проверяет ТОЛЬКО исполняемый контракт — то же, что
    firmware/application/Src/trigger.c::append_flex: магия, версия
    контейнера, count ≤ FLEX_MAX_RECORDS (отказ, не обрезание),
    json_len > 0, точная длина 12 + json_len + count*90 (лишний
    хвост и недостача — отказ), каждая запись — валидная trigger_t
    v3 по контракту прошивки (магия/формат/CRC8/поля).

    Содержимое JSON-части здесь НЕ проверяется — прошивка её не
    читает, и расхождение «ПК отверг, МК исполняет» создавало бы
    двойного владельца (ревью CM-R6). Валидность JSON-схемы —
    отдельная зона: parse_flex_rules() для конфигурации ПК и
    analyze_device_flex() для владельца исполнения."""
    if (
        not blob
        or len(blob) < _FLXB_HEADER_SIZE
        or blob[:4] != FLEX_BLOB_MAGIC
        or blob[4] not in FLEX_BLOB_VERSIONS
    ):
        return None
    count = blob[5]
    if count > FLEX_MAX_RECORDS:
        return None
    json_len = int.from_bytes(blob[8:12], "little")
    if json_len == 0 or json_len > len(blob) - _FLXB_HEADER_SIZE:
        return None
    rec_off = _FLXB_HEADER_SIZE + json_len
    if len(blob) - rec_off != count * TRIGGER_SIZE:
        return None
    for i in range(count):
        start = rec_off + i * TRIGGER_SIZE
        if not record_fields_valid(blob[start:start + TRIGGER_SIZE]):
            return None
    return blob[_FLXB_HEADER_SIZE:rec_off], count


def _parse_flex_doc(json_part: bytes) -> dict[str, Any] | None:
    """JSON-часть контейнера → документ или None."""
    try:
        doc = json.loads(json_part.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def parse_flex_rules(json_part: bytes) -> list[dict[str, Any]] | None:
    """JSON-часть контейнера → список правил, если схема валидна.

    Схема (ревью CM-R7): объект с ключом «rules» — список словарей;
    в каждом правиле события/условия/действия, если заданы, —
    списки словарей (или легаси-поля event/condition/action —
    словари), «active» — bool, «title» — строка. Повреждённый
    элемент отвергается здесь, а не поздним исключением в UI."""
    doc = _parse_flex_doc(json_part)
    if doc is None:
        return None
    rules = doc.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            return None
        for key in ("events", "conditions", "actions"):
            value = rule.get(key)
            if value is not None and not (
                isinstance(value, list)
                and all(isinstance(e, dict) for e in value)
            ):
                return None
        for key in ("event", "condition", "action"):
            value = rule.get(key)
            if value is not None and not isinstance(value, dict):
                return None
        if "active" in rule and not isinstance(rule["active"], bool):
            return None
        if "title" in rule and not isinstance(rule["title"], str):
            return None
    return rules


def analyze_device_flex(blob: bytes) -> dict[str, Any]:
    """Классификация содержимого области FLXH — владелец исполнения
    программ ГЛ (ревью CM-R5..CM-R7).

    state:
      «no-tail»  — исполняемого хвоста нет (не FLXB или повреждён):
                   прошивка такой контейнер отвергает целиком →
                   камень исполняет НИЧЕГО, владелец — ПК;
      «conflict» — хвост валиден и загружается прошивкой, но
                   соответствие его правилам недоказуемо: JSON
                   не читается по схеме либо байты хвоста не совпали
                   с компиляцией JSON-правил. Владелец — камень
                   (ПК вслепую не включается поверх работающего
                   хвоста); путь восстановления — повторная запись;
      «verified» — хвост валиден И побайтово совпадает с
                   детерминированной компиляцией правил из JSON:
                   камень исполняет именно эти программы (множество
                   managed — индексы правил на камне).
    Ключи rules/managed заполнены только когда JSON прочитан."""
    parsed = parse_flex_blob(blob)
    if parsed is None:
        return {"state": "no-tail", "rules": None, "managed": []}
    json_part, count = parsed
    rules = parse_flex_rules(json_part)
    if rules is None:
        # Хвост исполняется, а конфигурация не читается — нельзя ни
        # подтвердить владение, ни включить ПК вслепую.
        return {"state": "conflict", "rules": None, "managed": []}
    actual_tail = blob[_FLXB_HEADER_SIZE + len(json_part):]
    doc = _parse_flex_doc(json_part) or {}
    # Хвост мог быть записан любой ревизией писателя: кандидаты —
    # по версии контейнера, чтобы «мёртвые» ветки переменных не
    # превращали PC-программы писателя протокола ≤13 в managed:
    # v3 → 14 (определения из снимка «vars»), v1/v2 → 13 и 12.
    blob_version = blob[4]
    candidates = [14] if blob_version >= FLEX_BLOB_VERSION_VAR else []
    candidates += [13, 12]
    vars_snapshot = doc.get("vars")
    managed: list[bool] = []
    for proto in candidates:
        expected, managed = compile_rules(
            rules, protocol=proto,
            variables=(
                vars_snapshot if isinstance(vars_snapshot, dict)
                else None
            ),
        )
        if actual_tail == b"".join(pack_trigger(r) for r in expected):
            break
    else:
        try:
            aux_map_version = int(doc.get("format", 1) or 1)
        except (TypeError, ValueError):
            aux_map_version = 1
        return {
            "state": "conflict",
            # Черновик для диагностики показываем в новой
            # нумерации — как у verified ниже.
            "rules": migrate_rules(rules, aux_map_version),
            "managed": [],
        }
    # JSON format<2 — легаси-разметка номеров доп. каналов (1..4 =
    # PC10/PC11/PC12/PA15): для UI правила переводятся в новую
    # нумерацию, физический пин сохраняется (CM-HW-02). Сверка хвоста
    # выше шла по исходным правилам — как записаны.
    doc = _parse_flex_doc(json_part) or {}
    try:
        aux_map_version = int(doc.get("format", 1) or 1)
    except (TypeError, ValueError):
        aux_map_version = 1
    return {
        "state": "verified",
        "rules": migrate_rules(rules, aux_map_version),
        "managed": managed,
    }


def flexb_header_valid(header: bytes, blob_len: int) -> bool:
    """Геометрическая проверка FLXB по первым байтам блоба
    (storage_peek): магия, версия, число записей и json_len
    согласуются с полной длиной области.

    Только геометрия — записи и JSON не проверяются. Владелец
    исполнения устанавливается по полному содержимому через
    parse_flex_blob; эта функция — быстрый прё-фильтр."""
    if len(header) < _FLXB_HEADER_SIZE:
        return False
    count = header[5]
    json_len = int.from_bytes(header[8:12], "little")
    return (
        header[:4] == FLEX_BLOB_MAGIC
        and header[4] in FLEX_BLOB_VERSIONS
        and count <= FLEX_MAX_RECORDS
        and json_len > 0
        and _FLXB_HEADER_SIZE + json_len + count * TRIGGER_SIZE
        <= max(int(blob_len), 0)
    )


def extract_flex_json(blob: bytes) -> bytes:
    """JSON-часть блоба FLXH: из контейнера FLXB или весь блоб,
    если он старого (чисто JSON) формата."""
    parsed = parse_flex_blob(blob)
    if parsed is None:
        return blob
    return parsed[0]


def managed_flags(
    rules: list[dict[str, Any]],
    protocol: int,
    variables: dict[str, Any] | None = None,
) -> list[bool]:
    """Флаги «программа исполняется камнем» для UI-пропуска.

    Без прошивки protocol>=12 (без разбора FLXB) камень записей не
    видит — приложение исполняет все программы, как раньше."""
    if protocol < 12:
        return [False] * len(rules)
    _records, managed = compile_rules(
        rules, protocol=protocol, variables=variables
    )
    return managed
