/* Device configuration storage ("страница А" per ТЗ 11.2 — Device Name,
 * Serial Number, VID/PID) persisted in the dedicated configuration page
 * before the trigger pages. See firmware/PROTOCOL.md Part 3 for the exact
 * byte layout and the memory map (0x08008000..0x080087FF).
 *
 * This is a *different* "page A" concept than the bootloader/application
 * Flash split (0x08000000/0x08008000) — do not confuse the two; see the
 * note carried over from firmware/FIRMWARE_INPUT_REQUEST.md.
 */

#ifndef __DEVICE_CONFIG_H
#define __DEVICE_CONFIG_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define DEVICE_CONFIG_NAME_MAX     9U   /* ТЗ 11.2: Device Name <= 9 chars */
#define DEVICE_CONFIG_SERIAL_MAX   10U  /* ТЗ 11.2: Serial Number <= 10 chars */

/* Страница идентификации устройства перенесена в НАЧАЛО Flash за
 * bootloader'ом (отчёт мастера): 0x08008000..0x080087FF. Формат записей
 * не менялся — CFG0/CEX0/TNM0/VER1 те же, что на старой странице.
 * При пустой/битой новой странице содержимое один раз переносится со
 * старого адреса (миграция устройств, прошитых до переезда). */
#define DEVICE_CONFIG_PAGE_ADDR    0x08008000U
#define DEVICE_CONFIG_PAGE_ADDR_LEGACY 0x0803D800U
#define DEVICE_CONFIG_PAGE_SIZE    2048U
#define DEVICE_CONFIG_MAGIC        0x43464730U /* "CFG0" */
#define DEVICE_CONFIG_FORMAT_VERSION 1U
#define DEVICE_CONFIG_RECORD_SIZE  32U

#define DEVICE_CONFIG_DEFAULT_VID  0x0483U
#define DEVICE_CONFIG_DEFAULT_PID  0x5740U

/* In-RAM mirror of the persisted config, kept packed 1:1 with the on-Flash
 * layout documented in firmware/PROTOCOL.md Part 3 (32 bytes total). */
typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  device_name_len;
  char     device_name[DEVICE_CONFIG_NAME_MAX];
  uint8_t  serial_len;
  char     serial[DEVICE_CONFIG_SERIAL_MAX];
  uint16_t vid;
  uint16_t pid;
  uint8_t  reserved[2];
  uint8_t  crc8;
} device_config_t;

/* Extended settings record, stored at DEVICE_CONFIG_PAGE_ADDR + 32 inside
 * the same 2 KB page (the main 32-byte record stays byte-compatible with
 * older host firmware). Holds operator CAN bit rates: they must persist on
 * the device because triggers/gateways run autonomously without the PC
 * (ТЗ 12.4) and must come up at the configured speed after power cycles. */
#define DEVICE_EXT_CONFIG_OFFSET  32U
#define DEVICE_EXT_CONFIG_MAGIC   0x43455830U /* "CEX0" */
#define DEVICE_EXT_CONFIG_VERSION 1U
#define DEVICE_CONFIG_DEFAULT_BAUD_KBPS 500U

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint16_t can1_baud_kbps;
  uint16_t can2_baud_kbps;
  uint8_t  version;
  uint8_t  reserved[6];
  uint8_t  crc8;
} device_ext_config_t; /* 16 bytes */

/* Таблица имён триггеров — третья запись той же config-страницы
 * (сразу за расширенной). Имя не участвует в исполнении триггера и
 * поэтому НЕ лежит в trigger_t: записей в пуле до 70, RAM почти весь
 * занят CAN-кольцами и буферами — держать имя в каждой записи некуда.
 * Индекс имени = слот базовой записи группы (group_seq==0) в пуле
 * триггеров — тот же индекс, что принимает CMD_TRIGGER_READ.
 * Ограничено первыми TRIGGER_NAME_MAX слотами пула; триггеры дальше
 * работают как прежде, просто без сохранённого имени.
 * Хост пишет имена по одному (CMD_TRIGGER_NAME_WRITE — только RAM),
 * фиксация во Flash одной перезаписью страницы — CMD_TRIGGER_NAME_COMMIT
 * (иначе N имён = N стираний страницы подряд). */
#define TRIGGER_NAMES_OFFSET   (DEVICE_EXT_CONFIG_OFFSET + 16U) /* = 48 */
#define TRIGGER_NAMES_MAGIC    0x544E4D30U /* "TNM0" */
/* version 2: имя 16→21 байт (отчёт мастера — кириллические названия
 * резались до ~8 букв). Старая запись не пройдёт CRC — безвредно,
 * имена просто перезапишутся при ближайшем сохранении. */
