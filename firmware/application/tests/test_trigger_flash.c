/* Host-side unit test for the trigger Flash persistence algorithm
 * (firmware/application/Src/trigger.c: program_store() + Trigger_Init(),
 * storage v3: "TRGH" header + packed record list). Emulates the STM32F1
 * Flash pool as a plain in-memory buffer (no HAL/hardware needed) and
 * checks that writing all records and reading them back round-trips
 * every byte, including crc8 — this is the regression test for the
 * CURSOR_FIX_PROMPT.md 2.1 bug where sizeof(trigger_t) (53, odd)
 * truncated the halfword write loop and desynced the read stride from
 * the write stride.
 *
 * Build & run (from firmware/application/):
 *   gcc -std=c11 -Wall -Wextra -Itests/stubs -IInc tests/test_trigger_flash.c -o /tmp/test_trigger_flash
 *   /tmp/test_trigger_flash
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>

#include "trigger.h"

static uint8_t crc8(const uint8_t *data, uint32_t len)
{
  uint8_t crc = 0x00U;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

/* Emulated trigger pool (v3: header + packed record list), pre-erased to
 * 0xFF like real NOR Flash. */
static uint8_t s_flash_pool[TRIGGER_FLASH_END - TRIGGER_POOL_BASE];

/* Mirrors program_store()'s halfword-by-halfword program loop, but writes
 * into s_flash_pool instead of calling HAL_FLASH_Program(). Records are
 * packed right after the 16-byte "TRGH" header. */
static void emulated_flash_write_all_triggers(const trigger_t triggers[TRIGGER_MAX_RECORDS])
{
  memset(s_flash_pool, 0xFF, sizeof(s_flash_pool));

  trigger_header_t h;
  memset(&h, 0, sizeof(h));
  h.magic = TRIGGER_HEADER_MAGIC;
  h.version = TRIGGER_STORE_VERSION;
  h.count = TRIGGER_MAX_RECORDS;
  h.generation = 1U;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(trigger_header_t, crc8));
  memcpy(s_flash_pool, &h, sizeof(h));

  uint32_t offset = TRIGGER_HEADER_SIZE;
  for (uint8_t i = 0; i < TRIGGER_MAX_RECORDS; i++) {
    const uint16_t *src = (const uint16_t *)&triggers[i];
    for (uint32_t w = 0; w < (sizeof(trigger_t) / 2U); w++) {
      memcpy(&s_flash_pool[offset], &src[w], sizeof(uint16_t));
      offset += 2U;
    }
  }
}

/* Mirrors Trigger_Init()'s read loop: records follow the header with a
 * fixed sizeof(trigger_t) stride. */
static void emulated_trigger_init(trigger_t out[TRIGGER_MAX_RECORDS])
{
  const uint8_t *page = s_flash_pool + TRIGGER_HEADER_SIZE;
  for (uint8_t i = 0; i < TRIGGER_MAX_RECORDS; i++) {
    const trigger_t *flash_t = (const trigger_t *)(page + (uint32_t)i * sizeof(trigger_t));
    if (flash_t->magic == TRIGGER_MAGIC) {
      uint8_t computed = crc8((const uint8_t *)flash_t, offsetof(trigger_t, crc8));
      if (computed == flash_t->crc8) {
        memcpy(&out[i], flash_t, sizeof(trigger_t));
        continue;
      }
    }
    memset(&out[i], 0, sizeof(out[i]));
  }
}

int main(void)
{
  int failures = 0;

  printf("sizeof(trigger_t) = %zu bytes\n", sizeof(trigger_t));
  if ((sizeof(trigger_t) % 2U) != 0U) {
    printf("FAIL: sizeof(trigger_t) is odd, halfword Flash writes will truncate data\n");
    failures++;
  }
  if ((TRIGGER_HEADER_SIZE + TRIGGER_MAX_RECORDS * sizeof(trigger_t))
      > (TRIGGER_FLASH_END - TRIGGER_POOL_BASE)) {
    printf("FAIL: %d triggers of %zu bytes + header do not fit in the %u-byte trigger pool\n",
           TRIGGER_MAX_RECORDS, sizeof(trigger_t),
           (unsigned)(TRIGGER_FLASH_END - TRIGGER_POOL_BASE));
    failures++;
  }

  trigger_t originals[TRIGGER_MAX_RECORDS];
  memset(originals, 0, sizeof(originals));
  for (uint8_t i = 0; i < TRIGGER_MAX_RECORDS; i++) {
    trigger_t *t = &originals[i];
    t->magic = TRIGGER_MAGIC;
    t->enabled = (i % 2U);
    t->rx_channel = (i % 2U);
    t->rx_extended = 1U;
    t->rx_id = 0x100U + i;
    t->rx_id_mask = 0x7FFU;
    t->rx_dlc = 8U;
    for (uint8_t b = 0; b < 8U; b++) {
      t->rx_data[b] = (uint8_t)(i * 8U + b);
      t->rx_data_mask[b] = 0xFFU;
    }
    t->tx_channel = (uint8_t)(1U - (i % 2U));
    t->tx_extended = 0U;
    t->tx_id = 0x200U + i;
    t->tx_dlc = 8U;
    for (uint8_t b = 0; b < 8U; b++) {
      t->tx_data[b] = (uint8_t)(0x80U + i * 8U + b);
    }
    t->delay_ms = (uint16_t)(i * 100U);
    /* crc8 must be computed last, over every preceding byte, matching
     * load_default()/Trigger_Set() in trigger.c. */
    t->crc8 = crc8((const uint8_t *)t, offsetof(trigger_t, crc8));
  }

  emulated_flash_write_all_triggers(originals);

  trigger_t roundtripped[TRIGGER_MAX_RECORDS];
  emulated_trigger_init(roundtripped);

  for (uint8_t i = 0; i < TRIGGER_MAX_RECORDS; i++) {
    if (memcmp(&originals[i], &roundtripped[i], sizeof(trigger_t)) != 0) {
      printf("FAIL: slot %u did not round-trip byte-for-byte\n", i);
      failures++;
      continue;
    }
    uint8_t computed = crc8((const uint8_t *)&roundtripped[i], offsetof(trigger_t, crc8));
    if (computed != roundtripped[i].crc8) {
      printf("FAIL: slot %u crc8 mismatch after round-trip (computed=0x%02x stored=0x%02x)\n",
             i, computed, roundtripped[i].crc8);
      failures++;
    }
  }

  if (failures == 0) {
    printf("PASS: all %u trigger slots round-tripped correctly\n", (unsigned)TRIGGER_MAX_RECORDS);
    return 0;
  }
  printf("%d check(s) failed\n", failures);
  return 1;
}
