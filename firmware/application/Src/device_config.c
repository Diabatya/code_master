/* Device configuration storage implementation. See device_config.h and
 * firmware/PROTOCOL.md Part 3 for the on-Flash layout and rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "device_config.h"
#include "storage.h"

static device_config_t s_config;
static device_ext_config_t s_ext_config;
static trigger_names_t s_trigger_names;
static device_fw_ver_t s_fw_ver;
static device_app_cfg_t s_app_cfg; /* зеркало области CFGH за кодом */
static uint8_t s_config_valid;

static uint8_t crc8(const uint8_t *data, uint32_t len)
{
  /* CRC-8, polynomial 0x07 (same construction style as the rest of the
   * codebase's checksums, e.g. xor_checksum() in core/can_protocol.py,
   * kept simple/dependency-free since this only protects 31 bytes). */
  uint8_t crc = 0x00U;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      if (crc & 0x80U) {
        crc = (uint8_t)((crc << 1) ^ 0x07U);
      } else {
        crc = (uint8_t)(crc << 1);
      }
    }
  }
  return crc;
}

static void load_defaults(device_config_t *cfg)
{
  memset(cfg, 0, sizeof(*cfg));
  cfg->magic = DEVICE_CONFIG_MAGIC;
  memcpy(cfg->device_name, "CodeMaste", 9); /* "CodeMaster" truncated to 9 chars */
  cfg->device_name_len = 9U;
  cfg->serial_len = 0U;
  cfg->vid = DEVICE_CONFIG_DEFAULT_VID;
  cfg->pid = DEVICE_CONFIG_DEFAULT_PID;
  cfg->reserved[0] = DEVICE_CONFIG_FORMAT_VERSION;
  cfg->reserved[1] = DEVICE_CONFIG_RECORD_SIZE;
  cfg->crc8 = crc8((const uint8_t *)cfg, offsetof(device_config_t, crc8));
}

static void load_ext_defaults(device_ext_config_t *cfg)
{
  memset(cfg, 0, sizeof(*cfg));
  cfg->magic = DEVICE_EXT_CONFIG_MAGIC;
  cfg->can1_baud_kbps = DEVICE_CONFIG_DEFAULT_BAUD_KBPS;
  cfg->can2_baud_kbps = DEVICE_CONFIG_DEFAULT_BAUD_KBPS;
  cfg->version = DEVICE_EXT_CONFIG_VERSION;
  cfg->crc8 = crc8((const uint8_t *)cfg, offsetof(device_ext_config_t, crc8));
}

static void load_app_cfg_defaults(device_app_cfg_t *cfg)
{
  memset(cfg, 0, sizeof(*cfg));
  cfg->can1_baud_kbps = DEVICE_CONFIG_DEFAULT_BAUD_KBPS;
  cfg->can2_baud_kbps = DEVICE_CONFIG_DEFAULT_BAUD_KBPS;
  cfg->version = DEVICE_APP_CFG_VERSION;
}

static void load_names_defaults(trigger_names_t *names)
{
  memset(names, 0, sizeof(*names));
  names->magic = TRIGGER_NAMES_MAGIC;
  names->version = TRIGGER_NAMES_VERSION;
  names->crc8 = crc8((const uint8_t *)names, offsetof(trigger_names_t, crc8));
}

/* Версионная запись VER1: magic + CRC + разумная длина. Пустая
 * (стёртая Flash = 0xFF) или битая запись означает «версии нет» —
 * остальную конфигурацию это не трогает. */
static uint8_t flash_write_config(const device_config_t *cfg,
                                  const device_ext_config_t *ext,
                                  const trigger_names_t *names);

static uint8_t fw_ver_valid(const device_fw_ver_t *ver)
{
  return ver->magic == DEVICE_CONFIG_VER_MAGIC
         && ver->len <= DEVICE_CONFIG_VERSION_MAX
         && crc8((const uint8_t *)ver, offsetof(device_fw_ver_t, crc8)) == ver->crc8;
}

static uint8_t main_record_valid(const device_config_t *rec)
{
  return rec->magic == DEVICE_CONFIG_MAGIC
         && crc8((const uint8_t *)rec, offsetof(device_config_t, crc8)) == rec->crc8
         && rec->device_name_len <= DEVICE_CONFIG_NAME_MAX
         && rec->serial_len <= DEVICE_CONFIG_SERIAL_MAX
         && ((rec->reserved[0] == 0U && rec->reserved[1] == 0U)
             || (rec->reserved[0] == DEVICE_CONFIG_FORMAT_VERSION
                 && rec->reserved[1] == DEVICE_CONFIG_RECORD_SIZE));
}

