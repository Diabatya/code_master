/* Host test for the autonomous flexible-logic runtime in
 * firmware/application/Src/trigger.c — the PRODUCTION source is included
 * textually (not an algorithm copy): FLXB container loading, fire-limit
 * DATA-change tracking, busy-suppression and "X from event" substitution
 * run against HAL/CAN/Storage adapters below. Static internals
 * (s_user_count boundary, append_flex) are exercised through the public
 * Trigger_ReloadFlex() API; Flash-touching paths (find_store/erase) are
 * covered by test_trigger_flash.c.
 *
 * Covered (review CM-03/CM-04):
 *  - FLXB records load into the list tail behind user records;
 *  - full-DATA change detection for flex records (PC pad_8 semantics —
 *    wildcard bytes DO count as a change, unlike user triggers);
 *  - busy-suppression: a repeated identical frame while the program is
 *    running (delay pending) does not re-arm it; changed DATA is a new
 *    command — the armed send is flushed, not silently overwritten;
 *  - two different programs matching the same frame send twice;
 *  - FIRE_ON_BOOT flex records fire once at boot;
 *  - X-substitution zero-pads bytes beyond the event frame's DLC.
 *
 * Build & run (from the repository root):
 *   cc -I firmware/application/tests/stubs -I firmware/application/Inc \
 *      -I firmware/application/Src \
 *      firmware/application/tests/test_trigger_flex.c -o /tmp/test_trigger_flex
 *   /tmp/test_trigger_flex
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>

#include "main.h"
#include "trigger.h"
#include "storage.h"

/* ---- Controllable HAL_GetTick ------------------------------------- */
static uint32_t s_tick;
uint32_t HAL_GetTick(void) { return s_tick; }
void HAL_Delay(uint32_t Delay) { s_tick += Delay; }

/* ---- HAL Flash adapters: not exercised by the flex scenarios ------- */
void HAL_FLASH_Unlock(void) {}
void HAL_FLASH_Lock(void) {}
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t TypeProgram,
                                    uint32_t Address, uint64_t Data)
{
  (void)TypeProgram; (void)Address; (void)Data;
  return HAL_OK;
}
HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *pEraseInit,
                                    uint32_t *PageError)
{
  (void)pEraseInit; (void)PageError;
  return HAL_OK;
}

/* ---- Storage adapters ---------------------------------------------- */
uint32_t Storage_CodeEnd(void) { return 0x08010000U; }
uint32_t Storage_TrigGapBase(void) { return 0x08020000U; }
uint32_t Storage_TrigGapEnd(void) { return 0x08038000U; }
uint8_t Storage_RangeFree(uint32_t from, uint32_t to)
{
  (void)from; (void)to; return 1U;
}
void Storage_NoteTrig(uint32_t base, uint32_t pages)
{
  (void)base; (void)pages;
}
void Storage_NoteLegacyTrigDone(void) {}

/* FLXB blob under test — set by each scenario before Trigger_ReloadFlex(). */
static const uint8_t *s_flex_blob;
static uint32_t s_flex_len;
const uint8_t *Storage_FlexPayload(uint32_t *len_out)
{
  if (s_flex_blob == NULL) {
    return NULL;
  }
  *len_out = s_flex_len;
  return s_flex_blob;
}

/* ---- CAN adapter: transmissions are captured, not sent ------------- */
typedef struct {
  uint8_t channel;
  uint8_t extended;
  uint32_t id;
  uint8_t dlc;
  uint8_t data[8];
} sent_frame_t;

static sent_frame_t s_sent[64];
static int s_sent_count;

uint8_t CanBridge_Transmit(const can_frame_t *frame)
{
  if (s_sent_count < (int)(sizeof(s_sent) / sizeof(s_sent[0]))) {
    sent_frame_t *s = &s_sent[s_sent_count++];
    s->channel = frame->channel;
    s->extended = frame->extended;
    s->id = frame->id;
    s->dlc = frame->dlc;
    memcpy(s->data, frame->data, 8);
  }
  return 1U;
}

void App_KickWatchdog(void) {}

/* ---- The production implementation under test ---------------------- */
#include "trigger.c"

/* ---- Helpers ------------------------------------------------------- */
static int s_failures;
#define CHECK(cond, msg) do { \
  if (!(cond)) { printf("FAIL: %s\n", msg); s_failures++; } \
} while (0)

/* Minimal record matching "any frame on rx_channel" and sending
 * tx_id on tx_channel. All flex records carry TRIGGER_F_MUTE_ECHO —
 * the PC compiles programs not to react to the MCU's own echo. */
