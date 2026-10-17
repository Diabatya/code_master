/* Host test for the autonomous flexible-logic runtime in
 * firmware/application/Src/trigger.c — the PRODUCTION source is included
 * textually (not an algorithm copy): FLXB container loading, fire-limit
 * DATA-change tracking, the per-firing queue and "X from event"
 * substitution run against HAL/CAN/Storage adapters below.
 *
 * Covered (review CM-R1..CM-R3):
 *  - each matched frame enqueues its OWN send series with its own
 *    schedule (delay/interval preserved, no early flush);
 *  - identical frames during frame-delay are NOT suppressed (PC only
 *    suppresses while exec_delay is pending — compiled programs never
 *    have it); every frame is a new firing, subject to fire_limit;
 *  - TX failure goes through bounded retries and counted drops;
 *  - queue overflow is diagnosable (dropped counter), not silent;
 *  - pad_8 DATA-change tracking: DLC alone is not a DATA change;
 *  - FLXB container: exact-length contract, count<=16, bad record
 *    rejects the whole container (same contract as PC validation).
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

/* ---- CAN adapter: transmissions captured WITH their tick ----------- */
typedef struct {
  uint32_t tick;
  uint8_t channel;
  uint8_t extended;
  uint32_t id;
  uint8_t dlc;
  uint8_t data[8];
} sent_frame_t;

static sent_frame_t s_sent[256];
static int s_sent_count;
static int s_tx_fail;          /* >0: CanBridge_Transmit fails N times */
static int s_tx_fail_always;   /* nonzero: transmit never succeeds */
static int s_tx_fail_channel;  /* 0: s_tx_fail hits any channel;
                                  1/2: only that wire channel —
                                  CAN1/CAN2 independence (CM-R8) */

uint8_t CanBridge_Transmit(const can_frame_t *frame)
{
  if (s_tx_fail_always
      || (s_tx_fail > 0 && (s_tx_fail_channel == 0
                            || frame->channel == s_tx_fail_channel))) {
    if (s_tx_fail > 0) {
      s_tx_fail--;
    }
    return 0U;
  }
  if (s_sent_count < (int)(sizeof(s_sent) / sizeof(s_sent[0]))) {
    sent_frame_t *s = &s_sent[s_sent_count++];
    s->tick = s_tick;
    s->channel = frame->channel;
    s->extended = frame->extended;
    s->id = frame->id;
    s->dlc = frame->dlc;
    memcpy(s->data, frame->data, 8);
  }
  return 1U;
}

void App_KickWatchdog(void) {}

/* ---- AuxOut fake: logical state mirror, recorded commands and a
 *      pushable event queue (CM-HW-03). IsActive tracks the logical
 *      "channel works" state — the low PWM phase and 0% duty are
 *      still active, matching aux_out.c semantics. ---------------- */
static uint8_t s_aux_active[8];
static uint8_t s_aux_level[8];
static uint8_t s_aux_mode[8];
static int s_aux_set_calls;
static struct {
  uint8_t channel, mode;
  uint16_t pulse_on, pulse_off, pulse_count, pwm_freq;
  uint8_t duty;
  uint32_t time_ms;
} s_aux_last;

static struct { uint8_t ch; uint8_t on; } s_aux_ev[16];
static int s_aux_ev_head, s_aux_ev_count;

uint8_t AuxOut_Set(uint8_t channel, uint8_t mode,
                   uint16_t pulse_on_ms, uint16_t pulse_off_ms,
                   uint16_t pulse_count, uint16_t pwm_freq_hz,
                   uint8_t pwm_duty_pct, uint32_t pwm_time_ms)
{
  s_aux_set_calls++;
  s_aux_last.channel = channel;
  s_aux_last.mode = mode;
  s_aux_last.pulse_on = pulse_on_ms;
  s_aux_last.pulse_off = pulse_off_ms;
  s_aux_last.pulse_count = pulse_count;
  s_aux_last.pwm_freq = pwm_freq_hz;
  s_aux_last.duty = pwm_duty_pct;
  s_aux_last.time_ms = pwm_time_ms;
  if (channel >= 1U && channel <= 7U) {
    s_aux_mode[channel] = mode;
    s_aux_active[channel] = (mode != 0U) ? 1U : 0U;
    s_aux_level[channel] = (mode == 1U) ? 1U : 0U;
  }
  return 1U;
}

uint8_t AuxOut_IsActive(uint8_t channel)
{
  return (channel <= 7U) ? s_aux_active[channel] : 0U;
}

uint8_t AuxOut_Get(uint8_t channel)
{
  return (channel <= 7U) ? s_aux_level[channel] : 0U;
}

uint8_t AuxOut_GetMode(uint8_t channel)
{
  return (channel <= 7U) ? s_aux_mode[channel] : 0U;
}

uint8_t AuxOut_TakeEvent(uint8_t *channel_out, uint8_t *is_on_out)
{
  if (s_aux_ev_count == 0) {
    return 0U;
  }
  *channel_out = s_aux_ev[s_aux_ev_head].ch;
  *is_on_out = s_aux_ev[s_aux_ev_head].on;
  s_aux_ev_head = (s_aux_ev_head + 1) % 16;
  s_aux_ev_count--;
  return 1U;
}

uint32_t AuxOut_EventsDropped(void) { return 0U; }

static void aux_push_event(uint8_t ch, uint8_t on)
{
  int tail = (s_aux_ev_head + s_aux_ev_count) % 16;
  s_aux_ev[tail].ch = ch;
  s_aux_ev[tail].on = on;
  s_aux_ev_count++;
}

static void aux_reset(void)
{
  memset(s_aux_active, 0, sizeof(s_aux_active));
  memset(s_aux_level, 0, sizeof(s_aux_level));
  memset(s_aux_mode, 0, sizeof(s_aux_mode));
  s_aux_set_calls = 0;
  s_aux_ev_head = s_aux_ev_count = 0;
}

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

static void fix_crc(trigger_t *t)
{
  t->crc8 = crc8((const uint8_t *)t, offsetof(trigger_t, crc8));
}

/* FLXB container: "FLXB" + ver + count + reserved + json_len + json +
 * packed records — the layout append_flex() parses from FLXH. The JSON
 * part uses the SAME schema core/flex_compiler.py::build_flex_blob
 * emits (kind/format/rules) — the harness must feed production code
 * containers identical to what the PC writes (review CM-R6). */
