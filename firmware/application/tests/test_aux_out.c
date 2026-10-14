/* Host test for the PRODUCTION output state machine
 * firmware/application/Src/aux_out.c — the source is included
 * textually (not an algorithm copy). Covers review findings
 * CM-N4/CM-N5/CM-N6:
 *
 *  - ownership mask: channels 2..5 always, 1/6/7 only by bits;
 *    pin release → GPIO input; PA14 → SWJ remap off/on;
 *  - deadline: separate enable flag (stop_at=0 is a legal moment),
 *    wrap-safe signed comparison — expiry across a HAL_GetTick
 *    wraparound still fires;
 *  - PWM quantization on whole-ms grid (mirror of
 *    core/aux_channels.quantize_pwm); extreme duties are static;
 *  - Poll budget: a tick jump folds whole periods arithmetically —
 *    no burst of missed GPIO edges, folded edges counted in
 *    AuxOut_EdgesLate;
 *  - PULSE series length honoured, ends low, event on finish.
 *
 * Build & run (from the repository root):
 *   cc -I firmware/application/tests/stubs -I firmware/application/Inc \
 *      -I firmware/application/Src \
 *      firmware/application/tests/test_aux_out.c -o /tmp/test_aux_out
 *   /tmp/test_aux_out
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>

#include "main.h"
#include "aux_out.h"

/* ---- Controllable HAL_GetTick ------------------------------------- */
static uint32_t s_tick;
uint32_t HAL_GetTick(void) { return s_tick; }
void HAL_Delay(uint32_t Delay) { s_tick += Delay; }

/* ---- GPIO spy ------------------------------------------------------ */
typedef struct {
  const GPIO_TypeDef *port;
  uint16_t pin;
  uint8_t  mode;      /* last GPIO_MODE_* seen in HAL_GPIO_Init */
  uint8_t  level;     /* last driven level */
  uint32_t writes;    /* HAL_GPIO_WritePin call count */
} gpio_spy_t;

static gpio_spy_t *find_spy(const GPIO_TypeDef *port, uint16_t pin);
static gpio_spy_t s_gpio[16];
static int s_gpio_count;
static uint32_t s_writes_total;

static gpio_spy_t *spy(const GPIO_TypeDef *port, uint16_t pin)
{
  for (int i = 0; i < s_gpio_count; i++) {
    if (s_gpio[i].port == port && s_gpio[i].pin == pin) {
      return &s_gpio[i];
    }
  }
  gpio_spy_t *s = &s_gpio[s_gpio_count++];
  s->port = port;
  s->pin = pin;
  s->mode = 0xFFU;
  s->level = 0U;
  s->writes = 0U;
  return s;
}

static gpio_spy_t *find_spy(const GPIO_TypeDef *port, uint16_t pin)
{
  return spy(port, pin);
}

void HAL_GPIO_Init(GPIO_TypeDef *GPIOx, const GPIO_InitTypeDef *init)
{
  spy(GPIOx, init->Pin)->mode = (uint8_t)init->Mode;
}

void HAL_GPIO_WritePin(GPIO_TypeDef *GPIOx, uint16_t pin,
                       GPIO_PinState state)
{
  gpio_spy_t *s = spy(GPIOx, pin);
  s->level = (state == GPIO_PIN_SET) ? 1U : 0U;
  s->writes++;
  s_writes_total++;
}

GPIO_PinState HAL_GPIO_ReadPin(GPIO_TypeDef *GPIOx, uint16_t pin)
{
  return spy(GPIOx, pin)->level ? GPIO_PIN_SET : GPIO_PIN_RESET;
}

/* ---- AFIO spy ------------------------------------------------------ */
static uint32_t s_swj_calls;
static uint32_t s_swj_last;
void StubAfio_RemapSwj(uint32_t mapr_swj_cfg)
{
  s_swj_calls++;
  s_swj_last = mapr_swj_cfg;
}

/* ---- Framework ----------------------------------------------------- */
static int s_failures;
#define CHECK(cond, msg)                                               \
  do {                                                                 \
    if (!(cond)) {                                                     \
      s_failures++;                                                    \
      printf("FAIL %d: %s\n", __LINE__, msg);                          \
    }                                                                  \
  } while (0)

#define SWJ_DISABLE_BITS 0x07000000UL
#define SWJ_NOJTAG_BITS  0x02000000UL

