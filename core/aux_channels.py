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


# ---- ШИМ: единая сетка квантования ПК/МК (CM-N6) -------------------
# Программный ШИМ на HAL_GetTick: период и фазы — целые миллисекунды.
# Достижимые заполнения — решётка k*100/period; firmware округляет
# запрошенный duty до ближайшего узла, а фактические фазы отдаёт в
# CMD_AUX_GET. Формула зеркалит aux_out.c::aux_pwm_period_ms/Set.
AUX_PWM_MAX_FREQ_HZ = 500
AUX_PWM_MIN_PERIOD_MS = 2
AUX_MAX_DURATION_MS = 0x7FFFFFFF


def quantize_pwm(freq_hz: int, duty_pct: int) -> tuple[int, int, int] | None:
    """(period_ms, on_ms, off_ms) фактического сигнала или None.

    None — параметры недостижимы (freq вне 1..500, duty вне 0..100).
    Иначе — та же сетка, что применит прошивка: duty квантуется до
    ближайшего узла on = round(period*duty/100); на 500 Гц узлы —
    только 0/50/100%."""
    if not (1 <= int(freq_hz) <= AUX_PWM_MAX_FREQ_HZ):
        return None
    if not (0 <= int(duty_pct) <= 100):
        return None
    period = max(AUX_PWM_MIN_PERIOD_MS, (1000 + freq_hz // 2) // freq_hz)
    on = min(period, (period * duty_pct + 50) // 100)
    return period, int(on), int(period - on)


def effective_duty(freq_hz: int, duty_pct: int) -> float | None:
    """Фактическое заполнение после квантования (%) — для отображения
    «применено on/period», не «запрошено». None — недостижимо."""
    q = quantize_pwm(freq_hz, duty_pct)
    if q is None:
        return None
    period, on, _off = q
    return on * 100.0 / period


def aux_pin_conflict(channel: int) -> str | None:
    """Причина ограничения доступности канала для UI (CM-N4/CM-N7):
    None — свободен; иначе текст конфликта. PB3/PD2 физически едут на
    транзисторы терминаторов на существующих платах — до подтверждения
    новой разводки канал на них = переключение терминатора."""
    return {
        1: "SWCLK (SWD отладка) — включение отключает отладчик до reset",
        6: "PD2 на транзисторе терминатора CAN2 (плата ≤v1.x)",
        7: "PB3 на транзисторе терминатора CAN1 (плата ≤v1.x)",
    }.get(int(channel))


# ---- Настройки выхода переменной → действие ГЛ (CM-N7) -------------

def resolve_aux_from_vars(
    action: dict[str, Any], aux_configs: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Действие «Доп канал» с ``aux_use_var``: режим и параметры ШИМ
    берутся из настроек выхода переменной того же канала
    (``out_mode``/``pwm_freq``/``pwm_duty`` — см. _AuxDialog).
    Переменной нет — явные параметры действия сохраняются (честный
    override остаётся видимым в редакторе).
    ``aux_resolved`` — действие уже несёт применённый снимок
    параметров: повторный резолв не выполняется (CM-N14).
    Возвращает новый dict — входное действие не мутируется."""
    if not action.get("aux_use_var") or action.get("aux_resolved"):
        return action
    channel = _int(action.get("aux_channel", 1)) or 1
    for cfg in aux_configs or []:
        if cfg.get("type") != "aux":
            continue
        if _int(cfg.get("channel")) != channel:
            continue
        out = dict(action)
        out_mode = str(cfg.get("out_mode", "on") or "on")
        out["aux_mode"] = (
            out_mode if out_mode in ("off", "on", "pwm") else "on"
        )
        out["aux_pwm_freq"] = _int(cfg.get("pwm_freq"))
        out["aux_pwm_duty"] = _int(cfg.get("pwm_duty"))
        # Маркер применённого снимка (CM-N14): действие уже
        # содержит резолвнутые параметры — повторный резолв по
        # черновым настройкам переменных запрещён. Правки настроек
        # выхода вступают в силу только следующей записью в МК.
        out["aux_resolved"] = True
        # Срок работы из действия сохраняется: настройки переменной
        # задают форму сигнала, длительность — вопрос программы.
        return out
    return action


def resolve_rule_actions(
    rule: dict[str, Any], aux_configs: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Правило с резолвом «из настроек выхода» в КАЖДОМ действии.

    get_config отдаёт два представления действий: канонический список
    ``actions`` (его читает компилятор) и легаси-объединённый
    ``action`` (его читает PC-runtime). Резолвить надо оба — иначе
    исполняемый хвост FLXB и JSON-снимок расходятся (CM-N10).
    Возвращает новый dict — входное правило не мутируется."""
    out = dict(rule)
    actions = rule.get("actions")
    if isinstance(actions, list) and actions:
        out["actions"] = [
            resolve_aux_from_vars(a, aux_configs) if isinstance(a, dict) else a
            for a in actions
        ]
    legacy = rule.get("action")
    if isinstance(legacy, dict) and legacy:
        out["action"] = resolve_aux_from_vars(legacy, aux_configs)
    return out


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