static uint8_t s_blob[4096];
static uint32_t build_flex_blob_ver(const trigger_t *records,
                                    uint8_t count, uint8_t version)
{
  const char *json =
      "{\"kind\":\"code_master_flex_logic\",\"format\":1,"
      "\"rules\":[]}";
  uint32_t json_len = (uint32_t)strlen(json);
  memcpy(s_blob, "FLXB", 4);
  s_blob[4] = version;      /* container version */
  s_blob[5] = count;
  s_blob[6] = s_blob[7] = 0;
  memcpy(&s_blob[8], &json_len, 4);
  memcpy(&s_blob[12], json, json_len);
  (void)json;
  uint32_t off = 12U + json_len;
  for (uint8_t i = 0; i < count; i++) {
    memcpy(&s_blob[off], &records[i], sizeof(trigger_t));
    off += sizeof(trigger_t);
  }
  return off;
}

static uint32_t build_flex_blob(const trigger_t *records, uint8_t count)
{
  return build_flex_blob_ver(records, count, 1U);
}

/* Fresh engine state: no user records, the FLXB tail rebuilt from the
 * blob — same boundary condition Trigger_Init() produces. */
static void load_flex(const trigger_t *records, uint8_t count)
{
  s_flex_len = build_flex_blob(records, count);
  s_flex_blob = s_blob;
  s_sent_count = 0;
  s_tx_fail = 0;
  s_tx_fail_always = 0;
  s_tx_fail_channel = 0;
  s_tick = 1000;
  memset(s_triggers, 0, sizeof(s_triggers));
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_rx_state, 0, sizeof(s_rx_state));
  memset(s_src_state, 0, sizeof(s_src_state));
  memset(s_cache, 0, sizeof(s_cache));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  memset(s_flex_queue, 0, sizeof(s_flex_queue));
  s_count = 0U;
  s_user_count = 0U;
  s_fired_count = 0U;
  s_dropped_count = 0U;
  aux_reset();
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

/* Advance time, polling each ms — models the main loop cadence. */
static void run_ms(uint32_t ms)
{
  for (uint32_t k = 0; k < ms; k++) {
    s_tick++;
    Trigger_Poll();
  }
}

/* ---- Scenarios ----------------------------------------------------- */

/* A compiled record fires: the flex tail runs behind the (empty) user
 * list, invisible to Trigger_Count(). */
static void test_flex_record_fires(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.tx_data[0] = 0xAAU;
  fix_crc(&r);
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
  fix_crc(&r[1]);
  load_flex(r, 2U);

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 2,
        "two programs on one frame must send twice");
}

/* CM-R1 reproduction: event A at t=0 arms series 50/70/90; event B at
 * t=10 arms 60/80/100 — NEITHER may send before its own delay and the
 * armed series must keep its full schedule. */
static void test_series_schedules_preserved(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 50U;
  r.tx_count = 3U;
  r.tx_interval_ms = 20U;
  r.rx_flags |= TRIGGER_F_TX_EVENT_DATA;
  r.src_from[0] = 0xFFU;          /* byte0 of the response = event byte0 */
  r.tx_data[0] = 0xEEU;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  const uint8_t b[1] = {0x22};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 1U, b);

  Trigger_OnFrame(&fa);           /* t=1000: series A armed at 1050 */
  run_ms(10);                     /* t=1010 */
  Trigger_OnFrame(&fb);           /* t=1010: series B armed at 1060 */
  CHECK(s_sent_count == 0,
        "no send before the first delay expires");
  run_ms(40);                     /* t=1050: A's first send */
  CHECK(s_sent_count == 1 && s_sent[0].tick == 1050U
        && s_sent[0].data[0] == 0x11U,
        "A series first send at its own delay with A's data");
  run_ms(10);                     /* t=1060: B's first send */
  CHECK(s_sent_count == 2 && s_sent[1].tick == 1060U
        && s_sent[1].data[0] == 0x22U,
        "B series first send at its own delay with B's data");
  run_ms(50);                     /* t=1110: 1070 A, 1080 B, 1090 A, 1100 B */
  CHECK(s_sent_count == 6,
        "both series must complete all repeats");
  CHECK(s_sent[2].tick == 1070U && s_sent[3].tick == 1080U
        && s_sent[4].tick == 1090U && s_sent[5].tick == 1100U,
        "repeats keep their interval (50/70/90 and 60/80/100)");
}

/* Identical frame during frame-delay: NOT suppressed (PC raises
 * _rule_busy only for exec_delay, which never compiles) — each matched
 * frame is a new series. */
static void test_identical_frames_not_suppressed(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 50U;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);

  Trigger_OnFrame(&fa);           /* series at 1050 */
  Trigger_OnFrame(&fa);           /* same DATA — new series at 1050 too */
  run_ms(60);
  CHECK(s_sent_count == 2,
        "identical frame during delay fires again (PC parity)");
}

/* A new event arriving BETWEEN repeats of an older series: the old
 * series continues with its remaining sends. */
static void test_event_between_repeats(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 10U;
  r.tx_count = 3U;
  r.tx_interval_ms = 30U;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  const uint8_t b[1] = {0x22};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 1U, b);

  Trigger_OnFrame(&fa);           /* armed at 1010, repeats 1040/1070 */
  run_ms(15);                     /* t=1015: first send went out */
  Trigger_OnFrame(&fb);           /* B armed at 1025, repeats 1055/1085 */
  run_ms(75);                     /* t=1090: everything sent */
  CHECK(s_sent_count == 6,
        "old series must complete after the new event");
  /* A at 1010/1040/1070; B at 1025/1055/1085 — interleaved, each on
   * its own schedule. */
  CHECK(s_sent[0].tick == 1010U && s_sent[0].id == 0x456U
        && s_sent[1].tick == 1025U && s_sent[2].tick == 1040U
        && s_sent[3].tick == 1055U && s_sent[4].tick == 1070U
        && s_sent[5].tick == 1085U,
        "both series keep their own repeat schedule");
}

/* TX failure: the send goes through bounded retries (1 ms apart), and
 * after the limit the firing is dropped with the counter raised. */
