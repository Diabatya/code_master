"""CM-F02: компилятор программ ГЛ — события/условия/действия по
переменным и командам «Управления» (контейнер FLXB v3, протокол 14).

Покрытие: RX_VAR-записи (динамические численные и привязочные),
слушатели фронта флагов (статические, именованные флаги), updater-
записи статических, псевдо-действие SETFLAG, условие COND_FLAG,
команды «Переменные управления» со свёрнутым расписанием, версии
контейнера, снимок vars в JSON и сверка владельца исполнения."""

import json

from core.flex_compiler import (
    FLEX_BLOB_VERSION,
    FLEX_BLOB_VERSION_AUX,
    FLEX_BLOB_VERSION_VAR,
    FLEX_MAX_RECORDS,
    analyze_device_flex,
    build_flex_blob,
    compile_rules,
    parse_flex_blob,
)
from core.trigger_protocol import (
    TRIGGER_F_COND_FLAG,
    TRIGGER_F_RX_VAR,
    TRIGGER_F_TX_SETFLAG,
    TRIGGER_VAR_CHAN_FLAG,
    pack_trigger,
    unpack_trigger,
)


def _frame_event(can_id: str = "123", channel: int = 2, data: str = "") -> dict:
    return {
        "type": "frame", "channel": channel, "extended": False,
        "id": can_id, "dlc": 8, "data": data, "fire_limit": 0,
    }


def _frame_action(
    can_id: str = "456", channel: int = 0, data: str = "01 02",
    delay: int = 0, count: int = 1, between: int = 0,
) -> dict:
    return {
        "type": "frame", "frame_enabled": True, "channel": channel,
        "extended": False, "id": can_id, "dlc": 8, "data": data,
        "delay": delay, "count": count, "between": between,
    }


def _rule(
    events=None, actions=None, conditions=None, active=True,
    title: str = "P",
) -> dict:
    return {
        "title": title, "active": active,
        "events": events if events is not None else [_frame_event()],
        "conditions": conditions if conditions is not None else [],
        "actions": actions if actions is not None else [_frame_action()],
    }


def _num_var(name: str = "V", **over) -> dict:
    """Численная динамическая переменная: sblp=le, байты 0-1."""
    var = {
        "name": name, "type": "num", "storage": "ram",
        "channel": 2, "extended": False, "id": "100",
        "bytes": [0, 1], "sblp": "le",
        "points": [[0, 0.0], [65535, 65535.0]],
    }
    var.update(over)
    return var


def _cache_var(name: str = "D", **over) -> dict:
    """Динамическая переменная-привязка (dyn_cache): байты 2-3,
    символьные состояния из таблицы."""
    var = {
        "name": name, "type": "dyn_cache", "storage": "ram",
        "channel": 2, "extended": False, "id": "200",
        "bytes": [2, 3],
        "points": [[1, "Нейтраль"], [2, "Драйв"]],
    }
    var.update(over)
    return var


def _static_var(name: str = "Зажигание", **over) -> dict:
    """Статическая переменная: кадр 0x300 ставит 1, 0x301 — 0."""
    var = {
        "name": name, "type": "static", "storage": "ram",
        "frames": [
            {"id": "300", "data": "01", "value": 1},
            {"id": "301", "data": "00", "value": 0},
        ],
    }
    var.update(over)
    return var


def _flag_var(name: str = "Блок", **over) -> dict:
    var = {"name": name, "type": "flag", "storage": "ram"}
    var.update(over)
    return var


def _command(name: str = "Старт", frames=None) -> dict:
    return {
        "type": "command", "name": name,
        "frames": frames if frames is not None else [
            {"channel": 0, "id": "600", "dlc": 2, "data": "AA BB",
             "delay_before_send": 10, "count": 2, "delay_between": 50,
             "next_delay": 100},
            {"channel": 1, "id": "601", "dlc": 1, "data": "CC",
             "delay_before_send": 20, "count": 1, "delay_between": 0,
             "next_delay": 0},
        ],
        "cache": {},
    }


def _vars(read=None, control=None, aux=None) -> dict:
    return {
        "read": read or [],
        "control": control or [],
        "aux": aux or [],
    }


# ------------------------------------------------------------------
# RX_VAR: динамические переменные
# ------------------------------------------------------------------