void DeviceConfig_Init(void)
{
  const device_config_t *flash_cfg = (const device_config_t *)DEVICE_CONFIG_PAGE_ADDR;
  const device_config_t *legacy_cfg =
      (const device_config_t *)DEVICE_CONFIG_PAGE_ADDR_LEGACY;
  /* Активная страница: новая (в начале Flash) или старая раскладка —
   * устройства, прошитые до переезда, держат конфиг на 0x0803D800. */
  uint32_t page_addr = DEVICE_CONFIG_PAGE_ADDR;
  uint8_t migrated = 0U;

  if (main_record_valid(flash_cfg)) {
    memcpy(&s_config, flash_cfg, sizeof(s_config));
    s_config_valid = 1U;
  } else if (main_record_valid(legacy_cfg)) {
    /* Миграция со старой страницы: записи переносятся в новую область
     * одной перезаписью в конце Init (отчёт мастера — идентификация
     * хранится в начале Flash). */
    memcpy(&s_config, legacy_cfg, sizeof(s_config));
    s_config_valid = 1U;
    page_addr = DEVICE_CONFIG_PAGE_ADDR_LEGACY;
    migrated = 1U;
  } else {
    /* Blank/corrupt page: fall back to defaults without touching Flash (a
     * write only happens on an explicit CMD_CFG_WRITE/FACTORY_RESET). */
    load_defaults(&s_config);
    s_config_valid = 0U;
  }

  /* Extended record lives right after the main one in the same page; a
   * missing/invalid record simply means 500/500 kbit/s defaults — it was
   * introduced after devices shipped, so absence must not invalidate the
   * identity record. Читается ВСЕГДА, в т.ч. при валидной основной
   * записи — ранний return здесь оставлял s_ext_config в нулях,
   * GetCanBaud отдавал 0 и CAN вообще не стартовал после загрузки. */
  const device_ext_config_t *ext =
      (const device_ext_config_t *)(page_addr + DEVICE_EXT_CONFIG_OFFSET);
  if (ext->magic == DEVICE_EXT_CONFIG_MAGIC
      && crc8((const uint8_t *)ext, offsetof(device_ext_config_t, crc8)) == ext->crc8
      && ext->can1_baud_kbps > 0U && ext->can2_baud_kbps > 0U) {
    memcpy(&s_ext_config, ext, sizeof(s_ext_config));
  } else {
    load_ext_defaults(&s_ext_config);
  }

  /* Настройки приложения — область CFGH сразу за кодом (маркерное
   * хранилище, отчёт мастера). При отсутствии области значения берутся
   * из легаси-записи CEX0 и CFGH создаётся сразу — дальше страница
   * идентификации настройки CAN не хранит. */
  load_app_cfg_defaults(&s_app_cfg);
  if (!Storage_CfgRead((uint8_t *)&s_app_cfg, sizeof(s_app_cfg))
      || s_app_cfg.version == 0U) {
    s_app_cfg.can1_baud_kbps = s_ext_config.can1_baud_kbps;
    s_app_cfg.can2_baud_kbps = s_ext_config.can2_baud_kbps;
    s_app_cfg.can1_silent = 0U;
    s_app_cfg.can1_term = 0U;
    s_app_cfg.can2_silent = 0U;
    s_app_cfg.can2_term = 0U;
    s_app_cfg.version = DEVICE_APP_CFG_VERSION;
    memset(s_app_cfg.reserved, 0, sizeof(s_app_cfg.reserved));
    (void)Storage_CfgWrite((const uint8_t *)&s_app_cfg,
                           sizeof(s_app_cfg));
  }

  /* Таблица имён триггеров — как и ext-запись, появилась после выхода
   * устройств в поле: отсутствующая/битая запись означает «имён нет»,
   * но не трогает остальную конфигурацию. */
  const trigger_names_t *names =
      (const trigger_names_t *)(page_addr + TRIGGER_NAMES_OFFSET);
  if (names->magic == TRIGGER_NAMES_MAGIC
      && names->version == TRIGGER_NAMES_VERSION
      && crc8((const uint8_t *)names, offsetof(trigger_names_t, crc8)) == names->crc8) {
    memcpy(&s_trigger_names, names, sizeof(s_trigger_names));
  } else {
    load_names_defaults(&s_trigger_names);
  }

  /* Версия ПО (VER1) — читается из нового смещения; при его отсутствии
   * пробуем legacy-offset 32: образы до переноса шили VER1 туда
   * (перетиралась ext-записью при первом же сохранении конфигурации,
   * поэтому попадётся редко — но безвредно проверить). */
  memset(&s_fw_ver, 0, sizeof(s_fw_ver));
  const device_fw_ver_t *ver =
      (const device_fw_ver_t *)(page_addr + DEVICE_CONFIG_VER_OFFSET);
  if (fw_ver_valid(ver)) {
    memcpy(&s_fw_ver, ver, sizeof(s_fw_ver));
  } else {
    ver = (const device_fw_ver_t *)(page_addr
                                    + DEVICE_CONFIG_VER_OFFSET_LEGACY);
    if (fw_ver_valid(ver)) {
      memcpy(&s_fw_ver, ver, sizeof(s_fw_ver));
    }
  }

  if (migrated) {
    /* Перенос всей страницы на новый адрес одной записью — дальше
     * устройство живёт уже на новой карте памяти. */
    (void)flash_write_config(&s_config, &s_ext_config, &s_trigger_names);
  }
}

