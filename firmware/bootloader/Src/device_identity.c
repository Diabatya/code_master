#include <string.h>

#include "device_identity.h"

/* CRC-8, полином 0x07 — тот же алгоритм, что device_config.c::crc8
 * приложения. */
static uint8_t ident_crc8(const uint8_t *data, uint32_t len)
{
  uint8_t crc = 0x00U;
  for (uint32_t i = 0U; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; bit++) {
      if (crc & 0x80U) {
        crc = (uint8_t)((crc << 1) ^ 0x07U);
      } else {
        crc = (uint8_t)(crc << 1);
      }
    }
  }
  return crc;
}

/* Чтение Flash по абсолютному адресу — __weak, чтобы host-харнесс
 * (firmware/application/tests) подставлял буфер вместо реальных
 * адресов. */
__attribute__((weak)) void ident_flash_read(uint32_t addr, void *dst,
                                            uint32_t len)
{
  memcpy(dst, (const void *)addr, len);
}

uint8_t DeviceIdentity_Read(device_identity_t *out)
{
  static const uint32_t addrs[2] = {
    DEVICE_INFO_ADDR, DEVICE_INFO_ADDR_LEGACY,
  };
  for (uint8_t i = 0U; i < 2U; i++) {
    device_identity_t rec;
    ident_flash_read(addrs[i], &rec, sizeof(rec));
    if (rec.magic == DEVICE_CONFIG_MAGIC
        && ident_crc8((const uint8_t *)&rec, sizeof(rec) - 1U)
           == rec.crc8) {
      *out = rec;
      return 1U;
    }
  }
  return 0U;
}