/* Канал 3 = PC10 — свободный всегда (без конфликтов). */
#define CH_FREE  3U
/* Канал 1 = PA14 (SWCLK), 6 = PD2 (TERM2), 7 = PB3 (TERM1). */

/* The production state machine under test — included textually so
 * statics (deadline flag, event queue, edge counter) run unmodified. */
#include "aux_out.c"

static void reset_all(void)
{
  s_tick = 1000U;
  memset(s_gpio, 0, sizeof(s_gpio));
  s_gpio_count = 0;
  s_writes_total = 0U;
  s_swj_calls = 0U;
  s_swj_last = 0U;
}

static void tick(uint32_t ms)
{
  s_tick += ms;
  AuxOut_Poll();
}

/* ---- Scenarios ----------------------------------------------------- */

static void test_ownership_mask(void)
{
  reset_all();
  AuxOut_Init(0U);
  /* 2..5 доступны без битов, 1/6/7 — только по битам маски. */
  for (uint8_t ch = 2U; ch <= 5U; ch++) {
    CHECK(AuxOut_IsAvailable(ch), "channels 2..5 always available");
  }
  CHECK(!AuxOut_IsAvailable(1U), "ch1 needs PA14 bit");
  CHECK(!AuxOut_IsAvailable(6U), "ch6 needs PD2 bit");
  CHECK(!AuxOut_IsAvailable(7U), "ch7 needs PB3 bit");
  /* Команда на недоступный канал — отказ, пин не трогается. */
  uint32_t w = s_writes_total;
  CHECK(AuxOut_Set(6U, AUX_MODE_ON, 0, 0, 0, 0, 0, 0) == 0U,
        "Set on unavailable channel rejected");
  CHECK(s_writes_total == w, "rejected Set writes no GPIO");
  /* SWJ при PA14 не трогается без бита владения. */
  CHECK(s_swj_calls == 0U, "no SWJ remap without PA14 ownership");
}

static void test_pa14_swj_release_restore(void)
{
  reset_all();
  AuxOut_Init(AUX_OWN_PA14);
  CHECK(AuxOut_IsAvailable(1U), "ch1 available when owned");
  CHECK(s_swj_last == SWJ_DISABLE_BITS,
        "PA14 acquire disables SWJ (SWCLK released)");
  AuxOut_ApplyOwnedMask(0U);
  CHECK(!AuxOut_IsAvailable(1U), "ch1 unavailable after release");
  CHECK(s_swj_last == SWJ_NOJTAG_BITS,
        "PA14 release restores SWD (NOJTAG)");
  /* Пин вернулся в безопасный вход. */
  CHECK(find_spy(AUX_CH1_PORT, AUX_CH1_PIN)->mode == GPIO_MODE_INPUT,
        "released PA14 goes to input");
  /* Повторный ApplyOwnedMask без изменений — лишних remap нет. */
  uint32_t calls = s_swj_calls;
  AuxOut_ApplyOwnedMask(0U);
  CHECK(s_swj_calls == calls, "idempotent mask apply");
}

static void test_on_off_and_events(void)
{
  reset_all();
  AuxOut_Init(0U);
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_ON, 0, 0, 0, 0, 0, 0) == 1U,
        "ON accepted");
  CHECK(AuxOut_Get(CH_FREE) == 1U, "ON drives high");
  CHECK(AuxOut_IsActive(CH_FREE), "ON is active");
  uint8_t ch = 0, on = 0;
  CHECK(AuxOut_TakeEvent(&ch, &on) && ch == CH_FREE && on == 1U,
        "ON pushes turn-on event");
  /* Смена ON→OFF — событие выключения. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_OFF, 0, 0, 0, 0, 0, 0) == 1U,
        "OFF accepted");
  CHECK(AuxOut_Get(CH_FREE) == 0U, "OFF drives low");
  CHECK(!AuxOut_IsActive(CH_FREE), "OFF inactive");
  CHECK(AuxOut_TakeEvent(&ch, &on) && ch == CH_FREE && on == 0U,
        "OFF pushes turn-off event");
  CHECK(AuxOut_TakeEvent(&ch, &on) == 0U, "queue drained");
}

static void test_timed_on_deadline(void)
{
  reset_all();
  AuxOut_Init(0U);
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_ON, 0, 0, 0, 0, 0, 100U) == 1U,
        "timed ON accepted");
  tick(99U);
  CHECK(AuxOut_IsActive(CH_FREE), "still on before deadline");
  tick(1U);
  CHECK(!AuxOut_IsActive(CH_FREE), "auto-off at deadline");
  CHECK(AuxOut_Get(CH_FREE) == 0U, "level low after deadline");
}

static void test_deadline_at_stop_at_zero(void)
{
  /* CM-N5: stop_at==0 — легальный момент (now+duration на границе
   * wrap). Старый контракт «0 = нет срока» такой дедлайн терял. */
  reset_all();
  AuxOut_Init(0U);
  s_tick = 0xFFFFFF00UL;
  /* duration 0x100 → stop_at ровно 0. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_ON, 0, 0, 0, 0, 0, 0x100U) == 1U,
        "timed ON accepted");
  s_tick = 0xFFFFFFFFUL;
  AuxOut_Poll();
  CHECK(AuxOut_IsActive(CH_FREE), "before wrapped deadline");
  s_tick = 0U;            /* перенос через границу */
  AuxOut_Poll();
  CHECK(!AuxOut_IsActive(CH_FREE),
        "deadline at stop_at==0 fires across wrap");
}

