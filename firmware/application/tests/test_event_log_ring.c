/* Host-side unit test for the event log ring algorithm
 * (firmware/application/Src/event_log.c: EventLog_Init()/Add()/Read()).
 * Emulates the Flash pool as a plain in-memory buffer (erase = fill with
 * 0xFF, halfword program = plain byte writes — no HAL/hardware needed)
 * and re-implements the exact same slot/seq bookkeeping as event_log.c to
 * check: fresh-pool boot, ascending-seq pagination before any wraparound,
 * and after wraparound past the end of the pool (the tricky part: the
 * ring must still hand back records oldest-to-newest, and old records
 * must actually disappear once their page is overwritten).
 *
 * Build & run (from firmware/application/):
 *   gcc -std=c11 -Wall -Wextra -Itests/stubs -IInc tests/test_event_log_ring.c \
 *       -o /tmp/test_event_log_ring && /tmp/test_event_log_ring
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>

#include "event_log.h"

#define EVENT_LOG_MAGIC 0x4C45U

typedef struct __attribute__((packed)) {
  uint16_t magic;
  uint32_t seq;
  uint32_t timestamp_ms;
  uint8_t  type;
  uint8_t  channel;
  uint8_t  code;
  uint8_t  crc8;
  uint8_t  pad[2];
} wire_record_t;

static uint8_t s_pool[EVENT_LOG_TOTAL_SLOTS * EVENT_LOG_RECORD_SIZE];
static uint32_t s_next_seq;
static uint16_t s_next_slot;

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

static uint8_t *slot_ptr(uint16_t slot) { return &s_pool[(uint32_t)slot * EVENT_LOG_RECORD_SIZE]; }

static uint8_t slot_is_blank(uint16_t slot)
{
  const uint8_t *p = slot_ptr(slot);
  for (uint32_t i = 0; i < EVENT_LOG_RECORD_SIZE; i++) {
    if (p[i] != 0xFFU) return 0U;
  }
  return 1U;
}

static uint8_t load_slot(uint16_t slot, wire_record_t *out)
{
  memcpy(out, slot_ptr(slot), sizeof(*out));
  if (out->magic != EVENT_LOG_MAGIC || out->seq == 0U) return 0U;
  return crc8((const uint8_t *)out, offsetof(wire_record_t, crc8)) == out->crc8;
}

static void erase_page_for_slot(uint16_t slot)
{
  uint32_t page_start = (slot / EVENT_LOG_SLOTS_PER_PAGE) * EVENT_LOG_SLOTS_PER_PAGE;
  memset(slot_ptr((uint16_t)page_start), 0xFF, EVENT_LOG_FLASH_PAGE);
}

/* Mirrors EventLog_Init(): scan for highest seq, resume right after it. */
static void sim_init(void)
{
  uint32_t best_seq = 0U;
  int32_t best_slot = -1;
  for (uint16_t slot = 0U; slot < EVENT_LOG_TOTAL_SLOTS; slot++) {
    wire_record_t rec;
    if (load_slot(slot, &rec) && rec.seq >= best_seq) {
      best_seq = rec.seq;
      best_slot = (int32_t)slot;
    }
  }
  if (best_slot >= 0) {
    s_next_seq = best_seq + 1U;
    s_next_slot = (uint16_t)(((uint32_t)best_slot + 1U) % EVENT_LOG_TOTAL_SLOTS);
  } else {
    s_next_seq = 1U;
    s_next_slot = 0U;
  }
}

/* Mirrors EventLog_Add() (no throttling — the test drives seq directly). */
static void sim_add(uint8_t type, uint8_t channel, uint8_t code)
{
  wire_record_t rec;
  memset(&rec, 0, sizeof(rec));
  rec.magic = EVENT_LOG_MAGIC;
  rec.seq = s_next_seq;
  rec.timestamp_ms = s_next_seq * 10U;
  rec.type = type;
  rec.channel = channel;
  rec.code = code;
  rec.crc8 = crc8((const uint8_t *)&rec, offsetof(wire_record_t, crc8));

  if (!slot_is_blank(s_next_slot)) {
    erase_page_for_slot(s_next_slot);
  }
  memcpy(slot_ptr(s_next_slot), &rec, sizeof(rec));

  s_next_seq++;
  s_next_slot = (uint16_t)((s_next_slot + 1U) % EVENT_LOG_TOTAL_SLOTS);
}

/* Mirrors EventLog_Read(). */
static uint8_t sim_read(uint32_t after_seq, event_log_entry_t *out, uint8_t max_count)
{
  uint8_t count = 0U;
  for (uint16_t i = 0U; i < EVENT_LOG_TOTAL_SLOTS && count < max_count; i++) {
    uint16_t slot = (uint16_t)(((uint32_t)s_next_slot + i) % EVENT_LOG_TOTAL_SLOTS);
    wire_record_t rec;
    if (!load_slot(slot, &rec) || rec.seq <= after_seq) continue;
    out[count].seq = rec.seq;
    out[count].timestamp_ms = rec.timestamp_ms;
    out[count].type = rec.type;
    out[count].channel = rec.channel;
    out[count].code = rec.code;
    count++;
  }
  return count;
}

