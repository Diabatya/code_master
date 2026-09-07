#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "device_config.h"

_Static_assert(sizeof(device_config_t) == 32U, "device_config_t layout changed");
_Static_assert(offsetof(device_config_t, magic) == 0U, "magic offset");
_Static_assert(offsetof(device_config_t, device_name_len) == 4U, "name length offset");
_Static_assert(offsetof(device_config_t, device_name) == 5U, "name offset");
_Static_assert(offsetof(device_config_t, serial_len) == 14U, "serial length offset");
_Static_assert(offsetof(device_config_t, serial) == 15U, "serial offset");
_Static_assert(offsetof(device_config_t, crc8) == 31U, "CRC offset");

static uint8_t crc8(const uint8_t *data, size_t len)
{
  uint8_t crc = 0U;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; bit++) {
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

int main(void)
{
  device_config_t cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.magic = DEVICE_CONFIG_MAGIC;
  cfg.device_name_len = 6U;
  memcpy(cfg.device_name, "DEVICE", 6U);
  cfg.serial_len = 6U;
  memcpy(cfg.serial, "ABC123", 6U);
  cfg.vid = DEVICE_CONFIG_DEFAULT_VID;
  cfg.pid = DEVICE_CONFIG_DEFAULT_PID;
  cfg.reserved[0] = DEVICE_CONFIG_FORMAT_VERSION;
  cfg.reserved[1] = DEVICE_CONFIG_RECORD_SIZE;
  cfg.crc8 = crc8((const uint8_t *)&cfg, offsetof(device_config_t, crc8));

  device_config_t roundtrip;
  memcpy(&roundtrip, &cfg, sizeof(roundtrip));
  if (memcmp(&cfg, &roundtrip, sizeof(cfg)) != 0) {
    fprintf(stderr, "FAIL: config round-trip mismatch\n");
    return 1;
  }
  if (crc8((const uint8_t *)&roundtrip, offsetof(device_config_t, crc8)) != roundtrip.crc8) {
    fprintf(stderr, "FAIL: config CRC mismatch\n");
    return 1;
  }
  printf("PASS: device_config_t round-tripped, size=%zu bytes\n", sizeof(cfg));
  return 0;
}