def test_num_var_event_compiles_rx_var() -> None:
    """«Значение > 100» численной → RX_VAR с cmp=gt по сырому полю."""
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "100", "dir": "gt",
    }])]
    recs, managed = compile_rules(rules, protocol=14,
                                  variables=_vars(read=[_num_var()]))
    assert managed == [True] and len(recs) == 1
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["rx_var"] is True
    assert rec["rx_id"] == 0x100
    m = rec["rx_data_mask"]
    # offset=0, len=2, endian=1 (le), cmp=1 (gt), kind=1 (num)
    assert m[:5] == bytes([0, 2, 1, 1, 1])
    assert int.from_bytes(rec["rx_data"], "little") == 100


def test_num_var_lt_threshold_maps_to_raw() -> None:
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "50", "dir": "lt",
    }])]
    recs, _m = compile_rules(rules, protocol=14,
                             variables=_vars(read=[_num_var()]))
    m = recs[0]["rx_data_mask"]
    assert m[3] == 2  # cmp=lt
    assert int.from_bytes(recs[0]["rx_data"], "little") == 50


def test_num_var_unreachable_threshold_dead_branch() -> None:
    """Порог выше максимума карты — на ПК событие не срабатывает
    никогда: записей нет, программа «скомпилирована» в пустое."""
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "99999", "dir": "gt",
    }])]
    recs, managed = compile_rules(rules, protocol=14,
                                  variables=_vars(read=[_num_var()]))
    assert recs == [] and managed == [True]


def test_dyn_cache_binding_compiles_eq_per_raw() -> None:
    """«== имя состояния» по привязке: запись eq на каждое сырое
    значение имени (OR-семантика имён с тем же raw на ПК — здесь
    разные записи срабатывают на разных кадрах, дубля нет)."""
    rules = [_rule(events=[{
        "type": "dyn", "var": "D", "value": "Драйв",
    }])]
    recs, managed = compile_rules(rules, protocol=14,
                                  variables=_vars(read=[_cache_var()]))
    assert managed == [True] and len(recs) == 1
    m = recs[0]["rx_data_mask"]
    # offset=2, len=2, endian=0 (BE-окно dyn_cache), cmp=0, kind=0
    assert m[:5] == bytes([2, 2, 0, 0, 0])
    assert int.from_bytes(recs[0]["rx_data"], "little") == 2


def test_dyn_var_missing_definition_is_pc_owned() -> None:
    """Без определения переменной событие — «мёртвая» ветка: на ПК
    не срабатывает никогда, записей не даёт."""
    rules = [_rule(events=[{
        "type": "dyn", "var": "GHOST", "value": "x",
    }])]
    recs, managed = compile_rules(rules, protocol=14, variables=_vars())
    assert recs == [] and managed == [True]


def test_var_event_blocked_below_proto14() -> None:
    """Протокол 13: переменные не компилируются — честный отказ,
    а не молчаливое исполнение только на ПК."""
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "1", "dir": "gt",
    }])]
    recs, managed = compile_rules(rules, protocol=13,
                                  variables=_vars(read=[_num_var()]))
    assert managed == [False]


def test_num_var_sum_mode_uncompilable() -> None:
    """Без SBLP на ПК значение — сумма байтов; на камне не
    выражается — программа остаётся на ПК."""
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "10", "dir": "gt",
    }])]
    _r, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(read=[_num_var(sblp="")]))
    assert managed == [False]


def test_degrees_var_stays_pc() -> None:
    """«Градусы» (руль): на ПК значение — знаковый декод по веткам
    с делителями/инверсией, сырой порог на камне искажал бы
    семантику (срабатывание на чужом классе/конфликте, где ПК даёт
    invalid). Даже монотонная на вид таблица не компилируется —
    программа остаётся на ПК (CM-F05)."""
    deg = _num_var(
        out_range="degrees",
        steering={"enabled": True, "version": 3,
                  "signs": {"pos": {4: "div", 8: "not"},
                            "neg": {8: "div"}},
                  "inverted": False},
        points=[[0, 0.0], [16, 45.0], [20, 60.0]])
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "30", "dir": "gt",
    }])]
    _r, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[deg]))
    assert managed == [False]


# ------------------------------------------------------------------
# Статические переменные: слушатель + updater-записи
# ------------------------------------------------------------------


