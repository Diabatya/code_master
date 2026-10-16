/* Host-харнесс CM-F04: чтение идентичности загрузчиком из страницы
 * CFG0 (актуальный адрес, легаси-адрес, отказ на битой записи).
 * Компилируется с device_identity.c загрузчика; ident_flash_read
 * (__weak) подменяется буфером. */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "device_identity.h"

/* Псевдо-Flash: два окна по адресам DEVICE_INFO_ADDR/_LEGACY. */
static uint8_t s_flash_new[2048];
static uint8_t s_flash_legacy[2048];

void ident_flash_read(uint32_t addr, void *dst, uint32_t len)
{
  if (addr >= DEVICE_INFO_ADDR
      && addr + len <= DEVICE_INFO_ADDR + sizeof(s_flash_new)) {
    memcpy(dst, s_flash_new + (addr - DEVICE_INFO_ADDR), len);
    return;
  }
  if (addr >= DEVICE_INFO_ADDR_LEGACY
      && addr + len <= DEVICE_INFO_ADDR_LEGACY + sizeof(s_flash_legacy)) {
    memcpy(dst, s_flash_legacy + (addr - DEVICE_INFO_ADDR_LEGACY), len);
    return;
  }
  memset(dst, 0xFF, len); /* за пределами окон — стёртая Flash */
}

static uint8_t crc8(const uint8_t *data, uint32_t len)
{
  uint8_t crc = 0U;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; bit++) {
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U)
                          : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

static void write_identity(uint8_t *page, const char *name,
                           const char *serial)
{
  device_identity_t rec;
  memset(&rec, 0, sizeof(rec));
  rec.magic = DEVICE_CONFIG_MAGIC;
  rec.name_len = (uint8_t)strlen(name);
  memcpy(rec.name, name, rec.name_len);
  rec.serial_len = (uint8_t)strlen(serial);
  memcpy(rec.serial, serial, rec.serial_len);
  rec.vid = 0x0483U;
  rec.pid = 0x5740U;
  rec.crc8 = crc8((const uint8_t *)&rec, sizeof(rec) - 1U);
  memcpy(page, &rec, sizeof(rec));
}

static int fails = 0;
#define CHECK(cond, msg) do { \
    if (!(cond)) { printf("FAIL: %s\n", msg); fails++; } \
    else { printf("PASS: %s\n", msg); } \
  } while (0)

int main(void)
{
  _Static_assert(sizeof(device_identity_t) == 32U,
                 "device_identity_t must match device_config_t");
  device_identity_t out;

  /* 1. Оба адреса стёрты (0xFF) — записи нет. */
  memset(s_flash_new, 0xFF, sizeof(s_flash_new));
  memset(s_flash_legacy, 0xFF, sizeof(s_flash_legacy));
  CHECK(DeviceIdentity_Read(&out) == 0U, "blank pages -> no identity");

  /* 2. Валидная запись на актуальной странице. */
  write_identity(s_flash_new, "2 CAN", "SN0123");
  CHECK(DeviceIdentity_Read(&out) == 1U, "new page read");
  CHECK(out.name_len == 5U && memcmp(out.name, "2 CAN", 5) == 0,
        "device type name preserved");
  CHECK(out.serial_len == 6U && memcmp(out.serial, "SN0123", 6) == 0,
        "serial preserved");

  /* 3. Битый CRC — отказ (страница повреждена, не показывать мусор). */
  s_flash_new[10] ^= 0x55U;
  CHECK(DeviceIdentity_Read(&out) == 0U, "corrupt record rejected");

  /* 4. Актуальная страница пуста, легаси валидна — плата до переноса. */
  memset(s_flash_new, 0xFF, sizeof(s_flash_new));
  write_identity(s_flash_legacy, "2CAN FD", "OLD999");
  CHECK(DeviceIdentity_Read(&out) == 1U, "legacy page fallback");
  CHECK(out.name_len == 7U && memcmp(out.name, "2CAN FD", 7) == 0,
        "legacy name read");
  CHECK(out.serial_len == 6U && memcmp(out.serial, "OLD999", 6) == 0,
        "legacy serial read");

  /* 5. Приоритет актуальной страницы над легаси. */
  write_identity(s_flash_new, "2 CAN", "NEW777");
  CHECK(DeviceIdentity_Read(&out) == 1U, "both pages -> read ok");
  CHECK(out.serial_len == 6U && memcmp(out.serial, "NEW777", 6) == 0,
        "new page wins over legacy");

  printf(fails ? "FAILURES: %d\n" : "ALL PASS\n", fails);
  return fails ? 1 : 0;
}
