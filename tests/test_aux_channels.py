"""Тесты core/aux_channels — нумерация доп. каналов и миграция
легаси-разметки (CM-HW-02).

Физическая карта: новый 1=PA14, 2=PA15, 3=PC10, 4=PC11, 5=PC12,
6=PD2, 7=PB3; легаси 1..4 = PC10/PC11/PC12/PA15 → новые 3/4/5/2.
"""

from __future__ import annotations

import pytest

from core.aux_channels import (
    AUX_CH_COUNT,
    AUX_MAP_VERSION,
    AUX_MAX_DURATION_MS,
    AUX_PINS,
    LEGACY_TO_NEW,
    NEW_TO_LEGACY,
    aux_pin_conflict,
    effective_duty,
    migrate_aux_variables,
    migrate_rule,
    migrate_rules,
    quantize_pwm,
    resolve_aux_from_vars,
    to_legacy_channel,
    to_new_channel,
)


class TestChannelMaps:
    def test_legacy_to_new_physical_pins(self) -> None:
        # Физический пин сохраняется: легаси-1 был PC10 — остаётся им.
        assert LEGACY_TO_NEW == {1: 3, 2: 4, 3: 5, 4: 2}
        for new in LEGACY_TO_NEW.values():
            assert AUX_PINS[new] in ("PC10", "PC11", "PC12", "PA15")

    def test_new_to_legacy_inverse(self) -> None:
        for old, new in LEGACY_TO_NEW.items():
            assert NEW_TO_LEGACY[new] == old

    def test_new_only_channels_have_no_legacy(self) -> None:
        # PA14/PD2/PB3 не существовали в старой прошивке.
        assert to_legacy_channel(1) is None
        assert to_legacy_channel(6) is None
        assert to_legacy_channel(7) is None
        assert to_legacy_channel(3) == 1

    def test_to_new_passthrough_on_new_markup(self) -> None:
        assert to_new_channel(1, AUX_MAP_VERSION) == 1
        assert to_new_channel(7, AUX_MAP_VERSION) == 7

    def test_to_new_legacy_markup(self) -> None:
        assert to_new_channel(1, 1) == 3
        assert to_new_channel(4, 1) == 2
        # Каналов 5+ в легаси не было — не переводятся.
        assert to_new_channel(6, 1) == 6


class TestMigrateRule:
    def test_aux_event_migrated(self) -> None:
        rule = {
            "active": True,
            "events": [{"type": "aux", "channel": 1, "state": 1}],
        }
        out = migrate_rule(rule, aux_map_version=1)
        assert out["events"][0]["channel"] == 3

    def test_frame_event_untouched(self) -> None:
        rule = {
            "active": True,
            "events": [{"type": "frame", "channel": 1, "id": "0x100"}],
        }
        out = migrate_rule(rule, aux_map_version=1)
        assert out["events"][0]["channel"] == 1

    def test_aux_condition_migrated(self) -> None:
        rule = {
            "active": True,
            "events": [{"type": "frame", "id": "0x1"}],
            "conditions": [{"type": "aux", "channel": 4, "state": 1}],
        }
        out = migrate_rule(rule, aux_map_version=1)
        assert out["conditions"][0]["channel"] == 2

    def test_aux_action_migrated(self) -> None:
        rule = {
            "active": True,
            "events": [{"type": "frame", "id": "0x1"}],
            "actions": [
                {"type": "aux", "aux_channel": 2, "aux_mode": "on"},
                {"type": "frame", "id": "0x2"},
            ],
        }
        out = migrate_rule(rule, aux_map_version=1)
        assert out["actions"][0]["aux_channel"] == 4
        assert out["actions"][1].get("aux_channel") is None

    def test_aux_enabled_action_without_type(self) -> None:
        rule = {"action": {"aux_enabled": True, "aux_channel": 3}}
        out = migrate_rule(rule, aux_map_version=1)
        assert out["action"]["aux_channel"] == 5

    def test_can_channel_fields_not_migrated(self) -> None:
        # «channel» кадровых событий — номер CAN-канала, не aux.
        rule = {
            "event": {"type": "frame", "channel": 1, "id": "0x5"},
            "condition": {"type": "none"},
        }
        out = migrate_rule(rule, aux_map_version=1)
        assert out["event"]["channel"] == 1

    def test_idempotent(self) -> None:
        rule = {
            "events": [{"type": "aux", "channel": 1}],
            "actions": [{"type": "aux", "aux_channel": 2}],
        }
        once = migrate_rule(rule, aux_map_version=1)
        twice = migrate_rule(once, aux_map_version=AUX_MAP_VERSION)
        assert once == twice

    def test_new_markup_untouched(self) -> None:
        rule = {"events": [{"type": "aux", "channel": 1}]}
        assert migrate_rule(rule, aux_map_version=2) is rule