def test_static_event_listener_and_updaters() -> None:
    """«Статическая стала 1»: слушатель фронта флага + updater-
    записи «кадр → SETFLAG» из определения переменной."""
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[_static_var()]))
    assert managed == [True] and len(recs) == 3
    listener = unpack_trigger(pack_trigger(recs[0]))
    assert listener["rx_var"] is True
    assert listener["rx_channel"] == TRIGGER_VAR_CHAN_FLAG
    assert listener["rx_dlc"] == 1  # фронт «стала 1»
    flag_idx = listener["rx_data_mask"][0]
    upds = [unpack_trigger(pack_trigger(r)) for r in recs[1:]]
    assert {u["rx_id"] for u in upds} == {0x300, 0x301}
    for u in upds:
        assert u["tx_setflag"] is True
        assert u["tx_data"][0] == flag_idx
        assert u["tx_data"][1] == (1 if u["rx_id"] == 0x300 else 0)


def test_static_condition_cond_flag() -> None:
    rules = [_rule(
        conditions=[{"type": "static", "var": "Зажигание",
                     "state": 1}],
    )]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[_static_var()]))
    assert managed == [True]
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["cond_flag"] is True
    assert rec["src_dlc"] == 1  # «есть 1»
    # Updater-записи едут следом — бит живой.
    assert len(recs) == 3


def test_static_rom_storage_stays_pc() -> None:
    """ПЗУ-хранение статической на камне не восстанавливается —
    программа честно остаётся на ПК."""
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    _r, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(read=[_static_var(storage="flash")]))
    assert managed == [False]


def test_static_updaters_count_in_quota() -> None:
    """Updater-записи едят квоту FLEX_MAX_RECORDS: не влезшая
    программа целиком уходит на ПК, половинчатого исполнения нет."""
    many = _static_var(frames=[
        {"id": f"{0x300 + i:03X}", "data": "01", "value": 1}
        for i in range(FLEX_MAX_RECORDS)
    ])
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    _r, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[many]))
    assert managed == [False]


def test_static_nonbinary_value_stays_pc() -> None:
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    var = _static_var(frames=[{"id": "300", "data": "01", "value": 5}])
    _r, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[var]))
    assert managed == [False]


# ------------------------------------------------------------------
# Флаги: события, условия, действие SETFLAG
# ------------------------------------------------------------------


def test_flag_event_and_action() -> None:
    """«Флаг стал 1» → «поднять другой флаг»: слушатель + SETFLAG."""
    rules = [_rule(
        events=[{"type": "flag", "var": "Блок", "state": 1}],
        actions=[{"type": "var", "flag_var": "Сигнал",
                  "flag_mode": "on"}],
    )]
    recs, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(aux=[_flag_var("Блок"), _flag_var("Сигнал")]))
    assert managed == [True] and len(recs) == 1
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["rx_var"] is True
    assert rec["rx_channel"] == TRIGGER_VAR_CHAN_FLAG
    assert rec["rx_dlc"] == 1
    assert rec["tx_setflag"] is True
    assert rec["tx_data"][1] == 1  # вкл
    # Два флага — разные индексы.
    assert rec["rx_data_mask"][0] != rec["tx_data"][0]


def test_flag_pulse_action_auto_off() -> None:
    """«вкл на N мс» → SETFLAG с авто-выкл в tx_data[2..5] (u32)."""
    rules = [_rule(actions=[{
        "type": "var", "flag_var": "Блок", "flag_mode": "pulse",
        "flag_time": 500,
    }])]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(aux=[_flag_var()]))
    assert managed == [True]
    rec = recs[0]
    assert rec["tx_setflag"] is True
    assert int.from_bytes(rec["tx_data"][2:6], "little") == 500


def test_flag_condition() -> None:
    rules = [_rule(
        conditions=[{"type": "flag", "var": "Блок", "state": 0}],
    )]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(aux=[_flag_var()]))
    assert managed == [True]
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["cond_flag"] is True
    assert rec["src_dlc"] == 2  # «есть 0»


# ------------------------------------------------------------------
# Команды «Переменные управления»
# ------------------------------------------------------------------


def test_command_action_schedule() -> None:
    """Команда → серия tx-записей со свёрнутым расписанием:
    start_k = суммарное время предыдущих + delay_before_send."""
    rules = [_rule(actions=[{"type": "var", "var": "Старт"}])]
    recs, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(control=[_command()]))
    assert managed == [True] and len(recs) == 2
    first, second = recs
    assert first["tx_id"] == 0x600
    assert first["delay_ms"] == 10
    assert first["tx_count"] == 2
    assert first["tx_interval_ms"] == 50
    # Второй кадр: 10 + (2-1)*50 + 100 + 20 = 180
    assert second["tx_id"] == 0x601
    assert second["tx_channel"] == 1
    assert second["delay_ms"] == 180


