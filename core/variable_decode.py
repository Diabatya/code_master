"""Единый контракт извлечения RAW и декодирования численной
переменной (CM-N3): отображение в «Переменных» и численный рантайм
Гибкой логики обязаны собирать сырое значение одинаково —
одинаковый набор байтов, одинаковый порядок старшинства (SBLP),
одинаковая полнота поля.

Правила:
- ``bytes`` — индексы байтов кадра, поле неполное (кадр короче хотя
  бы одного выбранного байта) → значение НЕВАЛИДНО, частичная
  сумма/склейка не используется;
- ``sblp`` "be"/"le" — сборка по порядку старшинства; иначе —
  сумма байтов (легаси-контракт отчёта мастера);
- ``out_range == "steering"`` — значение только из
  ``steering_decoder``: конфликт, отсутствие класса/калибровки,
  неполный кадр и просроченные данные дают valid=False и не
  удовлетворяют численным условиям ГЛ;
- устаревание: ``stale_ms`` конфига декодера — политика свежести
  значения для условий ГЛ (0 = не устаревает).
"""

from __future__ import annotations

from typing import Any

from core import steering_decoder


def sblp_decode(window: bytes, endian: str) -> int | None:
    """Сборка числа из байтов по порядку старшинства (копия контракта
    ui._sblp_decode — модуль не зависит от Qt).

    ``"be"`` — «старший байт слева» (06 04 → 6·256+4);
    ``"le"`` — «старший байт справа» (09 03 → 9 + 3·256)."""
    if len(window) < 2:
        return None
    if endian == "le":
        window = window[::-1]
    return int.from_bytes(window, "big")


def extract_var_raw(
    cfg: dict[str, Any], data: bytes | None
) -> tuple[int | None, bool]:
    """Сырое значение численной переменной из кадра.

    Возвращает ``(raw, complete)``: ``complete=False`` — поле
    байтов не покрыто кадром целиком; тогда ``raw=None`` и
    декодировать/сравнивать его нельзя."""
    used = [int(p) for p in (cfg.get("bytes") or [])
            if isinstance(p, int) or str(p).strip().lstrip("-").isdigit()]
    if not used or not data:
        return None, False
    if any(p < 0 or p >= len(data) for p in used):
        return None, False
    sblp = str(cfg.get("sblp", "") or "")
    if sblp:
        window = bytes(data[p] for p in used)
        decoded = sblp_decode(window, sblp)
        return decoded, decoded is not None
    return sum(data[p] for p in used), True


def decode_var_value(
    cfg: dict[str, Any], data: bytes | None
) -> dict[str, Any]:
    """Полное декодирование численной переменной по кадру.

    Результат::
        complete  bool         — поле байтов покрыто кадром
        valid     bool         — значение пригодно для условий ГЛ
        value     float | None — численное значение (угол/интерполир.)
        raw       int | None
        status    str          — ok / incomplete / no-data /
                                 конфликты декодера / no-calibration…

    Нестеering-режимы возвращают value=None — маппинг по точкам
    графика у отображения и ГЛ исторически разный (экстраполяция vs
    насыщение) и остаётся на вызывающей стороне; единым является
    только контракт RAW и отказ при неполном поле."""
    used = cfg.get("bytes") or []
    if not used:
        return {"complete": False, "valid": False, "value": None,
                "raw": None, "raw_hex": "", "status": "no-field"}
    raw, complete = extract_var_raw(cfg, data)
    if not complete:
        return {"complete": False, "valid": False, "value": None,
                "raw": None, "raw_hex": "",
                "status": ("no-data" if not data else "incomplete")}
    if str(cfg.get("out_range", "") or "") == "steering":
        res = steering_decoder.decode_raw(raw, cfg.get("steering"))
        return {"complete": True, "valid": bool(res["valid"]),
                "value": res["angle"], "raw": raw,
                "raw_hex": res["raw_hex"], "status": res["status"]}
    return {"complete": True, "valid": True, "value": None,
            "raw": raw, "raw_hex": f"0x{raw:X}", "status": "ok"}


def is_steering(cfg: dict[str, Any]) -> bool:
    """Переменная декодируется декодером угла руля."""
    return str(cfg.get("out_range", "") or "") == "steering"


def stale_ms(cfg: dict[str, Any]) -> int:
    """Политика устаревания значения в мс (0 — не устаревает)."""
    if not is_steering(cfg):
        return 0
    conf = steering_decoder.normalize_config(cfg.get("steering"))
    return int(conf.get("stale_ms", 0) or 0)