static void test_tx_failure_retries_and_drops(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);

  /* Transient failure: first two attempts fail, third succeeds —
   * the frame is NOT lost (same retry path as user triggers). */
  s_tx_fail = 2;
  Trigger_OnFrame(&fa);           /* immediate send attempt fails */
  CHECK(s_sent_count == 0, "failed send is not counted");
  run_ms(5);
  CHECK(s_sent_count == 1, "send must be retried until success");

  /* Permanent failure: the firing exhausts retries and is dropped
   * with the counter raised — never silently. */
  load_flex(&r, 1U);
  s_tx_fail_always = 1;
  Trigger_OnFrame(&fa);
  run_ms(TRIGGER_TX_RETRY_MAX + 5U);
  CHECK(s_sent_count == 0, "dead bus must not produce sends");
  CHECK(s_dropped_count >= 1U,
        "exhausted retries must count as dropped");
  s_tx_fail_always = 0;
}

/* CM-R8: a failed attempt must NOT shift the series schedule.
 * delay=50, count=3, interval=20; event at t=1000 → planned
 * 1050/1070/1090. The 1050 attempt is rejected once by the bus —
 * the send lands at 1051, but the repeats MUST stay at 1070/1090. */
static void test_retry_keeps_series_schedule(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 50U;
  r.tx_count = 3U;
  r.tx_interval_ms = 20U;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);
  Trigger_OnFrame(&fa);           /* armed at 1050 */
  run_ms(49);                     /* t=1049 */
  s_tx_fail = 1;                  /* the 1050 attempt is rejected */
  run_ms(60);                     /* t=1110 */
  CHECK(s_sent_count == 3, "series must complete after a retry");
  if (s_sent_count == 3) {
    CHECK(s_sent[0].tick == 1051U
          && s_sent[1].tick == 1070U
          && s_sent[2].tick == 1090U,
          "a failed attempt shifts only the attempt, not the "
          "series (1051,1070,1090 — not 1051,1071,1091)");
  }

  /* interval=0: все повторы планируются на один и тот же срок —
   * идут по одному за тик опроса (документированная гранулярность:
   * Poll делает одну отправку на серию за проход). Отказ первой
   * попытки сдвигает только её — залпа и потерь нет. */
  r.tx_interval_ms = 0U;
  fix_crc(&r);
  load_flex(&r, 1U);
  Trigger_OnFrame(&fa);           /* armed at 1050 */
  run_ms(49);
  s_tx_fail = 1;                  /* отказ ровно на плановый срок */
  run_ms(10);
  CHECK(s_sent_count == 3, "interval=0 series must complete");
  if (s_sent_count == 3) {
    CHECK(s_sent[0].tick == 1051U
          && s_sent[1].tick == 1052U
          && s_sent[2].tick == 1053U,
          "interval=0: one send per poll tick after the deadline");
  }
}

/* CM-R8: каналы независимы — отказ CAN2 отодвигает только серию
 * CAN2 и только на время ретрая; серия CAN1 идёт по плану. */
static void test_retry_channels_independent(void)
{
  trigger_t recs[2];
  make_flex_record(&recs[0], 0U, 0x100U, 0U, 0x200U); /* CAN1 → CAN1 */
  make_flex_record(&recs[1], 0U, 0x100U, 1U, 0x201U); /* CAN1 → CAN2 */
  for (uint8_t i = 0; i < 2U; i++) {
    recs[i].delay_ms = 50U;
    recs[i].tx_count = 2U;
    recs[i].tx_interval_ms = 20U;
    fix_crc(&recs[i]);
  }
  load_flex(recs, 2U);

  const uint8_t a[1] = {0x11};
  can_frame_t fa = mk_frame(0U, 0x100U, 1U, a);
  Trigger_OnFrame(&fa);           /* обе серии вооружены на 1050 */
  run_ms(49);                     /* t=1049 */
  s_tx_fail_channel = 2;          /* глухой только CAN2 */
  s_tx_fail = 1;
  run_ms(30);                     /* до t=1080 */
  CHECK(s_sent_count == 4, "both channels must complete series");
  if (s_sent_count == 4) {
    CHECK(s_sent[0].tick == 1050U && s_sent[0].channel == 1U,
          "CAN1 send on schedule despite CAN2 outage");
    CHECK(s_sent[1].tick == 1051U && s_sent[1].channel == 2U,
          "CAN2 retry lands 1ms later");
    CHECK(s_sent[2].tick == 1070U && s_sent[2].channel == 1U
          && s_sent[3].tick == 1070U && s_sent[3].channel == 2U,
          "repeats on both channels keep the planned 1070");
  }
}

/* CM-R8: two series on a dead-then-alive bus keep independent
 * schedules; a long outage drops the whole series after the bounded
 * retry window and counts every unsent frame. */
static void test_retry_outage_policy(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 50U;
  r.tx_count = 2U;
  r.tx_interval_ms = 20U;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0x11};
  const uint8_t b[1] = {0x22};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 1U, b);

  s_tx_fail_always = 1;
  Trigger_OnFrame(&fa);           /* series A armed at 1050 */
  s_tick += 10;
  Trigger_OnFrame(&fb);           /* series B armed at 1060 */
  /* ~75 ms of bus silence covers both planned starts (1050/1060)
   * plus the 20 ms retry window: both series exhaust retries and
   * are dropped — bounded, counted, no endless loop. */
  run_ms(TRIGGER_TX_RETRY_MAX + 55U);
  CHECK(s_sent_count == 0, "dead bus must not produce sends");
  CHECK(s_dropped_count == 4U,
        "both series drop fully (2 sends each) after bounded retries");
  s_tx_fail_always = 0;

  /* Bus recovers: a NEW event fires a fresh series normally — the
   * drop did not poison the record. */
  Trigger_OnFrame(&fa);
  run_ms(70);
  CHECK(s_sent_count == 2, "record must fire again after recovery");
}

/* Queue overflow: more firings than FLEX_QUEUE_DEPTH — the overflow is
 * counted in dropped, earlier series still complete. */
static void test_queue_overflow_counted(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 100U;
  fix_crc(&r);
  load_flex(&r, 1U);

  uint8_t a[1] = {0x11};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);

  for (uint8_t k = 0; k < FLEX_QUEUE_DEPTH + 4U; k++) {
    a[0] = (uint8_t)(0x10U + k);  /* new DATA each time → new firing */
    fa = mk_frame(0U, 0x123U, 1U, a);
    Trigger_OnFrame(&fa);
    s_tick++;
  }
  CHECK(s_dropped_count >= 1U,
        "queue overflow must be counted, not silent");
  run_ms(120);
  CHECK(s_sent_count == FLEX_QUEUE_DEPTH,
        "queued firings must all send on schedule");
}