def test_command_with_exec_delay_folds() -> None:
    """«Задержка» действия прибавляется к расписанию первого кадра."""
    rules = [_rule(actions=[{
        "type": "var", "var": "Старт", "exec_delay": 40,
    }])]
    recs, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(control=[_command()]))
    assert managed == [True]
    assert recs[0]["delay_ms"] == 40 + 10
    assert recs[1]["delay_ms"] == 40 + 180


def test_command_x_from_cache_stays_pc() -> None:
    """«X» в кадре команды — подстановка из кэша команды ПК, на
    камне не выражается."""
    cmd = _command(frames=[{
        "channel": 0, "id": "600", "dlc": 2, "data": "AA X",
        "delay_before_send": 0, "count": 1, "delay_between": 0,
        "next_delay": 0}])
    rules = [_rule(actions=[{"type": "var", "var": "Старт"}])]
    _r, managed = compile_rules(
        rules, protocol=14, variables=_vars(control=[cmd]))
    assert managed == [False]


def test_static_legacy_command_derivation() -> None:
    """Наследие: «имя → 1» строится из кадров статической с
    value=1 — как _legacy_to_command на ПК."""
    rules = [_rule(actions=[{
        "type": "var", "var": "Зажигание", "var_value": 1,
    }])]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(read=[_static_var()]))
    assert managed == [True] and len(recs) == 1
    assert recs[0]["tx_id"] == 0x300


# ------------------------------------------------------------------
# Версии контейнера и снимок vars
# ------------------------------------------------------------------


def _blob_version(blob: bytes) -> int:
    return blob[4]


def test_blob_version_selection() -> None:
    """v1 — кадровые, v2 — aux, v3 — переменные; версия минимальна."""
    frame = _rule()
    v1 = build_flex_blob([frame], protocol=14)
    assert _blob_version(v1) == FLEX_BLOB_VERSION

    aux = _rule(
        events=[{"type": "aux", "channel": 1, "state": 1}],
    )
    v2 = build_flex_blob([aux], protocol=14)
    assert _blob_version(v2) == FLEX_BLOB_VERSION_AUX

    var_rule = _rule(events=[{
        "type": "num", "var": "V", "value": "1", "dir": "gt",
    }])
    v3 = build_flex_blob([var_rule], protocol=14,
                         variables=_vars(read=[_num_var()]))
    assert _blob_version(v3) == FLEX_BLOB_VERSION_VAR


def test_vars_snapshot_embedded_and_verified() -> None:
    """Снимок определений едет в JSON «vars»; analyze_device_flex
    воспроизводит компиляцию побайтово — verified + managed."""
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    blob = build_flex_blob(rules, protocol=14,
                           variables=_vars(read=[_static_var()]))
    json_len = int.from_bytes(blob[8:12], "little")
    doc = json.loads(blob[12:12 + json_len])
    assert doc["vars"]["static"]["Зажигание"]["type"] == "static"
    assert doc["vars"]["flags"]
    analysis = analyze_device_flex(blob)
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [True]


def test_vars_snapshot_determinism_referenced_failed_rule() -> None:
    """Программа со ссылкой на ПЗУ-статическую остаётся на ПК, но
    определение всё равно в снимке: сверка хвоста повторяет исход
    (иначе была бы вечная «conflict»)."""
    rules = [_rule(events=[{
        "type": "static", "var": "Зажигание", "edge": "on",
    }])]
    blob = build_flex_blob(
        rules, protocol=14,
        variables=_vars(read=[_static_var(storage="flash")]))
    analysis = analyze_device_flex(blob)
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [False]


