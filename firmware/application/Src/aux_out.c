/* Реализация доп. каналов — семь линий, см. aux_out.h и PINOUT.md.
 *
 * Все режимы кроме OFF строятся на одном автомате с фронтами по
 * HAL_GetTick: ON — фронтов нет (time_ms ограничивает срок), PULSE —
 * count полных периодов on/off, PWM — меандр on_ms/off_ms из частоты
 * и заполнения (time_ms ограничивает срок, 0 — до «Выкл»).
 *
 * События для ГЛ ставятся только на переходах ЛОГИЧЕСКОГО состояния:
 * OFF→активный режим = «канал включился», активный→OFF = «выключился»
 * (в т.ч. авто-выключение по сроку). Повторный ON на уже включённом
 * канале и смена ON→PWM без OFF перехода события не порождают —
 * поэтому правило «включился → включить» не зацикливается. */

#include <string.h>
#include "aux_out.h"
#include "main.h"

typedef struct {
  GPIO_TypeDef *port;
  uint16_t      pin;
  uint8_t       own_bit;   /* бит aux_pins_mask; 0 = доступен всегда */
} aux_pin_t;

/* Карта каналов — НОВАЯ нумерация (CMD_AUX_SET_EXT):
 * 1=PA14, 2=PA15, 3=PC10, 4=PC11, 5=PC12, 6=PD2, 7=PB3. */
static const aux_pin_t s_pins[AUX_CH_COUNT] = {
  { AUX_CH1_PORT, AUX_CH1_PIN, AUX_OWN_PA14 },
  { AUX_CH2_PORT, AUX_CH2_PIN, 0U },
  { AUX_CH3_PORT, AUX_CH3_PIN, 0U },
  { AUX_CH4_PORT, AUX_CH4_PIN, 0U },
  { AUX_CH5_PORT, AUX_CH5_PIN, 0U },
  { AUX_CH6_PORT, AUX_CH6_PIN, AUX_OWN_PD2 },
  { AUX_CH7_PORT, AUX_CH7_PIN, AUX_OWN_PB3 },
};

typedef struct {
  uint8_t  mode;         /* AUX_MODE_* */
  uint8_t  level;        /* текущий уровень на ноге */
  uint8_t  deadline_en;  /* срок задан — CM-N5: stop_at=0 теперь
                          * легальный момент, признак отдельный */
  uint16_t on_ms;        /* длительность высокого уровня */
  uint16_t off_ms;       /* длительность низкого уровня */
  uint16_t pulses_left;  /* сколько периодов осталось (pulse) */
  uint32_t next_tick;    /* HAL tick следующего фронта */
  uint32_t stop_at;      /* HAL tick авто-выключения при deadline_en */
} aux_ch_t;

static aux_ch_t s_ch[AUX_CH_COUNT];
static uint8_t  s_owned;       /* фактическая маска владения */
static uint8_t  s_swd_off;     /* PA14 отдан каналу → SWD выключен */
/* Счётчик пропущенных фронтов (скачок tick/бюджет Poll) — диагностика
 * точности ШИМ под нагрузкой (CM-N6). */
static uint32_t s_edges_late;

/* Очередь событий переходов: байты (channel-1)<<1 | is_on. */
static uint8_t  s_ev[AUX_EVENT_DEPTH];
static uint8_t  s_ev_head, s_ev_tail;
static uint32_t s_ev_dropped;