/* CM-R2: pad_8 DATA tracking — DLC alone is not a DATA change.
 * limit=2, wildcard DATA filter: A(dlc1)=11, A again, B(dlc2)=11 00 —
 * pad_8 makes B identical to A → only 2 sends total. */
static void test_pad8_dlc_not_a_change(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_fire_limit = 2U;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a1[1] = {0x11};
  const uint8_t a2[2] = {0x11, 0x00};
  const uint8_t a8[8] = {0x11, 0, 0, 0, 0, 0, 0, 0};
  const uint8_t b8[8] = {0x11, 0, 0x99, 0, 0, 0, 0, 0};

  can_frame_t f;

  f = mk_frame(0U, 0x123U, 1U, a1);
  Trigger_OnFrame(&f);            /* fire 1 */
  Trigger_OnFrame(&f);            /* fire 2 → suppress */
  f = mk_frame(0U, 0x123U, 2U, a2);
  Trigger_OnFrame(&f);            /* pad-8 equal → still suppressed */
  f = mk_frame(0U, 0x123U, 8U, a8);
  Trigger_OnFrame(&f);            /* pad-8 equal → still suppressed */
  CHECK(s_sent_count == 2,
        "DLC 1→2→8 with same padded bytes must not reset the limit");
  f = mk_frame(0U, 0x123U, 8U, b8);
  Trigger_OnFrame(&f);            /* real byte change → reset → fire */
  CHECK(s_sent_count == 3,
        "a real nonzero byte change resets the flex fire limit");

  /* DLC 2→1 dropping a nonzero byte IS a change. */
  const uint8_t c2[2] = {0x11, 0x00};
  const uint8_t c1[1] = {0x11};
  const uint8_t c2nz[2] = {0x11, 0x77};
  f = mk_frame(0U, 0x123U, 2U, c2nz);
  Trigger_OnFrame(&f);            /* changed data → fire (limit 2: 1st) */
  f = mk_frame(0U, 0x123U, 2U, c2nz);
  Trigger_OnFrame(&f);            /* fire 2 → suppress */
  f = mk_frame(0U, 0x123U, 1U, c1);
  Trigger_OnFrame(&f);            /* 11 77 → 11: change → fire */
  CHECK(s_sent_count == 6,
        "DLC drop losing a nonzero byte is a DATA change");
  (void)c2;

  /* DLC 0 → DLC 8 with all-zero data: pad_8 sees no change — the
   * limit stays exhausted. */
  const uint8_t z8[8] = {0};
  f = mk_frame(0U, 0x123U, 2U, c2nz);   /* 11 77 → limit exhausted on fire 2 */
  Trigger_OnFrame(&f);
  f = mk_frame(0U, 0x123U, 2U, c2nz);
  Trigger_OnFrame(&f);            /* suppressed */
  f = mk_frame(0U, 0x123U, 0U, NULL);   /* all zeros */
  Trigger_OnFrame(&f);            /* DATA change → reset → fire 1 */
  f = mk_frame(0U, 0x123U, 0U, NULL);
  Trigger_OnFrame(&f);            /* fire 2 → suppress */
  f = mk_frame(0U, 0x123U, 8U, z8);     /* pad-8 equal → suppressed */
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 10,
        "DLC 0→8 with zero data must not reset the limit");
}

/* Full-DATA change tracking: change in a byte the FILTER masks out
 * still resets the flex fire limit (PC compares pad_8 wholesale). */
static void test_full_data_tracking(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_fire_limit = 2U;
  /* Wildcard DATA filter — no mask bytes set. */
  fix_crc(&r);
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

/* FIRE_ON_BOOT flex record ("Включение устройства") fires once via
 * Trigger_FireOnBoot() — through the queue like a normal firing — and
 * never on bus frames. */
static void test_fire_on_boot(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.rx_flags |= TRIGGER_F_FIRE_ON_BOOT;
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "boot record must not react to bus frames");

  Trigger_FireOnBoot();
  run_ms(5);
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
  fix_crc(&r);
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

/* Each queued firing keeps ITS OWN event frame for X-substitution —
 * series A substitutes A's bytes, series B substitutes B's, even when
 * interleaved. */
static void test_x_subst_per_firing_snapshot(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  r.delay_ms = 20U;
  r.rx_flags |= TRIGGER_F_TX_EVENT_DATA;
  memset(r.src_from, 0xFF, 8);
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t a[1] = {0xA1};
  const uint8_t b[1] = {0xB2};
  can_frame_t fa = mk_frame(0U, 0x123U, 1U, a);
  can_frame_t fb = mk_frame(0U, 0x123U, 1U, b);
  Trigger_OnFrame(&fa);           /* armed at 1020 with A1 */
  s_tick += 5;
  Trigger_OnFrame(&fb);           /* armed at 1025 with B2 */
  run_ms(30);
  CHECK(s_sent_count == 2, "both firings must send");
  if (s_sent_count == 2) {
    CHECK(s_sent[0].data[0] == 0xA1U && s_sent[1].data[0] == 0xB2U,
          "each firing substitutes its own event data");
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
  fix_crc(&r);
  load_flex(&r, 1U);

  const uint8_t d[2] = {0x11, 0x22};
  can_frame_t f = mk_frame(0U, 0x123U, 2U, d); /* byte5 missing → 0 != 0x77 */
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0,
        "mask byte beyond DLC must not match a short frame");
}

/* ---- FLXB container validation (CM-R3) ------------------------------ */

static void flex_from_blob(uint8_t *blob, uint32_t len)
{
  s_flex_blob = blob;
  s_flex_len = len;
  s_sent_count = 0;
  memset(s_triggers, 0, sizeof(s_triggers));
  memset(s_flex_queue, 0, sizeof(s_flex_queue));
  s_count = 0U;
  s_user_count = 0U;
  Trigger_ReloadFlex();
}

static void test_flxb_validation(void)
{
  trigger_t r;
  make_flex_record(&r, 0U, 0x123U, 0U, 0x456U);
  uint32_t len = build_flex_blob(&r, 1U);
  uint8_t bad[4096];

  const uint8_t d[1] = {0x11};
  can_frame_t f = mk_frame(0U, 0x123U, 1U, d);

  /* Extra trailing bytes → container rejected. */
  memcpy(bad, s_blob, len);
  bad[len] = 0xEE;
  flex_from_blob(bad, len + 1U);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "trailing bytes must reject the container");

  /* Truncated inside the record area → rejected. */
  flex_from_blob(s_blob, len - 5U);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "truncated record area must reject");

  /* count > FLEX_MAX_RECORDS → rejected (not clamped — PC parity). */
  memcpy(bad, s_blob, len);
  bad[5] = FLEX_MAX_RECORDS + 1U;
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "count > 16 must reject the container");

  /* Unsupported container version → rejected. */
  memcpy(bad, s_blob, len);
  bad[4] = 9U;
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "unsupported version must reject");

  /* json_len = 0 → rejected. */
  memcpy(bad, s_blob, len);
  memset(&bad[8], 0, 4);
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "json_len=0 must reject");

  /* json_len ≈ UINT32_MAX: before the fix rec_off = 12 + json_len
   * overflowed to a small number and the "exact length" check could
   * be passed with crafted garbage. Rejected (CM-R6). */
  memcpy(bad, s_blob, len);
  {
    uint32_t huge = 0xFFFFFFF6U;
    memcpy(&bad[8], &huge, 4);
  }
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0,
        "json_len near UINT32_MAX must reject (overflow guard)");

  /* Corrupt JSON CONTENT is accepted: the firmware contract covers
   * the executable tail only — the MCU never parses JSON (same set
   * as the PC's parse_flex_blob; JSON schema lives in PC-only
   * parse_flex_rules, CM-R6). */
  memcpy(bad, s_blob, len);
  bad[12] = '!';
  bad[13] = '!';
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1,
        "corrupt JSON content does not invalidate the exec tail");

  /* Corrupted record CRC → whole container rejected (all-or-nothing:
   * a half-loaded program set is neither fully device-executed nor
   * consistent with the PC's managed flags). */
  memcpy(bad, s_blob, len);
  bad[len - 1] ^= 0x5AU;
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "bad record CRC must reject the container");

  /* Invalid record fields (tx_channel out of range) → rejected.
   * tx_channel is at record offset 32 (struct _TRIGGER_FORMAT). */
  memcpy(bad, s_blob, len);
  bad[12 + 2 + 32] = 0x55;        /* tx_channel → invalid */
  /* keep CRC consistent so the FIELD check is what fails */
  {
    uint8_t *rec = &bad[12 + 2];
    rec[sizeof(trigger_t) - 1] = crc8(rec, sizeof(trigger_t) - 1);
  }
  flex_from_blob(bad, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "invalid record fields must reject");

  /* Sanity: the untouched valid blob still loads. */
  flex_from_blob(s_blob, len);
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1, "valid container must load");
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

