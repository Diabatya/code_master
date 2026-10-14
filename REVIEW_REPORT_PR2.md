# Отчёт PR2 — «Автономная ГЛ, 7 выходов, угол руля» + ревью Round 1 (CM-N1…N7)

- **База:** `f4a1c12` (v1.1.72, протокол 12)
- **Ветка:** `feat/autonomous-flex-7out-steering`, кодовый HEAD `0d0ccec` + коммит правок раунда 1
- **PR:** https://github.com/Diabatya/code_master/pull/2 (не мержится в рамках задачи)
- **Аппаратные испытания:** не проводились — код, хост-тесты и C-харнессы

## Диагностика исходной задачи

| # | Вопрос | Находка | Итог |
|---|--------|---------|------|
| CM-HW-01 | Почему ГЛ не работает без USB-хоста | Путь CAN→`Trigger_OnFrame` не зависит от USB: `Protocol_Poll` кормит триггеры всегда (`APPLICATION_USE_VBUS_SENSE=0` → `usb_active=1`), CDC TX неблокирующий, `Trigger_Init` грузит FLXB из Flash при старте. Реальные причины «молчания»: aux-правила не компилировались на устройство и CFGH/FLXH стирались обновлением | Диагностировано, причины закрыты FLXB v2 + сохранением хранилища |
| CM-CDC-01 | Что пишет CDC-обновление, переживает ли хранилище | CDC-путь стирал страницы образа без чтения маркерных областей (preserve был только в DFU); CFGH с бод-рейтами CAN терялся → дефолт 250 кбод → «немая» шина | `storage_scan` + `plan_storage_relocation` + бэкап на ПК + readback — до первого стирания |
| CM-HW-02 | Семь выходов и конфликты | Было 4 канала (PC10/PC11/PC12/PA15); PD2/PB3 заняты терминаторами, PA14 — SWCLK | Карта 1..7, `aux_pins_mask` в CFGH, отзыв возвращает SWD/TERM |
| CM-HW-03 | Aux в автономной ГЛ | Записи `trigger_t` понимали только CAN-кадры | FLXB v2: RX_AUX_EVENT / COND_AUX / TX_AUX на камне |
| CM-HW-04 | Угол руля по кратности | Был только режим «Градусы» | `steering_decoder` v2: две таблицы знаков, AND, калибровка, `stale_ms` |

## Матрица автономности (что исполняется без USB-хоста)

| Функция | На МК автономно | Условие |
|---------|-----------------|---------|
| Триггеры (кадр → кадр) | да | FLXB/JSON в TRGH/FLXH из Flash |
| ГЛ: CAN-событие → CAN-действие | да | proto ≥12, запись в квоте 16 |
| ГЛ: aux-событие/условие/действие | да | proto ≥13, FLXB v2 |
| Aux: вкл/выкл/импульсы/ШИМ, сроки | да | канал доступен (маска владения) |
| Условия по переменным (символы/числа) | нет | декодер живёт на ПК — осознанно |
| Логирование EVLH | да | кольцо 4 страницы |
| Сохранение хранилища при обновлении | — | гарантирует ПК-сторона |

## Flash-план (протокол 13, `plan_storage_relocation`)

```
0x08000000..0x08007FFF  bootloader (32K, не трогается)
0x08008000              страница данных устройства (имя/SN/VER1)
0x08008800              APP1 — метаданные целостности (пишутся ПОСЛЕДНИМИ)
0x08009000..            код приложения
[app_end]               CFGH (1 страница, зарезервирована)
[cfg_end]               FLXH — гибкая логика
                        … зазор …
                        TRGH — триггеры (растёт к FLXH от центра)
                        EVLH — журнал (растёт к VARH от центра)
                        … зазор …
0x0803FFFF↓             VARH — переменные, якорь у верха Flash
```