static void aux_drive(uint8_t idx, uint8_t level)
{
  s_ch[idx].level = level;
  HAL_GPIO_WritePin(s_pins[idx].port, s_pins[idx].pin,
                    level ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

static void aux_push_event(uint8_t idx, uint8_t is_on)
{
  uint8_t next = (uint8_t)((s_ev_head + 1U) % AUX_EVENT_DEPTH);
  if (next == s_ev_tail) {
    s_ev_dropped++;
    return;
  }
  s_ev[s_ev_head] = (uint8_t)((idx << 1U) | (is_on ? 1U : 0U));
  s_ev_head = next;
}

/* Переход логического состояния → событие для ГЛ. */
static void aux_set_active(uint8_t idx, uint8_t active)
{
  uint8_t was = (s_ch[idx].mode != AUX_MODE_OFF) ? 1U : 0U;
  if (was != (active ? 1U : 0U)) {
    aux_push_event(idx, active);
  }
}

static void aux_deinit_pin(uint8_t idx)
{
  /* Пин уходит из-под AUX: в безопасный вход. Восстановление функции
   * — немедленное, а не «при случайной следующей инициализации»
   * (CM-N4): SWCLK возвращает AFIO-remap в ApplyOwnedMask, TERM-пины
   * реинициализирует CanBridge_RestoreTermPins(), вызываемая из
   * CMD_AUX_MAP сразу после смены маски. */
  GPIO_InitTypeDef gpio = {0};
  gpio.Pin = s_pins[idx].pin;
  gpio.Mode = GPIO_MODE_INPUT;
  gpio.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(s_pins[idx].port, &gpio);
}

static void aux_init_pin(uint8_t idx)
{
  GPIO_InitTypeDef gpio = {0};
  gpio.Pin = s_pins[idx].pin;
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(s_pins[idx].port, &gpio);
  HAL_GPIO_WritePin(s_pins[idx].port, s_pins[idx].pin, GPIO_PIN_RESET);
}

void AuxOut_ApplyOwnedMask(uint8_t own_mask)
{
  uint8_t eff = (uint8_t)(own_mask | AUX_OWN_ALWAYS);
  /* CM-N4: PA14 (канал 1) физически занят SWCLK — освободить его можно
   * только отключив SWD целиком (SWJ_DISABLE заодно освобождает и
   * PA13/PA15/PB3/PB4 — они наши AUX-пины или не используются).
   * Без remap пин продолжал бы работать как SWCLK и aux_init_pin не
   * давал бы управления. Возврат пина → SWJ_NOJTAG (SWD снова живой,
   * JTAG остаётся выключенным — PB3 остаётся доступным). Отладка
   * отваливается на время владения PA14 — документированный компромисс;
   * после reset/загрузчика SWD восстанавливается (маска применяется в
   * AuxOut_Init уже после MX_GPIO_Init). */
  if ((eff & AUX_OWN_PA14) != 0U && s_swd_off == 0U) {
    __HAL_AFIO_REMAP_SWJ_DISABLE();
    s_swd_off = 1U;
  } else if ((eff & AUX_OWN_PA14) == 0U && s_swd_off != 0U) {
    __HAL_AFIO_REMAP_SWJ_NOJTAG();
    s_swd_off = 0U;
  }
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    uint8_t want = (s_pins[i].own_bit == 0U || (eff & s_pins[i].own_bit)) ? 1U : 0U;
    uint8_t have = (s_owned & (1U << i)) ? 1U : 0U;
    if (want && !have) {
      s_ch[i].mode = AUX_MODE_OFF;
      s_ch[i].deadline_en = 0U;
      aux_init_pin(i);
      s_ch[i].level = 0U;
    } else if (!want && have) {
      if (s_ch[i].mode != AUX_MODE_OFF) {
        aux_set_active(i, 0U);
      }
      s_ch[i].mode = AUX_MODE_OFF;
      s_ch[i].deadline_en = 0U;
      aux_deinit_pin(i);
    }
  }
  s_owned = eff;
}

void AuxOut_Init(uint8_t own_mask)
{
  /* Тактирование портов — здесь, а не в MX_GPIO_Init: пины могут
   * уходить из-под CAN/отладки, порядок инициализации меняться. */
  __HAL_RCC_AFIO_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();
  s_owned = 0U;
  s_swd_off = 0U;
  s_ev_head = s_ev_tail = 0U;
  s_ev_dropped = 0U;
  s_edges_late = 0U;
  memset(s_ch, 0, sizeof(s_ch));
  AuxOut_ApplyOwnedMask(own_mask);
}

uint8_t AuxOut_IsAvailable(uint8_t channel)
{
  if (channel < 1U || channel > AUX_CH_COUNT) {
    return 0U;
  }
  return (s_owned & (1U << (channel - 1U))) ? 1U : 0U;
}

uint8_t AuxOut_OwnedMask(void)
{
  return s_owned;
}

/* CM-N6: квантование программного ШИМ на целые миллисекунды.
 * Период = round(1000/freq), минимум 2 мс; фазы целыми мс —
 * фактическое заполнение on/period может отличаться от запрошенного
 * (на 500 Гц сетка 0/50/100%). Фактические фазы читаются через
 * AuxOut_GetTiming/CMD_AUX_GET — ПК показывает реальный сигнал, а
 * не запрошенный. Та же формула на ПК — aux_channels.quantize_pwm. */
static uint16_t aux_pwm_period_ms(uint16_t freq_hz)
{
  uint16_t p = (uint16_t)((1000U + freq_hz / 2U) / freq_hz);
  return (p < 2U) ? 2U : p;
}

uint8_t AuxOut_Set(uint8_t channel, uint8_t mode,
                   uint16_t pulse_on_ms, uint16_t pulse_off_ms,
                   uint16_t pulse_count, uint16_t pwm_freq_hz,
                   uint8_t pwm_duty_pct, uint32_t pwm_time_ms)
{
  if (!AuxOut_IsAvailable(channel) || mode > AUX_MODE_PWM) {
    return 0U;
  }
  uint8_t idx = (uint8_t)(channel - 1U);
  uint32_t now = HAL_GetTick();

  /* CM-N5: срок проверяется ДО смены состояния — дельта сравнения
   * signed (wrap-safe до INT32_MAX); большие сроки отклоняются, а не
   * уходят в отрицательный signed-диапазон. */
  if (pwm_time_ms > AUX_MAX_DURATION_MS) {
    return 0U;
  }

  /* Полная валидация ДО смены состояния: отказ не оставляет канал
   * в полунастроенном режиме (CM-HW-02). */
  uint16_t on_ms = 0U, off_ms = 0U;
  if (mode == AUX_MODE_PULSE) {
    if (pulse_on_ms == 0U || pulse_off_ms == 0U || pulse_count == 0U) {
      return 0U;
    }
  } else if (mode == AUX_MODE_PWM) {
    /* duty 0 — легально: канал «работает» логически, уровень
     * постоянно низкий (единая трактовка ПК/МК). */
    if (pwm_freq_hz == 0U || pwm_freq_hz > 500U || pwm_duty_pct > 100U) {
      return 0U;
    }
    uint32_t period = aux_pwm_period_ms(pwm_freq_hz);
    /* Ближайший достижимый on — округление, а не floor: 99% при 500
     * Гц даёт фактические 100%, а не молчаливые 50%. */
    uint32_t on = (period * pwm_duty_pct + 50U) / 100U;
    if (on > period) {
      on = period;
    }
    on_ms = (uint16_t)on;
    off_ms = (uint16_t)(period - on);
  } else if (mode == AUX_MODE_ON) {
    /* «Включить на T мс» выражается через time_ms; нулевой срок =
     * бессрочно (для бессрочного есть отдельная команда — протокол
     * различает их полем duration, а не молчаливым 0). */
  } else {
    /* OFF — параметры не проверяются. */
  }

  /* Применение: новый запуск заменяет прежний режим и срок — старый
   * таймер (старый дедлайн) новый запуск не гасит. */
  uint8_t was_active = (s_ch[idx].mode != AUX_MODE_OFF) ? 1U : 0U;
  s_ch[idx].deadline_en = 0U;
  s_ch[idx].mode = mode;

  if (mode == AUX_MODE_OFF) {
    aux_drive(idx, 0U);
    if (was_active) {
      aux_push_event(idx, 0U);
    }
    return 1U;
  }
  if (mode == AUX_MODE_ON) {
    if (pwm_time_ms != 0U) {
      s_ch[idx].stop_at = now + pwm_time_ms;
      s_ch[idx].deadline_en = 1U;
    }
    aux_drive(idx, 1U);
    if (!was_active) {
      aux_push_event(idx, 1U);
    }
    return 1U;
  }
  if (mode == AUX_MODE_PULSE) {
    s_ch[idx].on_ms = pulse_on_ms;
    s_ch[idx].off_ms = pulse_off_ms;
    s_ch[idx].pulses_left = pulse_count;
    s_ch[idx].next_tick = now + pulse_on_ms;
    if (pwm_time_ms != 0U) {
      s_ch[idx].stop_at = now + pwm_time_ms;
      s_ch[idx].deadline_en = 1U;
    }
    aux_drive(idx, 1U);
    if (!was_active) {
      aux_push_event(idx, 1U);
    }
    return 1U;
  }
  /* PWM */
  s_ch[idx].on_ms = on_ms;
  s_ch[idx].off_ms = off_ms;
  s_ch[idx].pulses_left = 0U;
  s_ch[idx].next_tick = now + on_ms;
  if (pwm_time_ms != 0U) {
    s_ch[idx].stop_at = now + pwm_time_ms;
    s_ch[idx].deadline_en = 1U;
  }
  aux_drive(idx, (on_ms != 0U) ? 1U : 0U);
  if (!was_active) {
    aux_push_event(idx, 1U);
  }
  return 1U;
}

/* CM-N6: бюджет фронтов за один проход Poll — скачок tick или занятый
 * цикл не превращаются в залп GPIO-записей: пропущенные ЦЕЛЫЕ периоды
 * сворачиваются арифметикой (у PULSE они же съедают счётчик серий),
 * фронты обрабатываются в пределах одного периода, остаток уходит в
 * s_edges_late. Старые фронты «во времени» недостижимы — меандр
 * не воскресает задним числом. */
#define AUX_POLL_MAX_EDGES 8U

void AuxOut_Poll(void)
{
  uint32_t now = HAL_GetTick();
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    aux_ch_t *ch = &s_ch[i];
    if (!(s_owned & (1U << i))) {
      continue;
    }
    /* Авто-выключение по сроку — в т.ч. для постоянного Вкл
     * («включить на T мс»); признак срока отдельный (CM-N5):
     * stop_at=0 — легальный момент (now+duration при wraparound). */
    if (ch->mode != AUX_MODE_OFF && ch->deadline_en != 0U
        && (int32_t)(now - ch->stop_at) >= 0) {
      ch->mode = AUX_MODE_OFF;
      ch->deadline_en = 0U;
      aux_drive(i, 0U);
      aux_push_event(i, 0U);
      continue;
    }
    if (ch->mode == AUX_MODE_OFF || ch->mode == AUX_MODE_ON) {
      continue;
    }
    /* Крайние заполнения ШИМ — статические уровни, фронтов нет. */
    if (ch->mode == AUX_MODE_PWM && (ch->on_ms == 0U || ch->off_ms == 0U)) {
      continue;
    }
    uint32_t period = (uint32_t)ch->on_ms + ch->off_ms;
    /* Догон целыми периодами без записей в GPIO: если к следующему
     * фронту прошло больше периода — промежуточные фронты уже не
     * воспроизвести, сворачиваем их разом. */
    int32_t late = (int32_t)(now - ch->next_tick);
    if (late > 0 && (uint32_t)late >= period) {
      uint32_t skipped = (uint32_t)late / period;
      s_edges_late += skipped;
      ch->next_tick += skipped * period;
      if (ch->mode == AUX_MODE_PULSE) {
        /* Каждый пропущенный период — полный импульс серии. */
        if (skipped >= ch->pulses_left) {
          ch->pulses_left = 0U;
          ch->mode = AUX_MODE_OFF;
          aux_drive(i, 0U);
          aux_push_event(i, 0U);
          continue;
        }
        ch->pulses_left = (uint16_t)(ch->pulses_left - skipped);
      }
    }
    uint8_t edges = 0U;
    while (edges < AUX_POLL_MAX_EDGES
           && (int32_t)(now - ch->next_tick) >= 0) {
      edges++;
      if (ch->level != 0U) {
        aux_drive(i, 0U);
        ch->next_tick += ch->off_ms;
        if (ch->mode == AUX_MODE_PULSE && --ch->pulses_left == 0U) {
          ch->mode = AUX_MODE_OFF; /* серия закончилась — канал в нуле */
          aux_push_event(i, 0U);
          break;
        }
      } else {
        aux_drive(i, 1U);
        ch->next_tick += ch->on_ms;
      }
    }
    if (edges == AUX_POLL_MAX_EDGES
        && (int32_t)(now - ch->next_tick) >= 0) {
      /* Бюджет исчерпан — уровень согласован с фазой, недожатые
       * фронты считаем и переносим опору на «сейчас». */
      s_edges_late++;
      ch->next_tick = now;
    }
  }
}