/* ---- CM-HW-03: aux-события/условия/действия (FLXB v2) --------------- */

/* Запись «событие aux-перехода → кадр»: rx_aux_event по каналу/фронту
 * (rx_dlc: 1 = «включился», 2 = «выключился»). */
static void make_aux_event_record(trigger_t *t, uint8_t ch,
                                  uint8_t edge, uint32_t tx_id)
{
  make_flex_record(t, 2U, 0U, 0U, tx_id);
  t->rx_flags |= TRIGGER_F_RX_AUX_EVENT;
  /* Кадровые поля приёма у aux-события обязаны быть нулевыми —
   * trigger_fields_valid отбраковывает ненулевые mask/rtr/лимит. */
  t->rx_channel = ch;
  t->rx_dlc = edge;
  t->rx_id_mask = 0U;
  fix_crc(t);
}

/* Запись «кадр → aux-действие»: src_flags&TX_AUX, tx_channel = канал,
 * tx_dlc = режим AUX_MODE_*, tx_data = параметры. */
static void make_aux_action_record(trigger_t *t, uint32_t rx_id,
                                   uint8_t ch, uint8_t mode,
                                   const uint8_t params[8])
{
  make_flex_record(t, 2U, rx_id, 0U, 0U);
  t->src_flags |= TRIGGER_F_TX_AUX;
  t->tx_channel = ch;
  t->tx_dlc = mode;
  if (params != NULL) {
    memcpy(t->tx_data, params, 8);
  }
  fix_crc(t);
}

/* Событие «канал включился» исполняет запись без CAN-кадра —
 * автономный путь CM-HW-01: очередь aux_out → Trigger_Poll. */
static void test_aux_event_fires_on_transition(void)
{
  trigger_t t;
  make_aux_event_record(&t, 3U, 1U, 0x321U);
  load_flex(&t, 1);

  aux_push_event(3U, 1U);          /* канал 3 включился */
  Trigger_Poll();                  /* очередь разбирается в Poll */
  CHECK(s_sent_count == 1, "aux ON event must fire the record");
  CHECK(s_sent[0].id == 0x321U, "aux event fired wrong tx id");

  /* Повторное «включился» — повторная сработка (события не липкие). */
  aux_push_event(3U, 1U);
  Trigger_Poll();
  CHECK(s_sent_count == 2, "aux ON event must re-fire");
}

/* Фронт «выключился» не срабатывает на «включился» и наоборот. */
static void test_aux_event_edges_distinct(void)
{
  trigger_t t;
  make_aux_event_record(&t, 2U, 2U, 0x111U);  /* ждём OFF */
  load_flex(&t, 1);

  aux_push_event(2U, 1U);          /* ON — не наш фронт */
  Trigger_Poll();
  CHECK(s_sent_count == 0, "OFF-edge record must ignore ON event");
  aux_push_event(2U, 0U);          /* OFF — срабатывает */
  Trigger_Poll();
  CHECK(s_sent_count == 1, "OFF-edge record must fire on OFF");
}

/* Условие «доп. канал работает» (COND_AUX) фильтрует кадр по
 * логической активности канала — AuxOut_IsActive, не уровень ноги. */
static void test_aux_condition_gates_frame(void)
{
  trigger_t t;
  make_flex_record(&t, 2U, 0x100U, 0U, 0x200U);
  t.src_flags |= TRIGGER_F_COND_AUX;
  t.src_channel = 5U;
  t.src_dlc = 1U;                  /* «работает» */
  fix_crc(&t);
  load_flex(&t, 1);

  const uint8_t d[1] = {0x01};
  can_frame_t f = mk_frame(0U, 0x100U, 1U, d);
  s_aux_active[5] = 0U;
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "aux condition off must gate the frame");

  s_aux_active[5] = 1U;
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1, "aux condition on must pass the frame");

  /* «не работает» — src_dlc=2: зеркальное условие. */
  trigger_t t2;
  make_flex_record(&t2, 2U, 0x100U, 0U, 0x200U);
  t2.src_flags |= TRIGGER_F_COND_AUX;
  t2.src_channel = 5U;
  t2.src_dlc = 2U;
  fix_crc(&t2);
  load_flex(&t2, 1);
  s_aux_active[5] = 1U;
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 0, "aux not-active cond must block when on");
  s_aux_active[5] = 0U;
  Trigger_OnFrame(&f);
  CHECK(s_sent_count == 1, "aux not-active cond must pass when off");
}