def test_v3_records_rejected_by_old_version_parser() -> None:
    """parse_flex_blob принимает v3, но записи с новыми флагами
    валидируются по контракту — битая RX_VAR запись рушит контейнер."""
    rules = [_rule(events=[{
        "type": "num", "var": "V", "value": "1", "dir": "gt",
    }])]
    blob = build_flex_blob(rules, protocol=14,
                           variables=_vars(read=[_num_var()]))
    parsed = parse_flex_blob(blob)
    assert parsed is not None and parsed[1] == 1
    # Ломаем mask[1] (len=0) у единственной записи — валидация должна
    # отвергнуть контейнер целиком, без частичного исполнения.
    rec_off = len(blob) - 90
    rec = unpack_trigger(blob[rec_off:rec_off + 90])
    mask = bytearray(rec["rx_data_mask"])
    mask[1] = 0
    rec["rx_data_mask"] = bytes(mask)
    bad = bytearray(blob)
    bad[rec_off:rec_off + 90] = pack_trigger(rec)
    assert parse_flex_blob(bytes(bad)) is None


def test_flags_quota_overflow_stays_pc() -> None:
    """Больше FLEX_MAX_FLAGS разных флагов/статических в блобе —
    программа, упершаяся в лимит, уходит на ПК."""
    from core.trigger_protocol import FLEX_MAX_FLAGS
    rules = [
        _rule(
            events=[{"type": "flag", "var": f"F{i}", "state": 1}],
            title=f"R{i}",
        )
        for i in range(FLEX_MAX_FLAGS + 1)
    ]
    _recs, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(aux=[_flag_var(f"F{i}")
                             for i in range(FLEX_MAX_FLAGS + 1)]))
    # Первые 16 флагов разошлись по программам, 17-я — на ПК.
    assert managed[-1] is False


def test_flag_listener_edges_no_overlap() -> None:
    """«Стал 1» и «стал 0» одного флага — разные фронты, программа
    с обоими событиями компилируется (пересечения нет)."""
    rules = [_rule(events=[
        {"type": "flag", "var": "Блок", "state": 1},
        {"type": "flag", "var": "Блок", "state": 0},
    ])]
    recs, managed = compile_rules(
        rules, protocol=14, variables=_vars(aux=[_flag_var()]))
    assert managed == [True] and len(recs) == 2
    assert {r["rx_dlc"] for r in recs} == {1, 2}


def test_impulse_var_compiles_frame_match() -> None:
    """Импульсная переменная — обычный кадровый матч (протокол 12)."""
    impulse = {
        "name": "I", "type": "impulse", "storage": "ram",
        "id": "400", "data": "AA X",
    }
    rules = [_rule(events=[{"type": "impulse", "var": "I"}])]
    recs, managed = compile_rules(
        rules, protocol=12, variables=_vars(read=[impulse]))
    assert managed == [True] and len(recs) == 1
    assert recs[0]["rx_id"] == 0x400
    assert recs[0]["rx_data"][0] == 0xAA
    assert recs[0]["rx_data_mask"][0] == 0xFF
    assert recs[0]["rx_data_mask"][1] == 0


def test_field_dump_like_program_compiles() -> None:
    """Сценарий из дампа CM-F02: статическое «Зажигание» + команда
    из двух кадров — FLXB v3, владелец — камень."""
    rules = [
        _rule(
            events=[{"type": "static", "var": "Зажигание",
                     "edge": "on"}],
            actions=[{"type": "var", "var": "Вентилятор"}],
            title="Вентилятор по зажиганию",
        ),
    ]
    variables = _vars(
        read=[_static_var()],
        control=[_command("Вентилятор")],
    )
    blob = build_flex_blob(rules, protocol=14, variables=variables)
    assert _blob_version(blob) == FLEX_BLOB_VERSION_VAR
    analysis = analyze_device_flex(blob)
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [True]
    parsed = parse_flex_blob(blob)
    # (1 слушатель × 2 кадра команды) + 2 updater-записи.
    assert parsed is not None and parsed[1] == 4


def test_flexb_flags_wire_format() -> None:
    """Проводной вид: биты rx_flags/src_flags в упакованной записи."""
    rules = [_rule(
        events=[{"type": "static", "var": "Зажигание",
                 "edge": "on"}],
        conditions=[{"type": "flag", "var": "Блок", "state": 1}],
        actions=[{"type": "var", "flag_var": "Сигнал",
                  "flag_mode": "off"}],
    )]
    recs, managed = compile_rules(
        rules, protocol=14,
        variables=_vars(read=[_static_var()],
                        aux=[_flag_var("Блок"),
                             _flag_var("Сигнал")]))
    assert managed == [True]
    raw = pack_trigger(recs[0])
    assert raw[83] & TRIGGER_F_RX_VAR          # слушатель флага
    assert raw[86] & TRIGGER_F_COND_FLAG      # условие по флагу
    assert raw[86] & TRIGGER_F_TX_SETFLAG     # действие-флаг