uint8_t AuxOut_Get(uint8_t channel)
{
  if (!AuxOut_IsAvailable(channel)) {
    return 0U;
  }
  return s_ch[channel - 1U].level;
}

uint8_t AuxOut_IsActive(uint8_t channel)
{
  if (!AuxOut_IsAvailable(channel)) {
    return 0U;
  }
  return (s_ch[channel - 1U].mode != AUX_MODE_OFF) ? 1U : 0U;
}

uint8_t AuxOut_GetMode(uint8_t channel)
{
  if (!AuxOut_IsAvailable(channel)) {
    return AUX_MODE_OFF;
  }
  return s_ch[channel - 1U].mode;
}

void AuxOut_GetTiming(uint8_t channel, uint16_t *on_ms_out,
                      uint16_t *off_ms_out)
{
  if (!AuxOut_IsAvailable(channel)) {
    return;
  }
  const aux_ch_t *ch = &s_ch[channel - 1U];
  if (ch->mode == AUX_MODE_PWM || ch->mode == AUX_MODE_PULSE) {
    if (on_ms_out != NULL) {
      *on_ms_out = ch->on_ms;
    }
    if (off_ms_out != NULL) {
      *off_ms_out = ch->off_ms;
    }
  } else {
    if (on_ms_out != NULL) {
      *on_ms_out = 0U;
    }
    if (off_ms_out != NULL) {
      *off_ms_out = 0U;
    }
  }
}

uint32_t AuxOut_EdgesLate(void)
{
  return s_edges_late;
}

uint8_t AuxOut_TakeEvent(uint8_t *channel_out, uint8_t *is_on_out)
{
  if (s_ev_tail == s_ev_head) {
    return 0U;
  }
  uint8_t ev = s_ev[s_ev_tail];
  s_ev_tail = (uint8_t)((s_ev_tail + 1U) % AUX_EVENT_DEPTH);
  if (channel_out != NULL) {
    *channel_out = (uint8_t)((ev >> 1U) + 1U);
  }
  if (is_on_out != NULL) {
    *is_on_out = ev & 1U;
  }
  return 1U;
}

uint32_t AuxOut_EventsDropped(void)
{
  return s_ev_dropped;
}