/* Действие «доп. канал» исполняет AuxOut_Set — кадр на шину НЕ
 * отправляется, параметры ШИМ разворачиваются из tx_data. */
static void test_aux_action_executes_without_can(void)
{
  uint8_t params[8] = {0};
  params[0] = 100U;                /* freq u16 */
  params[1] = 0;
  params[2] = 25U;                 /* duty % — PWM 0% легален, но тут 25 */
  params[3] = 0xE8; params[4] = 0x03; /* time 1000 мс u32 */
  trigger_t t;
  make_aux_action_record(&t, 0x100U, 4U, 3U /*AUX_MODE_PWM*/, params);
  load_flex(&t, 1);

  const uint8_t d[1] = {0x01};
  can_frame_t f = mk_frame(0U, 0x100U, 1U, d);
  Trigger_OnFrame(&f);
  run_ms(1);
  CHECK(s_sent_count == 0, "aux action must NOT send CAN frames");
  CHECK(s_aux_set_calls == 1, "aux action must call AuxOut_Set");
  CHECK(s_aux_last.channel == 4U && s_aux_last.mode == 3U,
        "aux action wrong channel/mode");
  CHECK(s_aux_last.pwm_freq == 100U && s_aux_last.duty == 25U,
        "aux action wrong PWM params");
  CHECK(s_aux_last.time_ms == 1000U, "aux action wrong duration");
  CHECK(s_aux_active[4] == 1U, "aux action must set active state");
}

/* Каскад aux-событий ограничен бюджетом опроса — кольцевая программа
 * не запирает главный цикл: за Poll обрабатывается не более
 * AUX_EVENT_DEPTH событий. */
static void test_aux_event_budget_per_poll(void)
{
  trigger_t t;
  make_aux_event_record(&t, 1U, 1U, 0x321U);
  load_flex(&t, 1);

  for (int i = 0; i < 12; i++) {
    aux_push_event(1U, 1U);
  }
  Trigger_Poll();
  CHECK(s_sent_count == 8, "aux poll budget must cap fires per poll");
  Trigger_Poll();
  CHECK(s_sent_count == 12, "remaining aux events fire next poll");
}

/* Контейнер FLXB v2 принимается; неизвестная версия — отказ целиком. */
static void test_flxb_v2_container(void)
{
  trigger_t t;
  make_aux_event_record(&t, 3U, 1U, 0x321U);
  s_flex_len = build_flex_blob_ver(&t, 1, 2U);
  s_flex_blob = s_blob;
  s_sent_count = 0;
  memset(s_triggers, 0, sizeof(s_triggers));
  memset(s_flex_queue, 0, sizeof(s_flex_queue));
  s_count = s_user_count = 0U;
  aux_reset();
  Trigger_ReloadFlex();

  aux_push_event(3U, 1U);
  Trigger_Poll();
  CHECK(s_sent_count == 1, "FLXB v2 must load aux records");

  /* v4 — неизвестная версия: контейнер отвергается целиком. */
  s_flex_len = build_flex_blob_ver(&t, 1, 4U);
  s_flex_blob = s_blob;
  s_sent_count = 0;
  s_count = s_user_count = 0U;
  Trigger_ReloadFlex();
  aux_push_event(3U, 1U);
  Trigger_Poll();
  CHECK(s_sent_count == 0, "FLXB v4 must be rejected whole");
}

/* ---- CM-FR05: «последняя команда канала побеждает» ---------------- */

/* Отложенный ON (delay=120) отменяется мгновенным OFF того же канала:
 * команда OFF, выданная пока тикает чужая задержка, снимает висящий
 * в s_flex_queue ON — иначе канал включался бы ПОСЛЕ OFF. */
static void test_aux_off_cancels_pending_on(void)
{
  trigger_t recs[2];
  make_aux_action_record(&recs[0], 0x100U, 2U, AUX_MODE_ON, NULL);
  recs[0].delay_ms = 120U;
  fix_crc(&recs[0]);
  make_aux_action_record(&recs[1], 0x101U, 2U, AUX_MODE_OFF, NULL);
  load_flex(recs, 2);

  const uint8_t d[1] = {0x01};
  can_frame_t f_on = mk_frame(0U, 0x100U, 1U, d);
  can_frame_t f_off = mk_frame(0U, 0x101U, 1U, d);

  Trigger_OnFrame(&f_on);          /* ON вооружён на t=1120 */
  run_ms(10);                      /* t=1010 */
  Trigger_OnFrame(&f_off);         /* OFF — сейчас + отмена ON */
  CHECK(s_aux_set_calls == 1 && s_aux_last.mode == AUX_MODE_OFF
        && s_aux_last.channel == 2U,
        "OFF must execute immediately and alone");
  run_ms(140);                     /* t=1150 — срок ON давно вышел */
  CHECK(s_aux_set_calls == 1,
        "cancelled delayed ON must never execute");
  CHECK(s_aux_active[2] == 0U && s_aux_mode[2] == 0U,
        "channel must stay OFF after the pending ON is cancelled");
}

/* Отложенная команда снимает более СТАРУЮ отложенную того же канала:
 * ON@120 сменяется позже выданным OFF@50 — исполняется только OFF. */
static void test_aux_late_command_supersedes_queued(void)
{
  trigger_t recs[2];
  make_aux_action_record(&recs[0], 0x100U, 2U, AUX_MODE_ON, NULL);
  recs[0].delay_ms = 120U;
  fix_crc(&recs[0]);
  make_aux_action_record(&recs[1], 0x101U, 2U, AUX_MODE_OFF, NULL);
  recs[1].delay_ms = 50U;
  fix_crc(&recs[1]);
  load_flex(recs, 2);

  const uint8_t d[1] = {0x01};
  can_frame_t f_on = mk_frame(0U, 0x100U, 1U, d);
  can_frame_t f_off = mk_frame(0U, 0x101U, 1U, d);

  Trigger_OnFrame(&f_on);          /* ON вооружён на 1120 */
  run_ms(10);                      /* t=1010 */
  Trigger_OnFrame(&f_off);         /* OFF вооружён на 1060, ON снят */
  run_ms(45);                      /* t=1055 — ещё ничего */
  CHECK(s_aux_set_calls == 0, "nothing may fire before the OFF delay");
  run_ms(100);                     /* t=1155 — оба срока прошли */
  CHECK(s_aux_set_calls == 1 && s_aux_last.mode == AUX_MODE_OFF,
        "only the newer OFF command executes");
  CHECK(s_aux_active[2] == 0U, "channel must be OFF, not ON");
}