# ------------------------------------------------------------------
# CM-FR04: условие aux + условие flag в одной программе — проводной
# формат одной записи их не выражает (src_channel/src_id/src_dlc
# взаимоисключающие), программа остаётся на ПК целиком.
# ------------------------------------------------------------------


def _aux_cond(channel: int = 2, state: int = 1) -> dict:
    return {"type": "aux", "channel": channel, "state": state}


def _flag_cond(name: str = "Блок", state: int = 1) -> dict:
    return {"type": "flag", "var": name, "state": state}


def test_aux_and_flag_condition_stays_pc() -> None:
    """Обе полярности обоих условий: aux+flag → managed=False и
    невалидной записи в блобе нет (проба ревью CM-FR04)."""
    for aux_state in (1, 0):
        for flag_state in (1, 0):
            rules = [_rule(conditions=[
                _aux_cond(state=aux_state),
                _flag_cond(state=flag_state),
            ])]
            recs, managed = compile_rules(
                rules, protocol=15,
                variables=_vars(aux=[_flag_var()]))
            assert managed == [False], (aux_state, flag_state)
            assert recs == []


def test_aux_and_flag_condition_does_not_break_neighbors() -> None:
    """Программа с двойным условием уходит на ПК, но рабочая
    соседняя программа компилируется и блоб проходит сверку."""
    bad = _rule(title="both", conditions=[_aux_cond(), _flag_cond()])
    good = _rule(title="ok", conditions=[_aux_cond()])
    rules = [bad, good]
    recs, managed = compile_rules(
        rules, protocol=15, variables=_vars(aux=[_flag_var()]))
    assert managed == [False, True] and len(recs) == 1
    analysis = analyze_device_flex(
        build_flex_blob(rules, protocol=15,
                        variables=_vars(aux=[_flag_var()])))
    # Раньше невалидная запись роняла ВЕСЬ контейнер (state no-tail)
    # и добрососедская программа молчала — теперь хвост «verified».
    assert analysis["state"] == "verified"
    assert analysis["managed"] == [False, True]


def test_single_flag_condition_encodes_flag_fields() -> None:
    """Контракт COND_FLAG: src_channel==0, src_id=индекс, src_dlc=1/2."""
    rules = [_rule(conditions=[_flag_cond(state=0)])]
    recs, managed = compile_rules(
        rules, protocol=15, variables=_vars(aux=[_flag_var()]))
    assert managed == [True]
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["cond_flag"] is True
    assert rec["src_channel"] == 0 and rec["src_dlc"] == 2
    assert rec["src_id"] < 16


def test_single_aux_condition_encodes_aux_fields() -> None:
    """Контракт COND_AUX: src_channel=1..7, src_id==0, src_dlc=1/2."""
    rules = [_rule(conditions=[_aux_cond(channel=5, state=0)])]
    recs, managed = compile_rules(
        rules, protocol=15, variables=_vars(aux=[_flag_var()]))
    assert managed == [True]
    rec = unpack_trigger(pack_trigger(recs[0]))
    assert rec["cond_aux"] is True
    assert rec["src_channel"] == 5 and rec["src_id"] == 0
    assert rec["src_dlc"] == 2


def test_every_generated_record_passes_firmware_contract() -> None:
    """Все записи компиляции из тестового набора выше по файлу:
    managed=True только у записей, принимаемых и ПК, и МК."""
    from core.trigger_protocol import record_fields_valid
    variables = _vars(
        read=[_num_var(), _cache_var(), _static_var()],
        control=[_command()],
        aux=[_flag_var("Блок"), _flag_var("Сигнал")],
    )
    rules = [
        _rule(events=[{"type": "num", "var": "V", "value": "100",
                       "dir": "gt"}]),
        _rule(conditions=[_flag_cond(state=1)],
              actions=[{"type": "var", "flag_var": "Сигнал",
                        "flag_mode": "on"}]),
        _rule(events=[{"type": "static", "var": "Зажигание",
                       "edge": "on"}],
              actions=[{"type": "var", "var": "Старт"}]),
    ]
    recs, managed = compile_rules(rules, protocol=15,
                                  variables=variables)
    assert managed == [True, True, True]
    assert all(
        record_fields_valid(pack_trigger(rec)) for rec in recs
    )
