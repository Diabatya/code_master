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

from core.trigger_protocol import TRIGGER_SIZE, pack_trigger
from models.utils import hex_to_int

FLEX_BLOB_MAGIC = b"FLXB"
FLEX_BLOB_VERSION = 1
FLEX_MAX_RECORDS = 16  # должен совпадать с trigger.h

_FLXB_HEADER_SIZE = 12

# Варианты событий/действий, которые умеет движок триггеров.
_EVENT_NONE = "none"
_EVENT_FRAME = "frame"
_EVENT_POWER = "power"
_EVENT_PROGRAM = "program"
_ACT_FRAME = "frame"
_COND_NONE = "none"


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


def compile_rule(rule: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Программа ГЛ → список записей trigger_t (словари для pack_trigger).

    None — программа на камне не выражается (исполняет приложение).
    [] — программа компилируется, но записей не порождает (неактивна,
    нет рабочих событий или действий)."""
    if not rule.get("active", False):
        return []
    events, conditions, actions = _rule_parts(rule)

    # Условия ПРИ на камне не поддерживаются — переменные/флаги/доп.
    # каналы живут только в приложении.
    for cond in conditions:
        ctype = cond.get("type", _COND_NONE)
        if ctype not in (None, "", _COND_NONE):
            return None

    rx_parts: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        rx = _event_to_rx(event)
        if rx is None:
            return None
        if rx:
            rx_parts.append((idx, rx))
    if not rx_parts:
        return []
    # Пересекающиеся события: ИЛИ-семантика ПК (break на первом
    # совпавшем) в записях не выражается — на камне сработали бы обе
    # записи и действия исполнились бы дважды. Такая программа
    # остаётся на ПК (ревью CM-03).
    for i in range(len(rx_parts)):
        for j in range(i + 1, len(rx_parts)):
            if _rx_overlap(rx_parts[i][1], rx_parts[j][1]):
                return None
    # rx_cond_idx упаковывается в rx_flags битах 2..4 — максимум 7.
    if max(idx for idx, _ in rx_parts) > 7:
        return None

    tx_parts: list[tuple[int, dict[str, Any]]] = []
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            return None
        # «Задержка» выполнения и «Прервать если» — только на ПК.
        if int(action.get("exec_delay", 0) or 0) > 0:
            return None
        abort = action.get("abort") or {}
        if abort.get("type") not in (None, "", _EVENT_NONE):
            return None
        # Действия не-фреймового типа (переменные, флаги, доп. каналы,
        # кэш) делают программу некомпилируемой целиком.
        atype = action.get("type", _ACT_FRAME)
        non_frame = (
            str(action.get("var", "")).strip()
            or str(action.get("flag_var", "")).strip()
            or str(action.get("cachevar", "")).strip()
            or action.get("aux_enabled")
            or action.get("cache_enabled")
            or action.get("cv_op")
        )
        if atype not in (None, "", _ACT_FRAME, "none") or non_frame:
            return None
        tx = _action_to_tx(action)
        if tx is None:
            return None
        if tx:
            tx_parts.append((idx, tx))
    if not tx_parts:
        return []

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
            # «X из кадра события» — флаг rx_flags бит 5 + маска
            # подстановки в src_from (у скомпилированных записей
            # кэш-фильтр src_* не используется).
            record["rx_tx_event_data"] = bool(subst)
            records.append(record)
    return records


def compile_rules(
    rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[bool]]:
    """Все программы → (плоский список записей, флаги «исполняет МК»).

    Записи укладываются до FLEX_MAX_RECORDS; программа, не влезшая
    целиком, остаётся на исполнении приложением."""
    per_rule: list[list[dict[str, Any]] | None] = []
    for rule in rules:
        try:
            per_rule.append(compile_rule(rule))
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


def build_flex_blob(rules: list[dict[str, Any]]) -> bytes:
    """FLXB-контейнер для блоба FLXH: JSON-конфигурация + хвост из
    скомпилированных записей trigger_t, исполняемых прошивкой."""
    records, _managed = compile_rules(rules)
    json_part = json.dumps(
        {
            "kind": "code_master_flex_logic",
            "format": 1,
            "rules": rules,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    packed = bytearray()
    packed += FLEX_BLOB_MAGIC
    packed += bytes((FLEX_BLOB_VERSION, len(records) & 0xFF, 0, 0))
    packed += len(json_part).to_bytes(4, "little")
    packed += json_part
    for record in records:
        packed += pack_trigger(record)
    return bytes(packed)


def parse_flex_blob(blob: bytes) -> tuple[bytes, int] | None:
    """Разбор контейнера FLXB → (JSON-часть, число записей).

    None — блоб не FLXB либо заголовок/границы повреждены: по такому
    блобу нельзя считать программы исполняемыми камнем. Магия «FLXB»
    сама по себе не доказывает валидность исполняемой части — версия
    контейнера, длина JSON, число записей и границы проверяются
    (ревью CM-05)."""
    if (
        not blob
        or len(blob) < _FLXB_HEADER_SIZE
        or blob[:4] != FLEX_BLOB_MAGIC
        or blob[4] != FLEX_BLOB_VERSION
    ):
        return None
    count = blob[5]
    if count > FLEX_MAX_RECORDS:
        return None
    json_len = int.from_bytes(blob[8:12], "little")
    rec_off = _FLXB_HEADER_SIZE + json_len
    if (
        json_len == 0
        or rec_off > len(blob)
        or len(blob) - rec_off < count * TRIGGER_SIZE
    ):
        return None
    return blob[_FLXB_HEADER_SIZE:rec_off], count


def flexb_header_valid(header: bytes, blob_len: int) -> bool:
    """Проверка FLXB по первым байтам блоба (storage_peek): магия,
    версия, число записей и длина JSON согласуются с полной длиной
    области — без вычитки всего блоба."""
    if len(header) < _FLXB_HEADER_SIZE:
        return False
    fake_len = max(int(blob_len), _FLXB_HEADER_SIZE)
    fake = bytes(header[:_FLXB_HEADER_SIZE]) + b"\x00" * (
        fake_len - _FLXB_HEADER_SIZE
    )
    return parse_flex_blob(fake) is not None


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
    _records, managed = compile_rules(rules)
    return managed