/* Отмена — строго по каналу: OFF канала 3 не трогает отложенный ON
 * канала 2 — чужие расписания сохраняются. */
static void test_aux_cancel_is_per_channel(void)
{
  trigger_t recs[2];
  make_aux_action_record(&recs[0], 0x100U, 2U, AUX_MODE_ON, NULL);
  recs[0].delay_ms = 40U;
  fix_crc(&recs[0]);
  make_aux_action_record(&recs[1], 0x101U, 3U, AUX_MODE_OFF, NULL);
  load_flex(recs, 2);

  const uint8_t d[1] = {0x01};
  can_frame_t f_on = mk_frame(0U, 0x100U, 1U, d);
  can_frame_t f_off = mk_frame(0U, 0x101U, 1U, d);

  Trigger_OnFrame(&f_on);          /* ON ch2 вооружён на 1040 */
  run_ms(10);                      /* t=1010 */
  Trigger_OnFrame(&f_off);         /* OFF ch3 — мгновенно, чужой канал */
  CHECK(s_aux_set_calls == 1, "OFF ch3 executes now");
  run_ms(40);                      /* t=1050 — срок ON ch2 */
  CHECK(s_aux_set_calls == 2 && s_aux_last.mode == AUX_MODE_ON
        && s_aux_last.channel == 2U,
        "delayed ON of another channel must still fire");
  CHECK(s_aux_active[2] == 1U && s_aux_active[3] == 0U,
        "per-channel states: ch2 ON, ch3 OFF");
}

/* ---- CM-F02: записи переменных (FLXB v3, протокол 14) -------------- */

/* Updater статической переменной: кадр → псевдо-действие SETFLAG
 * (src_flags&TX_SETFLAG, tx_data[0]=индекс, [1]=значение,
 * [2..5]=авто-выкл мс). */
static void make_setflag_record(trigger_t *t, uint32_t rx_id,
                                uint8_t flag_idx, uint8_t val,
                                uint32_t auto_off_ms)
{
  make_flex_record(t, 2U, rx_id, 0U, 0U);
  t->src_flags |= TRIGGER_F_TX_SETFLAG;
  t->tx_channel = 0U;
  t->tx_dlc = 0U;
  t->tx_id = 0U;
  t->tx_data[0] = flag_idx;
  t->tx_data[1] = val;
  t->tx_data[2] = (uint8_t)(auto_off_ms & 0xFFU);
  t->tx_data[3] = (uint8_t)((auto_off_ms >> 8) & 0xFFU);
  t->tx_data[4] = (uint8_t)((auto_off_ms >> 16) & 0xFFU);
  t->tx_data[5] = (uint8_t)((auto_off_ms >> 24) & 0xFFU);
  fix_crc(t);
}

/* Слушатель фронта флага: rx_flags&RX_VAR, rx_channel=0xFF,
 * rx_dlc = фронт (0 любой/1 «стал 1»/2 «стал 0»),
 * rx_data_mask[0] = индекс флага. */
static void make_flag_listener(trigger_t *t, uint8_t flag_idx,
                               uint8_t edge, uint32_t tx_id)
{
  make_flex_record(t, 0U, 0U, 0U, tx_id);
  t->rx_flags |= TRIGGER_F_RX_VAR;
  t->rx_channel = TRIGGER_VAR_CHAN_FLAG;
  t->rx_dlc = edge;
  t->rx_id_mask = 0U;
  t->rx_data_mask[0] = flag_idx;
  fix_crc(t);
}

/* «Динамическая переменная»: rx_flags&RX_VAR, rx_channel=CAN-канал,
 * mask = offset/len/endian/cmp/kind, rx_data = значение u64. */
static void make_var_record(trigger_t *t, uint8_t ch, uint32_t rx_id,
                            uint8_t off, uint8_t len, uint8_t endian,
                            uint8_t cmp, uint8_t kind, uint64_t val,
                            uint32_t tx_id)
{
  make_flex_record(t, ch, rx_id, 0U, tx_id);
  t->rx_flags |= TRIGGER_F_RX_VAR;
  t->rx_data_mask[0] = off;
  t->rx_data_mask[1] = len;
  t->rx_data_mask[2] = endian;
  t->rx_data_mask[3] = cmp;
  t->rx_data_mask[4] = kind;
  for (uint8_t i = 0; i < 8U; i++) {
    t->rx_data[i] = (uint8_t)((val >> (8U * i)) & 0xFFU);
  }
  fix_crc(t);
}

/* Статическая переменная «Зажигание»: updater ставит бит по кадру,
 * слушатель фронта «стала 1» отправляет ответ — как программа
 * «статическая → команда» из дампа CM-F02. */
static void test_static_flag_listener_and_updater(void)
{
  trigger_t recs[3];
  make_flag_listener(&recs[0], 0U, 1U, 0x321U);      /* фронт ON → кадр */
  make_setflag_record(&recs[1], 0x300U, 0U, 1U, 0U); /* ON-кадр */
  make_setflag_record(&recs[2], 0x301U, 0U, 0U, 0U); /* OFF-кадр */
  load_flex(recs, 3);

  can_frame_t on = mk_frame(0U, 0x300U, 1U, (const uint8_t *)"\x01");
  can_frame_t off = mk_frame(0U, 0x301U, 1U, (const uint8_t *)"\x00");
  Trigger_OnFrame(&on);          /* флаг 0→1, фронт в очереди */
  Trigger_Poll();                /* раздача фронта слушателю */
  CHECK(s_sent_count == 1, "flag ON edge must fire the listener");
  Trigger_OnFrame(&on);          /* уже 1 — повторного фронта нет */
  Trigger_Poll();
  CHECK(s_sent_count == 1, "repeated ON frame must not refire");
  Trigger_OnFrame(&off);         /* 1→0: фронт OFF слушателю ON — нет */
  Trigger_Poll();
  CHECK(s_sent_count == 1, "OFF edge must not fire ON listener");
  Trigger_OnFrame(&on);          /* 0→1 снова — фронт */
  Trigger_Poll();
  CHECK(s_sent_count == 2, "second ON edge must fire again");
}