static void make_flex_record(trigger_t *t, uint8_t rx_ch, uint32_t rx_id,
                             uint8_t tx_ch, uint32_t tx_id)
{
  memset(t, 0, sizeof(*t));
  t->magic = TRIGGER_MAGIC;
  t->enabled = 1U;
  t->rx_channel = rx_ch;
  t->rx_id = rx_id;
  t->rx_id_mask = rx_id > 0x7FFU ? 0x1FFFFFFFU : 0x7FFU;
  t->rx_extended = rx_id > 0x7FFU ? 1U : 0U;
  t->tx_channel = tx_ch;
  t->tx_id = tx_id;
  t->tx_dlc = 8U;
  t->tx_count = 1U;
  t->reserved[0] = TRIGGER_FORMAT_VERSION;
  t->reserved[1] = TRIGGER_RECORD_SIZE;
  t->rx_flags = TRIGGER_F_MUTE_ECHO;
  t->crc8 = crc8((const uint8_t *)t, offsetof(trigger_t, crc8));
}

/* FLXB container: "FLXB" + ver + count + reserved + json_len + json +
 * packed records — the layout append_flex() parses from FLXH. */
static uint8_t s_blob[4096];
static uint32_t build_flex_blob(const trigger_t *records, uint8_t count)
{
  const char *json = "{}";
  uint32_t json_len = 2U;
  memcpy(s_blob, "FLXB", 4);
  s_blob[4] = 1U;           /* container version */
  s_blob[5] = count;
  s_blob[6] = s_blob[7] = 0;
  memcpy(&s_blob[8], &json_len, 4);
  memcpy(&s_blob[12], json, json_len);
  uint32_t off = 12U + json_len;
  for (uint8_t i = 0; i < count; i++) {
    memcpy(&s_blob[off], &records[i], sizeof(trigger_t));
    off += sizeof(trigger_t);
  }
  return off;
}

/* Fresh engine state: no user records, the FLXB tail rebuilt from the
 * blob — same boundary condition Trigger_Init() produces. */
static void load_flex(const trigger_t *records, uint8_t count)
{
  s_flex_len = build_flex_blob(records, count);
  s_flex_blob = s_blob;
  s_sent_count = 0;
  s_tick = 1000;
  memset(s_triggers, 0, sizeof(s_triggers));
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_rx_state, 0, sizeof(s_rx_state));
  memset(s_src_state, 0, sizeof(s_src_state));
  memset(s_cache, 0, sizeof(s_cache));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  s_count = 0U;
  s_user_count = 0U;
  Trigger_ReloadFlex();
}

static can_frame_t mk_frame(uint8_t ch, uint32_t id, uint8_t dlc,
                            const uint8_t *data)
{
  can_frame_t f;
  memset(&f, 0, sizeof(f));
  f.channel = ch;
  f.extended = id > 0x7FFU ? 1U : 0U;
  f.id = id;
  f.dlc = dlc;
  if (data != NULL) {
    memcpy(f.data, data, dlc < 8U ? dlc : 8U);
  }
  return f;
}

/* ---- Scenarios ----------------------------------------------------- */

/* A compiled record fires: the flex tail runs behind the (empty) user
 * list, invisible to Trigger_Count(). */
static void test_flex_record_fires(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.tx_data[0] = 0xAAU;
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  CHECK(Trigger_Count() == 0U,
        "flex records must not appear in Trigger_Count (user boundary)");

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1, "flex record must send on matching frame");
  CHECK(s_sent_count == 1 && s_sent[0].id == 0x456U
        && s_sent[0].data[0] == 0xAAU, "response id/data wrong");
}

/* Two DIFFERENT programs compiled to identical-match records: both
 * fire (no global dedup — programs are independent). */
static void test_two_programs_same_event(void)
{
  trigger_t r[2];
  make_flex_record(&r[0], 0U, 0x123U, 0U, 0x456U);
  make_flex_record(&r[1], 0U, 0x123U, 1U, 0x789U);
  r[1].crc8 = crc8((const uint8_t *)&r[1], offsetof(trigger_t, crc8));
  load_flex(r, 2U);

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 2,
        "two programs on one frame must send twice");
}

/* Full-DATA change tracking for flex records: the PC compares pad_8,
 * so a change in a byte the filter masks out still resets the fire
 * limit. frames A A A B — limit 2 must fire on A,A then on B (not
 * stay suppressed because only masked bytes are compared). */
static void test_full_data_tracking(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_fire_limit = 2U;
  /* Wildcard DATA filter — no mask bytes set. */
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  const uint8_t a[8] = {0x11, 0x22, 0, 0, 0, 0, 0, 0};
  const uint8_t b[8] = {0x11, 0x99, 0, 0, 0, 0, 0, 0};
  can_frame_t fa = mk_frame(0U, 0x123U, 8U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 8U, b);

  Trigger_OnFrame(&fa);            /* fire 1 */
  Trigger_OnFrame(&fa);            /* fire 2 → suppress */
  Trigger_OnFrame(&fa);            /* suppressed */
  CHECK(s_sent_count == 2, "fire_limit=2 must send exactly twice");
  Trigger_OnFrame(&fb);            /* byte1 changed → reset → fire */
  CHECK(s_sent_count == 3,
        "change in a masked-out byte must reset the flex fire limit");
}