const device_config_t *DeviceConfig_Get(void)
{
  return &s_config;
}

uint8_t DeviceConfig_IsValid(void)
{
  return s_config_valid;
}

/* Programs all records in one pass: any page rewrite (identity write,
 * CAN speed change, factory reset, trigger names commit) rewrites
 * main+extended+names together, so they survive each other's updates
 * instead of reverting to defaults on the next boot. */
static uint8_t flash_write_config(const device_config_t *cfg,
                                  const device_ext_config_t *ext,
                                  const trigger_names_t *names)
{
  HAL_FLASH_Unlock();

  /* Стирание страницы ~40 мс — кормим IWDG, чтобы запись конфигурации
   * в связке с другими ожиданиями не сбрасывала МК. */
  App_KickWatchdog();
  FLASH_EraseInitTypeDef erase_init = {
    .TypeErase   = FLASH_TYPEERASE_PAGES,
    .PageAddress = DEVICE_CONFIG_PAGE_ADDR,
    .NbPages     = 1U,
  };
  uint32_t page_error = 0U;
  if (HAL_FLASHEx_Erase(&erase_init, &page_error) != HAL_OK) {
    HAL_FLASH_Lock();
    return 0U;
  }
  App_KickWatchdog();

  const uint16_t *src = (const uint16_t *)cfg;
  uint32_t addr = DEVICE_CONFIG_PAGE_ADDR;
  /* sizeof(device_config_t) is 32 bytes (packed), i.e. 16 half-words. */
  for (uint32_t i = 0; i < (sizeof(device_config_t) / 2U); i++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src[i]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }

  src = (const uint16_t *)ext;
  addr = DEVICE_CONFIG_PAGE_ADDR + DEVICE_EXT_CONFIG_OFFSET;
  for (uint32_t i = 0; i < (sizeof(device_ext_config_t) / 2U); i++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src[i]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }

  src = (const uint16_t *)names;
  addr = DEVICE_CONFIG_PAGE_ADDR + TRIGGER_NAMES_OFFSET;
  for (uint32_t i = 0; i < (sizeof(trigger_names_t) / 2U); i++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src[i]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }

  /* Версионная запись VER1 живёт на этой же странице — стирание её
   * сносит, поэтому валидное зеркало дописываем обратно каждый раз
   * (отчёт мастера: «Версия ПО» должна показывать прошитую версию). */
  if (fw_ver_valid(&s_fw_ver)) {
    src = (const uint16_t *)&s_fw_ver;
    addr = DEVICE_CONFIG_PAGE_ADDR + DEVICE_CONFIG_VER_OFFSET;
    for (uint32_t i = 0; i < (sizeof(device_fw_ver_t) / 2U); i++) {
      if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src[i]) != HAL_OK) {
        HAL_FLASH_Lock();
        return 0U;
      }
      addr += 2U;
    }
  }

  HAL_FLASH_Lock();
  /* Сверяем реальное содержимое страницы: частично прошитая запись
   * (brown-out во время программирования) иначе всплывала бы только
   * после следующего включения — устройство теряло имя/серийник. */
  return (memcmp((const void *)DEVICE_CONFIG_PAGE_ADDR, cfg,
                 sizeof(device_config_t)) == 0
          && memcmp((const void *)(DEVICE_CONFIG_PAGE_ADDR + DEVICE_EXT_CONFIG_OFFSET),
                    ext, sizeof(device_ext_config_t)) == 0
          && memcmp((const void *)(DEVICE_CONFIG_PAGE_ADDR + TRIGGER_NAMES_OFFSET),
                    names, sizeof(trigger_names_t)) == 0
          && (!fw_ver_valid(&s_fw_ver)
              || memcmp((const void *)(DEVICE_CONFIG_PAGE_ADDR
                                       + DEVICE_CONFIG_VER_OFFSET),
                        &s_fw_ver, sizeof(device_fw_ver_t)) == 0))
             ? 1U
             : 0U;
}