/* Численная переменная «поле > порог»: фронт истинности — одна
 * сработка на переходе, пока условие держится — повторов нет
 * (_dyn_flags на ПК). */
static void test_dyn_var_threshold_edge(void)
{
  trigger_t recs[1];
  /* bytes[0..1] little-endian > 100 → кадр 0x321. */
  make_var_record(&recs[0], 2U, 0x100U, 0U, 2U, 1U, 1U, 1U, 100U,
                  0x321U);
  load_flex(recs, 1);

  uint8_t lo[8] = {50, 0};
  uint8_t hi[8] = {200, 0};
  can_frame_t f_lo = mk_frame(0U, 0x100U, 8U, lo);
  can_frame_t f_hi = mk_frame(0U, 0x100U, 8U, hi);
  Trigger_OnFrame(&f_lo);
  CHECK(s_sent_count == 0, "below threshold must not fire");
  Trigger_OnFrame(&f_hi);
  CHECK(s_sent_count == 1, "rising edge must fire once");
  Trigger_OnFrame(&f_hi);
  CHECK(s_sent_count == 1, "sustained value must not refire");
  Trigger_OnFrame(&f_lo);
  Trigger_OnFrame(&f_hi);
  CHECK(s_sent_count == 2, "second crossing must fire again");
}

/* dyn_cache: позиции поля накапливаются покадрово — значение
 * сравнивается когда все позиции записаны хотя бы раз. */
static void test_dyn_cache_partial_then_complete(void)
{
  trigger_t recs[1];
  /* bytes[2..3] BE == 0x0201 («Драйв») → кадр 0x321. */
  make_var_record(&recs[0], 2U, 0x200U, 2U, 2U, 0U, 0U, 0U, 0x0201U,
                  0x321U);
  load_flex(recs, 1);

  uint8_t half[8] = {0, 0, 2, 0};   /* DLC 3 — позиция 3 не записана */
  uint8_t rest[8] = {0, 0, 2, 1};   /* DLC 4 — поле целиком: 0x0201 */
  uint8_t other[8] = {0, 0, 2, 9};  /* raw 0x0209 — не совпадает */
  can_frame_t f_half = mk_frame(0U, 0x200U, 3U, half);
  can_frame_t f_rest = mk_frame(0U, 0x200U, 4U, rest);
  can_frame_t f_other = mk_frame(0U, 0x200U, 4U, other);
  Trigger_OnFrame(&f_half);
  CHECK(s_sent_count == 0, "incomplete field must not fire");
  Trigger_OnFrame(&f_rest);
  CHECK(s_sent_count == 1, "completed binding value must fire");
  Trigger_OnFrame(&f_other);
  Trigger_OnFrame(&f_other);
  CHECK(s_sent_count == 1, "non-matching value must stay silent");
}

/* Условие «флаг в состоянии» (COND_FLAG): кадровая запись срабатывает
 * только когда бит поднят — как «ПРИ статическая == 1» на ПК. */
static void test_cond_flag_gates_frame(void)
{
  trigger_t recs[2];
  make_flex_record(&recs[0], 2U, 0x123U, 0U, 0x456U);
  recs[0].src_flags |= TRIGGER_F_COND_FLAG;
  recs[0].src_id = 0U;
  recs[0].src_dlc = 1U;            /* «есть 1» */
  fix_crc(&recs[0]);
  make_setflag_record(&recs[1], 0x300U, 0U, 1U, 0U);
  load_flex(recs, 2);

  can_frame_t ev = mk_frame(0U, 0x123U, 8U, NULL);
  can_frame_t on = mk_frame(0U, 0x300U, 1U, (const uint8_t *)"\x01");
  Trigger_OnFrame(&ev);
  CHECK(s_sent_count == 0, "frame with flag down must not fire");
  Trigger_OnFrame(&on);
  Trigger_Poll();
  Trigger_OnFrame(&ev);
  CHECK(s_sent_count == 1, "frame with flag up must fire");
}

/* «вкл на N мс»: SETFLAG с авто-выкл — спустя срок бит падает,
 * слушатель фронта «стал 0» исполняется. */
static void test_setflag_pulse_auto_off(void)
{
  trigger_t recs[2];
  make_setflag_record(&recs[0], 0x300U, 0U, 1U, 50U); /* вкл на 50 мс */
  make_flag_listener(&recs[1], 0U, 2U, 0x321U);       /* фронт OFF */
  load_flex(recs, 2);

  can_frame_t on = mk_frame(0U, 0x300U, 1U, (const uint8_t *)"\x01");
  Trigger_OnFrame(&on);
  run_ms(30);
  Trigger_Poll();
  CHECK(s_sent_count == 0, "auto-off must not fire early");
  run_ms(30);                      /* 60 мс — авто-выкл + фронт OFF */
  Trigger_Poll();
  CHECK(s_sent_count == 1, "auto-off edge must fire OFF listener");
}

int main(void)
{
  test_flex_record_fires();
  test_two_programs_same_event();
  test_series_schedules_preserved();
  test_identical_frames_not_suppressed();
  test_event_between_repeats();
  test_tx_failure_retries_and_drops();
  test_retry_keeps_series_schedule();
  test_retry_channels_independent();
  test_retry_outage_policy();
  test_queue_overflow_counted();
  test_pad8_dlc_not_a_change();
  test_full_data_tracking();
  test_fire_on_boot();
  test_x_subst_zero_pad();
  test_x_subst_per_firing_snapshot();
  test_short_frame_no_match();
  test_flxb_validation();
  test_legacy_json_blob();
  test_aux_event_fires_on_transition();
  test_aux_event_edges_distinct();
  test_aux_condition_gates_frame();
  test_aux_action_executes_without_can();
  test_aux_event_budget_per_poll();
  test_flxb_v2_container();
  test_aux_off_cancels_pending_on();
  test_aux_late_command_supersedes_queued();
  test_aux_cancel_is_per_channel();
  test_static_flag_listener_and_updater();
  test_dyn_var_threshold_edge();
  test_dyn_cache_partial_then_complete();
  test_cond_flag_gates_frame();
  test_setflag_pulse_auto_off();

  if (s_failures == 0) {
    printf("PASS: all flex-runtime checks\n");
    return 0;
  }
  printf("FAILURES: %d\n", s_failures);
  return 1;
}
