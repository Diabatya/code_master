/* Device configuration storage implementation. See device_config.h and
 * firmware/PROTOCOL.md Part 3 for the on-Flash layout and rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "device_config.h"

static device_config_t s_config;
static device_ext_config_t s_ext_config;
static trigger_names_t s_trigger_names;
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

static void load_names_defaults(trigger_names_t *names)
{
  memset(names, 0, sizeof(*names));
  names->magic = TRIGGER_NAMES_MAGIC;
  names->version = TRIGGER_NAMES_VERSION;
  names->crc8 = crc8((const uint8_t *)names, offsetof(trigger_names_t, crc8));
}

void DeviceConfig_Init(void)
{
  const device_config_t *flash_cfg = (const device_config_t *)DEVICE_CONFIG_PAGE_ADDR;

  if (flash_cfg->magic == DEVICE_CONFIG_MAGIC) {
    uint8_t computed = crc8((const uint8_t *)flash_cfg, offsetof(device_config_t, crc8));
    if (computed == flash_cfg->crc8
        && flash_cfg->device_name_len <= DEVICE_CONFIG_NAME_MAX
        && flash_cfg->serial_len <= DEVICE_CONFIG_SERIAL_MAX
        && ((flash_cfg->reserved[0] == 0U && flash_cfg->reserved[1] == 0U)
            || (flash_cfg->reserved[0] == DEVICE_CONFIG_FORMAT_VERSION
                && flash_cfg->reserved[1] == DEVICE_CONFIG_RECORD_SIZE))) {
      memcpy(&s_config, flash_cfg, sizeof(s_config));
      s_config_valid = 1U;
    }
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
      (const device_ext_config_t *)(DEVICE_CONFIG_PAGE_ADDR + DEVICE_EXT_CONFIG_OFFSET);
  if (ext->magic == DEVICE_EXT_CONFIG_MAGIC
      && crc8((const uint8_t *)ext, offsetof(device_ext_config_t, crc8)) == ext->crc8
      && ext->can1_baud_kbps > 0U && ext->can2_baud_kbps > 0U) {
    memcpy(&s_ext_config, ext, sizeof(s_ext_config));
  } else {
    load_ext_defaults(&s_ext_config);
  }

  /* Таблица имён триггеров — как и ext-запись, появилась после выхода
   * устройств в поле: отсутствующая/битая запись означает «имён нет»,
   * но не трогает остальную конфигурацию. */
  const trigger_names_t *names =
      (const trigger_names_t *)(DEVICE_CONFIG_PAGE_ADDR + TRIGGER_NAMES_OFFSET);
  if (names->magic == TRIGGER_NAMES_MAGIC
      && names->version == TRIGGER_NAMES_VERSION
      && crc8((const uint8_t *)names, offsetof(trigger_names_t, crc8)) == names->crc8) {
    memcpy(&s_trigger_names, names, sizeof(s_trigger_names));
  } else {
    load_names_defaults(&s_trigger_names);
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

  HAL_FLASH_Lock();
  /* Сверяем реальное содержимое страницы: частично прошитая запись
   * (brown-out во время программирования) иначе всплывала бы только
   * после следующего включения — устройство теряло имя/серийник. */
  return (memcmp((const void *)DEVICE_CONFIG_PAGE_ADDR, cfg,
                 sizeof(device_config_t)) == 0
          && memcmp((const void *)(DEVICE_CONFIG_PAGE_ADDR + DEVICE_EXT_CONFIG_OFFSET),
                    ext, sizeof(device_ext_config_t)) == 0
          && memcmp((const void *)(DEVICE_CONFIG_PAGE_ADDR + TRIGGER_NAMES_OFFSET),
                    names, sizeof(trigger_names_t)) == 0)
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
    case 100U:  case 50U:  case 20U:  case 10U:
      return 1U;
    default:
      return 0U;
  }
}

uint32_t DeviceConfig_GetCanBaud(uint8_t channel)
{
  uint32_t baud = (channel == 0U) ? s_ext_config.can1_baud_kbps
                                  : s_ext_config.can2_baud_kbps;
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
  /* Страница теперь содержит валидную основную запись (пусть и дефолтную,
   * если конфиг был повреждён) — отмечаем её как действительную. */
  s_config_valid = 1U;
  return 1U;
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