uint8_t DeviceConfig_Write(const uint8_t *device_name, uint8_t device_name_len,
                            const uint8_t *serial, uint8_t serial_len,
                            uint16_t vid, uint16_t pid)
{
  if (device_name_len > DEVICE_CONFIG_NAME_MAX || serial_len > DEVICE_CONFIG_SERIAL_MAX) {
    return 0U;
  }

  device_config_t new_cfg;
  memset(&new_cfg, 0, sizeof(new_cfg));
  new_cfg.magic = DEVICE_CONFIG_MAGIC;
  new_cfg.device_name_len = device_name_len;
  if (device_name_len > 0U) {
    memcpy(new_cfg.device_name, device_name, device_name_len);
  }
  new_cfg.serial_len = serial_len;
  if (serial_len > 0U) {
    memcpy(new_cfg.serial, serial, serial_len);
  }
  new_cfg.vid = (vid != 0U) ? vid : DEVICE_CONFIG_DEFAULT_VID;
  new_cfg.pid = (pid != 0U) ? pid : DEVICE_CONFIG_DEFAULT_PID;
  new_cfg.reserved[0] = DEVICE_CONFIG_FORMAT_VERSION;
  new_cfg.reserved[1] = DEVICE_CONFIG_RECORD_SIZE;
  new_cfg.crc8 = crc8((const uint8_t *)&new_cfg, offsetof(device_config_t, crc8));

  if (memcmp(&new_cfg, &s_config, sizeof(new_cfg)) == 0) {
    return 1U;
  }

  if (!flash_write_config(&new_cfg, &s_ext_config, &s_trigger_names)) {
    return 0U;
  }

  memcpy(&s_config, &new_cfg, sizeof(s_config));
  s_config_valid = 1U;
  return 1U;
}

uint8_t DeviceConfig_FactoryReset(void)
{
  device_config_t defaults;
  load_defaults(&defaults);
  device_ext_config_t ext_defaults;
  load_ext_defaults(&ext_defaults);
  /* «Заводские настройки» стирают и имена триггеров — устройство
   * возвращается в состояние «с нуля», как при Trigger_ClearAll. */
  trigger_names_t names_defaults;
  load_names_defaults(&names_defaults);

  if (!flash_write_config(&defaults, &ext_defaults, &names_defaults)) {
    return 0U;
  }

  memcpy(&s_config, &defaults, sizeof(s_config));
  memcpy(&s_ext_config, &ext_defaults, sizeof(s_ext_config));
  memcpy(&s_trigger_names, &names_defaults, sizeof(s_trigger_names));
  s_config_valid = 1U;
  return 1U;
}

static uint8_t is_supported_can_baud(uint32_t baud_kbps)
{
  switch (baud_kbps) {
    case 1000U: case 500U: case 250U: case 125U:
    case 100U:  case 83U:  case 50U:  case 20U:
    case 10U:
      return 1U;
    default:
      return 0U;
  }
}

uint32_t DeviceConfig_GetCanBaud(uint8_t channel)
{
  /* Авторитетный источник — область CFGH (зеркало s_app_cfg); CEX0 в
   * странице идентификации остаётся совместимым дублем для старых
   * прошивок после отката. */
  uint32_t baud = (channel == 0U) ? s_app_cfg.can1_baud_kbps
                                  : s_app_cfg.can2_baud_kbps;
  /* Даже если ext-запись прошла CRC, но содержит неподдерживаемый бод
   * (повреждение Flash, несовместимая версия) — не скармливаем его
   * configure_bit_timing: иначе CAN не стартует вообще (полевой лог:
   * baud=0 в статистике после сброса). */
  return is_supported_can_baud(baud) ? baud : DEVICE_CONFIG_DEFAULT_BAUD_KBPS;
}