class TestMigrateCollections:
    def test_migrate_rules_list(self) -> None:
        rules = [
            {"events": [{"type": "aux", "channel": 1}]},
            {"events": [{"type": "frame", "channel": 0}]},
        ]
        out = migrate_rules(rules, aux_map_version=1)
        assert out[0]["events"][0]["channel"] == 3
        assert out[1]["events"][0]["channel"] == 0

    def test_migrate_rules_none(self) -> None:
        assert migrate_rules(None) is None
        assert migrate_rules("x") == "x"

    def test_migrate_aux_variables(self) -> None:
        aux = [
            {"type": "aux", "channel": 1},
            {"type": "flag", "channel": 1},
        ]
        out = migrate_aux_variables(aux, aux_map_version=1)
        assert out[0]["channel"] == 3
        assert out[1]["channel"] == 1

    def test_migrate_aux_variables_idempotent(self) -> None:
        aux = [{"type": "aux", "channel": 1}]
        once = migrate_aux_variables(aux, aux_map_version=1)
        twice = migrate_aux_variables(once, aux_map_version=AUX_MAP_VERSION)
        assert once == twice
        assert AUX_CH_COUNT == 7


# ================= CM-N6/N7: сетка ШИМ и настройки выхода =========


class TestPwmQuantization:
    """Сетка квантования ШИМ ПК == формуле aux_out.c (CM-N6)."""

    def test_500hz_grid(self) -> None:
        # Период 2 мс → достижимы только 0/50/100%.
        assert quantize_pwm(500, 50) == (2, 1, 1)
        assert quantize_pwm(500, 1) == (2, 0, 2)     # 1% → 0%
        assert quantize_pwm(500, 99) == (2, 2, 0)    # 99% → 100%
        assert quantize_pwm(500, 0) == (2, 0, 2)
        assert quantize_pwm(500, 100) == (2, 2, 0)

    def test_noninteger_period(self) -> None:
        # 333 Гц → round(1000/333)=3 мс; 50% → ближайший узел 2 мс.
        assert quantize_pwm(333, 50) == (3, 2, 1)

    def test_out_of_range(self) -> None:
        assert quantize_pwm(0, 50) is None
        assert quantize_pwm(501, 50) is None
        assert quantize_pwm(100, 101) is None
        assert quantize_pwm(100, -1) is None

    def test_effective_duty(self) -> None:
        assert effective_duty(500, 50) == 50.0
        assert effective_duty(333, 50) == pytest.approx(66.666, abs=0.01)
        assert effective_duty(0, 50) is None

    def test_duration_cap_matches_firmware(self) -> None:
        # INT32_MAX — та же граница, что AUX_MAX_DURATION_MS у МК.
        assert AUX_MAX_DURATION_MS == 0x7FFFFFFF


class TestPinConflicts:
    def test_free_channels(self) -> None:
        for ch in (2, 3, 4, 5):
            assert aux_pin_conflict(ch) is None

    def test_shared_pins_named(self) -> None:
        assert "SWCLK" in aux_pin_conflict(1)
        assert "CAN2" in aux_pin_conflict(6)
        assert "CAN1" in aux_pin_conflict(7)


class TestResolveAuxFromVars:
    """Действие ГЛ «из настроек выхода» (CM-N7)."""

    def _vars(self) -> list[dict]:
        return [
            {"type": "aux", "channel": 3, "out_mode": "pwm",
             "pwm_freq": 250, "pwm_duty": 40},
            {"type": "flag", "channel": 3, "storage": "ram"},
        ]

    def test_resolves_pwm_from_variable(self) -> None:
        action = {"aux_enabled": True, "aux_channel": 3,
                  "aux_use_var": True, "aux_mode": "on",
                  "aux_pwm_freq": 500, "aux_pwm_duty": 100}
        out = resolve_aux_from_vars(action, self._vars())
        assert out["aux_mode"] == "pwm"
        assert out["aux_pwm_freq"] == 250
        assert out["aux_pwm_duty"] == 40
        # Исходное действие не мутируется.
        assert action["aux_mode"] == "on"

    def test_off_on_modes(self) -> None:
        vars_ = [{"type": "aux", "channel": 5, "out_mode": "off"}]
        out = resolve_aux_from_vars(
            {"aux_channel": 5, "aux_use_var": True, "aux_mode": "on"},
            vars_,
        )
        assert out["aux_mode"] == "off"

    def test_missing_variable_keeps_override(self) -> None:
        action = {"aux_channel": 7, "aux_use_var": True,
                  "aux_mode": "pwm", "aux_pwm_freq": 100,
                  "aux_pwm_duty": 25}
        out = resolve_aux_from_vars(action, self._vars())
        assert out is action  # без переменной — явные параметры

    def test_flag_variable_not_aux(self) -> None:
        # Переменная-бит того же канала выходом не считается.
        out = resolve_aux_from_vars(
            {"aux_channel": 3, "aux_use_var": True, "aux_mode": "on"},
            [{"type": "flag", "channel": 3}],
        )
        assert out["aux_mode"] == "on"

    def test_no_use_var_noop(self) -> None:
        action = {"aux_channel": 3, "aux_mode": "off"}
        assert resolve_aux_from_vars(action, self._vars()) is action
