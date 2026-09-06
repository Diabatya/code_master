/* Device configuration storage implementation. See device_config.h and
 * firmware/PROTOCOL.md Part 3 for the on-Flash layout and rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "device_config.h"

static device_config_t s_config;

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
      return;
    }
  }
  /* Blank/corrupt page: fall back to defaults without touching Flash (a
   * write only happens on an explicit CMD_CFG_WRITE/FACTORY_RESET). */
  load_defaults(&s_config);
}

const device_config_t *DeviceConfig_Get(void)
{
  return &s_config;
}

static uint8_t flash_write_config(const device_config_t *cfg)
{
  HAL_FLASH_Unlock();

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

  HAL_FLASH_Lock();
  return 1U;
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

  if (!flash_write_config(&new_cfg)) {
    return 0U;
  }

  memcpy(&s_config, &new_cfg, sizeof(s_config));
  return 1U;
}

uint8_t DeviceConfig_FactoryReset(void)
{
  device_config_t defaults;
  load_defaults(&defaults);

  if (!flash_write_config(&defaults)) {
    return 0U;
  }

  memcpy(&s_config, &defaults, sizeof(s_config));
  return 1U;
}
