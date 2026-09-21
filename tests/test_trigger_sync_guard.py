"""Регрессия: автовычитка при подключении не должна молча затирать
открытую конфигурацию содержимым устройства (полевая жалоба:
«подтянул хлам и перезаписал всё»)."""

import sys
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.serial_manager import SerialManager
from core.trigger_protocol import pack_trigger, unpack_trigger
import ui.can_trigger_tab as trigger_module
from ui.can_trigger_tab import CanTriggerTab


class _FakeConfig:
    """Изолирует тест от реального config.json разработчика."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def _cfg_trigger(recv_id: str = "111", tx_id: str = "222") -> dict[str, Any]:
    return {
        "active": True,
        "recv_id": recv_id,
        "recv_dlc": 1,
        "recv_data": "AA",
        "recv_channel": 0,
        "recv_bit": 0,
        "recv_rtr": 0,
        "responses": [
            {"channel": 0, "bit": 0, "id": tx_id, "dlc": 1, "data": "BB", "delay": 0}
        ],
    }


def _device_records(tab: CanTriggerTab, indexes: list[int]) -> list[dict[str, Any]]:
    """Записи устройства в том виде, в каком их вернёт вычитка —
    многофреймовый триггер разворачивается в группу записей, как при
    реальной записи."""
    records = []
    for index in indexes:
        projected = tab._project_block_records(index)
        if projected is None:
            values = tab._device_trigger_values(index)
            values["enabled"] = 0
            projected = [values]
        for values in projected:
            records.append(unpack_trigger(pack_trigger(values)))
    return records


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def tab(qapp, monkeypatch):
    monkeypatch.setattr(trigger_module, "Config", _FakeConfig)
    widget = CanTriggerTab(SerialManager())
    yield widget
    widget.deleteLater()


def test_matching_device_state_applies(tab) -> None:
    """UI совпадает с устройством — вычитка применяется штатно."""
    tab.set_config([_cfg_trigger()])
    records = _device_records(tab, [0])
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True


def test_diverged_device_state_keeps_ui(tab) -> None:
    """Устройство расходится с открытой конфигурацией — экран сохранён,
    автозамена пропущена."""
    tab.set_config([_cfg_trigger()])
    other = _device_records(tab, [0])[0]
    other["rx_id"] = 0x333
    records = [unpack_trigger(pack_trigger(other))]
    tab._read_device_triggers = lambda: records

    assert tab.sync_from_device() is False
    assert len(tab._blocks) == 1
    assert tab._blocks[0]["recv"]["id"].text() == "111"


def test_force_read_replaces_ui(tab) -> None:
    """Ctrl+R (force) — оператор явно просит замену, устройство применяется."""
    tab.set_config([_cfg_trigger()])
    other = _device_records(tab, [0])[0]
    other["rx_id"] = 0x333
    records = [unpack_trigger(pack_trigger(other))]
    tab._read_device_triggers = lambda: records

    assert tab.sync_from_device(force=True) is True
    assert len(tab._blocks) == 1
    assert tab._blocks[0]["recv"]["id"].text() == "333"


def test_empty_ui_filled_from_device(tab) -> None:
    """Пустой экран (свежая вкладка) заполняется истиной устройства —
    прежнее поведение для чистого запуска."""
    tab.set_config([_cfg_trigger()])
    records = _device_records(tab, [0])
    tab.set_config([])
    assert len(tab._blocks) == 0
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1


def test_sync_does_not_persist_device_state_to_config(tab) -> None:
    """Автовычитка не зеркалит состояние МК в config.json — иначе
    фантомная запись из устройства сидела в UI при следующем запуске
    и «Сохранить» прошивало её обратно (бессмертный фантом). Файл
    меняется только явными действиями оператора."""
    tab.set_config([_cfg_trigger()])
    records = _device_records(tab, [0])
    tab._read_device_triggers = lambda: records
    tab._config._data.pop("triggers", None)
    assert tab.sync_from_device() is True
    assert tab._config.get("triggers") is None


def test_file_loaded_triggers_do_not_execute_until_save(tab) -> None:
    """«Загрузить конфигурацию» только заполняет поля: приложение не
    исполняет пришедшие из файла триггеры и на шину ничего не уходит,
    пока оператор не нажмёт «Сохранить» (полевая жалоба — устройство
    «отвечало» сразу после загрузки, как будто конфиг прогрузили в МК)."""
    sent: list[dict[str, Any]] = []
    tab._send_responses = sent.append
    tab._send_cached_frames = sent.append
    tab.set_config([_cfg_trigger()], suspend_execution=True)
    tab.process_frame(
        {"id": 0x111, "channel": 1, "data": b"\xaa", "rtr": False, "extended": False}
    )
    assert sent == []


def test_sync_from_device_clears_suspension(tab) -> None:
    """Вычитка устройства — подтверждённое состояние: подвеска снимается
    (управляемые МК триггеры и так пропускаются по device_managed)."""
    tab.set_config([_cfg_trigger()], suspend_execution=True)
    assert all(tab._pc_suspended)
    records = _device_records(tab, [0])
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True
    assert not any(tab._pc_suspended)


def test_commit_payload_clear_all_keyed_on_v3() -> None:
    """Прошивка v3 принимает стирание всех триггеров только с ключом:
    голый «CB 01 00» отвергается как фантом рассинхрона (полевой баг —
    мусорный COMMIT обнулял хранилище)."""
    assert trigger_module._commit_payload(0, 3) == bytes((0, 0xA5))
    assert trigger_module._commit_payload(0, 4) == bytes((0, 0xA5))


def test_commit_payload_clear_all_legacy_on_v2() -> None:
    """Старая прошивка ключ не знает — ей легаси-формат «CB 01 00»,
    иначе «удалить все триггеры» молча отваливалось бы на v2."""
    assert trigger_module._commit_payload(0, 2) == b"\x00"
    assert trigger_module._commit_payload(0, 0) == b"\x00"


def test_commit_payload_normal_unchanged() -> None:
    """Обычный COMMIT формата не менял — иначе запись триггеров
    отвалилась бы на всех прошивках."""
    assert trigger_module._commit_payload(5, 3) == b"\x05"
    assert trigger_module._commit_payload(5, 2) == b"\x05"


def _multi_response_trigger() -> dict[str, Any]:
    """Многофреймовый триггер: два фрейма ответа — разворачивается в
    группу записей МК (group_seq), исполняет устройство."""
    trigger = _cfg_trigger()
    trigger["responses"].append(
        {"channel": 0, "bit": 0, "id": "333", "dlc": 1, "data": "CC", "delay": 0}
    )
    return trigger


def _unexpandable_trigger() -> dict[str, Any]:
    """PC-only триггер: расписание не влезает в записи МК (суммарная
    задержка >65 с) — на МК пишется томбстоун с enabled=0, исполняет
    приложение."""
    trigger = _cfg_trigger()
    trigger["responses"] = [
        {
            "channel": 0, "bit": 0, "id": "222", "dlc": 1, "data": "BB",
            "delay_before_send": 9999, "delay_between": 9999,
            "count": 999, "next_delay": 0,
        }
    ]
    return trigger


def _tombstone(recv_id: int = 0x111, tx_id: int = 0x222) -> dict[str, Any]:
    """Запись МК, как её возвращает вычитка для PC-only триггера:
    enabled=0, хранит только первый фрейм ответа."""
    return unpack_trigger(pack_trigger({"enabled": 0, "rx_id": recv_id, "tx_id": tx_id}))


def test_pc_only_tombstone_restored_from_config(tab) -> None:
    """Вычитка томбстоуна (enabled=0) активного PC-only триггера
    восстанавливает блок из config.json целиком: галка на месте, все
    фреймы ответа, исполняет приложение. Без этого после переподключения
    триггер умирал на обоих исполнителях — «закрыл приложение, устройство
    перестало работать, в списке непонятные записи»."""
    tab._config._data["triggers"] = [_unexpandable_trigger()]
    tab._read_device_triggers = lambda: [_tombstone()]

    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1
    block = tab._blocks[0]
    assert block["group"].isChecked() is True
    assert tab._device_managed == [False]
    assert len(block["response"]["rows"]) == 1
    assert block["response"]["rows"][0]["count"].value() == 999


def test_user_disabled_record_stays_disabled(tab) -> None:
    """enabled=0 у триггера, который влезает в trigger_t — осознанное
    «выкл» оператора, а не томбстоун PC-only: галку не воскрешаем, иначе
    выключенное оживало бы при каждом переподключении."""
    cfg = _cfg_trigger()  # активный в config.json — сохранён до выключения
    tab._config._data["triggers"] = [cfg]
    tab._read_device_triggers = lambda: [_tombstone()]

    assert tab.sync_from_device() is True
    assert tab._blocks[0]["group"].isChecked() is False
    assert tab._device_managed == [True]


def test_inactive_config_tombstone_stays_disabled(tab) -> None:
    """Томбстоун, чей конфиг-триггер выключен — не воскресает."""
    cfg = _unexpandable_trigger()
    cfg["active"] = False
    tab._config._data["triggers"] = [cfg]
    tab._read_device_triggers = lambda: [_tombstone()]

    assert tab.sync_from_device() is True
    assert tab._blocks[0]["group"].isChecked() is False


def test_tombstone_without_config_match_stays_disabled(tab) -> None:
    """enabled=0 без пары в config.json — чужая или выключенная запись:
    не трогаем."""
    tab._read_device_triggers = lambda: [_tombstone()]

    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1
    assert tab._blocks[0]["group"].isChecked() is False


def test_legacy_trigger_without_active_loads_enabled(tab) -> None:
    """Старые файлы/выгрузки без ключа active: «триггер есть» = «включён» —
    иначе вся конфигурация грузилась выключенной и уходила в МК мёртвой."""
    cfg = _cfg_trigger()
    del cfg["active"]
    tab.set_config([cfg])
    assert tab._blocks[0]["group"].isChecked() is True


def test_validate_warns_filled_but_disabled(tab) -> None:
    """Заполненный, но выключенный триггер пишется в МК мёртвым —
    оператор должен это видеть до записи."""
    cfg = _cfg_trigger()
    cfg["active"] = False
    errors, warnings = tab._validate_config([cfg])
    assert not errors
    assert any("выключен" in w for w in warnings)


def test_validate_warns_pc_only_trigger(tab) -> None:
    """Триггер, не разворачивающийся в записи МК, исполняется
    приложением и умрёт с ним — предупреждаем при записи."""
    errors, warnings = tab._validate_config([_unexpandable_trigger()])
    assert not errors
    assert any("приложением" in w for w in warnings)


def test_validate_silent_for_multiframe(tab) -> None:
    """Многофреймовый триггер разворачивается в группу записей и
    исполняется самим МК — предупреждения «исполняется приложением»
    быть не должно."""
    errors, warnings = tab._validate_config([_multi_response_trigger()])
    assert not errors
    assert not any("приложением" in w for w in warnings)


def test_multiframe_projects_to_record_group(tab) -> None:
    """Триггер с двумя фреймами ответа раскладывается в две записи МК:
    общее условие приёма, вторая несёт group_seq>0 и абсолютную
    задержку (next_delay первой строки)."""
    cfg = _multi_response_trigger()
    cfg["responses"][0]["next_delay"] = 50
    tab.set_config([cfg])

    records = tab._project_block_records(0)
    assert records is not None and len(records) == 2
    assert records[0]["group_seq"] == 0
    assert records[0]["tx_id"] == 0x222
    assert records[1]["group_seq"] == 1
    assert records[1]["tx_id"] == 0x333
    assert records[1]["delay_ms"] == 50
    assert all(r["rx_id"] == 0x111 and r["enabled"] == 1 for r in records)


def test_multiframe_group_syncs_back_to_one_block(tab) -> None:
    """Группа записей устройства собирается обратно в один блок с
    двумя строками ответа — полный круг запись→вычитка."""
    cfg = _multi_response_trigger()
    cfg["responses"][0]["next_delay"] = 50
    tab.set_config([cfg])
    records = _device_records(tab, [0])

    tab.set_config([])
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1
    block = tab._blocks[0]
    assert block["group"].isChecked() is True
    assert tab._device_managed == [True]
    rows = block["response"]["rows"]
    assert len(rows) == 2
    assert rows[0]["id"].text() == "222"
    assert rows[1]["id"].text() == "333"
    assert rows[0]["next_delay"].value() == 50


def test_count_over_255_fragments_roundtrip(tab) -> None:
    """count>255 пишется фрагментами по 255 отправок; вычитка склеивает
    их обратно в одну строку с исходным count."""
    cfg = _cfg_trigger()
    cfg["responses"] = [
        {
            "channel": 0, "bit": 0, "id": "222", "dlc": 1, "data": "BB",
            "delay_before_send": 0, "delay_between": 20,
            "count": 300, "next_delay": 0,
        }
    ]
    tab.set_config([cfg])

    records = tab._project_block_records(0)
    assert records is not None and len(records) == 2
    assert records[0]["tx_count"] == 255 and records[0]["group_seq"] == 0
    assert records[1]["tx_count"] == 45
    assert records[1]["group_seq"] & 0x80
    assert records[1]["delay_ms"] == 255 * 20

    device_records = [unpack_trigger(pack_trigger(r)) for r in records]
    tab.set_config([])
    tab._read_device_triggers = lambda: device_records
    assert tab.sync_from_device() is True
    assert len(tab._blocks) == 1
    rows = tab._blocks[0]["response"]["rows"]
    assert len(rows) == 1
    assert rows[0]["count"].value() == 300
    assert rows[0]["delay_between"].value() == 20


def test_disabled_multiframe_group_stays_disabled(tab) -> None:
    """Выключенный многофреймовый триггер пишется группой с enabled=0 —
    вычитка собирает блок выключенным и heal его не воскрешает."""
    cfg = _multi_response_trigger()
    cfg["active"] = False
    tab.set_config([cfg])
    records = _device_records(tab, [0])
    assert len(records) == 2
    assert all(r["enabled"] == 0 for r in records)

    tab.set_config([])
    tab._read_device_triggers = lambda: records
    assert tab.sync_from_device() is True
    block = tab._blocks[0]
    assert block["group"].isChecked() is False
    assert len(block["response"]["rows"]) == 2
    # Не PC-only: запись полностью во Flash, приложение не исполняет.
    assert tab._device_managed == [True]


def test_validate_silent_for_normal_trigger(tab) -> None:
    """Обычный активный триггер — без новых предупреждений."""
    errors, warnings = tab._validate_config([_cfg_trigger()])
    assert not errors
    assert not any("выключен" in w or "приложением" in w for w in warnings)


def _cache_cfg() -> dict[str, Any]:
    """Триггер с двумя строками кэша и wildcard «X» в приёме и диапазонах."""
    return {
        "active": True,
        "cache": True,
        "recv_channel": 0, "recv_bit": 0, "recv_id": "111", "recv_dlc": 3,
        "recv_rtr": 0, "recv_data": "AA X 33",
        "responses": [],
        "cache_rows": [
            {
                "channel": 0, "bit": 0, "id": "300", "dlc": 2,
                "tx_channel": 1,
                "from": "11 X", "to": "22 X",
                "delay_before_send": 10, "delay_between": 5,
                "count": 2, "next_delay": 40,
            },
            {
                "channel": 0, "bit": 0, "id": "400", "dlc": 4,
                "tx_channel": 0,
                "from": "00 00 00 00", "to": "FF FF FF FF",
                "delay_before_send": 0, "delay_between": 7,
                "count": 3, "next_delay": 0,
            },
        ],
    }


def test_recv_wildcard_packs_zero_mask(tab) -> None:
    """«X» в поле приёма → байт не сравнивается (rx_data_mask=0), а при
    вычитке показывается как «X» — не «00»."""
    tab.set_config([_cache_cfg()])
    records = tab._project_block_records(0)
    assert records is not None and records
    assert records[0]["rx_data_mask"] == bytes((0xFF, 0x00, 0xFF, 0, 0, 0, 0, 0))

    device_records = _device_records(tab, [0])
    tab.set_config([])
    tab._read_device_triggers = lambda: device_records
    assert tab.sync_from_device() is True
    data_edits = tab._blocks[0]["recv"]["data"]
    assert data_edits[0].text() == "AA"
    assert data_edits[1].text() == "X"
    assert data_edits[2].text() == "33"


def test_cache_rows_project_to_record_group(tab) -> None:
    """Каждая строка кэша — своя запись с собственным src-фильтром и
    слотом кэша; group_seq и абсолютные задержки — как у многофрейма.
    «X» в От/До кодируется инвертированным диапазоном (from>to)."""
    tab.set_config([_cache_cfg()])
    records = tab._project_block_records(0)
    assert records is not None and len(records) == 2
    assert all(r["cache_enabled"] == 1 and r["rx_id"] == 0x111 for r in records)
    assert records[0]["src_id"] == 0x300 and records[0]["group_seq"] == 0
    assert records[0]["src_from"][0] == 0x11 and records[0]["src_to"][0] == 0x22
    # «X» — инвертированный диапазон: байт игнорируется, в кэше = 0x00.
    assert records[0]["src_from"][1] == 0xFF and records[0]["src_to"][1] == 0x00
    assert records[0]["delay_ms"] == 10 and records[0]["tx_count"] == 2
    assert records[1]["src_id"] == 0x400 and records[1]["group_seq"] == 1
    # 10 + 5*(2-1) повторов строки 0 + пауза 40 перед следующей строкой.
    assert records[1]["delay_ms"] == 55
    assert records[1]["tx_count"] == 3 and records[1]["tx_interval_ms"] == 7


def test_cache_rows_group_syncs_back(tab) -> None:
    """Группа кэш-записей устройства собирается обратно в строки кэша —
    включая «X» на месте инвертированных диапазонов."""
    tab.set_config([_cache_cfg()])
    device_records = _device_records(tab, [0])
    tab.set_config([])
    tab._read_device_triggers = lambda: device_records
    assert tab.sync_from_device() is True
    rows = tab._blocks[0]["cache"]["rows"]
    assert len(rows) == 2
    assert rows[0]["id"].text() == "300" and rows[1]["id"].text() == "400"
    assert rows[0]["from_data"][1].text() == "X"
    assert rows[0]["to_data"][1].text() == "X"
    assert rows[0]["from_data"][0].text() == "11"
    assert rows[0]["count"].value() == 2 and rows[1]["count"].value() == 3
    assert rows[0]["next_delay"].value() == 40


def test_pc_cache_wildcard_zeroing(tab) -> None:
    """PC-исполнение: кадр в диапазоне попадает в кэш, wildcard-байт
    обнуляется; вне диапазона — не кэшируется; срабатывание шлёт кэш."""
    cfg = _cache_cfg()
    # Нулевые задержки — иначе отправка уходит в QTimer.singleShot.
    cfg["cache_rows"][0]["delay_before_send"] = 0
    cfg["cache_rows"][0]["count"] = 1
    cfg["cache_rows"][0]["next_delay"] = 0
    tab.set_config([cfg])
    # Кадр источника: 0xB5 ∈ [0x11..0x22]? Нет — вне диапазона.
    src = {"id": 0x300, "channel": 1, "data": bytes([0x15, 0x77]),
           "extended": False, "rtr": False}
    tab.process_frame(src)
    # 0x15 в [0x11..0x22], байт 1 — wildcard → закэширован с 0x00.
    assert tab._pc_cache.get((0, 0))["data"] == bytes([0x15, 0x00])
    # За пределами диапазона — кэш не пополняется.
    tab._pc_cache.clear()
    tab.process_frame({**src, "data": bytes([0x05, 0x77])})
    assert (0, 0) not in tab._pc_cache
    # Срабатывание по приёму → ответ из кэша на канале строки (CAN2).
    tab.process_frame(src)
    sent: list[tuple] = []
    tab._send_frame = lambda *a, **k: sent.append(a)
    tab.process_frame({"id": 0x111, "channel": 1, "data": bytes([0xAA, 0x00, 0x33]),
                       "extended": False, "rtr": False})
    assert sent, "ответ из кэша не отправлен"
    assert sent[0][0] == 0x300  # id закэшированного кадра
    assert sent[0][2] == 1      # tx_channel строки = CAN2 (index 1)


def test_simulator_src_wildcard() -> None:
    """Симулятор повторяет прошивку: инвертированный диапазон = байт
    игнорируется при матче и обнуляется в кэше."""
    from core.trigger_simulator import simulate

    record = unpack_trigger(pack_trigger({
        "enabled": 1, "rx_channel": 0, "rx_id": 0x111,
        "rx_id_mask": 0x7FF, "rx_dlc": 0, "rx_data": b"\x00" * 8,
        "rx_data_mask": b"\x00" * 8,
        "tx_channel": 0, "cache_enabled": 1,
        "src_channel": 0, "src_id": 0x300, "src_dlc": 2,
        "src_from": bytes([0x10, 0xFF]), "src_to": bytes([0x20, 0x00]),
        "tx_count": 1, "delay_ms": 0, "tx_interval_ms": 0,
    }))
    frames = [
        {"time_ms": 0, "channel": 1, "id": 0x300, "data": bytes([0x15, 0x99])},
        {"time_ms": 10, "channel": 1, "id": 0x111, "data": b""},
    ]
    result = simulate([record], frames)
    tx = result["tx_frames"]
    assert tx and tx[0]["id"] == 0x300
    assert tx[0]["data"] == bytes([0x15, 0x00]), "wildcard-байт уходит как 0x00"