/* Busy-suppression: identical frame while the program is still running
 * (delay pending) does not re-arm; changed DATA is a new command — the
 * previously armed send is flushed before the new arm (PC sends both). */
static void test_busy_dedup(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 50U;
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  const uint8_t a[8] = {0x11, 0, 0, 0, 0, 0, 0, 0};
  const uint8_t b[8] = {0x22, 0, 0, 0, 0, 0, 0, 0};
  can_frame_t fa = mk_frame(0U, 0x123U, 8U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 8U, b);

  Trigger_OnFrame(&fa);            /* armed, sends at tick+50 */
  Trigger_OnFrame(&fa);            /* same DATA while busy → suppressed */
  s_tick += 60U;
  Trigger_Poll();                  /* first send goes out */
  CHECK(s_sent_count == 1,
        "identical repeat while busy must not re-arm");

  Trigger_OnFrame(&fa);            /* program idle again → new command */
  s_tick += 60U;
  Trigger_Poll();
  CHECK(s_sent_count == 2, "same DATA after completion must fire");

  Trigger_OnFrame(&fa);            /* armed again */
  Trigger_OnFrame(&fb);            /* new DATA while busy → flush + arm */
  CHECK(s_sent_count == 3,
        "new DATA while busy must flush the armed send, not drop it");
  s_tick += 60U;
  Trigger_Poll();
  CHECK(s_sent_count == 4,
        "changed DATA while busy must be a new command");
}

/* FIRE_ON_BOOT flex record ("Включение устройства") fires once via
 * Trigger_FireOnBoot() and never on bus frames. */
static void test_fire_on_boot(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_flags |= TRIGGER_F_FIRE_ON_BOOT;
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "boot record must not react to bus frames");

  Trigger_FireOnBoot();
  Trigger_Poll();
  s_tick += 10U;
  Trigger_Poll();
  CHECK(s_sent_count == 1, "boot record must fire once at boot");
}

/* X-substitution: positions marked src_from[i]==0xFF come from the
 * event frame; bytes beyond its DLC are zero (PC pads with _pad_8). */
static void test_x_subst_zero_pad(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_flags |= TRIGGER_F_TX_EVENT_DATA;
  memset(r.src_from, 0xFF, 8);    /* all positions from the event frame */
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  const uint8_t d[2] = {0xAB, 0xCD};
  can_frame_t f = mk_frame(0U, 0x123U, 2U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1, "X-subst record must send");
  if (s_sent_count == 1) {
    CHECK(s_sent[0].data[0] == 0xABU && s_sent[0].data[1] == 0xCDU,
          "X-subst bytes from event frame");
    CHECK(s_sent[0].data[2] == 0U && s_sent[0].data[7] == 0U,
          "bytes beyond event DLC must be zero-padded");
  }
}

/* A mask byte beyond a short frame's DLC must NOT match (host treats
 * missing bytes as 0 — review fix), so no send happens. */
static void test_short_frame_no_match(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_data[5] = 0x77U;
  r.rx_data_mask[5] = 0xFFU;
  r.crc8 = crc8((const uint8_t *)&r, offsetof(trigger_t, crc8));
  load_flex(&r, 1U);

  const uint8_t d[2] = {0x11, 0x22};
  can_frame_t f = mk_frame(0U, 0x123U, 2U, d); /* byte5 missing → 0 != 0x77 */
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0,
        "mask byte beyond DLC must not match a short frame");
}

/* Non-FLXB (legacy JSON) blob: no records load. */
static void test_legacy_json_blob(void)
{
  const char *json = "{\"rules\":[]}";
  s_flex_blob = (const uint8_t *)json;
  s_flex_len = 12U;
  s_sent_count = 0;
  memset(s_triggers, 0, sizeof(s_triggers));
  s_count = 0U;
  s_user_count = 0U;
  Trigger_ReloadFlex();

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "legacy JSON blob must yield no flex records");
}

int main(void)
{
  test_flex_record_fires();
  test_two_programs_same_event();
  test_full_data_tracking();
  test_busy_dedup();
  test_fire_on_boot();
  test_x_subst_zero_pad();
  test_short_frame_no_match();
  test_legacy_json_blob();

  if (s_failures == 0) {
    printf("PASS: all flex-runtime checks\n");
    return 0;
  }
  printf("FAILURES: %d\n", s_failures);
  return 1;
}
