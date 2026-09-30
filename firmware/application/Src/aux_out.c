/* Реализация доп. каналов OUT1..OUT4, см. aux_out.h.
 *
 * Все режимы кроме OFF строятся на одном автомате с фронтами по
 * HAL_GetTick: ON — фронтов нет, PULSE — count полных периодов
 * on/off, PWM — бесконечный меандр on_ms/off_ms из частоты и
 * заполнения (stop_at ограничивает время работы, 0 — до «Выкл»). */

#include "aux_out.h"
#include "main.h"

typedef struct {
  GPIO_TypeDef *port;
  uint16_t      pin;
} aux_pin_t;

static const aux_pin_t s_pins[AUX_CH_COUNT] = {
  { OUT1_PORT, OUT1_PIN },
  { OUT2_PORT, OUT2_PIN },
  { OUT3_PORT, OUT3_PIN },
  { OUT4_PORT, OUT4_PIN },
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

static void aux_drive(uint8_t idx, uint8_t level)
{
  s_ch[idx].level = level;
  HAL_GPIO_WritePin(s_pins[idx].port, s_pins[idx].pin,
                    level ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void AuxOut_Init(void)
{
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    s_ch[i].mode = AUX_MODE_OFF;
    s_ch[i].stop_at = 0U;
    aux_drive(i, 0U);
  }
}

uint8_t AuxOut_Set(uint8_t channel, uint8_t mode,
                   uint16_t pulse_on_ms, uint16_t pulse_off_ms,
                   uint16_t pulse_count, uint16_t pwm_freq_hz,
                   uint8_t pwm_duty_pct, uint32_t pwm_time_ms)
{
  if (channel < 1U || channel > AUX_CH_COUNT || mode > AUX_MODE_PWM) {
    return 0U;
  }
  uint8_t idx = (uint8_t)(channel - 1U);
  aux_ch_t *ch = &s_ch[idx];
  uint32_t now = HAL_GetTick();

  ch->mode = mode;
  ch->stop_at = 0U;

  if (mode == AUX_MODE_OFF) {
    aux_drive(idx, 0U);
    return 1U;
  }
  if (mode == AUX_MODE_ON) {
    aux_drive(idx, 1U);
    return 1U;
  }
  if (mode == AUX_MODE_PULSE) {
    if (pulse_on_ms == 0U || pulse_off_ms == 0U || pulse_count == 0U) {
      return 0U;
    }
    ch->on_ms = pulse_on_ms;
    ch->off_ms = pulse_off_ms;
    ch->pulses_left = pulse_count;
    ch->next_tick = now + pulse_on_ms;
    aux_drive(idx, 1U);
    return 1U;
  }
  /* AUX_MODE_PWM: программный ШИМ, период ≥ 2 мс — иначе опрос
   * в главном цикле не успевает за фронтами. */
  if (pwm_freq_hz == 0U || pwm_freq_hz > 500U
      || pwm_duty_pct == 0U || pwm_duty_pct > 100U) {
    return 0U;
  }
  uint32_t period = 1000U / pwm_freq_hz;
  uint32_t on = (period * pwm_duty_pct) / 100U;
  if (on == 0U) {
    on = 1U;
  }
  if (on >= period) {
    on = period - 1U;
  }
  ch->on_ms = (uint16_t)on;
  ch->off_ms = (uint16_t)(period - on);
  ch->pulses_left = 0U;
  ch->next_tick = now + on;
  if (pwm_time_ms != 0U) {
    ch->stop_at = now + pwm_time_ms;
  }
  aux_drive(idx, 1U);
  return 1U;
}

void AuxOut_Poll(void)
{
  uint32_t now = HAL_GetTick();
  for (uint8_t i = 0U; i < AUX_CH_COUNT; i++) {
    aux_ch_t *ch = &s_ch[i];
    if (ch->mode == AUX_MODE_OFF || ch->mode == AUX_MODE_ON) {
      continue;
    }
    /* Авто-выключение ШИМ по заданному времени. */
    if (ch->stop_at != 0U && (int32_t)(now - ch->stop_at) >= 0) {
      ch->mode = AUX_MODE_OFF;
      ch->stop_at = 0U;
      aux_drive(i, 0U);
      continue;
    }
    while ((int32_t)(now - ch->next_tick) >= 0) {
      if (ch->level != 0U) {
        aux_drive(i, 0U);
        ch->next_tick += ch->off_ms;
        if (ch->mode == AUX_MODE_PULSE && --ch->pulses_left == 0U) {
          ch->mode = AUX_MODE_OFF; /* серия закончилась — канал в нуле */
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
  if (channel < 1U || channel > AUX_CH_COUNT) {
    return 0U;
  }
  return s_ch[channel - 1U].level;
}