static void test_wrap_safe_edges(void)
{
  reset_all();
  AuxOut_Init(0U);
  s_tick = 0xFFFFFFF0UL;
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 100U, 50U, 0) == 1U,
        "PWM accepted near wrap");
  uint32_t edges = 0U;
  for (int i = 0; i < 40; i++) {
    uint8_t prev = AuxOut_Get(CH_FREE);
    s_tick++;            /* в т.ч. через 0xFFFFFFFF → 0 */
    AuxOut_Poll();
    if (AuxOut_Get(CH_FREE) != prev) {
      edges++;
    }
  }
  /* 100 Гц 50% → период 10 мс, фронт каждые 5 мс: за 40 тиков 8 фронтов. */
  CHECK(edges == 8U, "PWM edges toggle across tick wrap");
}

static void test_pwm_quantization(void)
{
  reset_all();
  AuxOut_Init(0U);
  uint16_t on = 0xFFFF, off = 0xFFFF;

  /* 500 Гц: период 2 мс; сетка duty = {0, 50, 100}%. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 500U, 50U, 0) == 1U,
        "500Hz accepted");
  AuxOut_GetTiming(CH_FREE, &on, &off);
  CHECK(on == 1U && off == 1U, "500Hz 50% -> 1+1ms");

  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 500U, 1U, 0) == 1U,
        "500Hz 1% accepted");
  AuxOut_GetTiming(CH_FREE, &on, &off);
  /* Ближайший узел к 1% при периоде 2 мс — 0%: канал активен,
   * уровень статический низкий, фронтов нет. */
  CHECK(on == 0U && off == 2U, "500Hz 1% quantizes to 0%");
  CHECK(AuxOut_IsActive(CH_FREE), "0% PWM is logically active");
  CHECK(AuxOut_Get(CH_FREE) == 0U, "0% PWM static low");

  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 500U, 99U, 0) == 1U,
        "500Hz 99% accepted");
  AuxOut_GetTiming(CH_FREE, &on, &off);
  CHECK(on == 2U && off == 0U, "500Hz 99% quantizes to 100%");
  CHECK(AuxOut_Get(CH_FREE) == 1U, "100% PWM static high");

  /* Нецелочисленный период: 333 Гц → round(1000/333)=3 мс; duty 50%
   * → ближайший узел on=(3*50+50)/100=2 → фактические 2/1 мс. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 333U, 50U, 0) == 1U,
        "333Hz accepted");
  AuxOut_GetTiming(CH_FREE, &on, &off);
  CHECK(on == 2U && off == 1U, "333Hz 50% -> 2+1ms (66.7% applied)");

  /* Границы валидации: 0/1000 Гц и 101% — отказ. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 0U, 50U, 0) == 0U,
        "0 Hz rejected");
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 501U, 50U, 0) == 0U,
        ">500 Hz rejected");
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 100U, 101U, 0) == 0U,
        "duty>100 rejected");
}

static void test_poll_budget_no_edge_burst(void)
{
  reset_all();
  AuxOut_Init(0U);
  /* 500 Гц 50% — период 2 мс. Скачок tick +100 мс = 100 просроченных
   * фронтов; Poll обязан свернуть их без залпа записей в GPIO. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PWM, 0, 0, 0, 500U, 50U, 0) == 1U,
        "PWM armed");
  s_writes_total = 0U;
  s_tick += 100U;
  AuxOut_Poll();
  /* Залп ограничен бюджетом: сотни фронтов не воспроизводятся. */
  CHECK(s_writes_total <= 9U, "no GPIO burst after tick jump");
  CHECK(AuxOut_EdgesLate() > 0U, "folded edges counted");
  /* Уровень согласован с фазой (0/1 — оба легальны, главное — не
   * застрял в середине серии и продолжает работать). */
  uint32_t w = s_writes_total;
  tick(3U);
  CHECK(s_writes_total > w, "PWM keeps toggling after catch-up");
}