int main(void)
{
  int failures = 0;
  memset(s_pool, 0xFF, sizeof(s_pool));

  /* 1) Fresh (blank) pool: Init must resume at seq=1, slot=0. */
  sim_init();
  if (s_next_seq != 1U || s_next_slot != 0U) {
    printf("FAIL: fresh pool did not resume at seq=1/slot=0 (got seq=%u slot=%u)\n",
           s_next_seq, s_next_slot);
    failures++;
  }

  /* 2) Partial fill (less than one lap): Read() must return ascending
   * seq order and skip the still-blank tail. */
  for (uint32_t i = 0; i < 10U; i++) {
    sim_add(2U, (uint8_t)(i % 2U), (uint8_t)i);
  }
  event_log_entry_t page[32];
  uint8_t n = sim_read(0U, page, 32U);
  if (n != 10U) {
    printf("FAIL: partial-fill read returned %u entries, expected 10\n", n);
    failures++;
  }
  for (uint8_t i = 0U; i < n; i++) {
    if (page[i].seq != i + 1U) {
      printf("FAIL: partial-fill entry %u has seq=%u, expected %u\n", i, page[i].seq, i + 1U);
      failures++;
      break;
    }
  }

  /* 3) Pagination: after_seq lets the caller resume mid-log. */
  n = sim_read(5U, page, 32U);
  if (n != 5U || page[0].seq != 6U || page[n - 1].seq != 10U) {
    printf("FAIL: after_seq=5 pagination returned wrong slice (n=%u first=%u last=%u)\n",
           n, n ? page[0].seq : 0U, n ? page[n - 1].seq : 0U);
    failures++;
  }

  /* 4) Wrap the whole pool past capacity: oldest records must be gone
   * (their page got erased), remaining ones must still read back in
   * ascending seq order with no gaps/duplicates. Erasure happens a
   * whole page (EVENT_LOG_SLOTS_PER_PAGE records) at a time, so the
   * "oldest alive == total_written - TOTAL_SLOTS + 1" formula only
   * holds exactly when the write count beyond the first full lap is
   * itself a multiple of the page size (chosen below) — otherwise the
   * currently-being-filled page is left partially blank and the still
   * page-aligned older pages survive intact (also correct, just not
   * expressible as a single linear formula, see comment in sim_add()). */
  uint32_t total_written = 10U;
  uint32_t fill_first_lap = (uint32_t)EVENT_LOG_TOTAL_SLOTS - total_written;
  uint32_t extra_pages = 2U * (uint32_t)EVENT_LOG_SLOTS_PER_PAGE;
  for (uint32_t i = 0; i < fill_first_lap + extra_pages; i++) {
    sim_add(6U, 0U, 0U);
  }
  total_written += fill_first_lap + extra_pages;

  /* EventLog_Read()'s max_count is a uint8_t (mirrors the real
   * CMD_EVENT_LOG wire limit) — pull the whole ring via the same
   * pagination loop the desktop side uses (SerialManager.read_full_event_log). */
  event_log_entry_t all[EVENT_LOG_TOTAL_SLOTS];
  uint32_t total_n = 0U;
  uint32_t cursor_seq = 0U;
  for (;;) {
    uint8_t got = sim_read(cursor_seq, page, 32U);
    if (got == 0U) break;
    memcpy(&all[total_n], page, (uint32_t)got * sizeof(page[0]));
    total_n += got;
    cursor_seq = page[got - 1U].seq;
    if (got < 32U) break;
  }

  uint32_t oldest_alive = total_written - (uint32_t)EVENT_LOG_TOTAL_SLOTS + 1U;
  if (total_n != (uint32_t)EVENT_LOG_TOTAL_SLOTS) {
    printf("FAIL: expected a full ring (%u entries) after exact-page wraparound, got %u\n",
           (unsigned)EVENT_LOG_TOTAL_SLOTS, total_n);
    failures++;
  }
  if (total_n == 0U || all[0].seq != oldest_alive) {
    printf("FAIL: after wraparound, oldest returned seq=%u, expected %u\n",
           total_n ? all[0].seq : 0U, oldest_alive);
    failures++;
  }
  if (total_n > 0U && all[total_n - 1U].seq != total_written) {
    printf("FAIL: after wraparound, newest returned seq=%u, expected %u\n",
           all[total_n - 1U].seq, total_written);
    failures++;
  }
  for (uint32_t i = 1U; i < total_n; i++) {
    if (all[i].seq != all[i - 1U].seq + 1U) {
      printf("FAIL: gap/duplicate after wraparound between entries %u (seq=%u) and %u (seq=%u)\n",
             i - 1U, all[i - 1U].seq, i, all[i].seq);
      failures++;
      break;
    }
  }
  if (s_next_seq != total_written + 1U) {
    printf("FAIL: seq counter got corrupted across wraparound (got %u, expected %u)\n",
           s_next_seq, total_written + 1U);
    failures++;
  }
  /* EventLog_Init() must resume at the same point after a (simulated)
   * reboot mid-ring — recompute from Flash content alone. */
  uint32_t seq_before_reinit = s_next_seq;
  uint16_t slot_before_reinit = s_next_slot;
  sim_init();
  if (s_next_seq != seq_before_reinit || s_next_slot != slot_before_reinit) {
    printf("FAIL: re-init after wraparound resumed at seq=%u/slot=%u, expected seq=%u/slot=%u\n",
           s_next_seq, s_next_slot, seq_before_reinit, slot_before_reinit);
    failures++;
  }

  if (failures == 0) {
    printf("PASS: event log ring boots blank, paginates ascending, survives wraparound\n");
    return 0;
  }
  printf("%d check(s) failed\n", failures);
  return 1;
}