uint8_t DeviceConfig_SetCanBaud(uint8_t channel, uint32_t baud_kbps)
{
  if (channel > 1U || !is_supported_can_baud(baud_kbps)) {
    return 0U;
  }

  device_ext_config_t new_ext = s_ext_config;
  if (channel == 0U) {
    new_ext.can1_baud_kbps = (uint16_t)baud_kbps;
  } else {
    new_ext.can2_baud_kbps = (uint16_t)baud_kbps;
  }
  new_ext.crc8 = crc8((const uint8_t *)&new_ext, offsetof(device_ext_config_t, crc8));

  if (memcmp(&new_ext, &s_ext_config, sizeof(new_ext)) == 0) {
    return 1U;
  }

  if (!flash_write_config(&s_config, &new_ext, &s_trigger_names)) {
    return 0U;
  }

  memcpy(&s_ext_config, &new_ext, sizeof(s_ext_config));
  /* Дублируем скорость в область CFGH за кодом — каноничное место
   * настроек приложения (маркерное хранилище). */
  s_app_cfg.can1_baud_kbps = new_ext.can1_baud_kbps;
  s_app_cfg.can2_baud_kbps = new_ext.can2_baud_kbps;
  (void)Storage_CfgWrite((const uint8_t *)&s_app_cfg, sizeof(s_app_cfg));
  /* Страница теперь содержит валидную основную запись (пусть и дефолтную,
   * если конфиг был повреждён) — отмечаем её как действительную. */
  s_config_valid = 1U;
  return 1U;
}

uint8_t DeviceConfig_GetCanSilent(uint8_t channel)
{
  return (channel == 0U) ? s_app_cfg.can1_silent : s_app_cfg.can2_silent;
}

uint8_t DeviceConfig_GetCanTerm(uint8_t channel)
{
  return (channel == 0U) ? s_app_cfg.can1_term : s_app_cfg.can2_term;
}

uint8_t DeviceConfig_SetCanMode(uint8_t channel, uint8_t silent,
                                uint8_t term)
{
  if (channel > 1U) {
    return 0U;
  }
  uint8_t s = silent ? 1U : 0U;
  uint8_t t = term ? 1U : 0U;
  uint8_t *silent_p = (channel == 0U) ? &s_app_cfg.can1_silent
                                      : &s_app_cfg.can2_silent;
  uint8_t *term_p = (channel == 0U) ? &s_app_cfg.can1_term
                                    : &s_app_cfg.can2_term;
  if (*silent_p == s && *term_p == t) {
    return 1U; /* без изменений — страницу не трогаем */
  }
  *silent_p = s;
  *term_p = t;
  return Storage_CfgWrite((const uint8_t *)&s_app_cfg,
                          sizeof(s_app_cfg));
}

const uint8_t *DeviceConfig_GetTriggerName(uint8_t index, uint8_t *len_out)
{
  if (index >= TRIGGER_NAME_MAX) {
    return NULL;
  }
  const char *name = s_trigger_names.names[index];
  /* Имя занимает до TRIGGER_NAME_LEN байт без терминатора — фактическая
   * длина до первого нулевого байта (или весь слот). */
  uint8_t len = 0U;
  while (len < TRIGGER_NAME_LEN && name[len] != 0) {
    len++;
  }
  if (len_out != NULL) {
    *len_out = len;
  }
  return (len > 0U) ? (const uint8_t *)name : NULL;
}

uint8_t DeviceConfig_StageTriggerName(uint8_t index, const uint8_t *name,
                                      uint8_t len)
{
  if (index >= TRIGGER_NAME_MAX || len > TRIGGER_NAME_LEN) {
    return 0U;
  }
  memset(s_trigger_names.names[index], 0, TRIGGER_NAME_LEN);
  if (len > 0U && name != NULL) {
    memcpy(s_trigger_names.names[index], name, len);
  }
  return 1U;
}

const uint8_t *DeviceConfig_GetFwVersion(uint8_t *len_out)
{
  if (!fw_ver_valid(&s_fw_ver)) {
    if (len_out != NULL) {
      *len_out = 0U;
    }
    return NULL;
  }
  if (len_out != NULL) {
    *len_out = s_fw_ver.len;
  }
  return (const uint8_t *)s_fw_ver.version;
}

uint8_t DeviceConfig_CommitTriggerNames(void)
{
  /* RAM-зеркало могло собираться с чистого листа (blank-страница) —
   * заголовок и CRC выставляем здесь, чтобы записанная запись прошла
   * валидацию при следующем старте. */
  s_trigger_names.magic = TRIGGER_NAMES_MAGIC;
  s_trigger_names.version = TRIGGER_NAMES_VERSION;
  s_trigger_names.crc8 =
      crc8((const uint8_t *)&s_trigger_names, offsetof(trigger_names_t, crc8));
  return flash_write_config(&s_config, &s_ext_config, &s_trigger_names);
}