#define TRIGGER_NAMES_VERSION  2U
#define TRIGGER_NAME_MAX       24U
#define TRIGGER_NAME_LEN       21U

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  version;
  uint8_t  reserved[4]; /* выравнивание: запись пишется halfword'ами */
  char     names[TRIGGER_NAME_MAX][TRIGGER_NAME_LEN]; /* UTF-8, 0-термин. */
  uint8_t  crc8;
} trigger_names_t; /* 4+1+4+504+1 = 514 bytes, чётный размер */

/* Запись версии ПО («VER1»), которую конфигуратор шьёт при
 * программировании МК — её номер показывает карточка устройства.
 * Лежит в той же config-странице и дописывается при КАЖДОМ обновлении
 * страницы из RAM-зеркала: стирание страницы иначе её сносило бы.
 * Смещение 1024 — свободная область за таблицей имён; старая раскладка
 * (offset 32) конфликтовала с ext-записью CEX0 и перетиралась ей.
 * Legacy-совместимость: при загрузке проверяется и старый offset 32
 * (если там VER1, а не CEX0 — читаем его). */
#define DEVICE_CONFIG_VER_OFFSET   1024U
#define DEVICE_CONFIG_VER_OFFSET_LEGACY 32U
#define DEVICE_CONFIG_VER_MAGIC    0x56455231U /* "VER1" */
#define DEVICE_CONFIG_VERSION_MAX  16U

typedef struct __attribute__((packed)) {
  uint32_t magic;   /* DEVICE_CONFIG_VER_MAGIC */
  uint8_t  len;     /* <= DEVICE_CONFIG_VERSION_MAX */
  char     version[DEVICE_CONFIG_VERSION_MAX];
  uint8_t  reserved[10];
  uint8_t  crc8;
} device_fw_ver_t; /* 32 bytes — половинками пишется в конец страницы */

/* Loads the config from Flash into the RAM mirror (call once at boot, before
 * MX_USB_DEVICE_Init() so the USB descriptors already see the right
 * name/serial on first enumeration). Falls back to defaults if the page is
 * blank/corrupt (magic or CRC mismatch). */
void DeviceConfig_Init(void);

/* Returns a pointer to the current in-RAM config (read-only for callers). */
const device_config_t *DeviceConfig_Get(void);

/* Returns 1 when the persisted page passed magic/CRC/format validation. */
uint8_t DeviceConfig_IsValid(void);

/* Validates and writes a new config to Flash (erase + program the whole
 * 2 KB page — see the timing caveat in firmware/PROTOCOL.md Part 3).
 * Returns 1 on success, 0 on invalid parameters or Flash-program failure.
 * Updates the in-RAM mirror on success. */
uint8_t DeviceConfig_Write(const uint8_t *device_name, uint8_t device_name_len,
                            const uint8_t *serial, uint8_t serial_len,
                            uint16_t vid, uint16_t pid);

/* Erases the config page and reloads defaults (CMD_CFG_FACTORY_RESET,
 * ТЗ 10.3 "Заводские настройки"). Returns 1 on success. */
uint8_t DeviceConfig_FactoryReset(void);

/* CAN bit rate (kbit/s) configured for channel 0/1 — from the extended
 * record; DEVICE_CONFIG_DEFAULT_BAUD_KBPS when absent/corrupt. */
uint32_t DeviceConfig_GetCanBaud(uint8_t channel);

/* Persists a new CAN bit rate for channel 0/1: erases+reprograms the whole
 * config page (main + extended records), keeping name/serial/VID/PID
 * untouched. Returns 1 on success. */
uint8_t DeviceConfig_SetCanBaud(uint8_t channel, uint32_t baud_kbps);

/* Имя триггера по слоту базовой записи (0..TRIGGER_NAME_MAX-1):
 * возвращает указатель на len байт в RAM-зеркале (не терминировано
 * нулём при len==TRIGGER_NAME_LEN) или NULL/len=0 для пустого слота. */
const uint8_t *DeviceConfig_GetTriggerName(uint8_t index, uint8_t *len_out);

/* Версия ПО, зашитая конфигуратором (запись VER1). Возвращает указатель
 * на len байт в RAM-зеркале или NULL/len=0, если запись отсутствует
 * или битая. Версия НЕ стирается заводскими настройками и не меняется
 * конфигурацией — это свойство прошитого образа. */
const uint8_t *DeviceConfig_GetFwVersion(uint8_t *len_out);

/* Складывает имя в RAM-зеркало таблицы (Flash не трогает — фиксация
 * только через DeviceConfig_CommitTriggerNames). Пустое len стирает
 * имя слота. Возвращает 0 при неверном индексе/длине. */
uint8_t DeviceConfig_StageTriggerName(uint8_t index, const uint8_t *name,
                                      uint8_t len);

/* Одна перезапись config-страницы (main + ext + имена) — вызывается
 * один раз после пачки StageTriggerName. Возвращает 1 при успехе. */
uint8_t DeviceConfig_CommitTriggerNames(void);

#ifdef __cplusplus
}
#endif

#endif /* __DEVICE_CONFIG_H */
