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
# Минимальный протокол, где прошивка исполняет aux-записи ГЛ.
FLEX_AUX_PROTOCOL = 13
FLEX_MAX_RECORDS = 16  # должен совпадать с trigger.h
AUX_CH_COUNT = 7  # должен совпадать с aux_out.h

_FLXB_HEADER_SIZE = 12

# Варианты событий/действий, которые умеет движок триггеров.
_EVENT_NONE = "none"
_EVENT_FRAME = "frame"
_EVENT_POWER = "power"
_EVENT_PROGRAM = "program"
_EVENT_AUX = "aux"
_ACT_FRAME = "frame"
_ACT_AUX = "aux"
_COND_NONE = "none"
_COND_AUX = "aux"

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
    if not 1 <= ch <= AUX_CH_COUNT:
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
    return {
        "tx_aux": True,
        "tx_channel": ch & 0xFF,
        "tx_dlc": mode & 0xFF,
        "tx_data": bytes(data),
        "delay_ms": delay & 0xFFFF,
        "tx_count": 1,
        "tx_interval_ms": 0,
    }


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
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Программа ГЛ → (записи trigger_t | None, причина отказа | None).

    allow_aux — прошивка протокола >=13 (FLXB v2) умеет события/
    условия/действия доп. каналов. Причины — для UI-бейджа
    «кто исполняет» (ревью CM-R4/R5)."""
    if not rule.get("active", False):
        return [], None
    events, conditions, actions = _rule_parts(rule)

    # Условие «доп. канал работает/не работает» — единственное,
    # переносимое на камень (proto>=13): одно на программу, проверка
    # логической активности в момент события (как ПК —
    # _condition_passed по зеркалу состояния). Переменные/флаги/
    # кэш-условия и комбинации — только на ПК.
    aux_cond: tuple[int, int] | None = None  # (канал, src_dlc)
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
        return None, "условие ПРИ — только на ПК"

    rx_parts: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            return None, "повреждённое событие — только на ПК"
        rx = _event_to_rx(event)
        if rx is None:
            return None, "событие не про CAN-кадр — только на ПК"
        if rx:
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
        # «Задержка» выполнения и «Прервать если» — только на ПК.
        if int(action.get("exec_delay", 0) or 0) > 0:
            return None, "задержка выполнения действия — только на ПК"
        abort = action.get("abort") or {}
        if abort.get("type") not in (None, "", _EVENT_NONE):
            return None, "прерыватель действия — только на ПК"
        # Действие «Доп. канал» переносится на камень с протокола 13 —
        # до валидации «не-фрейма», чтобы atype=aux доходил сюда.
        atype = action.get("type", _ACT_FRAME)
        if atype == _ACT_AUX or action.get("aux_enabled"):
            if not allow_aux:
                return None, "действие «доп. канал» — только на ПК"
            tx = _aux_action_to_tx(action)
            if tx is None:
                return None, "действие «доп. канал» повреждено — только на ПК"
            tx_parts.append((idx, tx))
            continue
        # Действия не-фреймового типа (переменные, флаги, кэш)
        # делают программу некомпилируемой целиком.
        non_frame = (
            str(action.get("var", "")).strip()
            or str(action.get("flag_var", "")).strip()
            or str(action.get("cachevar", "")).strip()
            or action.get("cache_enabled")
            or action.get("cv_op")
        )
        if atype not in (None, "", _ACT_FRAME, "none") or non_frame:
            return None, "действие не «отправка кадра» — только на ПК"
        tx = _action_to_tx(action)
        if tx is None:
            return None, "действие не «отправка кадра» — только на ПК"
        if tx:
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
            # «X из кадра события» — флаг rx_flags бит 5 + маска
            # подстановки в src_from (у скомпилированных записей
            # кэш-фильтр src_* не используется).
            record["rx_tx_event_data"] = bool(subst)
            records.append(record)
    return records, None


def compile_rule(
    rule: dict[str, Any],
    allow_aux: bool = False,
) -> list[dict[str, Any]] | None:
    """Программа ГЛ → список записей trigger_t (словари для pack_trigger).

    None — программа на камне не выражается (исполняет приложение).
    [] — программа компилируется, но записей не порождает (неактивна,
    нет рабочих событий или действий)."""
    records, _reason = _compile_rule(rule, allow_aux)
    return records


def executor_info(
    rules: list[dict[str, Any]],
    protocol: int,
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

    per_rule: list[tuple[list | None, str | None]] = []
    for rule in rules:
        try:
            per_rule.append(
                _compile_rule(rule, allow_aux=protocol >= FLEX_AUX_PROTOCOL)
            )
        except Exception:  # noqa: BLE001
            per_rule.append((None, "ошибка конфигурации — только на ПК"))

    listened = {
        str(event.get("var", "")).strip()
        for rule in rules
        for event in _rule_events(rule)
        if isinstance(event, dict) and event.get("type") == _EVENT_PROGRAM
    }

    out: list[dict[str, Any]] = []
    used = 0
    for i, rule in enumerate(rules):
        records, reason = per_rule[i]
        active = bool(rule.get("active"))
        if (
            records is not None
            and str(rule.get("title", "")).strip() in listened
        ):
            records = None
            reason = "зависит событие «программа начала работать» — только на ПК"
        if records is None:
            out.append({"managed": False, "active": active,
                        "reason": reason or "только на ПК"})
            continue
        if used + len(records) > FLEX_MAX_RECORDS:
            out.append({
                "managed": False,
                "active": active,
                "reason": "квота записей МК исчерпана (16)",
            })
            continue
        used += len(records)
        out.append({"managed": True, "active": active, "reason": None})
    return out


def compile_rules(
    rules: list[dict[str, Any]],
    protocol: int = 12,
) -> tuple[list[dict[str, Any]], list[bool]]:
    """Все программы → (плоский список записей, флаги «исполняет МК»).

    protocol >= 13 разрешает записи доп. каналов (FLXB v2). Записи
    укладываются до FLEX_MAX_RECORDS; программа, не влезшая целиком,
    остаётся на исполнении приложением."""
    allow_aux = protocol >= FLEX_AUX_PROTOCOL
    per_rule: list[list[dict[str, Any]] | None] = []
    for rule in rules:
        try:
            per_rule.append(compile_rule(rule, allow_aux))
        except Exception:  # noqa: BLE001
            # Битый элемент конфига не должен срывать запись всего
            # блоба — программа остаётся на исполнении приложения.
            per_rule.append(None)

    # Событие «программа <имя> начала работать» существует только в
    # рантайме ПК: программа, ушедшая на камень, стартует без уведомления
    # приложения — слушающая её программа никогда бы не сработала.
    # Целевая программа вместе со слушателем остаётся на ПК
    # (ревью CM-05).
    listened = {
        str(event.get("var", "")).strip()
        for rule in rules
        for event in _rule_events(rule)
        if isinstance(event, dict) and event.get("type") == _EVENT_PROGRAM
    }
    if listened:
        for i, rule in enumerate(rules):
            if (
                per_rule[i] is not None
                and str(rule.get("title", "")).strip() in listened
            ):
                per_rule[i] = None

    records: list[dict[str, Any]] = []
    managed: list[bool] = []
    for rule, rule_records in zip(rules, per_rule, strict=False):
        if rule_records is None:
            managed.append(False)
            continue
        if len(records) + len(rule_records) > FLEX_MAX_RECORDS:
            managed.append(False)
            continue
        records.extend(rule_records)
        managed.append(bool(rule_records) or bool(rule.get("active")))
    return records, managed


def build_flex_blob(rules: list[dict[str, Any]], protocol: int = 13) -> bytes:
    """FLXB-контейнер для блоба FLXH: JSON-конфигурация + хвост из
    скомпилированных записей trigger_t, исполняемых прошивкой.

    protocol — версия протокола целевой прошивки: >=13 компилирует и
    aux-записи; <13 пишет чисто кадровые программы (FLXB v1), чтобы
    блоб читался старой прошивкой после отката."""
    records, _managed = compile_rules(rules, protocol=protocol)
    version = FLEX_BLOB_VERSION
    if protocol >= FLEX_AUX_PROTOCOL and any(
        r.get("rx_aux_event") or r.get("tx_aux") or r.get("cond_aux")
        for r in records
    ):
        version = FLEX_BLOB_VERSION_AUX
    json_part = json.dumps(
        {
            "kind": "code_master_flex_logic",
            # format — разметка номеров доп. каналов в JSON-правилах
            # (aux_channels.AUX_MAP_VERSION): 2 = новая нумерация
            # 1..7. Не путать с версией контейнера FLXB (байт 4).
            "format": AUX_MAP_VERSION,
            "rules": rules,
        },
        ensure_ascii=False,
    ).encode("utf-8")
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
        or blob[4] not in (FLEX_BLOB_VERSION, FLEX_BLOB_VERSION_AUX)
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
    # Хвост мог быть записан любой ревизией писателя: сначала сверка
    # с компиляцией под текущий контракт (протокол 13, aux-записи
    # FLXB v2), затем — под легаси (12, aux-программы записей не
    # давали). Любое совпадение — verified, разница — conflict.
    expected, managed = compile_rules(rules, protocol=13)
    if actual_tail != b"".join(pack_trigger(r) for r in expected):
        expected, managed = compile_rules(rules, protocol=12)
        if actual_tail != b"".join(pack_trigger(r) for r in expected):
            conflict_rules = rules
            doc = _parse_flex_doc(json_part) or {}
            try:
                aux_map_version = int(doc.get("format", 1) or 1)
            except (TypeError, ValueError):
                aux_map_version = 1
            return {
                "state": "conflict",
                # Черновик для диагностики показываем в новой нумерации
                # — как у verified ниже.
                "rules": migrate_rules(conflict_rules, aux_map_version),
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
        and header[4] in (FLEX_BLOB_VERSION, FLEX_BLOB_VERSION_AUX)
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


def managed_flags(rules: list[dict[str, Any]], protocol: int) -> list[bool]:
    """Флаги «программа исполняется камнем» для UI-пропуска.

    Без прошивки protocol>=12 (без разбора FLXB) камень записей не
    видит — приложение исполняет все программы, как раньше."""
    if protocol < 12:
        return [False] * len(rules)
    _records, managed = compile_rules(rules, protocol=protocol)
    return managed
