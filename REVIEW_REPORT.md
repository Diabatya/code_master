# Отчёт по ревью CodeMaster-Devin-Review-1.1.71

- **Исходный SHA (база ревью):** `005ec117201fc50a1d191b3f46c95b1b44365d7f` (v1.1.71)
- **Итоговый SHA:** `99d043d` (HEAD ветки `fix/review-1.1.71`)
- **PR:** https://github.com/Diabatya/code_master/pull/1
- **CI:** workflow `Build and Release` на PR: https://github.com/Diabatya/code_master/actions/runs/38394536516 — test-python, build-firmware (с субмодулем), build-windows/macos/linux — все зелёные; release-job пропущен корректно (PR ≠ tag/main).

## Сводная таблица CM-01…CM-08

| # | Статус | Файл/функция | Изменение | Регресс-тест | До → После |
|---|--------|--------------|-----------|--------------|------------|
| CM-01 | Исправлено | `firmware/libs/STM32CubeF1` (gitlink), `firmware/{application,bootloader}/Makefile`, `.gitignore` | Восстановлен gitlink субмодуля `dad36d75`; убрано правило `firmware/libs/`; ранняя проверка `$(CUBE)` с понятной ошибкой | Чистая сборка make app + bootloader | Билд падал: субмодуль пуст/отсутствовал → собирается: app text 42968 / data 408 / bss 65100, bootloader text 19440 / data 396 / bss 8384 |
| CM-02 | Исправлено | `ui/settings_window.py::_save_current_config`, `_upload_config_blobs`, `_storage_write_verified`, `_storage_readback_matches`, `_refresh_device_flexb`; `core/serial_manager.py::StorageCommitUnknown`, `storage_upload`; `models/config.py::Config.save` | `Config.save()` → bool; этапы сохранения бросают `SaveStageError`; неуспешная запись не снимает dirty, не вызывает `commit_runtime`, не ставит `device_flexb`, не обновляет кэш; потерянный COMMIT разрешается вычиткой области | `tests/test_save_path.py` (13 тестов), `test_flex_compiler.py::test_parse_flex_blob_validates_container` | Ошибка записи → «Настройки записаны в устройство», dirty снят → ошибка этапа показывается, dirty/оверлей сохранены, исход неизвестной записи проверяется вычиткой |
| CM-03 | Исправлено | `core/flex_compiler.py::_events_may_overlap`, `compile_rule` | Пересекающиеся события одной программы (общий ID∩маскa, пересекающиеся каналы, совместимые DATA-маски) → программа не компилируется, остаётся на ПК | `test_flex_compiler.py::test_overlapping_*` | Кадр под два события → 2 записи → двойная отправка на МК (ПК — одна сработка) → такие программы исполняет только ПК, дублей нет |
| CM-04 | Исправлено | `firmware/application/Src/trigger.c::fire_track`, `rx_data_matches`, `Trigger_OnFrame`; `core/flex_compiler.py` | `fire_limit` 0/1 → unlimited (нормализация в компиляторе: в запись уходит >1); DATA-трекинг полного кадра с паддингом нулями за DLC; X-байты исключены из смены DATA; busy: новое событие при занятой записи досылает старый ответ немедленно, затем вооружает новый | `firmware/application/tests/test_trigger_flex.c`, `test_flex_compiler.py::test_fire_limit_normalized` | limit=1 запирал программу на МК после первой сработки; короткий кадр давал ложное совпадение/несовпадение с ПК; повторное событие теряло взведённую отправку → семантика ПК↔МК выровнена |
| CM-05 | Исправлено | `core/flex_compiler.py::parse_flex_blob`, `flexb_header_valid`, `build_flex_blob`; `ui/settings_window.py::_upload_config_blobs`, `_refresh_device_flexb`, `extract_flex_json`-потребители | FLXB валидируется (магия, версия=1, count≤16, границы JSON и записей, без хвоста); proto 11 → чистый JSON; device_flexb только по валидному FLXB и proto≥12; зависимость «программа начала работать» → вся связка остаётся на ПК | `test_flex_compiler.py::test_parse_flex_blob_*`, `test_flexb_header_valid_from_peek`, `test_program_event_pulls_target_to_pc`, `test_save_path.py::test_proto11_writes_plain_json_flex` | Битый FLXB мог уронить вычитку/дать ложный device-managed → валидация; proto11 не получает FLXB; program-события не теряют цель на камне |
| CM-06 | Исправлено | `.github/workflows/build.yml` | `pull_request` триггер; `test-python` job (pytest offscreen + ruff + compileall); `release.needs` = все билды+тесты; C-тесты в `build-firmware` после `submodules: recursive`; проверка метаданных и полный образ | Прогон CI на PR | Тесты и C-харнессы не гонялись в CI; release не ждал сборки → release только после зелёных job-ов |
| CM-07 | Исправлено | `core/serial_manager.py::auto_detect_can_speed`; `models/config.py::_normalize_loaded` | Метод возвращает `None` (unsupported), команда не отправляется, `can_speed_detected` не эмитится, `can_speed_auto` не взводится; легаси `can_speed_auto=True` нормализуется при загрузке | `tests/test_can_speed.py::test_auto_detect_can_speed_is_unsupported` | Ответ CanBridge_GetBaud(0) (настроенная скорость) трактовался как измерение → явный unsupported; ручной выбор скоростей сохранён |
| CM-08 | Исправлено | `models/version.py`; `tests/test_version_consistency.py` | `VERSION_TUPLE` вычисляется из `VERSION`; тесты проверяют tuple, `assets/version_info.py` и регенерацию `tools/generate_version_info.py` | `test_version_consistency.py` (3 теста) | `VERSION="1.1.71"` при `VERSION_TUPLE=(1,1,70)` → кортеж выводится, рассогласование ловит тест |