Обновление: скан маркеров → полный дамп + бэкап на ПК → план пересадки
(отказ до стирания при нехватке места) → инвалидация APP1 → код →
перезапись областей (target erase + write + readback) → APP1.
DFU mass-erase восстанавливает все области, CDC/UART — только накрытые
плюс те, что требуют пересадки. Обрыв любого этапа до APP1 оставляет
устройство в bootloader — «кирпича» нет.

## Round 1 — сводная таблица CM-N1…CM-N7

| # | Статус | Файл/функция | Изменение | Регресс-тест |
|---|--------|--------------|-----------|--------------|
| CM-N1 | Исправлено | `core/storage_scan.py::plan_storage_relocation`; `core/bootloader.py::_scan_marker_storage`, `_dump_storage_backup`, `flash_firmware`; `ui/flash_dialog.py` | Полный дамп хвоста до стирания (короткое чтение = отказ), бэкап+манифест на ПК, план с якорями и поколениями, erase+write+readback каждой области, APP1 последней; DFU восстанавливает moves+keep и TRG2 | `test_bootloader_preserve.py` (соседние CFGH+FLXH, поколения, нехватка места, короткое чтение, обрыв) |
| CM-N2 | Исправлено | `core/steering_decoder.py`; `ui/variables_tab.py::_SteeringDialog` | Две таблицы знаков × 10 делителей, «Кратно»/«Не кратно» XOR, AND в таблице, conflict/no-class, нейтраль по raw, `stale_ms`; миграция v1→v2 | `test_steering_decoder.py` (42 теста) |
| CM-N3 | Исправлено | `core/variable_decode.py`; `ui/flexible_logic_tab.py`; `ui/variables_tab.py` | Единый декодер raw/SBLP/градусы/steering/валидность/устаревание для UI и рантайма; `_dyn_ts` чистится при сбросе; invalid→условия не выполняются | `test_variable_decode.py`, `test_flex_steering_runtime.py` |
| CM-N4 | Исправлено | `aux_out.c::AuxOut_ApplyOwnedMask`; `can_bridge.c::RestoreTermPins`, TERM-гейт | PA14 через `SWJ_DISABLE`/`SWJ_NOJTAG`; TERM-пины немедленно возвращаются при отзыве; на старте отданные пины не инициализируются терминаторами | `test_aux_out.c` (харнесс настоящего aux_out.c) |
| CM-N5 | Исправлено | `aux_out.c` (автомат сроков) | Отдельный флаг дедлайна, `time_after` wrap-safe, «0=бесконечно» не путается с «истёк при now==0» | `test_aux_out.c` |
| CM-N6 | Исправлено | `aux_out.c`, `core/aux_channels.py::quantize_pwm`, `protocol.c::CMD_AUX_GET` | Целочисленная сетка мс одинакова на ПК/МК; расширенный GET возвращает фактические фазы (легаси-префикс 10 B сохранён); Poll догоняет целыми периодами без GPIO-залпа; лимит сроков един | `test_aux_channels.py`, `test_aux_out.c` |
| CM-N7 | Исправлено | `ui/variables_tab.py::_AuxDialog`; `flexible_logic_tab.py` (чекбокс + резолв); `settings_window.py` | Диалог канала показывает пин, конфликт SWD/TERM, фактическое квантование; «параметры из настроек выхода» резолвятся до компиляции FLXB | `test_aux_dialog.py`, `test_aux_channels.py` |

## Проверки

- pytest: **382 passed** (системный Python, `QT_QPA_PLATFORM=offscreen`; venv с битым Qt-плагином не используется)
- Ruff: clean; compileall: OK
- C-харнессы: `test_trigger_flash`, `test_device_config`, `test_event_log_ring`, `test_trigger_flex` (+aux-сценарии), `test_aux_out` (новый, настоящий `aux_out.c`) — все PASS
- ARM-сборки: application text **46104 B** (+2260 к v1.1.72, бюджет 224K), bootloader без изменений
