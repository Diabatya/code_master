"""Компилятор программ ГЛ в записи trigger_t (контейнер FLXB):
покрытие pack/unpack, подстановки «X из кадра события», отсева
некомпилируемых программ и квоты FLEX_MAX_RECORDS."""

import json

from core.flex_compiler import (
    FLEX_BLOB_MAGIC,
    FLEX_MAX_RECORDS,
    analyze_device_flex,
    build_flex_blob,
    compile_rule,
    compile_rules,
    extract_flex_json,
    flexb_header_valid,
    managed_flags,
    parse_flex_blob,
    parse_flex_rules,
)
from core.trigger_protocol import (
    TRIGGER_F_TX_EVENT_DATA,
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
) -> dict:
    return {
        "title": "P", "active": active,
        "events": events if events is not None else [_frame_event()],
        "conditions": conditions if conditions is not None else [],
        "actions": actions if actions is not None else [_frame_action()],
    }


def _record_dicts(rule) -> list[dict]:
    """compile_rule → распакованные записи (проверка проводного вида)."""
    records = compile_rule(rule)
    assert records is not None
    from core.trigger_protocol import pack_trigger

    return [unpack_trigger(pack_trigger(r)) for r in records]


def test_simple_frame_to_frame_compiles() -> None:
    recs = _record_dicts(_rule())
    assert len(recs) == 1
    rec = recs[0]
    assert rec["enabled"] == 1
    assert rec["rx_channel"] == 2          # событие «любой канал»
    assert rec["rx_id"] == 0x123
    assert rec["rx_id_mask"] == 0x7FF
    assert rec["tx_channel"] == 0          # действие CAN1
    assert rec["tx_id"] == 0x456
    assert rec["tx_dlc"] == 8
    assert rec["tx_data"][:2] == b"\x01\x02"


def test_event_data_mask() -> None:
    """Шаблон DATA события → rx_data/rx_data_mask (X — wildcard)."""
    recs = _record_dicts(_rule(events=[_frame_event(data="AA X 0F")]))
    rec = recs[0]
    assert rec["rx_data"][:3] == b"\xAA\x00\x0F"
    assert rec["rx_data_mask"][:3] == b"\xFF\x00\xFF"