## Повторное ревью (CodeMaster-Devin-PR1-Review, CM-R1…CM-R4)

Проверенный ревьюером HEAD: `39c8523`. Исправления — следующим коммитом на той же ветке.

| # | Статус | Файл/функция | Изменение | Регресс-тест | До → После |
|---|--------|--------------|-----------|--------------|------------|
| CM-R1 | Исправлено | `firmware/application/Src/trigger.c::Trigger_OnFrame`, `flex_fire`, `flex_enqueue`, `Trigger_Poll`, `Trigger_FireOnBoot`; `trigger.h::FLEX_QUEUE_DEPTH` | Удалён немедленный сброс прежней серии циклом отправок. Очередь `s_flex_queue[16]` — у каждого запуска свой срок/повторы/интервал и pad-8 снимок DATA события для X-подстановки. Подавления «занято» нет: на ПК `_rule_busy` растёт только от `exec_delay`, который на МК не компилируется — каждый совпавший кадр ставит свою серию (PC-parity). Отказ `CanBridge_Transmit` → ограниченные ретраи (20 × 1 мс) → `s_dropped_count`; переполнение очереди → dropped, диагностируется через `Trigger_GetStats`/CMD_TRIGGER_STATS | `test_trigger_flex.c`: `test_series_schedules_preserved` (метки 50/70/90 и 60/80/100), `test_identical_frames_not_suppressed`, `test_event_between_repeats`, `test_tx_failure_retries_and_drops`, `test_queue_overflow_counted`, `test_x_subst_per_firing_snapshot` | Кадр B в t=10 → сброс серии A досылкой 3 кадров сразу, без delay/interval → серии A и B сохраняют собственные расписания; потери — в счётчике dropped, не молча |
| CM-R2 | Исправлено | `trigger.c::fire_track` (параметр `full_data`), `Trigger_OnFrame` | Для записей ГЛ «смена DATA» — сравнение нормализованных pad-8 байт (`_pad_8` на ПК): DLC само по себе сменой не является. Пользовательские триггеры — прежняя семантика (маска + DLC), `fire_same_data` удалён | `test_trigger_flex.c::test_pad8_dlc_not_a_change` (DLC 1→2 с нулём, 2→1 с потерей ненулевого байта, 0→8 нулей, реальная смена), `test_full_data_tracking` | DLC 1→2 (`11` → `11 00`) сбрасывал лимит на МК → 3 ответа вместо 2 у ПК → pad-8 равенство, лимит исчерпан корректно |
| CM-R3 | Исправлено | `core/flex_compiler.py::parse_flex_blob`, `flexb_header_valid`; `core/trigger_protocol.py::record_fields_valid`; `trigger.c::append_flex`; `ui/settings_window.py::_refresh_device_flexb` | Единый контракт обеих сторон: точная длина `12 + json_len + count*90`, ver=1, count≤16 (отказ, не обрезание), json_len>0, JSON парсится в dict со списком `rules`, каждая запись — валидная trigger_t v3 (магия/формат/CRC8/поля — порт `trigger_fields_valid`); переполнение смещений проверяется в 64-бит. Контейнер принимается/отвергается целиком — «половина записей» невозможна. `device_flexb` передаётся только по полной вычитке и валидации содержимого (не по 12-байтному peek); пустая область → честный False; нечитаемость → флаг не трогается | `test_flex_compiler.py::test_flexb_extra_tail_rejected`, `test_flexb_json_zero_and_schema`, `test_flexb_record_crc_and_fields`; `test_trigger_flex.c::test_flxb_validation`, `test_legacy_json_blob`; `test_save_path.py::test_commit_unknown_mismatch_keeps_device_owner`, `test_commit_unknown_corrupt_blob_marks_pc_owner` | `blob+EXTRA` и `JSON="!"` проходили как валидные; count>16 обрезался в прошивке и отвергался на ПК → обе стороны отвергают одинаково, владелец только по подтверждённому содержимому |
| CM-R4 | Исправлено | `tests/test_save_path.py` (фикстура, `_FakeTab`); `ui/settings_window.py::_refresh_save_state`; `ui/can_monitor_tab.py::_apply_row_density`; `ui/variables_tab.py::_apply_row_density` | Фикстура закрывает порт на время конструктора — отложенная `singleShot(400, _sync_from_device)` не планируется и не стреляет в подменённые вкладки; `_FakeTab` моделирует полный контракт (`on_device_session_started`, `sync_from_device`); teardown обрабатывает deferred-delete. Латентный баг самого кода: отложенные singleShot-коллбеки падали на уже разрушенных C++-объектах — добавлены `isValid`-гарды | `test_save_path.py` — **20 тестов** (было 12): VAR exception, INFO None/exception/неверная длина, инвалидация кэша, повтор после отказа INFO, потерянный COMMIT с реальным сменённым содержимым FLEX (владелец — реальное содержимое камня) | Полный прогон падал 1 failed от чужого critical-диалога отложенной синхронизации; изолированно проходил → нестабильность устранена источником, не фильтрацией |
| UI | Сделано | `core/flex_compiler.py::executor_info`; `ui/flexible_logic_tab.py::RuleRowWidget.set_executor`, `_update_executor_badges` | В шапке каждой программы бейдж владельца: «устройство» (зелёный, автономно), «ПК» (голубой, тултип с конкретной причиной — условие/задержка/прерыватель/пересечение событий/квота/протокол<12/нет FLXB), «не записано», «выключена». Бейдж по снимку записанных правил (`_runtime_rules`) — показывает, что исполняется сейчас. Программа на ПК с закрытым приложением не работает — причина в тултипе | `executor_info` покрыт косвенно через `compile_rules`-тесты; визуальная проверка — GUI smoke | Владелец исполнения был невидим — оператор не знал, что программа требует запущенного приложения → явный бейдж с причиной |

