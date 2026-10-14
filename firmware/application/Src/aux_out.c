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
  uint16_t on_ms;        /* длительность высокого уровня */
  uint16_t off_ms;       /* длительность низкого уровня */
  uint16_t pulses_left;  /* сколько периодов осталось (pulse) */
  uint32_t next_tick;    /* HAL tick следующего фронта */
  uint32_t stop_at;      /* HAL tick авто-выключения, 0 — нет */
} aux_ch_t;

static aux_ch_t s_ch[AUX_CH_COUNT];
static uint8_t  s_owned;       /* фактическая маска владения */

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
  /* Пин уходит из-под AUX: в безопасный вход — его подхватит TERM/
   * SWD при следующем своём применении (CanBridge_SetTransceiverMode
   * перезаписывает направление при смене режима CAN). */
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
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    uint8_t want = (s_pins[i].own_bit == 0U || (eff & s_pins[i].own_bit)) ? 1U : 0U;
    uint8_t have = (s_owned & (1U << i)) ? 1U : 0U;
    if (want && !have) {
      s_ch[i].mode = AUX_MODE_OFF;
      s_ch[i].stop_at = 0U;
      aux_init_pin(i);
      s_ch[i].level = 0U;
    } else if (!want && have) {
      if (s_ch[i].mode != AUX_MODE_OFF) {
        aux_set_active(i, 0U);
      }
      s_ch[i].mode = AUX_MODE_OFF;
      s_ch[i].stop_at = 0U;
      aux_deinit_pin(i);
    }
  }
  s_owned = eff;
}

void AuxOut_Init(uint8_t own_mask)
{
  /* Тактирование портов — здесь, а не в MX_GPIO_Init: пины могут
   * уходить из-под CAN/отладки, порядок инициализации меняться. */
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();
  s_owned = 0U;
  s_ev_head = s_ev_tail = 0U;
  s_ev_dropped = 0U;
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

  /* Полная валидация ДО смены состояния: отказ не оставляет канал
   * в полунастроенном режиме (CM-HW-02). */
  uint16_t on_ms = 0U, off_ms = 0U;
  if (mode == AUX_MODE_PULSE) {
    if (pulse_on_ms == 0U || pulse_off_ms == 0U || pulse_count == 0U) {
      return 0U;
    }
  } else if (mode == AUX_MODE_PWM) {
    /* Программный ШИМ, период ≥ 2 мс — иначе опрос в главном цикле не
     * успевает за фронтами. duty 0 — легально: канал «работает»
     * логически, уровень постоянно низкий (единая трактовка ПК/МК). */
    if (pwm_freq_hz == 0U || pwm_freq_hz > 500U || pwm_duty_pct > 100U) {
      return 0U;
    }
    uint32_t period = 1000U / pwm_freq_hz;
    uint32_t on = (period * pwm_duty_pct) / 100U;
    if (pwm_duty_pct == 0U) {
      on = 0U;
    } else if (on == 0U) {
      on = 1U;
    }
    if (on >= period) {
      on = period; /* 100% — постоянный высокий уровень */
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
   * таймер (старый stop_at) новый запуск не гасит. */
  uint8_t was_active = (s_ch[idx].mode != AUX_MODE_OFF) ? 1U : 0U;
  s_ch[idx].stop_at = 0U;
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
  }
  aux_drive(idx, (on_ms != 0U) ? 1U : 0U);
  if (!was_active) {
    aux_push_event(idx, 1U);
  }
  return 1U;
}

void AuxOut_Poll(void)
{
  uint32_t now = HAL_GetTick();
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    aux_ch_t *ch = &s_ch[i];
    if (!(s_owned & (1U << i))) {
      continue;
    }
    if (ch->mode == AUX_MODE_OFF || ch->mode == AUX_MODE_ON) {
      /* Авто-выключение по сроку — в т.ч. для постоянного Вкл
       * («включить на T мс»); событие «канал выключился» — один раз. */
      if (ch->mode == AUX_MODE_ON && ch->stop_at != 0U
          && (int32_t)(now - ch->stop_at) >= 0) {
        ch->mode = AUX_MODE_OFF;
        ch->stop_at = 0U;
        aux_drive(i, 0U);
        aux_push_event(i, 0U);
      }
      continue;
    }
    if (ch->stop_at != 0U && (int32_t)(now - ch->stop_at) >= 0) {
      ch->mode = AUX_MODE_OFF;
      ch->stop_at = 0U;
      aux_drive(i, 0U);
      aux_push_event(i, 0U);
      continue;
    }
    /* Крайние заполнения ШИМ — статические уровни, фронтов нет. */
    if (ch->mode == AUX_MODE_PWM && (ch->on_ms == 0U || ch->off_ms == 0U)) {
      continue;
    }
    while ((int32_t)(now - ch->next_tick) >= 0) {
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