def test_x_substitution_marks_unspecified_positions() -> None:
    """Семантика ПК: payload строится НА кадре события — байт без
    валидного токена (X/пусто/за концом списка) берётся из пришедшего
    кадра; маска подстановки src_from покрывает их все."""
    recs = compile_rule(_rule(actions=[_frame_action(data="01 X 03")]))
    assert recs is not None and len(recs) == 1
    assert recs[0]["rx_tx_event_data"] is True
    # «01 X 03» → явные байты 0 и 2; все остальные — из события.
    expect = bytes([0x00, 0xFF, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    assert recs[0]["src_from"] == expect
    # Проводной вид: флаг реально уезжает в записи (rx_flags бит 5).
    from core.trigger_protocol import pack_trigger

    raw = pack_trigger(recs[0])
    # rx_flags — поле формата v3 по смещению 83 (см. trigger_t).
    assert raw[83] & TRIGGER_F_TX_EVENT_DATA


def test_explicit_token_no_subst() -> None:
    """Все 8 байт заданы явно — подстановки нет, флаг не взводится."""
    recs = compile_rule(_rule(
        actions=[_frame_action(data="01 02 03 04 05 06 07 08")],
    ))
    assert recs is not None and len(recs) == 1
    assert recs[0]["rx_tx_event_data"] is False
    assert recs[0]["src_from"] == b"\x00" * 8


def test_power_event_compiles_to_boot() -> None:
    recs = _record_dicts(_rule(events=[{"type": "power"}]))
    assert len(recs) == 1
    assert recs[0]["rx_fire_on_boot"] is True


def test_uncompilable_variants() -> None:
    """Условия ПРИ, нефреймовые события и действия (переменные/флаги/
    доп.каналы/кэш) — остаются на исполнении приложения."""
    assert compile_rule(_rule(conditions=[{"type": "static",
                                         "var": "V", "state": 1}])) is None
    assert compile_rule(_rule(events=[{"type": "static",
                                      "var": "V"}])) is None
    assert compile_rule(_rule(actions=[{"type": "var",
                                        "var": "V"}])) is None
    assert compile_rule(_rule(actions=[{
        "type": "aux", "aux_enabled": True, "aux_channel": 1,
        "aux_mode": "on",
    }])) is None
    # «Задержка» выполнения действия — единая, автономная на МК
    # (CM-F11): сворачивается в delay_ms первого отправления.
    recs = compile_rule(_rule(actions=[{
        **_frame_action(), "exec_delay": 100,
    }]))
    assert recs is not None and recs[0]["delay_ms"] == 100
    # Прерыватель действия — только на ПК.
    assert compile_rule(_rule(actions=[{
        **_frame_action(),
        "abort": {"type": "aux", "channel": 1, "state": 1},
    }])) is None


def test_multi_event_action_no_keyerror() -> None:
    """Декартово произведение событий×действий — без KeyError
    (регресс: pop('_subst') падал на второй итерации)."""
    recs = compile_rule(_rule(
        events=[_frame_event("111"), _frame_event("222", channel=0)],
        actions=[_frame_action("333"), _frame_action("444", data="X")],
    ))
    assert recs is not None and len(recs) == 4


def test_extended_inferred_from_wide_id() -> None:
    """ID > 0x7FF без галочки extended — иначе trigger_fields_valid
    прошивки отбрасывал бы запись молча."""
    recs = _record_dicts(_rule(events=[_frame_event("1ABCDEF")]))
    assert recs[0]["rx_extended"] == 1
    recs = _record_dicts(_rule(actions=[_frame_action("1ABCDEF")]))
    assert recs[0]["tx_extended"] == 1


def test_records_quota() -> None:
    """Свыше FLEX_MAX_RECORDS — программа остаётся на приложении."""
    rules = [
        _rule(events=[_frame_event(f"{0x100 + i:X}")])
        for i in range(FLEX_MAX_RECORDS + 2)
    ]
    records, managed = compile_rules(rules)
    assert len(records) == FLEX_MAX_RECORDS
    assert sum(managed) == FLEX_MAX_RECORDS
    assert managed[-2:] == [False, False]


def test_blob_roundtrip() -> None:
    rules = [_rule(), _rule(conditions=[{"type": "static", "var": "V",
                                        "state": 1}])]
    blob = build_flex_blob(rules)
    assert blob[:4] == FLEX_BLOB_MAGIC
    assert blob[4] == 1
    assert blob[5] == 1                      # скомпилировалась 1 из 2
    import json

    payload = json.loads(extract_flex_json(blob).decode("utf-8"))
    assert payload["rules"] == rules
    # Старый чисто-JSON блоб проходит как есть.
    legacy = json.dumps({"rules": rules}).encode()
    assert extract_flex_json(legacy) == legacy


def test_managed_flags_protocol_gate() -> None:
    rules = [_rule(), _rule(conditions=[{"type": "static", "var": "V",
                                        "state": 1}])]
    assert managed_flags(rules, 11) == [False, False]
    assert managed_flags(rules, 12) == [True, False]


def test_inactive_rule_produces_no_records() -> None:
    assert compile_rule(_rule(active=False)) == []
    records, managed = compile_rules([_rule(active=False)])
    assert records == [] and managed == [False]


# --- Пересекающиеся события ИЛИ (ревью CM-03) ------------------------


def test_overlapping_events_not_compiled() -> None:
    """Кадр, совпадающий с двумя событиями программы: на ПК — один
    запуск (break), на камне две записи дали бы две отправки →
    программа остаётся на ПК."""
    rule = _rule(events=[
        _frame_event("123", channel=2),
        _frame_event("123", channel=0),
    ])
    assert compile_rule(rule) is None
    _records, managed = compile_rules([rule])
    assert managed == [False]


def test_non_overlapping_events_compile() -> None:
    """Разные ID или непересекающиеся каналы — компилируются."""
    recs = compile_rule(_rule(events=[
        _frame_event("123", channel=0),
        _frame_event("456", channel=0),
    ]))
    assert recs is not None and len(recs) == 2
    recs = compile_rule(_rule(events=[
        _frame_event("123", channel=0),
        _frame_event("123", channel=1),   # CAN1 ≠ CAN2 — не пересекаются
    ]))
    assert recs is not None and len(recs) == 2


def test_overlapping_data_masks_not_compiled() -> None:
    """Маски DATA совместимы («AA» и «AA BB» одного ID пересекаются —
    кадр AA BB … совпадает с обоими фильтрами); «AA» и «BB» — нет."""
    assert compile_rule(_rule(events=[
        _frame_event("123", data="AA"),
        _frame_event("123", data="AA BB"),
    ])) is None
    recs = compile_rule(_rule(events=[
        _frame_event("123", data="AA"),
        _frame_event("123", data="BB"),
    ]))
    assert recs is not None and len(recs) == 2


def test_two_power_events_not_compiled() -> None:
    """Два «Включение устройства» — обе записи вооружились бы при
    старте (на ПК старт программы один); power+frame — компилируется."""
    assert compile_rule(_rule(events=[
        {"type": "power"}, {"type": "power"},
    ])) is None
    recs = compile_rule(_rule(events=[
        {"type": "power"}, _frame_event("123"),
    ]))
    assert recs is not None and len(recs) == 2


# --- fire_limit (ревью CM-04) -----------------------------------------


def test_fire_limit_normalized() -> None:
    """На ПК 0/1 — «без ограничения»; в запись уходит только >1,
    иначе легаси-конфиг с limit=1 запирал бы программу на камне."""
    for limit, expect in ((0, 0), (1, 0), (2, 2), (7, 7)):
        recs = compile_rule(_rule(
            events=[{**_frame_event(), "fire_limit": limit}],
        ))
        assert recs is not None and len(recs) == 1
        assert recs[0]["rx_fire_limit"] == expect


# --- Зависимость «программа начала работать» (ревью CM-05) ------------


def test_program_event_pulls_target_to_pc() -> None:
    """Слушатель старта программы видит его только на ПК: если цель
    ушла на камень, старт невидим → цель остаётся на ПК вместе со
    слушателем."""
    target = _rule()
    target["title"] = "Насос"
    listener = _rule(events=[{
        "type": "program", "var": "Насос",
    }])
    records, managed = compile_rules([target, listener])
    assert managed == [False, False]
    assert records == []
    # Без слушателя цель компилируется.
    records2, managed2 = compile_rules([target])
    assert managed2 == [True] and len(records2) == 1


# --- Валидация контейнера FLXB (ревью CM-05) --------------------------


def test_parse_flex_blob_validates_container() -> None:
    blob = build_flex_blob([_rule()])
    parsed = parse_flex_blob(blob)
    assert parsed is not None
    json_part, count = parsed
    assert count == 1 and json_part.startswith(b"{")

    # Чистый JSON старой версии — не FLXB.
    assert parse_flex_blob(b'{"rules":[]}') is None
    # Неверная версия контейнера.
    bad = bytearray(blob)
    bad[4] = 0xEE
    assert parse_flex_blob(bytes(bad)) is None
    # Число записей за пределом квоты.
    bad = bytearray(blob)
    bad[5] = FLEX_MAX_RECORDS + 1
    assert parse_flex_blob(bytes(bad)) is None
    # Усечённый JSON/хвост записей.
    assert parse_flex_blob(blob[:20]) is None
    bad = bytearray(blob)
    bad[8:12] = (len(blob) * 2).to_bytes(4, "little")
    assert parse_flex_blob(bytes(bad)) is None
    # Блоб обрезан внутри области записей.
    assert parse_flex_blob(blob[:-5]) is None


def test_flexb_header_valid_from_peek() -> None:
    blob = build_flex_blob([_rule()])
    assert flexb_header_valid(blob[:12], len(blob)) is True
    assert flexb_header_valid(blob[:12], 12) is False   # JSON не влезает
    bad = bytearray(blob)
    bad[4] = 9
    assert flexb_header_valid(bytes(bad[:12]), len(bad)) is False
    assert flexb_header_valid(b"JSON12345678", 100) is False
    assert flexb_header_valid(b"FL", 100) is False


def test_extract_flex_json_broken_container() -> None:
    """Повреждённый FLXB (невалидный заголовок) возвращается целиком —
    JSON-парсер вызывающего явно откажет, а не съест мусор."""
    blob = bytearray(build_flex_blob([_rule()]))
    blob[4] = 0xEE  # заведомо невалидная версия (v1/v2 — легальны)
    assert extract_flex_json(bytes(blob)) == bytes(blob)


# ---- CM-R3: строгая валидация контейнера -------------------------------


def test_flexb_extra_tail_rejected() -> None:
    """Точная длина: лишний хвост после записей отвергает контейнер
    (раньше проходил — «достаточная длина» вместо точной)."""
    blob = build_flex_blob([_rule()])
    assert parse_flex_blob(blob + b"EXTRA") is None


def test_flexb_json_zero_and_schema() -> None:
    """parse_flex_blob проверяет ТОЛЬКО исполняемый контракт (как
    firmware append_flex): json_len=0 и выход JSON за границы —
    отказ; СОДЕРЖИМОЕ JSON прошивка не читает, и хост не должен
    расходиться (CM-R6). Валидность схемы — parse_flex_rules."""
    blob = bytearray(build_flex_blob([_rule()]))
    json_len = int.from_bytes(blob[8:12], "little")

    # json_len = 0 — отказ на обеих сторонах.
    bad = bytearray(blob)
    bad[8:12] = (0).to_bytes(4, "little")
    assert parse_flex_blob(bytes(bad)) is None

    # json_len ≈ UINT32_MAX — переполнение 12+json_len не должно
    # маскировать выход за границы (CM-R6).
    bad = bytearray(blob)
    bad[8:12] = (0xFFFFFFF6).to_bytes(4, "little")
    assert parse_flex_blob(bytes(bad)) is None

    # Невалидный JSON той же длины — хвост исполняемый, контейнер
    # принимается (прошивка загрузит записи); схема отвергается
    # отдельным парсером.
    bad = bytearray(blob)
    bad[12:12 + json_len] = b"!" * json_len
    assert parse_flex_blob(bytes(bad)) is not None
    assert parse_flex_rules(b"!" * json_len) is None

    # Валидный JSON, но не схема конфигурации (нет "rules").
    assert parse_flex_rules(json.dumps({"kind": "x"}).encode()) is None
    # dict-JSON без списка rules.
    assert parse_flex_rules(
        json.dumps({"kind": "code_master_flex_logic", "rules": 5}
                   ).encode()
    ) is None
    # Правило не-dict и элемент списка не-dict — отказ схемы.
    assert parse_flex_rules(
        json.dumps({"rules": ["oops"]}).encode()
    ) is None
    assert parse_flex_rules(
        json.dumps({"rules": [{"title": "P", "events": "oops"}]}
                   ).encode()
    ) is None
    # UTF-8 мусор.
    assert parse_flex_rules(b"\xff\xfe") is None


def test_analyze_device_flex_states() -> None:
    """Классификация владельца (CM-R7): verified — хвост ==
    компиляции JSON; conflict — хвост загружается прошивкой, но не
    верифицирован; no-tail — исполняемых записей нет."""
    rules = [_rule()]
    blob = build_flex_blob(rules)
    a = analyze_device_flex(blob)
    assert a["state"] == "verified"
    assert a["managed"] == [True]

    # Легаси-JSON и мусор — исполняемого хвоста нет.
    assert analyze_device_flex(b'{"rules":[]}')["state"] == "no-tail"
    assert analyze_device_flex(b"\x00" * 40)["state"] == "no-tail"
    assert analyze_device_flex(blob + b"EXTRA")["state"] == "no-tail"

    # Битый JSON при валидном хвосте — conflict: камень исполняет
    # записи, а их соответствие конфигурации недоказуемо.
    bad = bytearray(blob)
    bad[12] = ord("!")
    bad[13] = ord("!")
    a = analyze_device_flex(bytes(bad))
    assert a["state"] == "conflict"
    assert a["rules"] is None

    # Хвост от ДРУГИХ правил при сохранённом JSON — conflict.
    other = build_flex_blob(
        [_rule(actions=[_frame_action(data="FF")])]
    )
    json_len = int.from_bytes(blob[8:12], "little")
    other_json_len = int.from_bytes(other[8:12], "little")
    franken = bytearray(blob[:12 + json_len])
    franken[5] = other[5]
    franken += other[12 + other_json_len:]
    a = analyze_device_flex(bytes(franken))
    assert a["state"] == "conflict"
    assert a["rules"] is not None      # JSON читается, но хвост чужой


def test_flexb_record_crc_and_fields() -> None:
    """Повреждённая CRC записи и запись с недопустимыми полями
    отвергают контейнер ЦЕЛИКОМ (all-or-nothing, как в прошивке):
    половина записей без другой половины невозможна."""
    blob = build_flex_blob([_rule()])
    json_len = int.from_bytes(blob[8:12], "little")
    rec_off = 12 + json_len

    bad = bytearray(blob)
    bad[-1] ^= 0x5A  # crc8 последней записи
    assert parse_flex_blob(bytes(bad)) is None

    # tx_channel = 55 — недопустимое поле (offset 32 записи v3),
    # crc исправлен: отказ именно из-за полей.
    from core.trigger_protocol import crc8 as _crc8
    bad = bytearray(blob)
    bad[rec_off + 32] = 0x55
    bad[-1] = _crc8(bytes(bad[rec_off:-1]))
    assert parse_flex_blob(bytes(bad)) is None

    # Неизвестный бит rx_flags (offset 83 записи) — отказ.
    bad = bytearray(blob)
    bad[rec_off + 83] |= 0x40
    bad[-1] = _crc8(bytes(bad[rec_off:-1]))
    assert parse_flex_blob(bytes(bad)) is None


def test_flexb_valid_blob_still_parses() -> None:
    """Контроль: валидный контейнер проходит строгую проверку."""
    blob = build_flex_blob([_rule(), _rule(active=False)])
    parsed = parse_flex_blob(blob)
    assert parsed is not None
    json_part, count = parsed
    assert json.loads(json_part.decode("utf-8"))["rules"]