## Команды верификации и результат

| Команда | Exit | Результат |
|---------|------|-----------|
| `QT_QPA_PLATFORM=offscreen python3 -m pytest -q` | 0 | **169 passed**, 4 warnings (было 157; +12: CM-R3/R4 сценарии) |
| `python3 -m pytest tests/test_save_path.py -q` | 0 | **20 passed** (точное число — было 12, не 13) |
| `python3 -m ruff check .` | 0 | All checks passed |
| `python3 -m compileall -q core ui models` | 0 | OK |
| `cc … test_trigger_flash.c && ./a.out` | 0 | PASS: 70 слотов round-trip |
| `cc … test_device_config.c && ./a.out` | 0 | PASS: round-trip, size=32 |
| `cc … test_event_log_ring.c && ./a.out` | 0 | PASS: blank boot, пагинация, wraparound |
| `cc … test_trigger_flex.c && ./a.out` | 0 | PASS: 15 сценариев — очередь с метками времени, ретраи/переполнение, pad_8, валидация FLXB |
| `git submodule update --init --recursive` | 0 | STM32CubeF1 @ dad36d75 |
| `make -C firmware/application` (clean) | 0 | text 43392, data 408, bss 64652, dec 108452; metadata start=0x08009000 size=43800 crc32=0x43C5A413 |
| `make -C firmware/bootloader` (clean) | 0 | text 19440, data 396, bss 8384, dec 28220 |
| `make_full_image.py --version 1.1.71` | 0 | full image 3 сегмента, 65292 B; VER1=v1.1.71; cdc app-only образ собран |
| GUI smoke (SettingsWindow offscreen + shutdown poller) | 0 | GUI SMOKE OK |

