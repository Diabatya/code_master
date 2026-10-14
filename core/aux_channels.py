"""Нумерация доп. каналов выхода и миграция конфигураций (CM-HW-02).

НОВАЯ карта (протокол 13, CMD_AUX_SET_EXT, aux_out.h):
    1 = PA14 — SWCLK, канал работает только при бите владения
    2 = PA15 — бывший OUT4
    3 = PC10 — бывший OUT1
    4 = PC11 — бывший OUT2
    5 = PC12 — бывший OUT3
    6 = PD2  — конфликт с CAN2_TERM (бит владения)
    7 = PB3  — конфликт с CAN1_TERM/JTAG (бит владения)

ЛЕГАСИ-карта (протокол <=12, CMD_AUX_SET 0xD3): 1..4 = PC10/PC11/
PC12/PA15. Старые приложения продолжают ездить по тем же физическим
пинам — молчаливой переадресации каналов нет, переводятся только
при записи/чтении (NEW_TO_LEGACY/LEGACY_TO_NEW).

aux_map_version — пометка разметки номеров в сохранённых данных:
    1/отсутствует — конфигурация старой нумерации (мигрируется);
    2 — новая нумерация. Миграция версионируется и идемпотентна:
    повторный прогон по уже мигрированным данным ничего не меняет.
"""

from __future__ import annotations

from typing import Any

AUX_CH_COUNT = 7

# Биты владения спорными пинами (зеркало aux_out.h AUX_OWN_*).
AUX_OWN_PA14 = 0x01
AUX_OWN_PD2 = 0x20
AUX_OWN_PB3 = 0x40
AUX_OWN_PINS = AUX_OWN_PA14 | AUX_OWN_PD2 | AUX_OWN_PB3

AUX_MAP_VERSION = 2

# Физические пины по новой нумерации — для UI-подписей и документации.
AUX_PINS = {
    1: "PA14", 2: "PA15", 3: "PC10", 4: "PC11",
    5: "PC12", 6: "PD2", 7: "PB3",
}

# Легаси-нумерация → новая: старый 1→3, 2→4, 3→5, 4→2 (физический
# пин сохраняется). Значения 5..8 старой разметки физического пина
# не имели (устройств с такими выходами не было) — не переводятся.
LEGACY_TO_NEW = {1: 3, 2: 4, 3: 5, 4: 2}
NEW_TO_LEGACY = {new: old for old, new in LEGACY_TO_NEW.items()}


def to_new_channel(channel: int, aux_map_version: int = 1) -> int:
    """Номер канала из данных разметки ``aux_map_version`` → новая."""
    if aux_map_version >= AUX_MAP_VERSION:
        return channel
    return LEGACY_TO_NEW.get(channel, channel)


def to_legacy_channel(channel: int) -> int | None:
    """Новый номер → легаси-номер для прошивок протокола <=12.

    None — у канала нет физического пина в старой прошивке
    (1=PA14, 6=PD2, 7=PB3 появились только в новой разметке)."""
    return NEW_TO_LEGACY.get(channel)


def migrate_rule(rule: dict[str, Any],
                 aux_map_version: int = 1) -> dict[str, Any]:
    """Программа ГЛ из данных старой разметки → новая нумерация.

    Переводятся «channel» aux-событий/условий и «aux_channel»
    aux-действий; кадровые поля (channel CAN1/CAN2) не трогаются —
    у события/условия перевод только при type == «aux»."""
    if aux_map_version >= AUX_MAP_VERSION or not isinstance(rule, dict):
        return rule
    out = dict(rule)
    for key in ("events",):
        items = rule.get(key)
        if isinstance(items, list):
            out[key] = [
                {**e, "channel": LEGACY_TO_NEW.get(
                    _int(e.get("channel")), _int(e.get("channel")))}
                if isinstance(e, dict) and e.get("type") == "aux" else e
                for e in items
            ]
    conds = rule.get("conditions")
    if isinstance(conds, list):
        out["conditions"] = [
            {**c, "channel": LEGACY_TO_NEW.get(
                _int(c.get("channel")), _int(c.get("channel")))}
            if isinstance(c, dict) and c.get("type") == "aux" else c
            for c in conds
        ]
    actions = rule.get("actions")
    if isinstance(actions, list):
        out["actions"] = [
            {**a, "aux_channel": LEGACY_TO_NEW.get(
                _int(a.get("aux_channel")), _int(a.get("aux_channel")))}
            if isinstance(a, dict)
            and (a.get("type") == "aux" or a.get("aux_enabled"))
            else a
            for a in actions
        ]
    # Легаси-одиночные поля event/condition/action (до списков).
    ev = rule.get("event")
    if isinstance(ev, dict) and ev.get("type") == "aux":
        out["event"] = {
            **ev,
            "channel": LEGACY_TO_NEW.get(
                _int(ev.get("channel")), _int(ev.get("channel"))),
        }
    cond = rule.get("condition")
    if isinstance(cond, dict) and cond.get("type") == "aux":
        out["condition"] = {
            **cond,
            "channel": LEGACY_TO_NEW.get(
                _int(cond.get("channel")), _int(cond.get("channel"))),
        }
    act = rule.get("action")
    if isinstance(act, dict) and act.get("aux_enabled"):
        out["action"] = {
            **act,
            "aux_channel": LEGACY_TO_NEW.get(
                _int(act.get("aux_channel")), _int(act.get("aux_channel"))),
        }
    return out


def migrate_rules(rules: list[dict[str, Any]] | None,
                  aux_map_version: int = 1) -> list[dict[str, Any]] | None:
    """Список программ → новая нумерация (идемпотентно)."""
    if aux_map_version >= AUX_MAP_VERSION or not isinstance(rules, list):
        return rules
    return [migrate_rule(r, aux_map_version) for r in rules]


def migrate_aux_variables(aux_configs: list[dict[str, Any]] | None,
                          aux_map_version: int = 1
                          ) -> list[dict[str, Any]] | None:
    """Колонка «Доп канал» вкладки Переменных — поле «channel»."""
    if aux_map_version >= AUX_MAP_VERSION or not isinstance(aux_configs, list):
        return aux_configs
    out: list[dict[str, Any]] = []
    for cfg in aux_configs:
        if isinstance(cfg, dict) and cfg.get("type") == "aux":
            cfg = {
                **cfg,
                "channel": LEGACY_TO_NEW.get(
                    _int(cfg.get("channel")), _int(cfg.get("channel"))),
            }
        out.append(cfg)
    return out


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