static void test_pulse_series(void)
{
  reset_all();
  AuxOut_Init(0U);
  /* 2 импульса 50/50 мс. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PULSE, 50U, 50U, 2U, 0, 0, 0) == 1U,
        "PULSE accepted");
  CHECK(AuxOut_Get(CH_FREE) == 1U, "pulse starts high");
  tick(50U);                       /* фронт вниз — импульс 1 */
  CHECK(AuxOut_Get(CH_FREE) == 0U, "low phase");
  tick(50U);                       /* фронт вверх — импульс 2 */
  CHECK(AuxOut_Get(CH_FREE) == 1U, "second pulse high");
  tick(50U);                       /* конец серии */
  CHECK(!AuxOut_IsActive(CH_FREE), "series finished -> OFF");
  CHECK(AuxOut_Get(CH_FREE) == 0U, "ends low");
  uint8_t ch = 0, on = 0xFF;
  /* События: вкл + выкл (фронты серии — не логические переходы). */
  int events = 0, saw_on = 0, saw_off = 0;
  while (AuxOut_TakeEvent(&ch, &on)) {
    events++;
    if (on) saw_on = 1; else saw_off = 1;
  }
  CHECK(events == 2 && saw_on && saw_off,
        "pulse emits exactly on+off events");
}

static void test_pulse_jump_consumes_series(void)
{
  reset_all();
  AuxOut_Init(0U);
  /* 3 импульса по 2+2 мс; скачок +12 мс = 3 полных периода —
   * серия сворачивается и завершается без фронтов-прострелов. */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_PULSE, 2U, 2U, 3U, 0, 0, 0) == 1U,
        "PULSE armed");
  s_tick += 12U;
  AuxOut_Poll();
  CHECK(!AuxOut_IsActive(CH_FREE), "jumped pulses end series");
  CHECK(AuxOut_Get(CH_FREE) == 0U, "ends low after jump");
}

static void test_all_channels_parallel(void)
{
  reset_all();
  AuxOut_Init(AUX_OWN_PA14 | AUX_OWN_PD2 | AUX_OWN_PB3);
  for (uint8_t ch = 1U; ch <= AUX_CH_COUNT; ch++) {
    CHECK(AuxOut_Set(ch, AUX_MODE_PWM, 0, 0, 0, 200U, 50U, 0) == 1U,
          "all 7 channels run PWM");
  }
  for (int i = 0; i < 20; i++) {
    tick(1U);
  }
  for (uint8_t ch = 1U; ch <= AUX_CH_COUNT; ch++) {
    CHECK(AuxOut_IsActive(ch), "all channels active");
  }
  /* У каждого канала своя нога — уровни независимы. */
  for (int i = 0; i < s_gpio_count; i++) {
    CHECK(s_gpio[i].writes > 0U, "each pin driven");
  }
}

static void test_duration_cap(void)
{
  reset_all();
  AuxOut_Init(0U);
  /* Срок > INT32_MAX ломал бы signed-дельту — отклоняем (CM-N5). */
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_ON, 0, 0, 0, 0, 0,
                   0x80000000UL) == 0U,
        "duration beyond wrap-safe range rejected");
  CHECK(AuxOut_Set(CH_FREE, AUX_MODE_ON, 0, 0, 0, 0, 0,
                   0x7FFFFFFFUL) == 1U,
        "max wrap-safe duration accepted");
}

int main(void)
{
  test_ownership_mask();
  test_pa14_swj_release_restore();
  test_on_off_and_events();
  test_timed_on_deadline();
  test_deadline_at_stop_at_zero();
  test_wrap_safe_edges();
  test_pwm_quantization();
  test_poll_budget_no_edge_burst();
  test_pulse_series();
  test_pulse_jump_consumes_series();
  test_all_channels_parallel();
  test_duration_cap();

  if (s_failures == 0) {
    printf("test_aux_out: PASS (all scenarios)\n");
    return 0;
  }
  printf("test_aux_out: %d failure(s)\n", s_failures);
  return 1;
}