Toolchain: `arm-none-eabi-gcc` (Arm GNU Toolchain 15.2.rel1, /opt/homebrew/bin). Python 3.x + PySide6, Qt offscreen.

## Контракт совместимости (протоколы 10/11/12, JSON/FLXB)

- **Proto 10** — хранилище недоступно: `_upload_config_blobs` явно отказывает запись VAR/FLEX с объяснением; блобы не пишутся и не читаются.
- **Proto 11** — блобы пишутся/читаются в прежнем формате чистого JSON (`{kind, format, rules/variables}`); FLXB не отправляется; `device_flexb=False`, программы ГЛ исполняет приложение.
- **Proto 12** — область FLXH принимает контейнер FLXB: `FLXB` + ver=1 + count≤16 + json_len + JSON + записи trigger_t (90 B). `device_flexb=True` только после полной вычитки и строгой валидации содержимого (`parse_flex_blob`): точная длина, JSON-схема, CRC/поля каждой записи (CM-R3).
- Старые чисто-JSON блобы остаются читаемыми через `extract_flex_json`; повреждённый FLXB возвращается целиком и отвергается валидатором, не роняя вычитку.

## Автономное исполнение ГЛ: что на МК, что на ПК

На МК (FLXB-записи, proto≥12): события «Фрейм» и «Включение устройства», без условий ПРИ; действия «Отправить кадр» (delay/count/between), X-подстановка байтов кадра события.

Остаётся на ПК: программы с условиями (static/dynamic/number/aux), событиями по переменным/флагам/«программа начала работать», действиями с переменными/кэшем/доп.каналами/exec_delay/interrupt-условиями, пересекающиеся события ИЛИ, сверх квоты `FLEX_MAX_RECORDS=16`. Владелец и причина видны бейджем в шапке каждой программы.

Семантика «смена DATA» для записей ГЛ приведена к хосту: pad_8-сравнение всех восьми байт кадра (DLC само по себе сменой не является; смена байта вне маски фильтра — является, как на ПК). Пользовательские триггеры — прежняя семантика по маске.

## Не проверено на железе (требует стенд)

- DFU/CDC-обновление с реальным устройством.
- Сохранение конфигурации после power-cycle.
- Автономное исполнение ГЛ без USB/ПК.
- Отсутствие дублирующих отправок на шине.
- Точность count/delay/between повторов.
- Независимость CAN1/CAN2 на обоих разъёмах.

## Нерешённое

- Хардварные проверки выше — нужен реальный блок.
- `CanBridge` не умеет аппаратный перебор скоростей — auto-detect честно помечен unsupported (по заданию CM-07 реализация сканирования не входит).

## Подтверждение незатронутого

- `main` не изменялся: работа в ветке `fix/review-1.1.71`.
- Релизы/теги не создавались; `v1.1.71` (тег без опубликованного release на момент ревью) не перезаписывался.
- Рабочие устройства не перепрошивались; проверки — host-тесты и Qt offscreen.
